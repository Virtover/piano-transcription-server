"""End-to-end checks of a running server: real jobs on its real workers,
GPUs, nodes, and RunPod, checked by what their MIDI holds. Run in the API
container, through the admin commands:

    python scripts/admin.py e2e                          # every case, placed as the server places jobs
    python scripts/admin.py e2e --backend remote         # on RunPod
    python scripts/admin.py e2e --case clip --mode arrangement
    python scripts/kube.py admin e2e                     # the same on Kubernetes

Each case uploads a generated test file, as an upload would be stored, and
queues it as an unbilled job: no user, nothing charged. The file has four
10-second sections of plucked notes, two a second: C4, E4, G4, then A4. None
is another's harmonic, so a part cut in the wrong place shows in the notes.
The jobs and their files are removed afterwards; the job list keeps them,
titled "e2e check". See docs/testing.md.
"""

import json
import struct
import subprocess
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from app import clips, job_storage
from app.admin.common import AdminError
from app.outsource import routing
from app.worker import tasks

TITLE = "e2e check"
SECONDS = 40.0
SECTION = 10.0
# The MIDI pitch of each section, in order.
PITCHES = (60, 64, 67, 69)
FREQUENCIES = (261.63, 329.63, 392.0, 440.0)
NOTES_PER_SECOND = 2
# A case's part: (start, end) in seconds, None for the whole file.
CASES = {
    "whole": None,
    "clip": (10.0, 20.0),
    "clip-to-end": (20.0, None),
}
MODES = ("transcription", "arrangement")
# The model may miss notes at the very start of the audio, and arrangements
# rewrite the music, so the checks leave room for both.
FOUND_SHARE = 0.5
ONSET_SLACK = 0.25


@dataclass
class Result:
    case: str
    mode: str
    job_id: str
    status: str = "not run"
    backend: str | None = None
    node: str | None = None
    seconds: float | None = None
    notes: dict[int, int] = field(default_factory=dict)
    onsets: tuple[float, float] | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == "completed" and not self.problems


def test_audio(path: Path) -> None:
    """The test file: plucked notes with a few harmonics, as an MP3."""
    frequency = "if(lt(t,10),{},if(lt(t,20),{},if(lt(t,30),{},{})))".format(*FREQUENCIES)
    pluck = (f"(sin(2*PI*({frequency})*t)+0.4*sin(4*PI*({frequency})*t)+0.2*sin(6*PI*({frequency})*t))"
             f"*exp(-4*mod(t,{1 / NOTES_PER_SECOND}))*0.5")
    subprocess.run(
        ["ffmpeg", "-y", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
         f"aevalsrc='{pluck}':s=44100:d={SECONDS}", "-c:a", "libmp3lame", "-b:a", "128k", "-f", "mp3", str(path)],
        check=True,
    )


def _varlen(data: bytes, index: int) -> tuple[int, int]:
    value = 0
    while True:
        byte = data[index]
        index += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, index


def midi_notes(data: bytes) -> list[tuple[float, int]]:
    """(onset in seconds, pitch) of every note of a standard MIDI file,
    with the tempo changes of any track."""
    if data[:4] != b"MThd":
        raise ValueError("not a MIDI file")
    division = struct.unpack(">H", data[12:14])[0]
    index, events = 14, []
    while index + 8 <= len(data):
        kind, length = data[index:index + 4], struct.unpack(">I", data[index + 4:index + 8])[0]
        track = data[index + 8:index + 8 + length]
        index += 8 + length
        if kind != b"MTrk":
            continue
        position, tick, status = 0, 0, 0
        while position < len(track):
            delta, position = _varlen(track, position)
            tick += delta
            byte = track[position]
            if byte == 0xFF:
                meta = track[position + 1]
                size, position = _varlen(track, position + 2)
                if meta == 0x51:
                    events.append((tick, 0, int.from_bytes(track[position:position + 3], "big")))
                position += size
                continue
            if byte in (0xF0, 0xF7):
                size, position = _varlen(track, position + 1)
                position += size
                continue
            if byte & 0x80:
                status, position = byte, position + 1
            if status & 0xF0 in (0xC0, 0xD0):
                position += 1
                continue
            pitch, velocity = track[position], track[position + 1]
            position += 2
            if status & 0xF0 == 0x90 and velocity:
                events.append((tick, 1, pitch))
    tempo, last, seconds, notes = 500000, 0, 0.0, []
    for tick, kind, value in sorted(events):
        seconds += (tick - last) * tempo / 1e6 / division
        last = tick
        if kind == 0:
            tempo = value
        else:
            notes.append((seconds, value))
    return notes


