"""Beats and bars from Beat This!'s frame-wise probabilities.

Beat This!'s own minimal postprocessing takes every peak above one half.
On piano it often changes the level it counts at within a piece: a passage
of triplets becomes three beats for one, a slow passage one beat for a
bar, and its downbeats then fall every 1, 2, or 3 beats. Here the beats
are decoded at one level for the whole piece: the level of most of its
peaks, with the tempo free to follow rubato within a factor of
TEMPO_SPREAD. The bars are then decoded over all beats at once, so that
weak downbeats in many bars add up and a single stray one does not move a
bar line. Both are Viterbi decodings, as in the dynamic Bayesian networks
of Böck et al., in numpy only.
"""

import numpy as np

FPS = 50
# Peaks of the beat probability: above this, and the highest within
# PEAK_FRAMES of them, as in the minimal postprocessing.
PEAK_THRESHOLD = 0.5
PEAK_FRAMES = 3
# The tempo may follow the performance within this factor of the main beat:
# below a factor of 2, so that half and double the beat are never both in
# reach and the level cannot change.
TEMPO_SPREAD = 1.4
# At most 127, so that the tempo of each frame's previous beat fits a byte:
# 54 bytes a second of audio.
TEMPO_STATES = 40
# Cost of a tempo change from one beat to the next, and the share of a beat
# in which the beat itself may fall: Böck et al.'s lambdas.
TRANSITION_LAMBDA = 100
OBSERVATION_LAMBDA = 16
# Beats per bar the bars are decoded in. 6/8 and 12/8 are 2 and 4 beats.
METERS = (2, 3, 4)
# Probability of a bar ending early, and of the meter changing at a bar line.
SHORT_BAR = 1e-3
METER_CHANGE = 1e-3
# The decoded downbeats must be this much more likely, on average, than the
# other beats, or the recording does not show where its bars are.
MIN_DOWNBEAT_CONTRAST = 0.25
EPSILON = 1e-6


def _peaks(activation: np.ndarray) -> np.ndarray:
    """Frames of the peaks of a probability."""
    if not len(activation):
        return np.zeros(0, dtype=int)
    padded = np.pad(activation, PEAK_FRAMES, constant_values=0)
    windows = np.lib.stride_tricks.sliding_window_view(padded, 2 * PEAK_FRAMES + 1)
    return np.flatnonzero((activation > PEAK_THRESHOLD) & (activation == windows.max(axis=1)))


def main_beat(activation: np.ndarray) -> float | None:
    """Frames between beats at the level most peaks are at, or None
    without enough peaks."""
    peaks = _peaks(activation)
    if len(peaks) < 3:
        return None
    return float(np.median(np.diff(peaks)))


def decode_beats(activation: np.ndarray, period: float, min_frames: float, max_frames: float) -> np.ndarray:
    """Beat frames from a beat probability per frame, about period frames
    apart, never closer than min_frames or further than max_frames. Only
    between the first and the last peak: silence around the music has no
    beats to find."""
    peaks = _peaks(activation)
    # A few frames more, so that the first and last beats are whole.
    first = max(0, int(peaks[0]) - PEAK_FRAMES)
    last = min(len(activation), int(peaks[-1]) + PEAK_FRAMES + 1)
    probability = np.clip(activation[first:last], EPSILON, 1 - EPSILON)

    low = max(min_frames, period / TEMPO_SPREAD)
    high = min(max_frames, period * TEMPO_SPREAD)
    # Frames per beat of each tempo; the states of a tempo are the frames
    # since its last beat.
    taus = np.unique(np.round(np.geomspace(low, max(low, high), TEMPO_STATES)).astype(int))
    offsets = np.concatenate([[0], np.cumsum(taus)])
    starts, ends = offsets[:-1], offsets[1:] - 1
    phase = np.arange(offsets[-1]) - np.repeat(starts, taus)
    on_beat = phase < np.maximum(1, np.repeat(taus, taus) / OBSERVATION_LAMBDA)
    change = -TRANSITION_LAMBDA * np.abs(taus[None, :] / taus[:, None] - 1)
    change -= np.log(np.exp(change).sum(axis=1, keepdims=True))

    beat = np.log(probability)
    no_beat = np.log((1 - probability) / (OBSERVATION_LAMBDA - 1))
    score = np.where(on_beat, beat[0], no_beat[0])
    # For each frame and tempo: the tempo of the beat before.
    previous = np.zeros((len(probability), len(taus)), dtype=np.int8)
    for frame in range(1, len(probability)):
        candidates = score[ends][:, None] + change
        best = candidates.argmax(axis=0)
        previous[frame] = best
        score = np.roll(score, 1)
        score[starts] = candidates[best, np.arange(len(taus))]
        score += np.where(on_beat, beat[frame], no_beat[frame])

    state = int(score.argmax())
    tempo = int(np.searchsorted(offsets, state, side="right") - 1)
    frames = []
    for frame in range(len(probability) - 1, -1, -1):
        if state == starts[tempo]:
            frames.append(frame)
            tempo = int(previous[frame, tempo])
            state = int(ends[tempo])
        else:
            state -= 1
    # Each beat on the highest probability near it.
    refined = []
    for frame in reversed(frames):
        start = max(0, frame - PEAK_FRAMES)
        refined.append(start + int(np.argmax(probability[start:frame + PEAK_FRAMES + 1])))
    return np.unique(np.array(refined, dtype=int) + first)


