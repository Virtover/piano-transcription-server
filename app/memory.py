"""RAM for local jobs: a worker starts a job only when the RAM it needs leaves WORKER_MIN_FREE_RAM_GIB free, so jobs
never push the machine into its out-of-memory killer, which would end every job of the worker, not one.

- What is free is the container's own: inside Docker or Kubernetes the limit and usage of its cgroup (the worker's
  memory limit, WORKER_MEMORY_LIMIT under Compose), without the file cache the kernel can drop; never more than the
  machine (or Docker Desktop's virtual machine) has available.
- A job's need comes from its mode and length (measured: job_ram_bytes), plus the libraries the first job of a kind
  loads into the worker process once.
- Each job's need is reserved while it runs, under a lock, so two jobs starting together cannot both take the last
  room. Only the part of the reservations the jobs do not use yet is held back: what the process grew by since it last
  ran no job is in use already, and the free RAM no longer counts it. So the guard costs no concurrency the machine has
  room for. A job with no other job reserved here always starts: one larger than the machine runs alone instead of
  waiting forever.

No torch import.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

GIB = 1024**3
MIB = 1024**2
# Measured on an RTX 3060 Ti worker (RssAnon of the process). Piano (Transkun) keeps the whole recording: 0.14 GiB for
# 4 minutes, 1.7 GiB for an hour. Ensemble keeps per-note data and the drum stage's results: 0.38 GiB for 4 minutes,
# 0.52 GiB for an hour, 0.34 GiB for 90 minutes (past an hour the drums' results are files); counted with a margin. The first job of each kind loads its libraries' kernels
# and buffers into the process, which keeps them: about 0.85 GiB for piano, 1.6 GiB for ensemble.
PIANO_BASE = 150 * MIB
PIANO_PER_SECOND = 0.5 * MIB
ENSEMBLE_BASE = 350 * MIB
ENSEMBLE_PER_SECOND = 0.12 * MIB
ENSEMBLE_PER_SECOND_PAST_HOUR = 0.03 * MIB
FIRST_USE = {"piano": 900 * MIB, "ensemble": 1700 * MIB}
# A job's piano levels (app.variants), of either mode, are made after its model step, in a child process beside the
# worker's: about 25 MiB with 630 MiB for ten hours of a dense band (19 notes a second); for piano transcriptions
# (about 6 notes a second) 27 MiB for 4 minutes and 33 MiB for 18. Counted on top, cautiously, with a band's rate.
LEVELS_BASE = 40 * MIB
LEVELS_PER_SECOND = 0.018 * MIB

CGROUP = Path("/sys/fs/cgroup")


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _stat(path: Path, *names: str) -> int:
    """The sum of the named counters of a memory.stat file."""
    total = 0
    for line in (_read(path) or "").splitlines():
        key, _, value = line.partition(" ")
        if key in names and value.strip().isdigit():
            total += int(value)
    return total


def cgroup_available(root: Path = CGROUP) -> int | None:
    """Bytes the container may still use under its cgroup's limit, the file cache it can drop counted as free; None
    without a limit. cgroup v2 (memory.max), else v1 (memory/memory.limit_in_bytes)."""
    limit = _read(root / "memory.max")
    if limit is not None:
        if not limit.isdigit():
            return None                                  # "max": no limit
        used = int(_read(root / "memory.current") or 0)
        # The kernel drops file cache, active or not, before it runs out (the jobs' memory-mapped files fill it).
        cache = _stat(root / "memory.stat", "inactive_file", "active_file")
        return max(0, int(limit) - (used - cache))
    limit = _read(root / "memory" / "memory.limit_in_bytes")
    if limit is None or not limit.isdigit() or int(limit) >= 1 << 60:
        return None
    used = int(_read(root / "memory" / "memory.usage_in_bytes") or 0)
    cache = _stat(root / "memory" / "memory.stat", "total_inactive_file", "total_active_file")
    return max(0, int(limit) - (used - cache))


def available_bytes() -> int | None:
    """RAM this process may still use: the machine's available memory, and no more than its container's limit leaves.
    None when neither can be read."""
    machine = None
    try:
        import psutil

        machine = psutil.virtual_memory().available
    except Exception:
        pass
    container = cgroup_available()
    values = [v for v in (machine, container) if v is not None]
    return min(values) if values else None


def process_bytes() -> int | None:
    """This process's anonymous memory (RssAnon), or None where it cannot be read."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("RssAnon:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def min_free_bytes() -> int:
    return int(settings.worker.min_free_ram_gib * GIB)


def job_kind(job: dict[str, str]) -> str:
    return "ensemble" if job.get("mode") == "arrangement" else "piano"


def job_ram_bytes(job: dict[str, str]) -> int:
    """The RAM a job holds at its peak, from its mode and length (without the first-use cost)."""
    try:
        seconds = max(0.0, float(job.get("duration") or 0))
    except ValueError:
        seconds = 0.0
    if job_kind(job) == "ensemble":
        return int(ENSEMBLE_BASE + ENSEMBLE_PER_SECOND * min(seconds, 3600)
                   + ENSEMBLE_PER_SECOND_PAST_HOUR * max(0.0, seconds - 3600)
                   + LEVELS_BASE + LEVELS_PER_SECOND * seconds)
    return int(PIANO_BASE + PIANO_PER_SECOND * seconds + LEVELS_BASE + LEVELS_PER_SECOND * seconds)


class Ledger:
    """The RAM reserved by the jobs running in this process."""

    def __init__(self, available=available_bytes, process=process_bytes):
        self.available = available
        self.process = process
        self.reserved: dict[str, int] = {}
        self.used_kinds: set[str] = set()
        # The process's memory while it ran no job: what it has above it is the running jobs' memory in use.
        self.idle_bytes: int | None = None
        self._lock = threading.Lock()

    def unused_reserved(self) -> int:
        """The part of the running jobs' reservations they do not use yet (call with the lock held)."""
        total = sum(self.reserved.values())
        now = self.process()
        if now is None or self.idle_bytes is None:
            return total
        return max(0, total - max(0, now - self.idle_bytes))

    def need(self, job: dict[str, str]) -> int:
        kind = job_kind(job)
        return job_ram_bytes(job) + (0 if kind in self.used_kinds else FIRST_USE[kind])

    def reserve(self, job_id: str, job: dict[str, str]) -> tuple[bool, int, int | None]:
        """(whether job_id may start now and holds its reservation, its need, what is free). Free RAM, less what the
        running jobs reserved (some of it in use already: a cautious count), must leave WORKER_MIN_FREE_RAM_GIB."""
        need = self.need(job)
        with self._lock:
            if job_id in self.reserved:
                return True, need, None
            if not self.reserved:
                self.idle_bytes = self.process()
            free = self.available()
            others = self.unused_reserved()
            if not self.reserved or free is None or free - others - need >= min_free_bytes():
                self.reserved[job_id] = need
                self.used_kinds.add(job_kind(job))
                return True, need, free
            return False, need, free

    def release(self, job_id: str) -> None:
        with self._lock:
            self.reserved.pop(job_id, None)


ledger = Ledger()
