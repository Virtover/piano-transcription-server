"""Ensemble jobs on a device (app/transcription/ensemble_transcription.py): the
device they take, the memory a step holds, how the networks' results become
tracks, and how a file becomes MIDI. The networks are replaced by fakes: the
real ones are compared with the package's in test_ensemble_models.py.

Needs the worker dependencies (requirements.worker.txt), as the module imports
torch and the piano model's module."""

import contextlib
import threading
import types

import pytest

np = pytest.importorskip("numpy")
ensemble_transcription = pytest.importorskip("app.transcription.ensemble_transcription")
torch = ensemble_transcription.torch

from app.transcription import device as device_module  # noqa: E402
from app.transcription.batching import GpuOutOfMemory  # noqa: E402
from app.transcription.budget import StepCancelled  # noqa: E402
from app.transcription.cancellation import TranscriptionCancelled  # noqa: E402
from app.transcription.ensemble import midi as ensemble_midi  # noqa: E402
from app.transcription.ensemble import pitched  # noqa: E402

GiB = 1024**3
piano = ensemble_transcription.piano_transcription


def cpu_tool_module():
    """app.transcription.ensemble.cpu, which imports `resource`: Windows has none, and the limit is then not set."""
    import sys

    try:
        import resource  # noqa: F401
    except ImportError:
        sys.modules["resource"] = types.SimpleNamespace(RLIMIT_AS=9, RLIMIT_CORE=4)
        try:
            from app.transcription.ensemble import cpu
        finally:
            del sys.modules["resource"]
        return cpu
    from app.transcription.ensemble import cpu

    return cpu


@pytest.fixture(autouse=True)
def ensemble_on(default_settings, monkeypatch):
    default_settings.worker.ensemble = True
    monkeypatch.setattr(device_module, "_fp32_gpus", False)
    monkeypatch.setattr(ensemble_transcription, "_free_memory", lambda device: None)


# --- The device ---------------------------------------------------------------------------------------

def test_select_ensemble_device_refuses_when_no_gpu_has_room(monkeypatch):
    monkeypatch.setattr(ensemble_transcription, "select_device", lambda: "cuda:0")
    monkeypatch.setattr(piano, "_cuda_capacities", {0: 2})
    # A GPU of 1.75 GiB in all: too small for an ensemble job beside the reserves.
    monkeypatch.setattr(piano, "_cuda_total_memory", {0: int(1.75 * GiB)})

    with pytest.raises(ensemble_transcription.EnsembleUnavailable):
        ensemble_transcription.select_ensemble_device()

    monkeypatch.setattr(piano, "_cuda_total_memory", {0: int(8 * GiB)})
    assert ensemble_transcription.select_ensemble_device() == "cuda:0"


def test_a_float32_gpu_needs_more_room(monkeypatch):
    monkeypatch.setattr(ensemble_transcription, "select_device", lambda: "cuda:0")
    monkeypatch.setattr(piano, "_cuda_capacities", {0: 2})
    # Room for 2.0 GiB of ensemble models, not for the 2.5 GiB of float32.
    total = int((device_module.gpu_reserve_gib(ensemble_transcription.settings.worker) + 0.25 + 2.2
                 + device_module.GPU_CONTEXT_GIB) * GiB)
    monkeypatch.setattr(piano, "_cuda_total_memory", {0: total})

    assert ensemble_transcription.select_ensemble_device() == "cuda:0"
    monkeypatch.setattr(device_module, "_fp32_gpus", True)
    with pytest.raises(ensemble_transcription.EnsembleUnavailable):
        ensemble_transcription.select_ensemble_device()


def test_the_gpu_with_the_most_free_memory_takes_the_job(monkeypatch):
    monkeypatch.setattr(ensemble_transcription, "select_device", lambda: "cuda:0")
    monkeypatch.setattr(piano, "_cuda_capacities", {0: 2, 1: 2, 2: 2})
    monkeypatch.setattr(piano, "_cuda_total_memory", {0: 8 * GiB, 1: 8 * GiB, 2: int(1.5 * GiB)})
    free = {0: 3 * GiB, 1: 6 * GiB, 2: 7 * GiB}
    monkeypatch.setattr(torch.cuda, "mem_get_info", lambda index: (free[index], 8 * GiB))

    # GPU 2 has the most free memory but is too small to take ensemble jobs at all.
    assert ensemble_transcription.select_ensemble_device() == "cuda:1"


