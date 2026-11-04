"""The tempo map of the MIDI result: the beats and bars of the recording.

Transkun writes note times in seconds at a fixed 120 BPM, so in a notation
program or DAW the bar lines do not follow the music. Beat This! finds the
beats and downbeats in the audio. Each beat becomes one quarter note, with a
tempo change at every beat, and the bars follow the downbeats. No note is
moved: every event keeps its time in seconds, to within a millisecond, so
syncopation, swing, and rubato play back as recorded and sit where they
belong on the grid. When the beats look unreliable, the MIDI stays as
Transkun wrote it.
"""

import bisect
import logging
import os
import statistics
import threading
import time
from collections import Counter
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

import mido

from app.config import settings


logger = logging.getLogger(__name__)

# Baked into the worker and RunPod images, checked against its SHA-256 when
# they are built. Never downloaded at run time: torch.load would read an
# unchecked file.
CHECKPOINT = Path("/opt/beat-this/final0.ckpt")

TICKS_PER_BEAT = 960
MIN_BEATS = 8
# Beats closer or further apart than this are not a tempo but an error.
MIN_BEAT_SECONDS = 60 / 400
MAX_BEAT_SECONDS = 60 / 20
# Intervals compared with the median of this many intervals around them.
NEIGHBORHOOD = 4
# An interval this close to a multiple of the local beat has missed beats.
MISSED_BEAT_TOLERANCE = 0.2
# A beat this much closer than the local beat to the one before is spurious.
EXTRA_BEAT_RATIO = 0.6
# After repairs, this share of intervals must be within REGULAR_TOLERANCE of
# the local beat. Rubato changes the beat gradually and passes; a fermata or
# a free passage is one long interval, and a few of them are allowed.
REGULAR_SHARE = 0.85
REGULAR_TOLERANCE = 0.3
# A downbeat further than this share of a beat from every beat is ignored.
DOWNBEAT_TOLERANCE = 0.25
MAX_BAR_BEATS = 12
# Below this share of bars in the main meter, the downbeats are too noisy to
# place bars, and there is no tempo map.
REGULAR_BAR_SHARE = 0.75


@dataclass(frozen=True)
class TempoMap:
    # Microseconds from the start of the audio to each beat; the first is 0.
    beats_us: list[int]
    # (beat index, beats in the bar) wherever the bar length changes.
    meters: list[tuple[int, int]]

    @cached_property
    def tempos(self) -> list[int]:
        """Microseconds per quarter note from each beat to the next."""
        return [end - start for start, end in zip(self.beats_us, self.beats_us[1:])]

    def tick(self, seconds: float) -> int:
        """The tick of a time in seconds. After the last beat, the last tempo runs on."""
        tempos = self.tempos
        time_us = seconds * 1_000_000
        index = max(0, min(bisect.bisect_right(self.beats_us, time_us) - 1, len(tempos) - 1))
        beats = index + (time_us - self.beats_us[index]) / tempos[index]
        return max(0, round(beats * TICKS_PER_BEAT))


def _local_beat(intervals: list[float], index: int) -> float:
    """The median interval around intervals[index], without it."""
    nearby = intervals[max(0, index - NEIGHBORHOOD):index] + intervals[index + 1:index + 1 + NEIGHBORHOOD]
    return statistics.median(nearby) if nearby else intervals[index]


def _drop_extra_beats(beats: list[float], downbeats: set[float]) -> list[float]:
    kept = list(beats)
    index = 1
    while index < len(kept) - 1:
        # Only the intervals around the beat, as they are after any beat
        # dropped before it: the whole list each time is quadratic.
        first = max(0, index - 1 - NEIGHBORHOOD)
        nearby = kept[first:index + 1 + NEIGHBORHOOD]
        intervals = [end - start for start, end in zip(nearby, nearby[1:])]
        local = _local_beat(intervals, index - 1 - first)
        # Between two beats that are one beat apart: a spurious beat.
        if (
            kept[index] - kept[index - 1] < EXTRA_BEAT_RATIO * local
            and abs(kept[index + 1] - kept[index - 1] - local) < REGULAR_TOLERANCE * local
            and kept[index] not in downbeats
        ):
            del kept[index]
        else:
            index += 1
    return kept


