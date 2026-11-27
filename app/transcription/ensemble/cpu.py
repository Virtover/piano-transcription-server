"""Ensemble jobs on the CPU: dettato.cpp (dettato_cpu/server.cpp) in a process of its own.

PyTorch runs dettato at about half real time on a CPU; dettato.cpp at about 0.8x on 8 cores (measured: 223 s of audio in
267 s), with the same models and scores (its docs/PARITY.md). A process of its own also isolates the job: a crash ends
only that process, a cancelled job ends it, and its address space is limited, so a job that would need more memory fails
alone instead of the system's OOM killer ending the worker.

The worker image has two builds: one for CPUs with AVX2 and FMA (x86-64-v3, since about 2013), and one for any x86-64
CPU, picked by /proc/cpuinfo. No torch import.
"""
from __future__ import annotations

import json
import logging
import os
import resource
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

from app.transcription.device import cpu_thread_count
from app.errors import UserFacingError
from app.transcription.cancellation import TranscriptionCancelled
from app.transcription import stream_audio
from app.transcription.ensemble.midi import Track

logger = logging.getLogger(__name__)

ROOT = Path("/opt/dettato-cpp")
MODEL = ROOT / "dettato-f16.gguf"
BINARIES = {"avx2": ROOT / "bin" / "dettato_server-avx2", "generic": ROOT / "bin" / "dettato_server-generic"}
# Address space of the process: a base (the model, ggml's compute buffers, thread stacks) and a share per second of
# audio (dettato.cpp's arrays grow about 2.2 MB per second; measured 1.4 GB of RSS for 223 s), with room to spare.
BASE_ADDRESS_SPACE = 6 * 1024**3
ADDRESS_SPACE_PER_SECOND = 8 * 1024**2
POLL_SECONDS = 0.5
# A recording longer than this runs in pieces (transcribe).
CHUNK_S = 20 * 60.0
MARGIN_S = 30.0


class CpuEnsembleFailed(UserFacingError, RuntimeError):
    def __init__(self) -> None:
        super().__init__("The ensemble transcription failed on the CPU")


def _cpu_flags() -> set[str]:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("flags"):
                return set(line.split(":", 1)[1].split())
    except OSError:
        pass
    return set()


def installed() -> bool:
    """Whether dettato.cpp is in this image: the worker image builds it, the RunPod image (GPU only) does not."""
    return MODEL.is_file() and all(path.is_file() for path in BINARIES.values())


def binary() -> Path:
    flags = _cpu_flags()
    return BINARIES["avx2"] if {"avx2", "fma", "f16c"} <= flags else BINARIES["generic"]


def check() -> None:
    """Log whether the binary and the model are there (at preload)."""
    path = binary()
    missing = [str(p) for p in (path, MODEL) if not p.is_file()]
    if missing:
        logger.warning("Ensemble jobs on the CPU need %s; the worker image includes them", ", ".join(missing))
    else:
        logger.info("Ensemble jobs on the CPU run %s", path.name)


def address_space_limit(seconds: float) -> int:
    return int(BASE_ADDRESS_SPACE + ADDRESS_SPACE_PER_SECOND * max(0.0, seconds))


def limit_process(pid: int, address_space: int) -> None:
    """Limit a started process's address space and turn its core dumps off. Set from here, after it started, not in
    the child before exec (preexec_fn), which can deadlock in a process with threads, as a worker is. The tool
    allocates its large buffers only after reading the audio and the model, well after this."""
    prlimit = getattr(resource, "prlimit", None)
    if prlimit is None:
        logger.warning("No address-space limit for dettato_server on this system")
        return
    try:
        prlimit(pid, resource.RLIMIT_AS, (address_space, address_space))
        prlimit(pid, resource.RLIMIT_CORE, (0, 0))
    except (OSError, ValueError) as error:
        # It ended already, or the limit cannot be set; the job still runs.
        logger.warning("Could not limit dettato_server's memory: %r", error)


def parse_result(data: dict) -> tuple[list[Track], dict]:
    """The tool's JSON -> tracks; anything malformed raises ValueError."""
    tracks_in = data["tracks"]
    if not isinstance(tracks_in, list):
        raise ValueError("tracks")
    tracks = []
    for t in tracks_in:
        name, program, is_drum = t["name"], t["program"], t["is_drum"]
        if not isinstance(name, str) or not isinstance(program, int) or not isinstance(is_drum, bool):
            raise ValueError("track")
        tracks.append(Track(name[:64], max(0, min(127, program)), is_drum))
    for note in data["notes"]:
        onset, offset, pitch, velocity, index = note
        if not (isinstance(index, int) and 0 <= index < len(tracks)):
            raise ValueError("note track")
        onset, offset = float(onset), float(offset)
        if not (np.isfinite(onset) and np.isfinite(offset)) or onset < 0:
            raise ValueError("note time")
        tracks[index].notes.append((onset, max(offset, onset), int(pitch), int(velocity)))
    tracks = [t for t in tracks if t.notes]
    info = {"drums": {"model": data.get("drum_model") or None}, "tuning_cents": data.get("tuning_cents"),
            "transcribed": data.get("transcribed"), "dropped": data.get("dropped"), "inserted": data.get("inserted")}
    return tracks, info