def expected_pitches(part: tuple[float, float | None] | None) -> tuple[set[int], set[int]]:
    """The sections' pitches a part holds, and those it must not."""
    start, end = part if part else (0.0, None)
    end = SECONDS if end is None else end
    inside = {pitch for number, pitch in enumerate(PITCHES)
              if start < (number + 1) * SECTION and number * SECTION < end}
    return inside, set(PITCHES) - inside


def check(result: Result, part, length: float, notes: list[tuple[float, int]]) -> None:
    """What the MIDI must hold: notes only within the part's length, the
    part's sections, and none of the others."""
    if not notes:
        result.problems.append("the MIDI has no notes")
        return
    onsets = [onset for onset, _ in notes]
    result.onsets = (round(min(onsets), 2), round(max(onsets), 2))
    if max(onsets) > length + ONSET_SLACK:
        result.problems.append(f"a note at {max(onsets):.2f} s, past the part's {length:g} s")
    counts = Counter(pitch for _, pitch in notes if pitch in PITCHES)
    result.notes = dict(sorted(counts.items()))
    inside, outside = expected_pitches(part)
    if result.mode == "transcription":
        for pitch in inside:
            # The notes of the section's part of the clip, two a second
            start, end = part if part else (0.0, None)
            number = PITCHES.index(pitch)
            covered = min((number + 1) * SECTION, end or SECONDS) - max(number * SECTION, start)
            wanted = covered * NOTES_PER_SECOND * FOUND_SHARE
            if counts[pitch] < wanted:
                result.problems.append(f"only {counts[pitch]} notes of pitch {pitch}, expected at least {wanted:g}")
        for pitch in outside:
            if counts[pitch] > 1:
                result.problems.append(f"{counts[pitch]} notes of pitch {pitch}, which is outside the part")
    else:
        # An arrangement rewrites the music: the part's pitches lead, others stay few
        total = sum(counts.values())
        if not total or counts.most_common(1)[0][0] not in inside:
            result.problems.append(f"the leading pitch is not one of the part's {sorted(inside)}")
        stray = sum(counts[pitch] for pitch in outside)
        if total and stray > 0.2 * total:
            result.problems.append(f"{stray} of {total} notes are of pitches outside the part")


def queue(case: str, mode: str, backend: str) -> tuple[Result, tuple | None, float]:
    """Store the test file as an upload and queue it, unbilled."""
    part = CASES[case]
    clip = clips.check_clip(*part, SECONDS) if part else None
    length = clip.length(SECONDS) if clip else SECONDS
    job_id = str(uuid.uuid4())
    result = Result(case, mode, job_id)
    folder = job_storage.storage().workspace(job_id)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        upload = folder / tasks.UPLOAD_FILE_NAME
        test_audio(upload)
        job_storage.storage().save(job_id, upload)
    finally:
        job_storage.storage().release(folder)
    fields = {
        "status": "queued", "queued_at": str(time.time()), "progress": "0.0", "duration": str(length),
        "full_cost": "0", "source": "upload", "title": TITLE, "upload_title": TITLE,
        **(clip.fields() if clip else {}),
    }
    if mode == "arrangement":
        fields.update(mode=tasks.ARRANGEMENT, style="level2")
    tasks.redis.hset(tasks.job_key(job_id), mapping=fields)
    arrangement = mode == "arrangement"
    if backend == "auto":
        chosen = routing.choose_backend(tasks.redis, job_id, arrangement)
    else:
        chosen = backend
        if chosen == "local":
            # Kept on this server's workers: never moved to RunPod.
            tasks.redis.hset(tasks.job_key(job_id), "outsourced", "1")
        routing.mark_active(tasks.redis, chosen, job_id)
    tasks.redis.hset(tasks.job_key(job_id), "backend", chosen)
    tasks.send_job(job_id, chosen, None, TITLE)
    tasks.record_job(job_id)
    return result, part, length


