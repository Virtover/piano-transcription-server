from concurrent.futures import Future
from dataclasses import dataclass, field

import pytest

from app.transcription import device
from app.transcription.batching import (
    GpuOutOfMemory,
    fail,
    is_cancelled,
    report_progress,
    resolve,
    run_splitting_on_out_of_memory,
)
from app.worker import launcher


class FakeOutOfMemory(RuntimeError):
    pass


@dataclass
class Request:
    name: str
    progress: list = field(default_factory=list)
    cancelled: bool = False
    result: Future = field(default_factory=Future)
    reported_progress: float = 0.0

    def __post_init__(self):
        self.progress_callback = self.progress.append
        self.cancellation_callback = lambda: self.cancelled


class Gpu:
    """Fake transcription that runs out of memory above `limit` requests."""

    def __init__(self, limit, finish_before_oom=()):
        self.limit = limit
        self.finish_before_oom = set(finish_before_oom)
        self.calls = []
        self.freed = 0
        self.lowered = []

    def transcribe(self, requests):
        self.calls.append([request.name for request in requests])
        for request in requests:
            report_progress(request, 0.25)
        if len(requests) > self.limit:
            for request in requests:
                if request.name in self.finish_before_oom:
                    resolve(request, f"early {request.name}")
            raise FakeOutOfMemory()
        for request in requests:
            report_progress(request, 1.0)
            resolve(request, f"midi {request.name}")

    def run(self, requests, **kwargs):
        run_splitting_on_out_of_memory(
            requests,
            self.transcribe,
            lambda error: isinstance(error, FakeOutOfMemory),
            self.free,
            self.lowered.append,
            **kwargs,
        )

    def free(self):
        self.freed += 1


def requests(count):
    return [Request(f"r{index}") for index in range(count)]


def test_batch_that_fits_runs_once():
    gpu = Gpu(limit=4)
    batch = requests(4)

    gpu.run(batch)

    assert gpu.calls == [["r0", "r1", "r2", "r3"]]
    assert [request.result.result() for request in batch] == ["midi r0", "midi r1", "midi r2", "midi r3"]
    assert gpu.lowered == []


def test_out_of_memory_splits_in_half_and_retries_each_half():
    gpu = Gpu(limit=2)
    batch = requests(5)

    gpu.run(batch)

    assert gpu.calls == [
        ["r0", "r1", "r2", "r3", "r4"],
        ["r0", "r1"],
        ["r2", "r3", "r4"],
        ["r2"],
        ["r3", "r4"],
    ]
    assert [request.result.result() for request in batch] == [f"midi r{index}" for index in range(5)]
    assert gpu.lowered == [2, 1]
    assert gpu.freed == 2


def test_single_request_out_of_memory_fails_clearly():
    gpu = Gpu(limit=0)
    batch = requests(2)

    gpu.run(batch)

    for request in batch:
        with pytest.raises(GpuOutOfMemory, match="ran out of memory"):
            request.result.result()


def test_single_request_out_of_memory_can_be_handed_off():
    gpu = Gpu(limit=0)
    handed_off = []

    gpu.run(requests(1), on_single_out_of_memory=handed_off.append)

    assert [request.name for request in handed_off] == ["r0"]
    assert not handed_off[0].result.done()


def test_requests_finished_before_out_of_memory_are_not_retried():
    gpu = Gpu(limit=2, finish_before_oom={"r0"})
    batch = requests(3)

    gpu.run(batch)

    assert gpu.calls[1:] == [["r1"], ["r2"]]
    assert batch[0].result.result() == "early r0"
    assert [request.result.result() for request in batch[1:]] == ["midi r1", "midi r2"]


def test_retried_progress_never_goes_backwards():
    gpu = Gpu(limit=1)
    batch = requests(2)

    gpu.run(batch)

    for request in batch:
        assert request.progress == sorted(request.progress)
        assert request.progress == [0.25, 1.0]


