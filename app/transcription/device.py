import logging
import os
import subprocess

from app.config import settings

try:
    import psutil
except ImportError:
    psutil = None


logger = logging.getLogger(__name__)

# WORKER_BATCH_SIZE=auto in CPU mode. CPU mode also runs this many worker threads.
DEFAULT_CPU_BATCH_SIZE = 6


def physical_cpu_count() -> int:
    detected = psutil.cpu_count(logical=False) if psutil else None
    return max(1, detected or os.cpu_count() or 1)


def automatic_cpu_threads() -> int:
    return max(1, physical_cpu_count() - 2)


def cpu_thread_count() -> int:
    """CPU threads for PyTorch and ffmpeg: WORKER_THREADS, at most the
    physical core count, or physical cores minus two for `auto`."""
    configured = settings.worker.threads
    return min(configured, physical_cpu_count()) if configured else automatic_cpu_threads()


def cpu_batch_size() -> int:
    return settings.worker.batch_size or DEFAULT_CPU_BATCH_SIZE


def gpu_batch_size(capacity: int) -> tuple[int, str | None]:
    """Batch size for a GPU that fits `capacity` concurrent jobs.

    `auto` uses the capacity, capped by WORKER_MAX_CONCURRENCY. A fixed
    WORKER_BATCH_SIZE is used as given, but never above the capacity; the
    second value is a warning when it was lowered.
    """
    configured = settings.worker.batch_size
    if configured is None:
        size = capacity
        if settings.worker.max_concurrency:
            size = min(size, settings.worker.max_concurrency)
        return max(1, size), None
    if configured > capacity >= 1:
        return capacity, (
            f"WORKER_BATCH_SIZE={configured} is more than this GPU's capacity of "
            f"{capacity} job(s); using {capacity}"
        )
    return configured, None


def nvidia_smi_free_memory() -> dict[str, int]:
    """Free memory in bytes for each GPU UUID, as reported by nvidia-smi."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=uuid,memory.free",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {}

    free_memory = {}
    for line in result.stdout.splitlines():
        try:
            uuid, free_mib = (value.strip() for value in line.split(","))
            free_memory[uuid.removeprefix("GPU-").lower()] = int(free_mib) * 1024**2
        except ValueError:
            continue
    return free_memory


def device_free_memory(
    torch,
    device_index: int,
    smi_free_memory: dict[str, int],
) -> tuple[int, int]:
    """Free and total memory of a CUDA device.

    Under WSL, CUDA does not count memory used by Windows programs such as
    the desktop or a browser and can report more than 1 GiB too much free
    memory. nvidia-smi sees that usage, so the lower of the two is used.
    """
    free_memory, total_memory = torch.cuda.mem_get_info(device_index)
    uuid = str(torch.cuda.get_device_properties(device_index).uuid).lower()
    device_smi_free_memory = smi_free_memory.get(uuid)
    if device_smi_free_memory is not None and device_smi_free_memory < free_memory:
        logger.info(
            "GPU %s: using nvidia-smi free memory %.2f GiB instead of CUDA's %.2f GiB",
            device_index,
            device_smi_free_memory / 1024**3,
            free_memory / 1024**3,
        )
        free_memory = device_smi_free_memory
    return free_memory, total_memory


def cuda_job_capacities() -> list[tuple[int, int, int, int]] | None:
    try:
        import torch

        if not torch.cuda.is_available():
            return None

        model_budget = settings.worker.model_memory_gib * 1024**3
        job_budget = settings.worker.memory_per_job_gib * 1024**3
        capacities = []
        smi_free_memory = nvidia_smi_free_memory()
        for device_index in range(torch.cuda.device_count()):
            free_memory, total_memory = device_free_memory(
                torch,
                device_index,
                smi_free_memory,
            )
            available_memory = max(0, free_memory - model_budget)
            capacity = int(available_memory // job_budget)
            capacities.append(
                (
                    device_index,
                    capacity,
                    free_memory,
                    total_memory,
                )
            )

        return capacities
    except (ImportError, RuntimeError):
        return None