def finish(result: Result, part, length: float, deadline: float) -> None:
    key = tasks.job_key(result.job_id)
    while time.time() < deadline:
        job = tasks.redis.hgetall(key)
        if job.get("status") in {"completed", "failed", "cancelled"}:
            break
        time.sleep(2)
    else:
        job = tasks.redis.hgetall(key)
        tasks.cancel_job(result.job_id)
        result.problems.append("did not finish in time; cancelled")
    result.status = job.get("status", "missing")
    result.backend, result.node = job.get("backend"), job.get("node")
    result.seconds = float(job["duration"]) if job.get("duration") else None
    if result.status != "completed":
        result.problems.append(job.get("error_detail") or job.get("error") or f"ended {result.status}")
        return
    if result.seconds is None or abs(result.seconds - length) > 0.01:
        result.problems.append(f"the job was to transcribe {result.seconds} s, not the part's {length:g} s")
    try:
        # As the MIDI route reads it: from the bucket, or a file of this machine
        if job_storage.s3_key(job["result"]) is not None:
            bucket, object_key = job_storage.stored_result(job["result"])
            data = bucket.read_result(object_key)
        else:
            data = Path(job["result"]).read_bytes()
        notes = midi_notes(data)
    except Exception as error:
        result.problems.append(f"the MIDI could not be read: {type(error).__name__}: {error}")
        return
    check(result, part, length, notes)


def clean(job_id: str) -> None:
    try:
        tasks.redis.delete(tasks.job_key(job_id))
        job_storage.delete_job_files(job_id)
    except Exception:
        pass


def run(args) -> None:
    from app.config import settings

    cases = [args.case] if args.case else list(CASES)
    modes = [args.mode] if args.mode else [mode for mode in MODES
                                         if mode == "transcription" or settings.worker.arrangement]
    if args.backend == "remote" and not routing.outsourcing_enabled():
        raise AdminError("outsourcing is off on this server (OUTSOURCE_PROVIDER); there is no remote backend")
    deadline = time.time() + args.timeout
    started = [queue(case, mode, args.backend) for case in cases for mode in modes]
    if not args.json:
        print(f"Queued {len(started)} job(s) on backend {args.backend}; waiting up to {args.timeout:g} s", flush=True)
    results = []
    for result, part, length in started:
        try:
            finish(result, part, length, deadline)
        finally:
            if not args.keep:
                clean(result.job_id)
        results.append(result)
        if not args.json:
            where = f"{result.backend or '?'}" + (f" on {result.node}" if result.node else "")
            notes = ", ".join(f"{pitch}×{count}" for pitch, count in result.notes.items()) or "-"
            onsets = f"{result.onsets[0]}–{result.onsets[1]} s" if result.onsets else "-"
            print(f"{'PASS' if result.passed else 'FAIL'}  {result.case:<12} {result.mode:<14} {where:<22} "
                  f"notes {notes}; onsets {onsets}  [{result.job_id[:8]}]", flush=True)
            for problem in result.problems:
                print(f"      {problem}", flush=True)
    failed = [result for result in results if not result.passed]
    if args.json:
        print(json.dumps([{**result.__dict__, "passed": result.passed} for result in results], indent=2))
    if failed:
        raise AdminError(f"{len(failed)} of {len(results)} end-to-end checks failed")
    if not args.json:
        print(f"All {len(results)} end-to-end checks passed")
