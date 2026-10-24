import os
import shutil
import signal
import sys
import time
from dataclasses import dataclass

from app.config import ensure_valid, settings
from app.transcription.device import (
    cpu_batch_size,
    cpu_thread_count,
    cuda_job_capacities,
    gpu_batch_size,
    physical_cpu_count,
)


# Must match the queue names in app.worker.tasks, which is not imported here
# so that this module starts without connecting to Redis.
LOCAL_QUEUE = "default"
REMOTE_QUEUE = "remote"


@dataclass(frozen=True)
class WorkerPlan:
    """Resolved worker resources, computed once at startup."""

    processes: int
    threads: int
    # Set in CPU mode: threads for PyTorch, ffmpeg, OpenMP, and MKL.
    cpu_threads: int | None
    summary: str
    warnings: tuple[str, ...] = ()


def gpu_capacity(capacities: list[tuple[int, int, int, int]] | None) -> tuple[int, int, str] | None:
    if not capacities:
        return None

    device_capacities = [capacity for _, capacity, _, _ in capacities]
    total_capacity = sum(device_capacities)
    if total_capacity < 1:
        return None

    parts = []
    for index, capacity, free_memory, _ in capacities:
        batch = f"batch {gpu_batch_size(capacity)[0]}, " if capacity > 0 else ""
        parts.append(f"GPU {index}: {capacity} job(s), {batch}{free_memory / 1024**3:.2f} GiB free")
    summary = ", ".join(parts)
    return len(device_capacities), total_capacity, summary


def plan_worker(capacities: list[tuple[int, int, int, int]] | None) -> WorkerPlan:
    gpu = gpu_capacity(capacities)
    if gpu:
        device_count, gpu_jobs, gpu_summary = gpu
        warnings = tuple(
            f"GPU {index}: {warning}"
            for index, capacity, _, _ in capacities
            if capacity > 0 and (warning := gpu_batch_size(capacity)[1])
        )
        threads = min(physical_cpu_count(), gpu_jobs)
        if settings.worker.max_concurrency:
            threads = min(threads, settings.worker.max_concurrency)
        return WorkerPlan(
            processes=1,
            threads=threads,
            cpu_threads=None,
            summary=f"{device_count} GPU(s), up to {gpu_jobs} job(s) by VRAM ({gpu_summary})",
            warnings=warnings,
        )

    return WorkerPlan(
        processes=1,
        threads=cpu_batch_size(),
        cpu_threads=cpu_thread_count(),
        summary=f"{physical_cpu_count()} CPU(s), no CUDA GPU detected, batch {cpu_batch_size()}",
    )


def remote_plan() -> WorkerPlan:
    from redis import Redis

    from app.outsource import capacity
    from app.outsource.provider import get_provider

    redis = Redis.from_url(settings.service.redis_url, decode_responses=True)
    if settings.outsource.max_concurrency is None:
        capacity.refresh_max_workers(redis, get_provider())
    threads = capacity.remote_worker_threads(redis)
    limit = capacity.remote_job_limit(redis)
    mode = "auto: max workers x requests per worker" if settings.outsource.max_concurrency is None else "OUTSOURCE_MAX_CONCURRENCY"
    return WorkerPlan(
        processes=1,
        threads=threads,
        cpu_threads=None,
        summary=f"remote dispatcher for {settings.outsource.provider}, up to {limit} remote job(s) ({mode})",
    )


def idle(reason: str) -> None:
    """Wait without doing anything until the container is stopped. The
    remote-worker service always runs, so that OUTSOURCE_PROVIDER in .env is
    the only switch; exiting instead would keep it from coming back when
    Docker restarts."""
    print(reason, flush=True)
    # As PID 1 in a container, Python ignores SIGTERM unless it handles it.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))
    while True:
        time.sleep(3600)


def main(arguments: list[str] | None = None) -> None:
    """Start Dramatiq for a role: `local` (default) runs the model on this
    machine and consumes the default queue; `remote` dispatches to the
    outsourcing provider and consumes the remote queue, without GPU libraries."""
    arguments = sys.argv[1:] if arguments is None else arguments
    role = arguments[0] if arguments else "local"
    if role not in ("local", "remote"):
        raise SystemExit(f"Unknown worker role {role!r}; use local or remote")

    if role == "remote":
        if settings.outsource.provider == "none":
            idle("Outsourcing is off (OUTSOURCE_PROVIDER=none): the remote worker is idle. "
                 "Set OUTSOURCE_PROVIDER in .env and run `docker compose up -d` to turn it on.")
            return
        ensure_valid("remote")
        plan = remote_plan()
        queue = REMOTE_QUEUE
    else:
        ensure_valid("worker")
        plan = plan_worker(cuda_job_capacities())
        queue = LOCAL_QUEUE

    if plan.cpu_threads:
        # Read by the OpenMP and MKL libraries themselves.
        os.environ["OMP_NUM_THREADS"] = str(plan.cpu_threads)
        os.environ["MKL_NUM_THREADS"] = str(plan.cpu_threads)
    for warning in plan.warnings:
        print(f"Warning: {warning}", flush=True)
    print(
        f"Starting Dramatiq on queue {queue} with {plan.processes} process(es) and "
        f"{plan.threads} thread(s) per process ({plan.summary})",
        flush=True,
    )

    dramatiq = shutil.which("dramatiq")
    if dramatiq is None:
        raise RuntimeError("The dramatiq executable is not installed")

    os.execv(
        dramatiq,
        [
            dramatiq,
            "app.worker.tasks",
            "--processes",
            str(plan.processes),
            "--threads",
            str(plan.threads),
            "--queues",
            queue,
        ],
    )


if __name__ == "__main__":
    main()