def decode_bars(probabilities: np.ndarray) -> list[tuple[int, int]]:
    """(beats in the bar, position in the bar) of each beat, from the
    downbeat probability at each beat."""
    states = [(meter, position) for meter in METERS for position in range(meter)]
    index = {state: number for number, state in enumerate(states)}
    transition = np.full((len(states), len(states)), -np.inf)
    for (meter, position), number in index.items():
        if position < meter - 1:
            transition[number, index[meter, position + 1]] = np.log(1 - SHORT_BAR)
            transition[number, index[meter, 0]] = np.log(SHORT_BAR)
        else:
            transition[number, index[meter, 0]] = np.log(1 - METER_CHANGE * (len(METERS) - 1))
            for other in METERS:
                if other != meter:
                    transition[number, index[other, 0]] = np.log(METER_CHANGE)

    probability = np.clip(probabilities, EPSILON, 1 - EPSILON)
    downbeat = np.array([position == 0 for _, position in states])
    emission = np.where(downbeat, np.log(probability)[:, None], np.log(1 - probability)[:, None])
    score = emission[0].copy()
    previous = np.zeros((len(probability), len(states)), dtype=np.int8)
    for beat in range(1, len(probability)):
        candidates = score[:, None] + transition
        previous[beat] = candidates.argmax(axis=0)
        score = candidates.max(axis=0) + emission[beat]
    path = [int(score.argmax())]
    for beat in range(len(probability) - 1, 0, -1):
        path.append(int(previous[beat, path[-1]]))
    return [states[number] for number in reversed(path)]


def decode(
    beat_activation: np.ndarray, downbeat_activation: np.ndarray, min_seconds: float, max_seconds: float,
) -> tuple[list[float], list[float], float]:
    """Beat and downbeat times in seconds from Beat This!'s probabilities per
    frame, and how much more likely its downbeats are than the other beats.
    No downbeats when that is below MIN_DOWNBEAT_CONTRAST."""
    # A model that failed on the device can give NaN: no beat there.
    beat_activation = np.nan_to_num(np.asarray(beat_activation, dtype=float), nan=0.0, posinf=1.0, neginf=0.0)
    downbeat_activation = np.nan_to_num(np.asarray(downbeat_activation, dtype=float), nan=0.0, posinf=1.0, neginf=0.0)
    period = main_beat(beat_activation)
    if period is None:
        return [], [], 0.0
    frames = decode_beats(beat_activation, period, min_seconds * FPS, max_seconds * FPS)
    beats = [float(frame) / FPS for frame in frames]
    if len(frames) < 2:
        return beats, [], 0.0
    # The highest downbeat probability within the beat's peak.
    windows = [
        downbeat_activation[max(0, frame - PEAK_FRAMES):frame + PEAK_FRAMES + 1].max() for frame in frames
    ]
    probabilities = np.array(windows)
    bars = decode_bars(probabilities)
    downbeat = np.array([position == 0 for _, position in bars])
    if downbeat.all() or not downbeat.any():
        return beats, [], 0.0
    contrast = float(probabilities[downbeat].mean() - probabilities[~downbeat].mean())
    if contrast < MIN_DOWNBEAT_CONTRAST:
        return beats, [], contrast
    return beats, [beat for beat, down in zip(beats, downbeat) if down], contrast
