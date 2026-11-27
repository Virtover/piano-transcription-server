"""The precision the networks run in on a device. Added to the original, which always uses bfloat16 on CUDA.

bfloat16 is native from Ampere GPUs (compute capability 8.0) on. Older GPUs, such as Pascal (GTX 10 series) and Turing,
have no bfloat16 kernels for every operation the networks use (cuDNN's GRU among them), so they run in float32: more
precise, slower, and with about twice the activation memory. The CPU runs float32, as in the original.
"""
from __future__ import annotations

import functools

import torch


@functools.lru_cache(maxsize=None)
def _bf16(device: str) -> bool:
    d = torch.device(device)
    if d.type != "cuda" or not torch.cuda.is_available():
        return False
    return torch.cuda.get_device_capability(d.index if d.index is not None else torch.cuda.current_device()) >= (8, 0)


def bf16(device) -> bool:
    """Whether the networks run in bfloat16 on device."""
    return _bf16(str(device))


def autocast(device):
    """The autocast context of the networks on device: bfloat16 where it is native, else off (float32)."""
    return torch.autocast("cuda", dtype=torch.bfloat16, enabled=bf16(device))
