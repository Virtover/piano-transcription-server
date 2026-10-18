import threading
from concurrent.futures import Future

import numpy as np
import pytest

# Needs the worker dependencies (requirements.worker.txt).
piano_transcription = pytest.importorskip("app.transcription.piano_transcription")
torch = piano_transcription.torch

from app.config import settings  # noqa: E402
from app.transcription.batching import GpuOutOfMemory  # noqa: E402
from app.transcription.cancellation import TranscriptionCancelled  # noqa: E402


class FakeModel(torch.nn.Module):
    """Transkun's interface, running out of memory above `limit` segments."""

    segmentHopSizeInSecond = 8
    segmentSizeInSecond = 16
    fs = 44100
    hopSize = 1024
    windowSize = 4096
    targetMIDIPitch = list(range(90))

    def __init__(self, limit):
        super().__init__()
        self.weight = torch.nn.Parameter(torch.zeros(1))
        self.limit = limit
        self.batch_sizes = []

    def transcribeFrames(self, frames, forcedStartPos, **kwargs):
        self.batch_sizes.append(frames.shape[0])
        if frames.shape[0] > self.limit:
            raise torch.cuda.OutOfMemoryError("CUDA out of memory (fake)")
        return [[] for _ in range(frames.shape[0])], [0] * len(forcedStartPos)


def request(seconds=20, progress=None, cancelled=lambda: False):
    return piano_transcription._BatchRequest(
        audio=np.zeros((seconds * 44100, 1), dtype=np.float32),
        progress_callback=(progress.append if progress is not None else None),
        cancellation_callback=cancelled,
        result=Future(),
    )


def test_batch_out_of_memory_propagates_without_resolving_requests():
    requests = [request(), request()]

    with pytest.raises(torch.cuda.OutOfMemoryError):
        piano_transcription._transcribe_audio_batch(FakeModel(limit=1), requests)

    assert not any(item.result.done() for item in requests)


def test_progress_callback_failure_fails_only_that_request():
    def cancelled_during_progress(value):
        raise TranscriptionCancelled()

    failing = request()
    failing.progress_callback = cancelled_during_progress
    progress = []
    healthy = request(progress=progress)

    piano_transcription._transcribe_audio_batch(FakeModel(limit=8), [failing, healthy])

    assert isinstance(failing.result.exception(), TranscriptionCancelled)
    assert healthy.result.result() == []
    assert progress == sorted(progress) and progress[-1] == 1.0


@pytest.fixture
def batcher(monkeypatch):
    def make(limit, batch_size):
        model = FakeModel(limit)
        monkeypatch.setattr(piano_transcription, "load_model", lambda device: model)
        monkeypatch.setattr(settings.worker, "batch_size", batch_size)
        monkeypatch.setattr(settings.worker, "batch_timeout", 1.0)
        return piano_transcription._GpuBatcher("cpu"), model

    return make


def submit_all(batcher, count):
    progress = [[] for _ in range(count)]
    results = [None] * count

    def run(index):
        try:
            results[index] = batcher.submit(np.zeros((20 * 44100, 1), dtype=np.float32), progress[index].append, None)
        except Exception as error:
            results[index] = error

    threads = [threading.Thread(target=run, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results, progress


def test_batcher_splits_on_out_of_memory_and_lowers_batch_size(batcher):
    gpu_batcher, model = batcher(limit=2, batch_size=4)

    results, progress = submit_all(gpu_batcher, 4)

    assert results == [[], [], [], []]
    assert 4 in model.batch_sizes
    assert gpu_batcher.batch_size == 2
    for values in progress:
        assert values == sorted(values)
        assert values[-1] == 1.0


def test_batcher_fails_single_request_that_never_fits(batcher):
    gpu_batcher, _ = batcher(limit=0, batch_size=2)

    results, _ = submit_all(gpu_batcher, 1)

    assert isinstance(results[0], GpuOutOfMemory)
    assert gpu_batcher.batch_size == 2


def test_batch_size_for_device_uses_startup_capacity(monkeypatch):
    monkeypatch.setattr(piano_transcription, "_cuda_capacities", {0: 3, 1: 7})
    monkeypatch.setattr(settings.worker, "batch_size", None)
    monkeypatch.setattr(settings.worker, "max_concurrency", None)

    assert piano_transcription.batch_size_for_device("cuda:0") == 3
    assert piano_transcription.batch_size_for_device("cuda:1") == 7

    monkeypatch.setattr(settings.worker, "batch_size", 5)
    assert piano_transcription.batch_size_for_device("cuda:0") == 3
    assert piano_transcription.batch_size_for_device("cuda:1") == 5
    assert piano_transcription.batch_size_for_device("cpu") == 5
