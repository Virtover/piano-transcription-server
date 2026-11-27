"""The MIDI files of a job's result: its variants.

A piano job has one, raw: Transkun's transcription. An ensemble job has six: raw, dettato's multi-track file with its
tempo map, and five piano versions of it (app/transcription/ensemble/piano_levels.py), full, advanced, intermediate,
easy, and beginner. The levels are made once, where the job ran, when its result is stored: by the local or node
worker that transcribed it, by the RunPod handler for an outsourced job (which returns them with the result), and,
for a result from a RunPod endpoint on an older image that returns none, by the remote worker that received it
(FallbackLevels), after the job is completed with its raw result. They are kept next to the result in job storage,
under fixed names, and deleted with it.
Only the levels worth keeping are: one without notes, or with the same notes as the next harder level kept, is
left out (a lone melody gives the same notes at every level; drums alone give none, and then the job has raw only).

Making them can never fail a job: the raw result is delivered whatever happens, and the job's variants field lists
what exists. Each run is a child process (python -m app.variants) with a CPU time, an address-space limit, and a
lower priority, so a long or pathological result cannot hold a worker's CPU, memory, or GIL; a result of more than
MAX_NOTES notes gets none, with a log line. Files are written to a temporary name, flushed to disk, and renamed, so a
crash or a power cut leaves either a whole file or none.

No import of app.config here: the child process and the RunPod handler use this module too.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path


logger = logging.getLogger(__name__)

RAW = "raw"
# In the order a job's status lists them: the most complete playable version first, the raw file last.
LEVELS = ("full", "advanced", "intermediate", "easy", "beginner")
ORDER = LEVELS + (RAW,)
# The result's own name in job storage (app.job_storage.RESULT_FILE_NAME).
RAW_FILE_NAME = "transcription.mid"
LEVEL_PREFIX = "piano-"
# Without a variant, an ensemble job's download is this one when it exists: apps released before ensemble jobs sent
# mode "arrangement" and expect a piano part.
ENSEMBLE_DEFAULT = "full"

# The output field in which a RunPod endpoint returns the levels it made of an ensemble result: {level: base64
# MIDI}. Endpoints on older images send none; the server then makes them (FallbackLevels).
LEVELS_FIELD = "piano_levels_base64"

# More notes than this, about ten hours of a dense band, get no levels: their analysis would need about 900 MB.
MAX_NOTES = 1_000_000
# CPU seconds one run may use (a ten-hour band takes under a minute), and the most it may wait for in all.
CPU_SECONDS = 300
WALL_SECONDS = 2 * CPU_SECONDS
# Address space of the child: its interpreter, mido, and MAX_NOTES notes' analysis, with room to spare.
MEMORY_BYTES = 3 * 1024 ** 3
NICENESS = 10
TEMPORARY_SUFFIX = ".partial"


def file_name(variant: str) -> str:
    """The file a variant is kept in, next to the result."""
    if variant == RAW:
        return RAW_FILE_NAME
    if variant not in LEVELS:
        raise ValueError(f"no variant {variant!r}")
    return f"{LEVEL_PREFIX}{variant}.mid"


LEVEL_FILE_NAMES = frozenset(file_name(level) for level in LEVELS)


def is_result_file(name: str) -> bool:
    """Whether a kept file is part of the result (kept until the job expires), not one of its inputs."""
    return name == RAW_FILE_NAME or name in LEVEL_FILE_NAMES


def ordered(levels: Iterable[str]) -> list[str]:
    """The levels among the given names, in ORDER, without raw."""
    names = set(levels)
    return [level for level in LEVELS if level in names]


def stored_field(levels: Iterable[str]) -> str:
    """A job's variants field: the levels made, then raw."""
    return ",".join(ordered(levels) + [RAW])


def job_variants(job: dict[str, str]) -> list[str]:
    """The variants a completed job has, in ORDER: raw always, and the levels its variants field names. Jobs
    without the field (piano jobs, and ensemble jobs completed before levels existed) have raw only."""
    stored = job.get("variants") or ""
    return ordered(name.strip() for name in stored.split(",")) + [RAW]


def default_variant(job: dict[str, str], ensemble: bool) -> str:
    """The variant a download without one returns: full for an ensemble job that has it, else raw."""
    return ENSEMBLE_DEFAULT if ensemble and ENSEMBLE_DEFAULT in job_variants(job) else RAW


def write_atomically(path: Path, data: bytes) -> None:
    """path holds data, or, after a crash or power cut at any point, what it held before: written to a temporary
    name beside it, flushed to disk, renamed over it, and the folder flushed too."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}{TEMPORARY_SUFFIX}")
    try:
        with open(temporary, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    sync_folder(path.parent)


def sync_folder(folder: Path) -> None:
    """Flush a folder's entries (a rename) to disk; not possible, nor needed, on Windows."""
    if os.name != "posix":
        return
    descriptor = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _clear(folder: Path) -> None:
    """Remove level files and temporary files a previous attempt left in folder."""
    for name in LEVEL_FILE_NAMES:
        (folder / name).unlink(missing_ok=True)
    for path in folder.glob(f".{LEVEL_PREFIX}*{TEMPORARY_SUFFIX}"):
        path.unlink(missing_ok=True)


def existing(folder: Path) -> list[str]:
    """The levels whose files are in folder, in ORDER."""
    return [level for level in LEVELS if (folder / file_name(level)).is_file()]


