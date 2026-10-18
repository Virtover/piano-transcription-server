import subprocess
import sys
import types

import pytest

from app.transcription import device
from app.worker import launcher

GiB = 1024**3
MiB = 1024**2


@pytest.fixture
def gpus(monkeypatch):
    """Fake torch with configurable GPUs: (uuid, CUDA free bytes, total bytes)."""
    state = {"devices": [], "smi": "", "smi_error": None}

    cuda = types.SimpleNamespace(
        is_available=lambda: bool(state["devices"]),
        device_count=lambda: len(state["devices"]),
        mem_get_info=lambda index: state["devices"][index][1:],
        get_device_properties=lambda index: types.SimpleNamespace(uuid=state["devices"][index][0]),
    )
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(cuda=cuda))

    def fake_run(command, **kwargs):
        assert command[0] == "nvidia-smi"
        if state["smi_error"]:
            raise state["smi_error"]
        return subprocess.CompletedProcess(command, 0, stdout=state["smi"])

    monkeypatch.setattr(device.subprocess, "run", fake_run)
    return state


def test_nvidia_smi_output_is_parsed(gpus):
    gpus["smi"] = "GPU-AAAA-1, 5812\nGPU-bbbb-2, 100\nnot a line\n"

    assert device.nvidia_smi_free_memory() == {"aaaa-1": 5812 * MiB, "bbbb-2": 100 * MiB}


@pytest.mark.parametrize(
    "error",
    [FileNotFoundError("nvidia-smi"), subprocess.CalledProcessError(9, "nvidia-smi"), subprocess.TimeoutExpired("nvidia-smi", 10)],
)
def test_missing_or_failing_nvidia_smi_is_ignored(gpus, error):
    gpus["smi_error"] = error

    assert device.nvidia_smi_free_memory() == {}


def test_lower_nvidia_smi_free_memory_is_used(gpus):
    # WSL: CUDA reports 7.0 GiB free, but Windows programs use 1.3 GiB more.
    gpus["devices"] = [("aaaa-1", 7126 * MiB, 8 * GiB)]
    gpus["smi"] = "GPU-aaaa-1, 5812\n"

    [(index, capacity, free, total)] = device.cuda_job_capacities()

    assert free == 5812 * MiB
    # floor((5.68 - 1.0) / 1.1) = 4 jobs instead of 5
    assert (index, capacity, total) == (0, 4, 8 * GiB)


def test_cuda_free_memory_is_used_when_lower(gpus):
    gpus["devices"] = [("aaaa-1", 5 * GiB, 8 * GiB)]
    gpus["smi"] = "GPU-aaaa-1, 6000\n"

    assert device.cuda_job_capacities()[0][1:3] == (3, 5 * GiB)


def test_cuda_free_memory_is_used_without_nvidia_smi(gpus):
    gpus["devices"] = [("aaaa-1", 7126 * MiB, 8 * GiB)]
    gpus["smi_error"] = FileNotFoundError("nvidia-smi")

    assert device.cuda_job_capacities()[0][1:3] == (5, 7126 * MiB)


def test_gpus_are_matched_by_uuid_not_index(gpus):
    gpus["devices"] = [("aaaa-1", 10 * GiB, 12 * GiB), ("bbbb-2", 10 * GiB, 12 * GiB)]
    gpus["smi"] = "GPU-bbbb-2, 2048\nGPU-aaaa-1, 9216\n"

    capacities = device.cuda_job_capacities()

    assert [(free, capacity) for _, capacity, free, _ in capacities] == [(9 * GiB, 7), (2 * GiB, 0)]


def test_no_cuda(gpus):
    assert device.cuda_job_capacities() is None


def test_launcher_summary_shows_free_memory(gpus, monkeypatch):
    gpus["devices"] = [("aaaa-1", 7126 * MiB, 8 * GiB)]
    gpus["smi"] = "GPU-aaaa-1, 5812\n"

    assert launcher.gpu_capacity(device.cuda_job_capacities()) == (1, 4, "GPU 0: 4 job(s), batch 4, 5.68 GiB free")


def test_launcher_uses_cpu_when_no_job_fits(gpus, monkeypatch):
    gpus["devices"] = [("aaaa-1", 2 * GiB, 4 * GiB)]
    gpus["smi"] = "GPU-aaaa-1, 1500\n"

    assert launcher.gpu_capacity(device.cuda_job_capacities()) is None