def _fill_missed_beats(beats: list[float]) -> list[float]:
    intervals = [end - start for start, end in zip(beats, beats[1:])]
    filled = [beats[0]]
    for index, interval in enumerate(intervals):
        local = _local_beat(intervals, index)
        count = round(interval / local)
        if count in (2, 3) and abs(interval / local - count) < MISSED_BEAT_TOLERANCE * count:
            step = interval / count
            filled.extend(beats[index] + step * part for part in range(1, count))
        filled.append(beats[index + 1])
    return filled


def _is_regular(beats: list[float]) -> bool:
    intervals = [end - start for start, end in zip(beats, beats[1:])]
    out_of_range = [
        round(start, 2) for start, interval in zip(beats, intervals)
        if not MIN_BEAT_SECONDS <= interval <= MAX_BEAT_SECONDS
    ]
    regular = sum(
        abs(interval - _local_beat(intervals, index)) <= REGULAR_TOLERANCE * _local_beat(intervals, index)
        for index, interval in enumerate(intervals)
    )
    if out_of_range or regular < REGULAR_SHARE * len(intervals):
        logger.info(
            "Beats too irregular for a tempo map: %d beats, %.0f%% regular (%.0f%% needed), "
            "%.0f to %.0f BPM, beyond 20 to 400 BPM at %s s",
            len(beats), 100 * regular / len(intervals), 100 * REGULAR_SHARE,
            60 / max(intervals), 60 / min(intervals), out_of_range[:5] or "none",
        )
        return False
    return True


def _downbeat_indices(beats: list[float], downbeats: list[float]) -> list[int]:
    indices = []
    for downbeat in downbeats:
        after = bisect.bisect_left(beats, downbeat)
        index = min((i for i in (after - 1, after) if 0 <= i < len(beats)), key=lambda i: abs(beats[i] - downbeat))
        neighbor = beats[index + 1] - beats[index] if index + 1 < len(beats) else beats[index] - beats[index - 1]
        if abs(beats[index] - downbeat) <= DOWNBEAT_TOLERANCE * neighbor:
            indices.append(index)
    return sorted(set(indices))


def _bar_starts(beat_count: int, downbeats: list[int]) -> tuple[list[int], int] | None:
    """The beat indices where bars start, and the main meter, or None when
    the downbeats are too few or too inconsistent to place bars: a guessed
    meter would put every bar line in the wrong place. Bars before the first
    downbeat lead up to it: a short first bar is the pickup."""
    lengths = [end - start for start, end in zip(downbeats, downbeats[1:])]
    candidates = [length for length in lengths if 2 <= length <= MAX_BAR_BEATS]
    if not candidates:
        logger.info("Too few downbeats for a tempo map: %d", len(downbeats))
        return None
    meter = Counter(candidates).most_common(1)[0][0]
    in_meter = sum(length == meter for length in lengths)
    if in_meter < REGULAR_BAR_SHARE * len(lengths):
        logger.info(
            "Downbeats too inconsistent for a tempo map: %d of %d bars in %d beats (%.0f%% needed)",
            in_meter, len(lengths), meter, 100 * REGULAR_BAR_SHARE,
        )
        return None

    first = downbeats[0]
    starts = [first]
    for start, length in zip(downbeats, lengths):
        # Missed downbeats: bars of the main meter, the last one shorter
        # if they do not fill the gap.
        if length > meter and (length % meter == 0 or length > MAX_BAR_BEATS):
            starts.extend(range(start + meter, start + length, meter))
        starts.append(start + length)
    starts.extend(range(starts[-1] + meter, beat_count, meter))

    leading = list(range(first - meter, 0, -meter))
    if first > 0:
        leading.append(0)
    return sorted(set(leading + starts)), meter


