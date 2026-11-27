"""Running out of disk space: keep MIN_FREE_DISK_GIB free on the data disk, before it runs out rather than after.

- A worker takes a job only when the job's estimated files (job_disk_bytes) leave MIN_FREE_DISK_GIB free; otherwise the
  job stays queued and is offered again (app.worker.tasks), and fails clearly once it waited too long.
- A job whose write fails for lack of space (ENOSPC, or a full quota) is tried again once, on another device or later,
  then fails with StorageFull's message.
- The API refuses new submissions while less than MIN_FREE_DISK_GIB is free (503, error code storage_full).
- The API's leader warns while less than twice that is free (watch).

No torch import.
"""
from __future__ import annotations

import errno
import logging
import os
import platform
import shutil
import time
from pathlib import Path

from app.config import settings
from app.errors import UserFacingError

logger = logging.getLogger(__name__)

GIB = 1024**3
# A job's files, from its length: the download (compressed audio or video), the 44.1 kHz WAV (88 KB a second), the
# FLAC sent to RunPod, the MIDI; an ensemble job also keeps its resampled copies and features (about 0.25 MB a second).
JOB_BASE_BYTES = 50 * 1024**2
PIANO_BYTES_PER_SECOND = 200 * 1024
ENSEMBLE_BYTES_PER_SECOND = 450 * 1024
STORAGE_FULL = "storage_full"
WATCH_INTERVAL = 300.0
REPORT_INTERVAL = 3600.0


class StorageFull(UserFacingError, RuntimeError):
    code = STORAGE_FULL

    def __init__(self) -> None:
        super().__init__("The server is out of storage space; try again later")


def data_dir() -> Path:
    return Path(settings.service.data_dir)


# An empty folder on the host's drive, mounted by Docker Compose
# (HOST_DISK_PROBE): under Docker Desktop, its free space is the host drive's.
HOST_PROBE = Path("/run/host-disk")


def in_docker_desktop_vm() -> bool:
    """Docker Desktop runs containers in a virtual machine (WSL 2 on Windows,
    LinuxKit on macOS) whose disk can grow up to its limit, not to what the
    host's drive has free."""
    release = platform.release().lower()
    return "microsoft" in release or "linuxkit" in release


def host_free_bytes() -> int | None:
    """Free bytes on the host drive that holds Docker Desktop's virtual disk,
    or None outside Docker Desktop or without the probe. On Linux, Docker's
    volumes are on the host's own disks, which the data folder already
    measures."""
    if not in_docker_desktop_vm() or not os.path.ismount(HOST_PROBE):
        return None
    try:
        return shutil.disk_usage(HOST_PROBE).free
    except OSError:
        return None


def free_bytes(path: Path | None = None) -> int | None:
    """Free bytes the data folder (or path) can still use, or None when it
    cannot be read: its own disk's free space and, under Docker Desktop, no
    more than the host drive has, since the virtual disk can only grow into
    what that drive has free."""
    try:
        free = shutil.disk_usage(path or data_dir()).free
    except OSError:
        return None
    host = host_free_bytes()
    return free if host is None else min(free, host)


def min_free_bytes() -> int:
    return int(settings.limits.min_free_disk_gib * GIB)


def job_disk_bytes(job: dict[str, str]) -> int:
    """The disk space a job's files take at most, estimated from its length and mode."""
    try:
        seconds = max(0.0, float(job.get("duration") or 0))
    except ValueError:
        seconds = 0.0
    per_second = ENSEMBLE_BYTES_PER_SECOND if job.get("mode") == "arrangement" else PIANO_BYTES_PER_SECOND
    return int(JOB_BASE_BYTES + per_second * seconds)


def room_for(need: int, path: Path | None = None) -> bool:
    """Whether need more bytes leave MIN_FREE_DISK_GIB free. True when the free space cannot be read: a disk this
    server cannot measure is not a reason to stop."""
    free = free_bytes(path)
    return free is None or free - need >= min_free_bytes()


def is_disk_full(error: BaseException | None) -> bool:
    """Whether error, or what caused it, is a write that failed for lack of space."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, StorageFull):
            return True
        if isinstance(error, OSError) and error.errno in (errno.ENOSPC, errno.EDQUOT):
            return True
        error = error.__cause__ or error.__context__
    return False


def level(path: Path | None = None) -> str:
    """ok, low (less than twice MIN_FREE_DISK_GIB free) or full (less than MIN_FREE_DISK_GIB), or unknown."""
    free = free_bytes(path)
    if free is None:
        return "unknown"
    if free < min_free_bytes():
        return "full"
    return "low" if free < 2 * min_free_bytes() else "ok"


def watch(stop=None, clock=time.monotonic, sleep=time.sleep) -> None:
    """Log the data disk's state every WATCH_INTERVAL while it is low; report it at most every REPORT_INTERVAL while
    it is full. Runs in the API's leader."""
    from app import error_report

    reported_at = None
    while stop is None or not stop():
        try:
            state, free = level(), free_bytes()
            if state in ("low", "full"):
                text = (f"The data disk ({data_dir()}) has {free / GIB:.1f} GiB free; MIN_FREE_DISK_GIB is "
                        f"{settings.limits.min_free_disk_gib:g}")
                if state == "full":
                    logger.error("%s: new jobs are refused until space is freed", text)
                    if reported_at is None or clock() - reported_at >= REPORT_INTERVAL:
                        error_report.report_exception(RuntimeError(text), {"Disk": "full"})
                        reported_at = clock()
                else:
                    logger.warning("%s: it is running low", text)
        except Exception:
            logger.exception("Could not check the data disk")
        sleep(WATCH_INTERVAL)