def test_other_errors_are_not_retried():
    batch = requests(2)

    def broken(requests):
        raise ValueError("model failed")

    with pytest.raises(ValueError, match="model failed"):
        run_splitting_on_out_of_memory(batch, broken, lambda error: False, lambda: None, lambda size: None)


def test_future_is_never_resolved_twice():
    request = Request("r")

    resolve(request, "first")
    resolve(request, "second")
    fail(request, RuntimeError("late"))

    assert request.result.result() == "first"


def test_failing_progress_callback_fails_only_its_request():
    cancelled = Request("cancelled")
    other = Request("other")

    def cancelled_callback(value):
        raise RuntimeError("job was cancelled")

    cancelled.progress_callback = cancelled_callback

    assert report_progress(cancelled, 0.5) is False
    assert report_progress(other, 0.5) is True
    assert isinstance(cancelled.result.exception(), RuntimeError)
    assert not other.result.done()


def test_failing_cancellation_callback_counts_as_cancelled():
    request = Request("r")

    def broken():
        raise ConnectionError("redis down")

    request.cancellation_callback = broken

    assert is_cancelled(request) is True
    assert isinstance(request.result.exception(), ConnectionError)


# --- Batch size rules -------------------------------------------------------

@pytest.mark.parametrize(
    ("configured", "max_concurrency", "capacity", "expected", "warns"),
    [
        (None, None, 4, 4, False),
        (None, None, 20, 20, False),
        (None, 3, 8, 3, False),
        (None, None, 0, 1, False),
        (6, None, 4, 4, True),
        (6, None, 8, 6, False),
        (2, None, 8, 2, False),
    ],
)
def test_gpu_batch_size(default_settings, configured, max_concurrency, capacity, expected, warns):
    default_settings.worker.batch_size = configured
    default_settings.worker.max_concurrency = max_concurrency

    size, warning = device.gpu_batch_size(capacity)

    assert size == expected
    assert (warning is not None) == warns
    if warns:
        assert warning == f"WORKER_BATCH_SIZE={configured} is more than this GPU's capacity of {capacity} job(s); using {capacity}"


def test_auto_batch_size_is_per_gpu(default_settings, monkeypatch):
    monkeypatch.setattr(launcher, "physical_cpu_count", lambda: 16)
    gib = 1024**3

    plan = launcher.plan_worker([(0, 3, 4 * gib, 8 * gib), (1, 10, 12 * gib, 24 * gib)])

    assert "GPU 0: 3 job(s), batch 3," in plan.summary
    assert "GPU 1: 10 job(s), batch 10," in plan.summary
    assert plan.threads == 13
    assert plan.warnings == ()


def test_fixed_batch_size_is_clamped_with_warning_at_startup(default_settings, monkeypatch):
    monkeypatch.setattr(launcher, "physical_cpu_count", lambda: 16)
    default_settings.worker.batch_size = 6
    gib = 1024**3

    plan = launcher.plan_worker([(0, 4, 6 * gib, 8 * gib)])

    assert "batch 4" in plan.summary
    assert plan.warnings == ("GPU 0: WORKER_BATCH_SIZE=6 is more than this GPU's capacity of 4 job(s); using 4",)


def test_cpu_batch_size_uses_cpu_and_ram(default_settings, monkeypatch):
    monkeypatch.setattr(device, "physical_cpu_count", lambda: 8)
    memory = type("Memory", (), {"available": 16 * 1024**3})()
    monkeypatch.setattr(
        device,
        "psutil",
        type("Psutil", (), {"cpu_count": lambda logical=False: 8, "virtual_memory": lambda: memory}),
    )

    assert default_settings.worker.batch_size is None
    assert device.cpu_batch_size() == 6
    assert launcher.plan_worker(None).threads == 6

    default_settings.worker.cpu_model_memory_gib = 4
    default_settings.worker.cpu_memory_per_job_gib = 3
    memory.available = 5 * 1024**3
    assert device.cpu_batch_size() == 1

    default_settings.worker.batch_size = 3
    assert launcher.plan_worker(None).threads == 3
