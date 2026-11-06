import logging
import os
import subprocess

from app.config import gpu_reserve_gib, settings

try:
    import psutil
except ImportError:
    psutil = None


logger = logging.getLogger(__name__)

# VRAM of the tempo map's beat tracker (app/transcription/tempo.py), which
# runs on the job's GPU one job at a time. Only its weights and one 30 s
# chunk are on the GPU, so this does not depend on the audio's length.
TEMPO_MAP_MEMORY_GIB = 0.25

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
    configured = settings.worker.batch_size
    if configured is not None:
        return configured

    cpu_limit = cpu_thread_count()
    if psutil is None:
        return cpu_limit

    available_memory = psutil.virtual_memory().available
    worker = settings.worker
    # Any thread may run an arrangement when they are on.
    per_job = max(worker.cpu_memory_per_job_gib, worker.cpu_arrangement_memory_gib) if worker.arrangement \
        else worker.cpu_memory_per_job_gib
    memory_limit = int(
        max(0, available_memory - worker.cpu_reserve_gib * 1024**3)
        // (per_job * 1024**3)
    )
    return max(1, min(cpu_limit, memory_limit))


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


def gpu_budget_gib(free_memory: float, config=None) -> float:
    """GiB of free_memory bytes of VRAM left for model steps, after what is
    loaded once (WORKER_GPU_RESERVE_GIB) and the beat tracker of the tempo
    map when it is on. Transcription jobs and arrangement steps share it."""
    worker = (config or settings).worker
    reserved = gpu_reserve_gib(worker) + (TEMPO_MAP_MEMORY_GIB if worker.tempo_map else 0)
    return max(0.0, free_memory / 1024**3 - reserved)


def job_capacity(free_memory: float, config=None) -> int:
    """Transcription jobs that fit in free_memory bytes of VRAM."""
    worker = (config or settings).worker
    # Rounded, so that a budget of exactly n jobs is not lost to float error.
    return int(round(gpu_budget_gib(free_memory, config) / worker.memory_per_job_gib, 9))


def arrangement_fits(free_memory: float, config=None) -> bool:
    """Whether a GPU with free_memory bytes free takes arrangement jobs: the
    feature is on and one arrangement step fits in its budget."""
    worker = (config or settings).worker
    return worker.arrangement and gpu_budget_gib(free_memory, config) + 1e-9 >= worker.arrangement_memory_gib


def cpu_budget_gib() -> float:
    """GiB of system RAM for CPU steps, after WORKER_CPU_RESERVE_GIB. Steps
    run one at a time on the CPU, so this only limits what one step takes."""
    if psutil is None:
        return float("inf")
    return max(0.0, psutil.virtual_memory().available / 1024**3 - settings.worker.cpu_reserve_gib)


def cuda_job_capacities() -> list[tuple[int, int, int, int]] | None:
    if settings.worker.enforce_cpu:
        return None
    try:
        import torch

        if not torch.cuda.is_available():
            return None

        capacities = []
        smi_free_memory = nvidia_smi_free_memory()
        for device_index in range(torch.cuda.device_count()):
            free_memory, total_memory = device_free_memory(
                torch,
                device_index,
                smi_free_memory,
            )
            capacity = job_capacity(free_memory)
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
