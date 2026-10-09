import math
import os
from typing import Final

try:
    import psutil
except ImportError:
    psutil = None


DEFAULT_MODEL_MEMORY_GIB: Final = 3.0
DEFAULT_MEMORY_PER_JOB_GIB: Final = 0.65


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


def cuda_job_capacities() -> list[tuple[int, int, int, int]] | None:
    try:
        import torch

        if not torch.cuda.is_available():
            return None

        model_budget = model_memory_gib() * 1024**3
        job_budget = memory_per_job_gib() * 1024**3
        capacities = []
        for device_index in range(torch.cuda.device_count()):
            free_memory, total_memory = torch.cuda.mem_get_info(
                device_index,
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
