import math
import os
from typing import Final


DEFAULT_MEMORY_PER_JOB_GIB: Final = 3.0


def memory_per_job_gib() -> float:
    value = os.environ.get(
        "WORKER_MEMORY_PER_JOB_GIB",
        str(DEFAULT_MEMORY_PER_JOB_GIB),
    ).strip()

    try:
        parsed = float(value)
    except ValueError as error:
        raise ValueError(
            "WORKER_MEMORY_PER_JOB_GIB must be a positive number"
        ) from error

    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(
            "WORKER_MEMORY_PER_JOB_GIB must be a positive number"
        )

    return parsed


def cuda_job_capacities() -> list[tuple[int, int, int, int]] | None:
    try:
        import torch

        if not torch.cuda.is_available():
            return None

        memory_budget = memory_per_job_gib() * 1024**3
        capacities = []
        for device_index in range(torch.cuda.device_count()):
            free_memory, total_memory = torch.cuda.mem_get_info(
                device_index,
            )
            capacity = int(free_memory // memory_budget)
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
