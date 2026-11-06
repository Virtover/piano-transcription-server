import random
import threading
import time

import pytest

np = pytest.importorskip("numpy")
# Needs the worker dependencies (requirements.worker.txt).
arrangement = pytest.importorskip("app.transcription.arrangement")
torch = arrangement.torch

from app.transcription.batching import GpuOutOfMemory  # noqa: E402
from app.transcription.budget import DeviceBudget  # noqa: E402
from app.transcription.cancellation import TranscriptionCancelled  # noqa: E402

WINDOW = 4


class FakeModel:
    """AMT-APC's windows: a window's outputs are its request's ID and start
    frame, so the joined outputs show which windows ran, in which order."""

    def __init__(self, budget):
        self.budget = budget
        self.log = []
        self.lock = threading.Lock()
        self.out_of_memory = {}
        self.errors = set()
        self.delay = 0.0
        self.peak = 0.0

    def run_segment(self, model, window, style, device):
        request_id, start = window
        with self.lock:
            self.log.append((request_id, start))
            self.peak = max(self.peak, self.budget.in_use_gib)
            remaining = self.out_of_memory.get((request_id, start), 0)
            if remaining:
                self.out_of_memory[(request_id, start)] = remaining - 1
        if remaining:
            raise torch.cuda.OutOfMemoryError("CUDA out of memory (fake)")
        if (request_id, start) in self.errors:
            raise ValueError("broken window (fake)")
        if self.delay:
            time.sleep(self.delay)
        value = np.full((WINDOW, 88), request_id * 1000 + start, dtype=np.float32)
        return value, value, value, value.astype(np.int64)


@pytest.fixture
def fake(monkeypatch, tmp_path):
    budget = DeviceBudget(4.0)
    model = FakeModel(budget)
    weights = tmp_path / "apc.pth"
    weights.write_bytes(b"fake")
    monkeypatch.setattr(arrangement, "WEIGHTS", weights)
    monkeypatch.setattr(arrangement, "budget_for_device", lambda device: budget)
    monkeypatch.setattr(arrangement.amt_apc, "load_model", lambda path, device: object())
    # A feature is its request's ID in every frame; windows of WINDOW frames.
    monkeypatch.setattr(arrangement.amt_apc, "pad_features", lambda feature: feature)
    monkeypatch.setattr(arrangement.amt_apc, "segment_starts", lambda count: list(range(0, count, WINDOW)))
    monkeypatch.setattr(arrangement.amt_apc, "segment_input", lambda padded, start: (int(padded[0]), start))
    monkeypatch.setattr(arrangement.amt_apc, "run_segment", model.run_segment)
    monkeypatch.setattr(arrangement.amt_apc, "style_vector", lambda style: np.zeros(24, dtype=np.float32))
    batcher = arrangement._ArrangementBatcher("cuda:0")
    return batcher, model, budget


def feature(request_id, frames):
    return np.full(frames, request_id, dtype=np.float32)


def expected(request_id, frames):
    starts = np.arange(frames) // WINDOW * WINDOW
    return request_id * 1000 + starts


def submit_all(batcher, jobs, cancelled=None, progress=None):
    """Submit (request_id, frames) jobs from their own threads; results by ID."""
    results = {}

    def run(request_id, frames):
        try:
            results[request_id] = batcher.submit(
                feature(request_id, frames),
                "level2",
                (progress.setdefault(request_id, []).append if progress is not None else None),
                (lambda: cancelled(request_id)) if cancelled else None,
            )
        except Exception as error:
            results[request_id] = error

    threads = [threading.Thread(target=run, args=job) for job in jobs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads), "a request never finished"
    return results


def test_the_windows_are_joined_in_order_and_trimmed_to_the_audio(fake):
    batcher, _, _ = fake

    onset, offset, mpe, velocity = submit_all(batcher, [(1, 10)])[1]

    assert len(onset) == 10
    assert list(onset[:, 0]) == list(expected(1, 10))
    assert list(velocity[:, 0]) == list(expected(1, 10))


def test_jobs_take_turns_one_window_at_a_time(fake):
    batcher, model, _ = fake
    model.delay = 0.01

    submit_all(batcher, [(1, 40), (2, 40), (3, 40)])

    # Once all three run, none gets two windows while another waits.
    first_all = max(next(i for i, (rid, _) in enumerate(model.log) if rid == r) for r in (1, 2, 3))
    shortest_end = min(max(i for i, (rid, _) in enumerate(model.log) if rid == r) for r in (1, 2, 3))
    turns = [rid for rid, _ in model.log[first_all:shortest_end]]
    assert all(len(set(turns[i:i + 3])) == 3 for i in range(len(turns) - 2))


def test_a_cancelled_job_stops_and_the_others_finish(fake):
    batcher, model, budget = fake
    model.delay = 0.005
    progress = {}

    def cancelled(request_id):
        return request_id == 2 and len(progress.get(2, [])) > 3

    results = submit_all(batcher, [(1, 60), (2, 60)], cancelled, progress)

    assert isinstance(results[2], TranscriptionCancelled)
    assert list(results[1][0][:, 0]) == list(expected(1, 60))
    ran = [start for rid, start in model.log if rid == 2]
    assert len(ran) < 15
    assert budget.in_use_gib == 0


