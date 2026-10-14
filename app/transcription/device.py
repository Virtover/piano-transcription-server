import logging
import math
import os
import subprocess
from typing import Final

try:
    import psutil
except ImportError:
    psutil = None


DEFAULT_MODEL_MEMORY_GIB: Final = 1.0
DEFAULT_MEMORY_PER_JOB_GIB: Final = 1.1


logger = logging.getLogger(__name__)


def physical_cpu_count() -> int:
    detected = psutil.cpu_count(logical=False) if psutil else None
    return max(1, detected or os.cpu_count() or 1)


def automatic_cpu_threads() -> int:
    return max(1, physical_cpu_count() - 2)


def memory_setting_gib(name: str, default: float) -> float:
    value = os.environ.get(name, str(default)).strip()

    try:
        parsed = float(value)
    except ValueError as error:
        raise ValueError(f"{name} must be a positive number") from error

    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{name} must be a positive number")

    return parsed


def model_memory_gib() -> float:
    return memory_setting_gib(
        "WORKER_MODEL_MEMORY_GIB",
        DEFAULT_MODEL_MEMORY_GIB,
    )


def memory_per_job_gib() -> float:
    return memory_setting_gib(
        "WORKER_MEMORY_PER_JOB_GIB",
        DEFAULT_MEMORY_PER_JOB_GIB,
    )


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

        model_budget = model_memory_gib() * 1024**3
        job_budget = memory_per_job_gib() * 1024**3
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
