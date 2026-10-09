import os
import shutil

from app.transcription.device import (
    automatic_cpu_threads,
    cuda_job_capacities,
    physical_cpu_count,
)


DEFAULT_CPU_CONCURRENCY = 1


def configured_value(name: str) -> int | None:
    value = os.environ.get(name, "").strip().lower()

    if not value or value == "auto":
        return None

    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer or 'auto'") from error

    if parsed < 1:
        raise ValueError(f"{name} must be at least 1")

    return parsed


def cpu_count() -> int:
    return physical_cpu_count()


def cpu_thread_count() -> int:
    configured = configured_value("WORKER_THREADS")
    automatic = automatic_cpu_threads()
    return min(configured, cpu_count()) if configured else automatic


def batch_size() -> int:
    return configured_value("WORKER_BATCH_SIZE") or 6


def configure_cpu_environment(gpu: tuple[int, int, str] | None) -> None:
    if gpu:
        return

    thread_count = str(cpu_thread_count())
    os.environ["WORKER_THREADS"] = thread_count
    os.environ["OMP_NUM_THREADS"] = thread_count
    os.environ["MKL_NUM_THREADS"] = thread_count


def gpu_capacity() -> tuple[int, int, str] | None:
    capacities = cuda_job_capacities()
    if not capacities:
        return None

    device_capacities = [capacity for _, capacity, _, _ in capacities]
    total_capacity = sum(device_capacities)
    if total_capacity < 1:
        return None

    summary = ", ".join(
        f"GPU {index}: {capacity} job(s)"
        for index, capacity in enumerate(device_capacities)
    )
    return len(device_capacities), total_capacity, summary


def worker_capacity() -> tuple[int, int, str]:
    configured_max = configured_value("WORKER_MAX_CONCURRENCY")
    gpu = gpu_capacity()
    if gpu:
        device_count, gpu_capacity_value, gpu_summary = gpu
        automatic_capacity = min(cpu_count(), gpu_capacity_value)
        automatic_processes = 1
        resource_summary = (
            f"{device_count} GPU(s), up to {gpu_capacity_value} job(s) by VRAM "
            f"({gpu_summary})"
        )
    else:
        automatic_capacity = DEFAULT_CPU_CONCURRENCY
        automatic_processes = 1
        resource_summary = (
            f"{cpu_count()} CPU(s), no CUDA GPU detected, "
            "one batched CPU job"
        )

    if configured_max:
        automatic_capacity = min(automatic_capacity, configured_max)

    processes = automatic_processes
    if gpu:
        threads = automatic_capacity
    else:
        threads = batch_size()

    return processes, threads, resource_summary


def main() -> None:
    processes, threads, resource_summary = worker_capacity()
    configure_cpu_environment(gpu_capacity())
    print(
        f"Starting Dramatiq with {processes} process(es) and "
        f"{threads} thread(s) per process ({resource_summary})",
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
            str(processes),
            "--threads",
            str(threads),
        ],
    )


if __name__ == "__main__":
    main()
