"""A clip: the part of a video or file a job transcribes, chosen by the
client with start_seconds and end_seconds. The API checks it against the
measured length and stores it with the job; the worker cuts the audio to it
when converting, and the job is charged only for the clip."""

import math
from dataclasses import dataclass


# Shorter clips are refused: the model needs some audio to work with.
MIN_CLIP_SECONDS = 1.0
# How far past the measured end a clip may end. A client reads a file's
# length on the device, or a video's from the site, and may differ a little
# from the server; such an end means "to the end".
END_TOLERANCE = 2.0
# The job fields a clip is stored in.
START_FIELD = "clip_start"
END_FIELD = "clip_end"
INVALID_CLIP = "invalid_clip"


class InvalidClip(ValueError):
    """The requested part does not lie within the video or file."""


@dataclass(frozen=True)
class Clip:
    start: float = 0.0
    # None: to the end of the audio.
    end: float | None = None

    def length(self, duration: float) -> float:
        return (duration if self.end is None else self.end) - self.start

    def fields(self) -> dict[str, str]:
        fields = {START_FIELD: f"{self.start:.3f}"}
        if self.end is not None:
            fields[END_FIELD] = f"{self.end:.3f}"
        return fields


def _time(value: float) -> str:
    seconds = max(0, int(value))
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


def check_clip(start: float | None, end: float | None, duration: float) -> Clip | None:
    """The clip of start and end seconds within audio of duration seconds;
    None for the whole of it. Raises InvalidClip for a part outside it."""
    if start is None and end is None:
        return None
    start = 0.0 if start is None else float(start)
    if not math.isfinite(start) or start < 0:
        raise InvalidClip("The start must be a time of 0 seconds or later")
    if end is not None:
        end = float(end)
        if not math.isfinite(end) or end <= 0:
            raise InvalidClip("The end must be a time after 0 seconds")
        if end > duration + END_TOLERANCE:
            raise InvalidClip(f"The end is after the end of the audio, at {_time(duration)}")
        if end >= duration:
            end = None
    # Rounded as stored, so the length charged is the length cut.
    start = round(start, 3)
    end = None if end is None else round(end, 3)
    if (duration if end is None else end) - start < MIN_CLIP_SECONDS:
        if start >= duration:
            raise InvalidClip(f"The start is after the end of the audio, at {_time(duration)}")
        raise InvalidClip(f"The part must be at least {MIN_CLIP_SECONDS:g} second long, and end after it starts")
    if start == 0 and end is None:
        return None
    return Clip(start, end)


def stored_clip(job: dict[str, str]) -> Clip | None:
    """A job's clip, from its fields; None for the whole audio, also for
    fields that cannot be read."""
    try:
        start = float(job[START_FIELD]) if job.get(START_FIELD) else 0.0
        end = float(job[END_FIELD]) if job.get(END_FIELD) else None
    except ValueError:
        return None
    if not math.isfinite(start) or start < 0 or (end is not None and (not math.isfinite(end) or end <= start)):
        return None
    if start == 0 and end is None:
        return None
    return Clip(start, end)
