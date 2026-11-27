"""Long recordings without holding them in memory: the converted WAV read from disk, and resampled copies written to
files, a block at a time. No torch import.

A 10-hour recording is 6 GB as float32 at 44.1 kHz; a job that kept a few copies of it (the source, 16 kHz, retuned, the
drums' scaled copy) would need tens of GB of RAM. Instead every copy is a float32 file read through np.memmap, which the
models slice like an array. soxr's streaming resampler gives the same samples as its one-shot call (checked for every
rate used here, with the same length), so the files hold exactly what the in-memory path computes.
"""
from __future__ import annotations

import struct
import tempfile
from pathlib import Path

import numpy as np

BLOCK_SECONDS = 30.0


class Pcm16Wav:
    """A 16-bit PCM WAV file, as the server converts recordings (convert_to_wav), read from disk.
    read(a, b) gives float32 mono samples [a, b) as app.transcription.piano_transcription.read_audio scales them
    (int16 / 2**15, channels averaged)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        with self.path.open("rb") as f:
            header = f.read(12)
            if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
                raise ValueError("not a WAV file")
            fmt = None
            offset = 12
            while True:
                chunk = f.read(8)
                if len(chunk) < 8:
                    raise ValueError("no data chunk")
                kind, size = chunk[:4], struct.unpack("<I", chunk[4:])[0]
                offset += 8
                if kind == b"fmt ":
                    fmt = f.read(size)
                    offset += size
                elif kind == b"data":
                    if fmt is None:
                        raise ValueError("data before fmt")
                    audio_format, channels, rate, _, _, bits = struct.unpack("<HHIIHH", fmt[:16])
                    if audio_format not in (1, 0xFFFE) or bits != 16 or channels < 1:
                        raise ValueError("not 16-bit PCM")
                    self.rate, self.channels = int(rate), int(channels)
                    available = self.path.stat().st_size - offset
                    # ffmpeg writing to a pipe leaves the size 0 or too large: the file's own length decides.
                    size = min(size, available) if size else available
                    frames = size // (2 * channels)
                    self._data = np.memmap(self.path, dtype="<i2", mode="r", offset=offset, shape=(frames, channels))
                    return
                else:
                    f.seek(size + (size & 1), 1)
                    offset += size + (size & 1)

    def __len__(self) -> int:
        return self._data.shape[0]

    @property
    def seconds(self) -> float:
        return len(self) / self.rate

    def read(self, a: int, b: int) -> np.ndarray:
        block = np.float32(self._data[max(0, a):max(0, b)]) / 2**15
        return block[:, 0] if self.channels == 1 else block.mean(axis=1).astype(np.float32)


class ArrayReader:
    """An in-memory mono float32 array with the same read(a, b)."""

    def __init__(self, samples: np.ndarray, rate: int):
        self.samples, self.rate = np.asarray(samples, np.float32), int(rate)

    def __len__(self) -> int:
        return len(self.samples)

    def read(self, a: int, b: int) -> np.ndarray:
        return self.samples[max(0, a):max(0, b)]


def blocks(reader, block: int | None = None):
    """(start, samples) of consecutive blocks of reader."""
    block = block or int(BLOCK_SECONDS * reader.rate)
    for a in range(0, len(reader), block):
        yield a, reader.read(a, min(len(reader), a + block))


def resample(reader, rate_out: float, path: Path | None = None, quality: str = "HQ", gain: float | None = None) -> np.ndarray:
    """reader's samples (times gain, as float32 x * gain), resampled with soxr to rate_out: into the float32 file path,
    returned as a read-only np.memmap, or, without path, into memory. Equal to soxr.resample of the whole signal."""
    import soxr

    rate_in = reader.rate
    if path is None and rate_out == rate_in and gain is None:
        return np.ascontiguousarray(np.concatenate([b for _, b in blocks(reader)]) if len(reader) else np.zeros(0, np.float32))
    stream = soxr.ResampleStream(rate_in, rate_out, 1, dtype="float32", quality=quality)
    out = open(path, "wb") if path is not None else None
    parts = []
    total = 0
    try:
        n = len(reader)
        if n == 0:
            data = stream.resample_chunk(np.zeros(0, np.float32), last=True)
            (out.write(data.tobytes()) if out else parts.append(data))
            total += len(data)
        for a, x in blocks(reader):
            if gain is not None:
                x = x * gain
            data = stream.resample_chunk(np.ascontiguousarray(x, dtype=np.float32), last=a + len(x) >= n)
            if out is not None:
                out.write(np.ascontiguousarray(data, dtype=np.float32).tobytes())
            else:
                parts.append(data)
            total += len(data)
    finally:
        if out is not None:
            out.close()
    if path is None:
        return np.concatenate(parts).astype(np.float32) if parts else np.zeros(0, np.float32)
    if total == 0:
        return np.zeros(0, np.float32)
    return np.memmap(path, dtype=np.float32, mode="r", shape=(total,))


def mean_square(reader) -> float:
    """The mean of the squared samples, in float64, a block at a time."""
    total, n = 0.0, 0
    for _, x in blocks(reader):
        x = np.asarray(x, np.float64)
        total += float(np.dot(x, x))
        n += len(x)
    return total / n if n else 0.0


def scratch(work_dir: Path | None, prefix: str) -> tempfile.TemporaryDirectory:
    """A folder for a job's resampled copies, in work_dir (the job's folder) or the system's temporary folder."""
    return tempfile.TemporaryDirectory(prefix=prefix, dir=work_dir)
