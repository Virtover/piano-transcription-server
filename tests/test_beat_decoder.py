import numpy as np
import pytest

from app.transcription import beat_decoder
from app.transcription.beat_decoder import FPS, decode, decode_bars, decode_beats

MIN_FRAMES, MAX_FRAMES = 60 / 400 * FPS, 60 / 20 * FPS


def activation(frames, length):
    """A probability per frame like Beat This!'s: a peak at each of frames."""
    result = np.full(length, 0.01)
    for frame in frames:
        frame = int(round(frame))
        result[frame] = 0.9
        for side in (frame - 1, frame + 1):
            if 0 <= side < length:
                result[side] = max(result[side], 0.4)
    return result


def test_a_triplet_passage_keeps_the_beat_of_the_piece():
    # Beats every 0.6 s, and from 30 s to 50 s peaks on every triplet
    # eighth, as strong as the beats: once three beats for one.
    beats = np.arange(30, 3000, 30)
    triplets = np.arange(1500, 2500, 10)
    found = decode_beats(activation(np.union1d(beats, triplets), 3000), 30, MIN_FRAMES, MAX_FRAMES)

    intervals = np.diff(found)
    assert intervals.min() >= 27 and intervals.max() <= 33
    assert set(beats).issubset(set(found))


def test_a_passage_heard_at_half_the_beat_keeps_the_beat_of_the_piece():
    beats = np.arange(30, 3000, 30)
    # From 30 s to 50 s only every other beat has a peak.
    heard = [beat for beat in beats if not 1500 <= beat < 2500 or (beat // 30) % 2 == 0]
    found = decode_beats(activation(heard, 3000), 30, MIN_FRAMES, MAX_FRAMES)

    assert np.diff(found).max() <= 33
    assert len(found) == pytest.approx(len(beats), abs=2)


def test_no_beats_in_the_silence_around_the_music():
    beats = np.arange(1000, 2000, 25)
    found = decode_beats(activation(beats, 4000), 25, MIN_FRAMES, MAX_FRAMES)

    assert found[0] == 1000 and found[-1] == beats[-1]


def test_rubato_is_followed():
    # From 100 to 70 BPM and back, gradually.
    beats = [25.0]
    for index in range(150):
        beats.append(beats[-1] + 60 / (85 + 15 * np.cos(2 * np.pi * index / 75)) * FPS)
    found = decode_beats(activation(beats, int(beats[-1]) + 10), 35, MIN_FRAMES, MAX_FRAMES)

    assert np.round(beats).astype(int).tolist() == found.tolist()


def downbeat_probabilities(bars, meter, rng):
    """Noisy downbeat probabilities, as on piano: about 0.7 on downbeats, 0.15 elsewhere."""
    probabilities = np.where(np.arange(bars * meter) % meter == 0, 0.7, 0.15)
    return np.clip(probabilities + rng.normal(0, 0.08, len(probabilities)), 0.01, 0.99)


@pytest.mark.parametrize("meter", [2, 3, 4])
def test_bars_from_noisy_downbeats_with_strays_and_misses(meter):
    rng = np.random.default_rng(meter)
    probabilities = downbeat_probabilities(40, meter, rng)
    # A stray downbeat inside a bar, and a missed one.
    probabilities[5 * meter + 1] = 0.9
    probabilities[9 * meter] = 0.05

    bars = decode_bars(probabilities)

    assert [position for _, position in bars] == [index % meter for index in range(40 * meter)]


def test_a_meter_change_is_followed():
    rng = np.random.default_rng(1)
    probabilities = np.concatenate([downbeat_probabilities(20, 2, rng), downbeat_probabilities(20, 4, rng)])

    bars = decode_bars(probabilities)

    assert [meter for meter, _ in bars] == [2] * 40 + [4] * 80


def test_a_pickup_before_the_first_bar():
    rng = np.random.default_rng(2)
    probabilities = np.concatenate([[0.1], downbeat_probabilities(20, 3, rng)])

    bars = decode_bars(probabilities)

    assert bars[1] == (3, 0) and bars[0][1] != 0


def test_decode_gives_beats_and_downbeats_in_seconds():
    beats = np.arange(50, 2500, 25)
    downbeat = activation(beats[::4], 2600) * 0.8

    found, downbeats, contrast = decode(activation(beats, 2600), downbeat, 0.15, 3)

    assert found == pytest.approx(beats / FPS)
    assert downbeats == pytest.approx(beats[::4] / FPS)
    assert contrast > beat_decoder.MIN_DOWNBEAT_CONTRAST


def test_unclear_downbeats_give_beats_without_bars():
    beats = np.arange(50, 2500, 25)
    rng = np.random.default_rng(3)
    downbeat = np.clip(rng.normal(0.2, 0.03, 2600), 0, 1)

    found, downbeats, contrast = decode(activation(beats, 2600), downbeat, 0.15, 3)

    assert len(found) == len(beats)
    assert downbeats == []
    assert contrast < beat_decoder.MIN_DOWNBEAT_CONTRAST


def test_too_few_peaks_give_nothing():
    assert decode(np.zeros(0), np.zeros(0), 0.15, 3) == ([], [], 0.0)
    assert decode(activation([100, 125], 300), np.zeros(300), 0.15, 3) == ([], [], 0.0)


def test_nan_probabilities_give_no_beats_and_no_bars_from_them():
    beats = np.arange(50, 2500, 25)
    beat = activation(beats, 2600)
    beat[1000:1500] = np.nan
    downbeat = np.full(2600, np.nan)

    found, downbeats, contrast = decode(beat, downbeat, 0.15, 3)

    assert downbeats == [] and contrast == 0.0
    assert all(np.isfinite(found))
    assert decode(np.full(2600, np.nan), np.full(2600, np.nan), 0.15, 3) == ([], [], 0.0)


def test_the_tempo_states_fit_the_back_pointers():
    assert beat_decoder.TEMPO_STATES <= np.iinfo(np.int8).max
    assert sum(beat_decoder.METERS) <= np.iinfo(np.int8).max