def test_the_cpu_takes_ensemble_jobs_only_when_they_are_on(monkeypatch, default_settings):
    monkeypatch.setattr(ensemble_transcription, "select_device", lambda: "cpu")

    assert ensemble_transcription.select_ensemble_device() == "cpu"
    default_settings.worker.ensemble = False
    with pytest.raises(ensemble_transcription.EnsembleUnavailable):
        ensemble_transcription.select_ensemble_device()


def test_preload_does_nothing_when_ensemble_jobs_are_off(monkeypatch, default_settings):
    default_settings.worker.ensemble = False
    monkeypatch.setattr(ensemble_transcription, "select_ensemble_device", lambda: pytest.fail("selected"))

    ensemble_transcription.preload()


def test_preload_loads_the_models_on_the_gpu_and_checks_the_cpu_tool(monkeypatch):
    loaded = []
    monkeypatch.setattr(ensemble_transcription, "get_models", loaded.append)
    monkeypatch.setattr(ensemble_transcription, "select_ensemble_device", lambda: "cuda:0")

    ensemble_transcription.preload()

    assert loaded == ["cuda:0"]
    cpu = cpu_tool_module()
    checked = []
    monkeypatch.setattr(cpu, "check", lambda: checked.append(1))
    monkeypatch.setattr(ensemble_transcription, "select_ensemble_device", lambda: "cpu")
    ensemble_transcription.preload()
    assert checked == [1] and loaded == ["cuda:0"]

    def none():
        raise ensemble_transcription.EnsembleUnavailable()

    monkeypatch.setattr(ensemble_transcription, "select_ensemble_device", none)
    ensemble_transcription.preload()
    assert loaded == ["cuda:0"] and checked == [1]


# --- A phase in the memory budget ---------------------------------------------------------------------

class FakeBudget:
    def __init__(self, error=None):
        self.held, self.error = [], error

    @contextlib.contextmanager
    def use(self, gib, cancelled=None):
        if self.error:
            raise self.error
        self.held.append(gib)
        yield


@pytest.fixture
def budget(monkeypatch):
    fake = FakeBudget()
    monkeypatch.setattr(ensemble_transcription, "budget_for_device", lambda device: fake)
    return fake


def test_a_phase_holds_the_ensemble_memory_of_the_device(budget, default_settings, monkeypatch):
    default_settings.worker.ensemble_memory_gib = 2.0

    assert ensemble_transcription._phase("cuda:0", lambda: False, lambda: "done") == "done"
    monkeypatch.setattr(device_module, "_fp32_gpus", True)
    ensemble_transcription._phase("cuda:0", lambda: False, lambda: None)

    # More where the GPUs run float32.
    assert budget.held == [2.0, 2.5]


@pytest.mark.parametrize("error", [StepCancelled(), pitched.Cancelled()])
def test_a_cancelled_phase_ends_the_job_as_cancelled(budget, error):
    def run():
        raise error

    with pytest.raises(TranscriptionCancelled):
        ensemble_transcription._phase("cuda:0", lambda: False, run)


def test_a_cancelled_wait_for_memory_ends_the_job_as_cancelled(monkeypatch):
    monkeypatch.setattr(ensemble_transcription, "budget_for_device", lambda device: FakeBudget(StepCancelled()))

    with pytest.raises(TranscriptionCancelled):
        ensemble_transcription._phase("cuda:0", lambda: True, lambda: None)


def test_a_phase_out_of_memory_is_tried_once_more(budget):
    calls = []

    def run():
        calls.append(1)
        if len(calls) == 1:
            raise torch.cuda.OutOfMemoryError("out of memory")
        return "second try"

    assert ensemble_transcription._phase("cuda:0", lambda: False, run) == "second try"
    assert len(calls) == 2


def test_a_phase_that_runs_out_of_memory_twice_fails_the_job_for_a_retry_elsewhere(budget):
    def run():
        raise torch.cuda.OutOfMemoryError("out of memory")

    with pytest.raises(GpuOutOfMemory):
        ensemble_transcription._phase("cuda:0", lambda: False, run)


def test_any_other_error_is_not_retried(budget):
    calls = []

    def run():
        calls.append(1)
        raise ValueError("bug")

    with pytest.raises(ValueError):
        ensemble_transcription._phase("cuda:0", lambda: False, run)
    assert calls == [1]


# --- From the networks' results to tracks ---------------------------------------------------------------