def build_tempo_map(beats: list[float], downbeats: list[float]) -> TempoMap | None:
    """A tempo map from beat and downbeat times in seconds, or None when the
    beats are too few or too irregular to trust."""
    beats = sorted({round(beat, 6) for beat in beats if beat >= 0})
    downbeats = [round(downbeat, 6) for downbeat in downbeats if downbeat >= 0]
    if len(beats) < MIN_BEATS:
        return None
    beats = _fill_missed_beats(_drop_extra_beats(beats, set(downbeats)))
    if not _is_regular(beats):
        return None

    # Tick 0 is the start of the audio, so the time before the first beat
    # becomes whole beats too, at about the tempo of the first one.
    first, step = beats[0], beats[1] - beats[0]
    if first >= 0.001:
        lead = max(1, round(first / step))
        beats = [first * part / lead for part in range(lead)] + beats
    else:
        beats = [0.0] + beats[1:]

    beats_us = [round(beat * 1_000_000) for beat in beats]
    bars = _bar_starts(len(beats), _downbeat_indices(beats, downbeats))
    if bars is None:
        return None
    starts, meter = bars
    meters = []
    for start, end in zip(starts, starts[1:] + [starts[-1] + meter]):
        if not meters or meters[-1][1] != end - start:
            meters.append((start, end - start))
    return TempoMap(beats_us=beats_us, meters=meters)


def _seconds_of_ticks(midi: mido.MidiFile):
    """A function from a tick of midi to seconds, with its tempo changes."""
    changes = sorted(
        (tick, message.tempo)
        for track in midi.tracks
        for tick, message in _absolute(track)
        if message.type == "set_tempo"
    )
    # (tick, seconds at that tick, tempo from there)
    segments = [(0, 0.0, 500_000)]
    for tick, tempo in changes:
        start_tick, start_seconds, current = segments[-1]
        seconds = start_seconds + mido.tick2second(tick - start_tick, midi.ticks_per_beat, current)
        if tick == start_tick:
            segments[-1] = (tick, start_seconds, tempo)
        else:
            segments.append((tick, seconds, tempo))
    ticks = [segment[0] for segment in segments]

    def seconds(tick: int) -> float:
        start_tick, start_seconds, tempo = segments[bisect.bisect_right(ticks, tick) - 1]
        return start_seconds + mido.tick2second(tick - start_tick, midi.ticks_per_beat, tempo)

    return seconds


def _absolute(track: mido.MidiTrack):
    tick = 0
    for message in track:
        tick += message.time
        yield tick, message


def _relative(events: list[tuple[int, mido.Message]]) -> mido.MidiTrack:
    track = mido.MidiTrack()
    previous = 0
    for tick, message in events:
        track.append(message.copy(time=tick - previous))
        previous = tick
    return track


def apply_tempo_map(midi: mido.MidiFile, tempo_map: TempoMap) -> mido.MidiFile:
    """midi on the beats of tempo_map, every event at the same time in seconds."""
    seconds = _seconds_of_ticks(midi)
    result = mido.MidiFile(type=1, ticks_per_beat=TICKS_PER_BEAT)
    grid = [
        (index * TICKS_PER_BEAT, mido.MetaMessage("set_tempo", tempo=tempo))
        for index, tempo in enumerate(tempo_map.tempos)
    ] + [
        (index * TICKS_PER_BEAT, mido.MetaMessage("time_signature", numerator=length, denominator=4))
        for index, length in tempo_map.meters
    ]
    for number, track in enumerate(midi.tracks):
        events = [
            (tempo_map.tick(seconds(tick)), message)
            for tick, message in _absolute(track)
            # mido ends every track when it writes the file.
            if message.type not in ("set_tempo", "time_signature", "end_of_track")
        ]
        if number == 0:
            # sorted is stable: at the same tick, the grid comes first.
            events = sorted(grid + events, key=lambda event: event[0])
        result.tracks.append(_relative(events))
    return result


