import pytest

# Needs the worker dependencies (requirements.worker.txt); skipped in an API-only environment.
piano_transcription = pytest.importorskip("app.transcription.piano_transcription")
from app.config import settings


@pytest.fixture
def gpus(monkeypatch):
    state = {"capacities": None, "free": {}, "capacity_calls": 0}

    def capacities():
        state["capacity_calls"] += 1
        return state["capacities"]

    monkeypatch.setattr(piano_transcription, "_cuda_capacities", None)
    monkeypatch.setattr(piano_transcription, "cuda_job_capacities", capacities)
    monkeypatch.setattr(
        piano_transcription.torch.cuda,
        "mem_get_info",
        lambda index: (state["free"][index], 8 * 1024**3),
    )
    return state


def test_cpu_without_cuda(gpus):
    assert piano_transcription.select_device() == "cpu"


def test_cpu_when_no_gpu_has_capacity_at_first_job(gpus):
    gpus["capacities"] = [(0, 0, 512, 8192)]

    assert piano_transcription.select_device() == "cpu"


def test_enforce_cpu_ignores_available_gpu(gpus, monkeypatch):
    monkeypatch.setattr(settings.worker, "enforce_cpu", True)
    gpus["capacities"] = [(0, 4, 6 * 1024**3, 8 * 1024**3)]

    assert piano_transcription.select_device() == "cpu"


def test_busy_gpu_keeps_receiving_jobs(gpus):
    gpus["capacities"] = [(0, 4, 6 * 1024**3, 8 * 1024**3)]
    assert piano_transcription.select_device() == "cuda:0"

    # Later the GPU is full of running jobs; it must not fall back to the CPU.
    gpus["capacities"] = [(0, 0, 100, 8 * 1024**3)]
    assert piano_transcription.select_device() == "cuda:0"
    assert gpus["capacity_calls"] == 1


def test_jobs_go_to_usable_gpu_with_most_free_memory(gpus):
    gpus["capacities"] = [(0, 2, 0, 0), (1, 0, 0, 0), (2, 3, 0, 0)]
    gpus["free"] = {0: 3 * 1024**3, 1: 7 * 1024**3, 2: 1 * 1024**3}
    assert piano_transcription.select_device() == "cuda:0"

    gpus["free"] = {0: 1 * 1024**3, 1: 7 * 1024**3, 2: 2 * 1024**3}
    assert piano_transcription.select_device() == "cuda:2"
