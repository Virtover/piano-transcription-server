"""The ensemble models in the worker image: the bounded-memory pipeline
(app/transcription/ensemble) against the vendored package's own
(app/transcription/dettato).

Skipped without torch (the repo's .venv) and without the weights (the worker
and RunPod images have them in /opt/dettato, or DETTATO_WEIGHTS). They run on
the CPU, and on a GPU where there is one; keep the audio short, the CPU is slow.
"""

import copy
import functools

import numpy as np
import pytest

torch = pytest.importorskip("torch")
nn = torch.nn

from app.transcription.dettato import hub  # noqa: E402
from app.transcription.ensemble import drums, pitched  # noqa: E402

RATE = 16000
WEIGHT_FOLDERS = ("dettato", "dettato-evidence", "dettato-grouper", "dettato-drums")


def have_weights() -> bool:
    return all((hub.weights_dir() / folder / "model.safetensors").is_file() for folder in WEIGHT_FOLDERS)


needs_weights = pytest.mark.skipif(not have_weights(), reason="the ensemble weights are not installed (/opt/dettato)")
DEVICES = ["cpu", *(["cuda:0"] if torch.cuda.is_available() else [])]


def synthetic(seconds: float = 40.0, rate: int = RATE, seed: int = 0) -> np.ndarray:
    """Plucked tones, a note every 0.35 to 0.55 s, and a few bursts of noise: mono float32."""
    rng = np.random.default_rng(seed)
    x = np.zeros(int(seconds * rate), np.float64)
    length = np.arange(int(0.6 * rate)) / rate
    pitches = [57, 60, 64, 67, 72, 76]
    start, index = 0.5, 0
    while start < seconds - 1.0:
        f0 = 440.0 * 2 ** ((pitches[index % len(pitches)] - 69) / 12)
        tone = sum(np.exp(-length * (4 + 2 * h)) * np.sin(2 * np.pi * f0 * h * length) / h for h in range(1, 5))
        first = int(start * rate)
        x[first:first + len(tone)] += tone[:len(x) - first]
        start += 0.35 + 0.1 * (index % 3)
        index += 1
    for burst in rng.uniform(1.0, seconds - 1.0, 5):
        first = int(burst * rate)
        noise = rng.standard_normal(int(0.05 * rate)) * np.exp(-np.arange(int(0.05 * rate)) / (0.01 * rate))
        x[first:first + len(noise)] += 0.2 * noise
    return (0.5 * x / np.abs(x).max()).astype(np.float32)


@functools.lru_cache(maxsize=None)
def models(device: str):
    """The four networks as ensemble_transcription.get_models loads them (without importing that module, which needs
    the whole worker environment)."""
    import threading
    import types

    from app.transcription.dettato.drums import DrumTranscriber
    from app.transcription.dettato.evidence import EvidenceRepairer
    from app.transcription.dettato.grouper import Grouper
    from app.transcription.dettato.transcriber import Transcriber

    return types.SimpleNamespace(
        device=device, transcriber=Transcriber.from_pretrained(device), repairer=EvidenceRepairer.from_pretrained(device),
        grouper=Grouper.from_pretrained(device), drums=drums.make_long(DrumTranscriber.from_pretrained(device)),
        grouper_lock=threading.Lock())


def recorded(repairer, store):
    """Wrap repairer._run (an instance attribute, undone by the caller) to keep each call's repaired notes."""
    original = repairer._run

    def run(res, ev):
        result = original(res, ev)
        store.append(result[0])
        return result

    return original, run


def notes_of(repaired: list[dict]):
    """(pitch, onset) of the repaired notes of every call, sorted."""
    rows = [(int(p), float(o)) for rep in repaired for o, p in zip(rep["onset"], rep["pitch"])]
    return sorted(rows)


# --- Pitched notes: the same as the package, segment by segment --------------------------------------------