def transcribe(x, rate: int, source_rate: int | None, progress: Callable[[float], None],
               cancelled: Callable[[], bool], stats: dict | None = None,
               work_dir: Path | None = None) -> tuple[list[Track], dict]:
    """The tracks of x (mono float32 at rate, an array or a stream_audio reader), whose source was recorded at
    source_rate (None: taken as rate). Its files (the audio, 4 bytes per sample, and the result) are in a folder in
    work_dir, the job's own folder.

    dettato.cpp keeps arrays of the whole recording (about 2.2 MB per second), so a recording longer than CHUNK_S runs
    in pieces of CHUNK_S with MARGIN_S of context each side: each keeps the notes that start in its own part, and the
    pieces' tracks of the same instrument become one track. Its drum model then decides per piece."""
    stats = stats if stats is not None else {}
    reader = x if hasattr(x, "read") else stream_audio.ArrayReader(x, rate)
    n = len(reader)
    chunk, margin = int(CHUNK_S * rate), int(MARGIN_S * rate)
    if n <= chunk + 2 * margin:
        return _transcribe_part(reader, 0, n, rate, source_rate, progress, cancelled, stats, work_dir)
    merged: dict[tuple, Track] = {}
    parts = range(0, n, chunk)
    infos = []
    for number, c0 in enumerate(parts):
        c1 = min(n, c0 + chunk)
        a, b = max(0, c0 - margin), min(n, c1 + margin)
        part_progress = (lambda value, number=number: progress((number + value) / len(parts)))
        tracks, info = _transcribe_part(reader, a, b, rate, source_rate, part_progress, cancelled, {}, work_dir)
        infos.append(info)
        lo, hi = (c0 - a) / rate, (c1 - a) / rate
        for track in tracks:
            key = (track.name, track.program, track.is_drum)
            target = merged.setdefault(key, Track(track.name, track.program, track.is_drum))
            target.notes.extend((on + a / rate, off + a / rate, pitch, vel) for on, off, pitch, vel in track.notes
                                if lo <= on < hi or (c1 == n and on >= lo))
    tracks = sorted((t for t in merged.values() if t.notes), key=lambda t: (t.is_drum, -len(t.notes)))
    for track in tracks:
        track.notes.sort()
    stats["parts"] = len(parts)
    return tracks, {"drums": {"model": [i["drums"]["model"] for i in infos]}, "parts": len(parts)}


def _transcribe_part(reader, a: int, b: int, rate: int, source_rate: int | None, progress: Callable[[float], None],
                     cancelled: Callable[[], bool], stats: dict, work_dir: Path | None) -> tuple[list[Track], dict]:
    """Samples [a, b) of reader through dettato_server -> (tracks with times from a, info)."""
    stats = stats if stats is not None else {}
    path = binary()
    seconds = (b - a) / rate
    limit = address_space_limit(seconds)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="ensemble-", dir=work_dir) as directory:
        audio_path = Path(directory) / "audio.f32"
        out_path = Path(directory) / "result.json"
        with audio_path.open("wb") as f:
            block = int(stream_audio.BLOCK_SECONDS * rate)
            for start in range(a, b, block):
                f.write(np.ascontiguousarray(reader.read(start, min(b, start + block)), dtype="<f4").tobytes())

        command = [str(path), str(MODEL), str(audio_path), str(int(rate)), str(int(source_rate or rate)),
                   str(cpu_thread_count() or 0), str(out_path)]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                   text=True, env={"PATH": os.environ.get("PATH", "")})
        limit_process(process.pid, limit)
        errors: list[str] = []
        stderr_thread = threading.Thread(target=lambda: errors.extend(process.stderr.readlines()[-20:]), daemon=True)
        stderr_thread.start()

        def read_progress() -> None:
            for line in process.stdout:
                parts = line.split()
                if len(parts) == 2 and parts[0] == "progress":
                    try:
                        progress(max(0.0, min(1.0, float(parts[1]))))
                    except ValueError:
                        pass

        progress_thread = threading.Thread(target=read_progress, daemon=True)
        progress_thread.start()
        try:
            while process.poll() is None:
                if cancelled():
                    raise TranscriptionCancelled()
                time.sleep(POLL_SECONDS)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            progress_thread.join(timeout=5)
            stderr_thread.join(timeout=5)
        if process.returncode != 0:
            logger.warning("dettato_server exited with %s after %.0f s for %.0f s of audio: %s", process.returncode,
                           time.monotonic() - started, seconds, "".join(errors).strip()[-500:])
            raise CpuEnsembleFailed()
        try:
            tracks, info = parse_result(json.loads(out_path.read_text()))
        except (OSError, ValueError, KeyError, TypeError) as error:
            logger.warning("dettato_server wrote no valid result: %r", error)
            raise CpuEnsembleFailed() from error
    stats["model_s"] = time.monotonic() - started
    return tracks, info