@pytest.fixture
def networks(monkeypatch, budget):
    """Fake models: what each stage was given, and what they answer."""
    seen = {}
    answers = {
        "groups": [
            {"instrument": "acoustic_grand_piano", "program": 0, "notes": [(0.5, 1.0, 60, 80), (1.0, 1.5, 64, 90)]},
            {"instrument": "violin", "program": 40, "notes": np.array([[0.0, 2.0, 70, 70]])},
        ],
        "hits": [{"onset": 0.5, "offset": 0.6, "pitch": 36, "velocity": 100}],
    }

    def transcribe(models, x, progress, cancelled, stats, work_dir=None):
        seen["x16"] = x
        progress(0.5)
        progress(1.0)
        return {"state": True}

    def group(models, state, prediction):
        return answers["groups"], {"tuning_cents": 1.0}

    def drum_hits(drums, x44, source_rate, work_dir=None):
        seen["x44"], seen["source_rate"] = x44, source_rate
        return answers["hits"], {"model": "drum-kit"}

    monkeypatch.setattr(ensemble_transcription, "get_models",
                        lambda device: types.SimpleNamespace(grouper_lock=threading.Lock(), drums=object()))
    monkeypatch.setattr(pitched, "transcribe", transcribe)
    monkeypatch.setattr(pitched, "predict", lambda models, state: "prediction")
    monkeypatch.setattr(pitched, "group", group)
    monkeypatch.setattr(ensemble_transcription.ensemble_drums, "transcribe", drum_hits)
    seen["answers"] = answers
    return seen


def run_ensemble(audio, rate=44100, source_rate=48000, cancelled=None):
    progress, stats = [], {}
    tracks, info = ensemble_transcription.transcribe_ensemble(
        audio, rate, source_rate, "cuda:0", progress.append, cancelled, stats)
    return tracks, info, progress, stats


def test_groups_become_named_tracks_and_the_drums_come_last(networks):
    tracks, info, progress, stats = run_ensemble(np.zeros(44100, np.float32))

    assert [(t.name, t.program, t.is_drum) for t in tracks] == [
        ("acoustic grand piano", 0, False), ("violin", 40, False), ("drums", 0, True)]
    assert tracks[0].notes == [(0.5, 1.0, 60, 80), (1.0, 1.5, 64, 90)]
    assert tracks[1].notes == [(0.0, 2.0, 70, 70)]
    assert tracks[2].notes == [(0.5, 0.6, 36, 100)]
    assert info["drums"] == {"model": "drum-kit"} and info["tuning_cents"] == 1.0
    assert stats["model_s"] >= 0
    # The progress of the pitched pass is 85 % of the job, then the stages report 0.88, 0.9 and 1.
    assert progress == [0.425, 0.85, 0.88, 0.9, 1.0]
    # The result builds into a MIDI file.
    assert len(ensemble_midi.build(tracks).tracks) == 4


def test_no_drum_hits_give_no_drum_track(networks):
    networks["answers"]["hits"] = []

    tracks, _, _, _ = run_ensemble(np.zeros(44100, np.float32))

    assert [t.name for t in tracks] == ["acoustic grand piano", "violin"]


def test_the_source_rate_goes_to_the_drums_and_the_audio_is_resampled_for_the_networks(networks):
    run_ensemble(np.zeros(44100, np.float32), rate=44100, source_rate=32000)

    assert networks["source_rate"] == 32000
    assert len(networks["x44"]) == 44100 and len(networks["x16"]) == 16000


def test_other_rates_and_channels_are_converted_to_mono_44100(networks):
    stereo = np.stack([np.full(22050, 0.5, np.float32), np.full(22050, -0.25, np.float32)], axis=1)

    run_ensemble(stereo, rate=22050, source_rate=None)

    assert networks["source_rate"] is None
    assert abs(len(networks["x44"]) - 44100) <= 2 and abs(len(networks["x16"]) - 16000) <= 2
    x44 = networks["x44"].read(0, len(networks["x44"]))        # the drums read the 44.1 kHz audio a block at a time
    assert x44.dtype == np.float32 and np.asarray(networks["x16"]).dtype == np.float32
    assert np.allclose(x44[2000:-2000], 0.125, atol=0.01)


def test_a_job_cancelled_after_the_notes_stops_before_the_drums(networks, monkeypatch):
    drums_ran = []
    monkeypatch.setattr(ensemble_transcription.ensemble_drums, "transcribe",
                        lambda *args: drums_ran.append(1) or ([], {}))

    with pytest.raises(TranscriptionCancelled):
        run_ensemble(np.zeros(44100, np.float32), cancelled=lambda: True)

    assert drums_ran == []