def test_a_window_out_of_memory_once_is_retried(fake):
    batcher, model, _ = fake
    model.out_of_memory[(1, 8)] = 1

    onset = submit_all(batcher, [(1, 12)])[1][0]

    assert list(onset[:, 0]) == list(expected(1, 12))
    assert model.log.count((1, 8)) == 2


def test_a_window_that_never_fits_fails_only_its_job(fake):
    batcher, model, budget = fake
    model.out_of_memory[(1, 4)] = 5

    results = submit_all(batcher, [(1, 12), (2, 12)])

    assert isinstance(results[1], GpuOutOfMemory)
    assert list(results[2][0][:, 0]) == list(expected(2, 12))
    assert budget.in_use_gib == 0


def test_an_error_fails_only_its_job_and_the_batcher_keeps_working(fake):
    batcher, model, _ = fake
    model.errors.add((1, 4))

    results = submit_all(batcher, [(1, 12), (2, 12)])
    again = submit_all(batcher, [(3, 8)])

    assert isinstance(results[1], ValueError)
    assert list(results[2][0][:, 0]) == list(expected(2, 12))
    assert list(again[3][0][:, 0]) == list(expected(3, 8))


def test_progress_rises_to_one(fake):
    batcher, _, _ = fake
    progress = {}

    submit_all(batcher, [(1, 20)], progress=progress)

    assert progress[1] == sorted(progress[1])
    assert progress[1][-1] == 1.0


def test_an_empty_audio_gives_no_frames(fake):
    batcher, _, _ = fake

    onset = submit_all(batcher, [(1, 0)])[1][0]

    assert len(onset) == 0


def test_many_jobs_beside_transcription_steps_stay_in_the_budget(fake):
    """High load: 30 arrangements of random lengths while Transkun-sized
    steps take the same device budget from other threads."""
    batcher, model, budget = fake
    random.seed(3)
    jobs = [(request_id, random.randint(1, 80)) for request_id in range(1, 31)]
    stop = threading.Event()
    transcription_peak = []

    def transcription_steps():
        while not stop.is_set():
            with budget.use(random.choice([1.1, 2.2, 3.3])):
                transcription_peak.append(budget.in_use_gib)
                time.sleep(0.001)

    others = [threading.Thread(target=transcription_steps) for _ in range(3)]
    for thread in others:
        thread.start()
    try:
        results = submit_all(batcher, jobs)
    finally:
        stop.set()
        for thread in others:
            thread.join(timeout=10)

    for request_id, frames in jobs:
        assert list(results[request_id][0][:, 0]) == list(expected(request_id, frames))
    assert model.peak <= budget.total_gib + 1e-9
    assert max(transcription_peak) <= budget.total_gib + 1e-9
    assert budget.in_use_gib == 0


def test_a_stopped_batcher_fails_its_waiting_jobs_instead_of_hanging(fake, monkeypatch):
    from app.transcription.batching import BatcherStopped

    batcher, _, _ = fake

    def stop(request):
        raise SystemExit()

    monkeypatch.setattr(batcher, "_turn", stop)
    started = time.monotonic()
    results = submit_all(batcher, [(1, 8)])

    assert isinstance(results[1], BatcherStopped)
    assert time.monotonic() - started < 15
    assert not batcher.thread.is_alive()


def test_get_batcher_replaces_one_whose_thread_ended(monkeypatch):
    class Dead:
        thread = threading.Thread(target=lambda: None)

    dead = Dead()
    dead.thread.start()
    dead.thread.join()
    made = []
    monkeypatch.setattr(arrangement, "_batchers", {"cuda:0": dead})
    monkeypatch.setattr(arrangement, "_ArrangementBatcher", lambda device: made.append(device) or "new")

    assert arrangement.get_batcher("cuda:0") == "new"
    assert made == ["cuda:0"]


def test_model_audio_is_mono_at_the_model_rate():
    stereo = np.stack([np.ones(44100), -np.ones(44100)], axis=1).astype(np.float32) * 0.5
    stereo[:, 0] += 0.25

    mono = arrangement.model_audio(stereo, 44100)

    assert mono.dtype == np.float32
    assert abs(len(mono) - arrangement.amt_apc.SAMPLE_RATE) <= 1
    assert np.allclose(mono[100:-100], 0.125, atol=1e-3)


def test_select_arrangement_device_refuses_when_no_gpu_has_room(monkeypatch):
    piano_transcription = arrangement.piano_transcription
    monkeypatch.setattr(arrangement.settings.worker, "arrangement", True)
    monkeypatch.setattr(arrangement, "select_device", lambda: "cuda:0")
    monkeypatch.setattr(piano_transcription, "_cuda_capacities", {0: 2})
    monkeypatch.setattr(piano_transcription, "_cuda_free_memory", {0: int(1.5 * 1024**3)})

    with pytest.raises(arrangement.ArrangementUnavailable):
        arrangement.select_arrangement_device()

    monkeypatch.setattr(piano_transcription, "_cuda_free_memory", {0: int(4 * 1024**3)})
    assert arrangement.select_arrangement_device() == "cuda:0"