def _event_seconds(midi: mido.MidiFile) -> list[list[float]]:
    """The time in seconds of each event that apply_tempo_map keeps, by track."""
    seconds = _seconds_of_ticks(midi)
    return [
        [seconds(tick) for tick, message in _absolute(track)
         if message.type not in ("set_tempo", "time_signature", "end_of_track")]
        for track in midi.tracks
    ]


def _same_timing(original: mido.MidiFile, result: mido.MidiFile, tempo_map: TempoMap) -> bool:
    """Whether every event of result plays when it does in original, to
    within a millisecond and half a tick of the slowest beat."""
    tolerance = 0.001 + max(tempo_map.tempos) / TICKS_PER_BEAT / 2_000_000
    before, after = _event_seconds(original), _event_seconds(result)
    return len(before) == len(after) and all(
        len(track) == len(track_after)
        and all(abs(a - b) <= tolerance for a, b in zip(track, track_after))
        for track, track_after in zip(before, after)
    )


# Beat This!'s input: log mel spectrogram frames of 22050 Hz audio, as its
# LogMelSpect makes them, and the chunks and borders of its Spect2Frames.
SPECTROGRAM_RATE = 22050
FFT_SIZE = 1024
HOP = 441
CHUNK_FRAMES = 1500
BORDER_FRAMES = 6
# Frames of spectrogram made at once, so that memory does not grow with
# the length of the audio: two minutes.
SPECTROGRAM_BLOCK_FRAMES = 6000


class _Spectrogram:
    """Beat This!'s LogMelSpect on CPU, a block at a time: the same frames,
    in memory that does not grow with the audio's length."""

    def __init__(self):
        import torchaudio

        # LogMelSpect's settings, without its centering: the signal is padded
        # here as centering pads it, and each block takes the samples of its frames.
        self.mel = torchaudio.transforms.MelSpectrogram(
            sample_rate=SPECTROGRAM_RATE, n_fft=FFT_SIZE, hop_length=HOP, f_min=30, f_max=11000,
            n_mels=128, mel_scale="slaney", normalized="frame_length", power=1, center=False,
        )

    def __call__(self, signal):
        import numpy as np
        import torch

        frames = len(signal) // HOP + 1
        half = FFT_SIZE // 2
        blocks = []
        for first in range(0, frames, SPECTROGRAM_BLOCK_FRAMES):
            last = min(first + SPECTROGRAM_BLOCK_FRAMES, frames)
            # The block's samples of the signal reflected at both ends, as
            # centering pads it, without a padded copy of the whole signal.
            # Callers give more than half an FFT of samples, so one
            # reflection is enough.
            positions = np.abs(np.arange(first * HOP - half, (last - 1) * HOP + half))
            positions = np.where(positions >= len(signal), 2 * (len(signal) - 1) - positions, positions)
            samples = signal[positions]
            blocks.append(torch.log1p(1000 * self.mel(torch.from_numpy(samples)).T))
        return torch.cat(blocks)


