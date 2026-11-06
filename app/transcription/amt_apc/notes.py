"""From the network's piano rolls to notes.

This is AMT.mpe2note and AMT.note2midi of the original, as Pipeline.wav2midi
calls them: the "B" outputs, the thresholds of constants, velocity 0 ignored,
and the shorter of the offset and frame ends. Arithmetic stays in float32 where
the original's does, so a note starts and ends at the same moment.
"""

import numpy as np
from transkun.Data import Note

from app.transcription.amt_apc import constants

HOP_SECONDS = float(constants.HOP_SAMPLES / constants.SAMPLE_RATE)


def _peaks(values: np.ndarray, threshold: float) -> list[tuple[int, float]]:
    """The local maxima at or above the threshold as (frame, time in seconds).

    A plateau of equal values counts at each of its frames, as in the original,
    when the values beyond it are lower on both sides. The time is refined by
    the neighbours to better than a frame.
    """
    count = len(values)
    peaks = []
    for i in np.flatnonzero(values >= threshold).tolist():
        left = True
        for before in range(i - 1, -1, -1):
            if values[i] > values[before]:
                break
            if values[i] < values[before]:
                left = False
                break
        right = True
        for after in range(i + 1, count):
            if values[i] > values[after]:
                break
            if values[i] < values[after]:
                right = False
                break
        if not (left and right):
            continue
        if i == 0 or i == count - 1 or values[i - 1] == values[i + 1]:
            time = i * HOP_SECONDS
        elif values[i - 1] > values[i + 1]:
            time = i * HOP_SECONDS - (
                HOP_SECONDS * 0.5 * (values[i - 1] - values[i + 1]) / (values[i] - values[i + 1])
            )
        else:
            time = i * HOP_SECONDS + (
                HOP_SECONDS * 0.5 * (values[i + 1] - values[i - 1]) / (values[i] - values[i - 1])
            )
        peaks.append((i, time))
    return peaks


def notes(onset: np.ndarray, offset: np.ndarray, mpe: np.ndarray, velocity: np.ndarray) -> list[Note]:
    """The notes of piano rolls [frames, 88] (velocity as int8 classes), sorted by start and then pitch.

    A note starts at an onset peak and ends at the first offset peak or the
    first frame below the threshold, whichever is earlier, or at the next
    onset of the same key. Notes shorter than MIN_DURATION are dropped.
    """
    found = []
    for key in range(constants.NUM_NOTE):
        onsets = _peaks(np.ascontiguousarray(onset[:, key]), constants.THRESHOLD_ONSET)
        offsets = _peaks(np.ascontiguousarray(offset[:, key]), constants.THRESHOLD_OFFSET)
        frame = mpe[:, key]
        end_time = 0.0
        frame_time = 0.0
        key_notes = []
        for index, (onset_frame, onset_time) in enumerate(onsets):
            if index + 1 < len(onsets):
                next_frame, next_time = onsets[index + 1]
            else:
                next_frame = len(mpe)
                next_time = (next_frame - 1) * HOP_SECONDS

            offset_frame = onset_frame + 1
            has_offset = False
            for peak_frame, peak_time in offsets:
                if onset_frame < peak_frame:
                    offset_frame, end_time, has_offset = peak_frame, peak_time, True
                    break
            if offset_frame > next_frame:
                offset_frame, end_time = next_frame, next_time

            frame_end = onset_frame + 1
            has_frame_end = False
            for position in range(onset_frame + 1, next_frame):
                if frame[position] < constants.THRESHOLD_FRAME:
                    frame_end, has_frame_end = position, True
                    frame_time = position * HOP_SECONDS
                    break

            if not has_offset and not has_frame_end:
                end = float(next_time)
            elif has_offset and not has_frame_end:
                end = float(end_time)
            elif not has_offset and has_frame_end:
                end = float(frame_time)
            elif offset_frame <= frame_end:
                end = float(end_time)
            else:
                end = float(frame_time)

            velocity_value = int(velocity[onset_frame, key])
            if velocity_value > 0:
                key_notes.append(
                    {"pitch": key + constants.NOTE_MIN, "start": float(onset_time), "end": end, "velocity": velocity_value}
                )
            # A key struck again before its note ended cuts that note short.
            if len(key_notes) > 1 and key_notes[-1]["start"] < key_notes[-2]["end"]:
                key_notes[-2]["end"] = key_notes[-1]["start"]
        found.extend(key_notes)

    found.sort(key=lambda note: note["pitch"])
    found.sort(key=lambda note: note["start"])
    return [
        Note(start=note["start"], end=note["end"], pitch=note["pitch"], velocity=note["velocity"])
        for note in found
        if note["end"] - note["start"] >= constants.MIN_DURATION
    ]
