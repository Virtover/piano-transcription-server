import numpy as np
import pytest

pytest.importorskip("torch")
pytest.importorskip("transkun")

from app.transcription import amt_apc
from app.transcription.amt_apc import constants

KEY = 40  # MIDI pitch 61


def rolls(frames: int = 100):
    return (
        np.zeros((frames, constants.NUM_NOTE), dtype=np.float32),
        np.zeros((frames, constants.NUM_NOTE), dtype=np.float32),
        np.zeros((frames, constants.NUM_NOTE), dtype=np.float32),
        np.zeros((frames, constants.NUM_NOTE), dtype=np.int8),
    )


def note_rolls(onset_frame: int, end_frame: int, velocity: int = 64):
    onset, offset, mpe, vel = rolls()
    onset[onset_frame, KEY] = 0.9
    mpe[onset_frame:end_frame, KEY] = 0.9
    vel[onset_frame, KEY] = velocity
    return onset, offset, mpe, vel


@pytest.mark.parametrize("style", amt_apc.STYLES)
def test_style_vector_is_a_float32_vector_of_the_style_size(style):
    vector = amt_apc.style_vector(style)
    assert vector.dtype == np.float32
    assert vector.shape == (constants.STYLE_DIM,)
    assert vector.tolist() == list(np.float32(amt_apc.STYLE_VECTORS[style]))


def test_style_vector_rejects_an_unknown_style():
    with pytest.raises(ValueError, match="Unknown style"):
        amt_apc.style_vector("level9")


def test_default_style_is_one_of_the_styles():
    assert amt_apc.DEFAULT_STYLE in amt_apc.STYLES
    assert amt_apc.SAMPLE_RATE == 16000


@pytest.mark.parametrize(
    "frames, starts, padded",
    [(0, [], 64), (1, [0], 576), (511, [0], 576), (512, [0], 576), (513, [0, 512], 1088)],
)
def test_segments_cover_the_feature(frames, starts, padded):
    feature = np.ones((frames, constants.N_BINS), dtype=np.float32)
    tensor = amt_apc.pad_features(feature)
    assert amt_apc.segment_starts(frames) == starts
    assert tuple(tensor.shape) == (padded, constants.N_BINS)
    assert (tensor[: constants.MARGIN_BEFORE] == constants.MIN_VALUE).all()
    assert (tensor[constants.MARGIN_BEFORE : constants.MARGIN_BEFORE + frames] == 1).all()
    assert (tensor[constants.MARGIN_BEFORE + frames :] == constants.MIN_VALUE).all()
    for start in starts:
        window = amt_apc.segment_input(tensor, start)
        assert tuple(window.shape) == (1, constants.N_BINS, 576)


def test_features_of_silence_have_the_frame_shape():
    feature = amt_apc.features(np.zeros(16000, dtype=np.float32))
    assert feature.shape == (16000 // constants.HOP_SAMPLES + 1, constants.N_BINS)
    assert feature.dtype == np.float32
    assert amt_apc.features(np.zeros(0, dtype=np.float32)).shape == (0, constants.N_BINS)


def test_a_clear_note_becomes_one_note():
    (note,) = amt_apc.notes(*note_rolls(10, 30, velocity=72))
    hop = constants.HOP_SAMPLES / constants.SAMPLE_RATE
    assert note.pitch == KEY + constants.NOTE_MIN
    assert note.velocity == 72
    assert note.start == pytest.approx(10 * hop)
    assert note.end == pytest.approx(30 * hop)


def test_an_offset_peak_ends_the_note_before_the_frames_do():
    onset, offset, mpe, velocity = note_rolls(10, 30)
    offset[20, KEY] = 1.5
    (note,) = amt_apc.notes(onset, offset, mpe, velocity)
    assert note.end == pytest.approx(20 * constants.HOP_SAMPLES / constants.SAMPLE_RATE)


def test_below_the_thresholds_there_is_no_note():
    onset, offset, mpe, velocity = note_rolls(10, 30)
    onset[10, KEY] = 0.4
    assert amt_apc.notes(onset, offset, mpe, velocity) == []


def test_velocity_zero_is_ignored():
    assert amt_apc.notes(*note_rolls(10, 30, velocity=0)) == []


def test_a_note_shorter_than_the_minimum_is_dropped():
    # 4 frames of 16 ms are 64 ms, under the 80 ms minimum; 6 frames are 96 ms.
    assert amt_apc.notes(*note_rolls(10, 14)) == []
    assert len(amt_apc.notes(*note_rolls(10, 16))) == 1


def test_a_repeated_key_cuts_the_earlier_note_short():
    onset, offset, mpe, velocity = note_rolls(10, 60)
    onset[40, KEY] = 0.9
    velocity[40, KEY] = 50
    first, second = amt_apc.notes(onset, offset, mpe, velocity)
    assert first.end == pytest.approx(second.start)
    assert second.velocity == 50


def test_the_model_loads_strictly_and_runs_a_window():
    import torch

    from app.transcription.amt_apc.model import Spec2MIDI

    model = Spec2MIDI().eval()
    state = model.state_dict()
    assert not any(key.startswith(("encoder.scale", "decoder.scale")) for key in state)
    spec = torch.zeros(1, constants.N_BINS, 576)
    outputs = amt_apc.run_segment(model, spec, amt_apc.style_vector("level2"), "cpu")
    onset, offset, mpe, velocity = outputs
    assert onset.shape == offset.shape == mpe.shape == velocity.shape == (512, constants.NUM_NOTE)
    assert velocity.dtype == np.int8