@needs_weights
@pytest.mark.parametrize("device", DEVICES)
def test_the_segments_repair_the_same_notes_as_the_package(device, monkeypatch):
    from app.transcription.dettato import Dettato

    monkeypatch.setattr(pitched, "SEGMENT_S", 16.0)          # several segments in 40 s
    m = models(device)
    x = synthetic(40.0)

    # The package's pipeline, at the window step the server uses (a window every 4 s).
    transcriber_run = m.transcriber.run

    def run_at_server_step(audio, hop_frac=0.25, batch=2, taps=(), pad_batch=False, sub_hop_frac=None, sub_taps=(),
                           buf_device=None):
        return transcriber_run(audio, hop_frac=0.5, batch=batch, taps=taps, pad_batch=pad_batch, sub_hop_frac=0.5,
                               sub_taps=sub_taps, buf_device=buf_device)

    reference_notes = []
    with monkeypatch.context() as patch:
        original, record = recorded(m.repairer, reference_notes)
        patch.setattr(m.transcriber, "run", run_at_server_step)
        patch.setattr(m.repairer, "_run", record)
        Dettato(m.transcriber, m.repairer, m.grouper, m.drums, device).pitched(x)
    assert len(reference_notes) == 1                        # the package repairs the whole song at once

    # The server's: a segment at a time.
    segment_notes, stats = [], {}
    with monkeypatch.context() as patch:
        original, record = recorded(m.repairer, segment_notes)
        patch.setattr(m.repairer, "_run", record)
        state = pitched.transcribe(m, x, None, None, stats)
        pred = pitched.predict(m, state)
        groups, info = pitched.group(m, state, pred)

    assert stats["segments"] == 3 and len(segment_notes) == 3
    expected, found = notes_of(reference_notes), notes_of(segment_notes)
    assert len(expected) > 20, "the synthetic song should give notes"
    assert len(found) == len(expected)
    assert [pitch for pitch, _ in found] == [pitch for pitch, _ in expected]
    # The repaired onsets are the package's within a millisecond. The repairer's onsets move in steps of 5 ms, and the
    # evidence it reads differs in the sixth decimal (float16 sums in another order), so now and then one onset takes
    # the next step (measured on the CPU: one note of 86): never more than one step, and not many.
    differences = [abs(a[1] - b[1]) for a, b in zip(expected, found)]
    assert max(differences) < 0.0051 + 1e-9, max(differences)
    assert sum(difference > 0.001 for difference in differences) <= max(1, len(expected) // 50)

    # The grouper's input is these notes, and its output is tracks with notes.
    assert info["notes_in"] == len(found)
    assert groups and all({"instrument", "program", "notes"} <= set(group) for group in groups)
    assert info["notes_out"] == sum(len(group["notes"]) for group in groups) > 0


@needs_weights
def test_a_cancelled_pass_raises_cancelled(monkeypatch):
    monkeypatch.setattr(pitched, "SEGMENT_S", 16.0)
    m = models("cpu")
    calls = []

    def cancelled():
        calls.append(1)
        return len(calls) >= 2

    with pytest.raises(pitched.Cancelled):
        pitched.transcribe(m, synthetic(20.0), None, cancelled, {})


@needs_weights
def test_progress_rises_to_one_over_the_pass(monkeypatch):
    monkeypatch.setattr(pitched, "SEGMENT_S", 16.0)
    seen = []

    pitched.transcribe(models("cpu"), synthetic(20.0), seen.append, None, {})

    assert seen and seen == sorted(seen) and seen[-1] == pytest.approx(1.0)


# --- The drums: chunked networks give the unchunked probabilities ------------------------------------------

def shrink_chunks(patched, gru_chunk=100, cq_chunk=400):
    """make_long took the module's chunk sizes as defaults when it built the wrappers: set small ones, so that a short
    input runs in several chunks."""
    count = {"gru": 0, "cq": 0}
    for net in (patched.kit, *patched.experts):
        for module in net.modules():
            if isinstance(module, drums.ChunkedGRU):
                module.chunk = gru_chunk
                count["gru"] += 1
            elif isinstance(module, drums.LongCQ):
                module.chunk, module.margin = cq_chunk, drums.CQ_MARGIN
                count["cq"] += 1
    return count


@needs_weights
@pytest.mark.parametrize("device", DEVICES)
def test_chunked_drum_networks_give_the_probabilities_of_the_original(device):
    from app.transcription.dettato.drums import DrumTranscriber
    from app.transcription.dettato.drums.features import features
    from app.transcription.dettato import precision

    reference = DrumTranscriber.from_pretrained(device)
    patched = copy.deepcopy(reference)
    drums.make_long(patched)
    counts = shrink_chunks(patched)
    assert counts["gru"] > 0 and counts["cq"] > 0
    x44 = synthetic(20.0, 44100)
    feats = features(x44).astype(np.float32)
    # 100 frames a second: 2000 frames, far beyond the CQ chunk of 400 and its margins; 1000 GRU steps in 100s.
    assert len(feats) > drums.CQ_MARGIN * 2 + 400

    expected = reference.probabilities(feats)
    found = patched.probabilities(feats)

    # bfloat16 keeps three digits: a chunk boundary moves the rounding.
    tolerance = 5e-2 if precision.bf16(device) else 1e-3
    assert abs(found["router"] - expected["router"]) < tolerance
    for name in ("kit", "kit_vel", "drum", "timpani", "vel"):
        assert found[name].shape == expected[name].shape, name
        assert np.abs(found[name] - expected[name]).max() < tolerance, name


@needs_weights
def test_the_blocked_features_are_the_features_of_the_package(monkeypatch):
    from app.transcription.dettato.drums.features import features as package_features

    monkeypatch.setattr(drums, "FEATURE_BLOCK_S", 5.0)       # four blocks in 20 s
    x44 = synthetic(20.0, 44100)

    from app.transcription import stream_audio

    blocked = drums.features(stream_audio.ArrayReader(x44, 44100)).astype(np.float32)
    whole = package_features(x44).astype(np.float32)

    assert blocked.shape == whole.shape
    difference = np.abs(blocked - whole)
    # float16 features: the same to within their precision, also at the block edges.
    assert difference.mean() < 1e-3 and np.percentile(difference, 99.9) < 0.05


def test_a_chunked_gru_is_the_gru_it_replaces():
    torch.manual_seed(0)
    for bidirectional in (False, True):
        gru = nn.GRU(12, 16, num_layers=2, batch_first=True, bidirectional=bidirectional).eval()
        chunked = drums.ChunkedGRU(gru, chunk=64).eval()
        x = torch.randn(2, 500, 12)

        with torch.no_grad():
            expected, expected_h = gru(x)
            found, found_h = chunked(x)

        assert torch.allclose(found, expected, atol=1e-4), bidirectional
        assert torch.allclose(found_h, expected_h, atol=1e-4), bidirectional
        # A short input is the GRU itself.
        with torch.no_grad():
            assert torch.equal(chunked(x[:, :50])[0], gru(x[:, :50])[0])


def test_a_long_cq_branch_is_the_branch_it_replaces():
    from app.transcription.dettato.drums.model import CQBranch

    torch.manual_seed(0)
    branch = CQBranch().eval()
    long = drums.LongCQ(branch, chunk=400, margin=drums.CQ_MARGIN).eval()
    x = torch.randn(1, 1500, 120) * 3

    with torch.no_grad():
        expected, found = branch(x), long(x)

    assert found.shape == expected.shape
    assert torch.allclose(found, expected, atol=1e-3)


# --- The drums with less host memory: the package's numbers and hits ------------------------------------------


def test_blocked_hit_picking_is_the_packages():
    from app.transcription.dettato.drums.infer import peaks

    rng = np.random.default_rng(1)
    for _ in range(20):
        prob = rng.random((rng.integers(1, 400), 5)).astype(np.float32) ** 3
        expected = peaks(prob, 0.3)
        for block in (1, 2, 7, 64, 1000):
            found = drums._peaks(prob, 0.3, block=block)
            assert np.array_equal(found[0], expected[0]) and np.array_equal(found[1], expected[1]), block


@needs_weights
@pytest.mark.parametrize("device", DEVICES)
def test_the_drum_probabilities_and_hits_are_the_packages(device):
    from app.transcription.dettato.drums import DrumTranscriber
    from app.transcription.dettato.drums.features import features

    model = DrumTranscriber.from_pretrained(device)
    feats = features(synthetic(20.0, 44100))        # float16, as the server stores them
    expected = model.probabilities(feats.astype(np.float32))
    found = drums.probabilities(model, feats)

    assert found["router"] == expected["router"]
    for name in drums.HEADS:
        assert np.array_equal(found[name], expected[name]), name
    for router in (0.0, 1.0):                       # both models' decoding
        P = dict(expected, router=router)
        for a, b in zip(drums.decode(model, P), model.decode(P)):
            assert np.array_equal(np.asarray(a), np.asarray(b))


@needs_weights
def test_long_drum_probabilities_go_to_files_in_blocks(tmp_path, monkeypatch):
    from app.transcription.dettato.drums import DrumTranscriber
    from app.transcription.dettato.drums.features import features

    monkeypatch.setattr(drums, "LONG_S", 6.0)
    monkeypatch.setattr(drums, "LONG_MARGIN_S", 2.0)
    model = DrumTranscriber.from_pretrained("cpu")
    feats = features(synthetic(20.0, 44100))
    whole = model.probabilities(feats.astype(np.float32))

    found = drums.probabilities(model, feats, tmp_path)

    assert 0.0 <= found["router"] <= 1.0
    for name in drums.HEADS:
        assert isinstance(found[name], np.memmap) and (tmp_path / f"{name}.npy").is_file(), name
        assert found[name].shape == whole[name].shape, name
    in_memory = {k: (np.asarray(v) if k in drums.HEADS else v) for k, v in found.items()}
    for a, b in zip(drums.decode(model, found), model.decode(in_memory)):
        assert np.array_equal(np.asarray(a), np.asarray(b))


@needs_weights
def test_the_drum_stage_gives_the_packages_hits(tmp_path):
    from app.transcription import stream_audio
    from app.transcription.dettato.drums import DrumTranscriber

    model = DrumTranscriber.from_pretrained("cpu")
    x44 = synthetic(20.0, 44100)
    expected = model.transcribe(x44)

    hits, info = drums.transcribe(model, stream_audio.ArrayReader(x44, 44100), 48000, tmp_path)

    assert info == {"router": model.last_info["router"], "model": model.last_info["model"]}
    assert [(h["onset"], h["pitch"]) for h in hits] == [(h["onset"], h["pitch"]) for h in expected]
    assert list(tmp_path.iterdir()) == []               # the scratch folder is gone
