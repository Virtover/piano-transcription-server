import os
import shutil
from dataclasses import dataclass

from app.config import ensure_valid, settings
from app.transcription.device import (
    cpu_batch_size,
    cpu_thread_count,
    cuda_job_capacities,
    gpu_batch_size,
    physical_cpu_count,
)


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


def main() -> None:
    ensure_valid("worker")
    plan = plan_worker(cuda_job_capacities())
    if plan.cpu_threads:
        # Read by the OpenMP and MKL libraries themselves.
        os.environ["OMP_NUM_THREADS"] = str(plan.cpu_threads)
        os.environ["MKL_NUM_THREADS"] = str(plan.cpu_threads)
    for warning in plan.warnings:
        print(f"Warning: {warning}", flush=True)
    print(
        f"Starting Dramatiq with {plan.processes} process(es) and "
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
        ],
    )


if __name__ == "__main__":
    main()