def test_the_cpu_runs_the_tool_in_the_cpu_budget(monkeypatch, default_settings):
    cpu = cpu_tool_module()
    default_settings.worker.cpu_ensemble_memory_gib = 3.0
    fake = FakeBudget()
    monkeypatch.setattr(ensemble_transcription, "budget_for_device", lambda device: fake)
    calls = []

    def run_tool(x, rate, source_rate, progress, cancelled, stats, work_dir):
        calls.append((len(x), rate, source_rate, work_dir))
        return [ensemble_midi.Track("Piano", 0, False, [(0.0, 1.0, 60, 80)])], {"drums": {}}

    monkeypatch.setattr(cpu, "transcribe", run_tool)

    tracks, _ = ensemble_transcription.transcribe_ensemble(
        np.zeros(22050, np.float32), 22050, 44100, "cpu", None, None, None, "/job")

    assert [t.name for t in tracks] == ["Piano"]
    # The tool gets 44.1 kHz audio; its memory is held in the CPU budget.
    assert calls[0][1:] == (44100, 44100, "/job") and abs(calls[0][0] - 44100) <= 2
    assert fake.held == [3.0]


# --- A file ---------------------------------------------------------------------------------------------

def test_a_file_becomes_midi_with_its_progress(monkeypatch, tmp_path):
    steps, progress, calls = [], [], {}
    monkeypatch.setattr(ensemble_transcription, "select_ensemble_device", lambda: "cpu")
    monkeypatch.setattr(ensemble_transcription, "configure_cpu_threads", lambda: steps.append("threads"))
    audio = np.zeros(44100, np.float32)
    monkeypatch.setattr(ensemble_transcription, "read_audio", lambda path: (44100, audio))

    def transcribe(audio_in, rate, source_rate, device, report, cancelled, stats, work_dir):
        calls["transcribe"] = (rate, source_rate, device, work_dir)
        report(0.0)
        report(0.5)
        report(1.0)
        return [ensemble_midi.Track("Piano", 0, False, [(0.0, 1.0, 60, 80)])], {}

    def write(tracks, output_path, audio_in, rate, device):
        steps.append("written")
        output_path.write_bytes(b"MThd")

    monkeypatch.setattr(ensemble_transcription, "transcribe_ensemble", transcribe)
    monkeypatch.setattr(ensemble_transcription, "write_result", write)
    output = tmp_path / "transcription.mid"
    (tmp_path / "audio.wav").write_bytes(b"not a PCM WAV")          # not streamed: read whole by read_audio

    ensemble_transcription.transcribe_ensemble_file(
        tmp_path / "audio.wav", output, source_rate=48000, progress_callback=progress.append,
        model_done_callback=lambda: steps.append("model done"))

    assert calls["transcribe"] == (44100, 48000, "cpu", tmp_path)
    # The place on the models is given back before the MIDI is written.
    assert steps == ["threads", "model done", "written"]
    assert progress[0] == 0.07 and progress[-1] == 1.0 and 0.96 in progress
    assert progress == sorted(progress)
    assert all(0.15 <= value <= 0.95 for value in progress[2:-2])
    assert output.read_bytes() == b"MThd"


def test_a_file_cancelled_before_it_starts_runs_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(ensemble_transcription, "select_ensemble_device", lambda: "cpu")
    monkeypatch.setattr(ensemble_transcription, "read_audio", lambda path: pytest.fail("read"))

    with pytest.raises(TranscriptionCancelled):
        ensemble_transcription.transcribe_ensemble_file(
            tmp_path / "audio.wav", tmp_path / "out.mid", cancellation_callback=lambda: True)


def test_a_file_without_room_for_ensemble_jobs_is_refused(monkeypatch, tmp_path):
    def refuse():
        raise ensemble_transcription.EnsembleUnavailable()

    monkeypatch.setattr(ensemble_transcription, "select_ensemble_device", refuse)

    with pytest.raises(ensemble_transcription.EnsembleUnavailable, match="not available on this server"):
        ensemble_transcription.transcribe_ensemble_file(tmp_path / "audio.wav", tmp_path / "out.mid")


def test_the_result_is_written_as_a_midi_file(monkeypatch, tmp_path):
    written = []

    def write_midi_file(build, output_path, audio, rate, device):
        build(output_path)
        written.append((device, rate))

    monkeypatch.setattr(ensemble_transcription, "write_midi_file", write_midi_file)
    tracks = [ensemble_midi.Track("Piano", 0, False, [(0.0, 1.0, 60, 80)]),
              ensemble_midi.Track("drums", 0, True, [(0.5, 0.6, 36, 100)])]
    path = tmp_path / "transcription.mid"

    ensemble_transcription.write_result(tracks, path, np.zeros(10, np.float32), 44100, "cuda:0")

    import mido

    loaded = mido.MidiFile(str(path))
    assert len(loaded.tracks) == 3 and written == [("cuda:0", 44100)]
