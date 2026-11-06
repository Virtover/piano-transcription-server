"""The log-mel spectrogram AMT-APC reads."""

from functools import lru_cache

import numpy as np
import torch
import torchaudio

from app.transcription.amt_apc import constants


@lru_cache(maxsize=1)
def _mel_spectrogram() -> torchaudio.transforms.MelSpectrogram:
    return torchaudio.transforms.MelSpectrogram(
        sample_rate=constants.SAMPLE_RATE,
        n_fft=constants.FFT_BINS,
        win_length=constants.WINDOW_LENGTH,
        hop_length=constants.HOP_SAMPLES,
        pad_mode=constants.PAD_MODE,
        n_mels=constants.MEL_BINS,
        norm="slaney",
    )


def features(audio: np.ndarray) -> np.ndarray:
    """The log-mel feature [frames, n_bins] of mono float32 audio at 16 kHz.

    The original (AMT.wav2feature) loads a file, averages the channels, and
    resamples with torchaudio's Resample. The caller does that here, with soxr,
    which can differ from torchaudio's resampler in the last bits of each
    sample; the notes that follow are the same except where a value sits on a
    threshold. The spectrogram itself is the original's, computed on the CPU.
    """
    if audio.size == 0:
        return np.zeros((0, constants.N_BINS), dtype=np.float32)
    with torch.inference_mode():
        mel = _mel_spectrogram()(torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32)))
        return torch.log(mel + constants.LOG_OFFSET).T.contiguous().numpy()