class _Tracker:
    """Beat This! with device memory that does not grow with the audio's
    length: only one chunk at a time is on the device. Its Audio2Beats makes
    the whole spectrogram at once, on the device: about 0.5 MiB per second
    of audio."""

    def __init__(self, device: str):
        import torch
        from beat_this.inference import load_model
        from beat_this.model.postprocessor import Postprocessor

        self.device = torch.device(device)
        if not CHECKPOINT.is_file():
            raise FileNotFoundError(f"{CHECKPOINT} is missing; the worker and RunPod images include it")
        self.model = load_model(str(CHECKPOINT), self.device)
        self.postprocess = Postprocessor(type="minimal")
        self.spectrogram = _Spectrogram()

    def __call__(self, audio, sample_rate: int):
        import numpy as np
        import soxr
        import torch
        from beat_this.inference import aggregate_prediction, split_piece

        signal = np.asarray(audio, dtype=np.float32)
        if signal.ndim == 2:
            signal = signal.mean(axis=1)
        # Too short for MIN_BEATS beats.
        if len(signal) < MIN_BEATS * MIN_BEAT_SECONDS * sample_rate:
            return [], []
        if sample_rate != SPECTROGRAM_RATE:
            signal = soxr.resample(signal, sample_rate, SPECTROGRAM_RATE)
        with torch.inference_mode():
            spectrogram = self.spectrogram(np.ascontiguousarray(signal, dtype=np.float32))
            chunks, starts = split_piece(spectrogram, CHUNK_FRAMES, border_size=BORDER_FRAMES, avoid_short_end=True)
            predictions = []
            for chunk in chunks:
                prediction = self.model(chunk.unsqueeze(0).to(self.device))
                predictions.append({key: prediction[key][0].float().cpu() for key in ("beat", "downbeat")})
            beat, downbeat = aggregate_prediction(
                predictions, starts, len(spectrogram), CHUNK_FRAMES, BORDER_FRAMES, "keep_first", "cpu",
            )
        return self.postprocess(beat, downbeat)


_lock = threading.Lock()
_trackers: dict[str, _Tracker] = {}


def _tracker(device: str) -> _Tracker:
    """The tracker for device, loaded on first use; call with _lock held."""
    if device not in _trackers:
        _trackers[device] = _Tracker(device)
    return _trackers[device]


def preload_tracker(device: str) -> None:
    """Load Beat This! for device now, so that the first job's MIDI does
    not wait for it. Nothing when the tempo map is off."""
    if settings.worker.tempo_map:
        with _lock:
            _tracker(device)


def detect_beats(audio, sample_rate: int, device: str) -> tuple[list[float], list[float]]:
    """Beat and downbeat times in seconds of audio (samples, or samples x channels)."""
    # One at a time: its GPU memory is reserved once, and on CPU it uses
    # every thread.
    with _lock:
        beats, downbeats = _tracker(device)(audio, sample_rate)
    return [float(beat) for beat in beats], [float(downbeat) for downbeat in downbeats]


def add_tempo_map(midi_path: Path, audio, sample_rate: int, device: str = "cpu") -> bool:
    """Rewrite midi_path on the beats of audio. Never fails the job: on any
    error, or beats that cannot be trusted, the file stays as it was."""
    if not settings.worker.tempo_map:
        return False
    try:
        started = time.monotonic()
        try:
            beats, downbeats = detect_beats(audio, sample_rate, device)
        except Exception as error:
            if device == "cpu" or "out of memory" not in str(error).lower():
                raise
            # Another program took the memory reserved for it.
            logger.warning("%s ran out of memory for beat tracking; tracking on CPU", device)
            beats, downbeats = detect_beats(audio, sample_rate, "cpu")
        detected = time.monotonic()
        tempo_map = build_tempo_map(beats, downbeats)
        if tempo_map is None:
            logger.info(
                "No tempo map from %d beats, found in %.1f s; MIDI kept at 120 BPM",
                len(beats), detected - started,
            )
            return False
        original = mido.MidiFile(midi_path)
        result = apply_tempo_map(original, tempo_map)
        if not _same_timing(original, result, tempo_map):
            logger.warning("Tempo map would move notes; MIDI kept at 120 BPM")
            return False
        # Written beside it and moved over it, so that a failed write, such
        # as a full disk, leaves midi_path as it was.
        rewritten = midi_path.with_name(f"{midi_path.name}.tempo")
        try:
            result.save(rewritten)
            os.replace(rewritten, midi_path)
        finally:
            rewritten.unlink(missing_ok=True)
        logger.info(
            "Tempo map: %d beats, %d downbeats, found in %.1f s, written in %.1f s",
            len(beats), len(downbeats), detected - started, time.monotonic() - detected,
        )
        return True
    except Exception:
        logger.warning("Tempo map failed; MIDI kept at 120 BPM", exc_info=True)
        return False