def make_levels(midi_path: Path, folder: Path, job_id: str = "", max_notes: int = MAX_NOTES) -> list[str]:
    """Make the piano levels of an ensemble result at midi_path, as files in folder (file_name), in a child process
    within CPU_SECONDS, MEMORY_BYTES, and MAX_NOTES. The levels made, in ORDER; never raises: a level that could not be
    made is left out, with a log line."""
    started = time.monotonic()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        _clear(folder)
        root = Path(__file__).resolve().parent.parent
        completed = subprocess.run(
            [sys.executable, "-m", "app.variants", str(midi_path), str(folder), str(max_notes)],
            cwd=root,
            capture_output=True,
            timeout=WALL_SECONDS,
            check=False,
        )
        report = _report(completed.stdout)
        made = existing(folder)
        if completed.returncode != 0 or report.get("error"):
            errors = completed.stderr.decode("utf-8", "replace").strip().splitlines()
            reason = report.get("error") or (errors[-1] if errors else f"exit status {completed.returncode}")
            logger.warning("Job %s: piano levels not made (%s); its result has %s", job_id or midi_path, reason,
                           ", ".join(made + [RAW]))
        else:
            logger.info("Job %s: piano levels made in %.1f s", job_id or midi_path, time.monotonic() - started)
        return made
    except subprocess.TimeoutExpired:
        made = existing(folder)
        logger.warning("Job %s: piano levels took longer than %d s and were stopped; its result has %s",
                       job_id or midi_path, WALL_SECONDS, ", ".join(made + [RAW]))
        return made
    except Exception:
        logger.exception("Job %s: piano levels could not be made", job_id or midi_path)
        try:
            return existing(folder)
        except OSError:
            return []
    finally:
        try:
            for path in folder.glob(f".{LEVEL_PREFIX}*{TEMPORARY_SUFFIX}"):
                path.unlink(missing_ok=True)
        except OSError:
            pass


def _report(output: bytes) -> dict:
    for line in reversed(output.decode("utf-8", "replace").splitlines()):
        try:
            report = json.loads(line)
        except ValueError:
            continue
        if isinstance(report, dict):
            return report
    return {}


def _limit_child() -> None:
    """The child's own limits, on Linux: CPU time, address space, a lower priority than the transcriptions, and, if
    its container runs out of memory all the same, the first process the kernel ends, rather than the worker."""
    try:
        import resource
    except ImportError:
        return
    try:
        Path("/proc/self/oom_score_adj").write_text("1000")
    except OSError:
        pass
    for limit, value, hard in ((resource.RLIMIT_CPU, CPU_SECONDS, CPU_SECONDS + 5),
                               (resource.RLIMIT_AS, MEMORY_BYTES, MEMORY_BYTES)):
        # never above a limit the container already sets
        current = resource.getrlimit(limit)[1]
        if current != resource.RLIM_INFINITY:
            value, hard = min(value, current), min(hard, current)
        resource.setrlimit(limit, (value, hard))
    try:
        os.nice(NICENESS)
    except OSError:
        pass


def distinct(made) -> Iterator[tuple[str, bytes]]:
    """The levels worth keeping of (level, bytes) given from the hardest (LEVELS' order): none without notes, and none
    with the same notes (start, end, pitch, hand) as the next harder level kept, which a simple source gives, such as
    a lone melody at every level. full is kept whenever it has notes."""
    from app.transcription.ensemble import piano_levels

    kept = None
    for level, midi in made:
        notes = piano_levels.played_notes(midi)
        if notes and notes != kept:
            kept = notes
            yield level, midi


def distinct_levels(data: bytes, max_notes: int = MAX_NOTES) -> Iterator[tuple[str, bytes]]:
    """(level, bytes) of the levels of an ensemble result worth keeping (distinct), the hardest first."""
    from app.transcription.ensemble import piano_levels

    return distinct(piano_levels.levels(data, LEVELS, max_notes=max_notes))


def _child(midi_path: Path, folder: Path, max_notes: int) -> dict:
    """In the child process: each level made, from the hardest, and written in turn if it is worth keeping, so the
    levels made before a failure stay."""
    from app.transcription.ensemble import piano_levels

    data = midi_path.read_bytes()
    report: dict = {"levels": []}
    try:
        for level, midi in distinct_levels(data, max_notes):
            write_atomically(folder / file_name(level), midi)
            report["levels"].append(level)
    except piano_levels.TooManyNotes as error:
        report["error"] = f"too many notes: {error}"
    except (ValueError, EOFError, OSError) as error:
        report["error"] = f"{type(error).__name__}: {error}"
    except MemoryError:
        report["error"] = f"more memory than {MEMORY_BYTES // 1024 ** 2} MB"
    return report


def main(argv: list[str] | None = None) -> int:
    midi_path, folder, max_notes = argv if argv is not None else sys.argv[1:]
    _limit_child()
    report = _child(Path(midi_path), Path(folder), int(max_notes))
    print(json.dumps(report), flush=True)
    return 0


class FallbackLevels:
    """Levels a remote worker makes for results from a RunPod endpoint that returns none (an older image), after
    the job is completed with its raw result: at most `limit` at once per process, in background threads, so they
    never delay a result's delivery or the worker's other jobs. A result that finds every place taken gets none,
    with a log line. A restart while one runs leaves that job with raw only."""

    def __init__(self, limit: int = 1):
        self._places = threading.BoundedSemaphore(limit)

    def submit(self, job_id: str, work: Callable[[], None]) -> bool:
        """Run work in the background if a place is free; False, logged, if not."""
        if not self._places.acquire(blocking=False):
            logger.warning("Job %s: no piano levels; the endpoint returned none and the remote worker is busy "
                           "making another job's", job_id)
            return False

        def run() -> None:
            try:
                work()
            except Exception:
                logger.exception("Job %s: piano levels could not be made", job_id)
            finally:
                self._places.release()

        threading.Thread(target=run, name=f"levels-{job_id}", daemon=True).start()
        return True


if __name__ == "__main__":
    sys.exit(main())
