"""Input features of the drum model: 280 log-power bands per 10 ms frame, from 44.1 kHz mono audio.

    lo (128)  mel bands 20 Hz - 7.8 kHz      (16 kHz signal, 512-point STFT)
    hi (32)   mel bands 7.8 - 20 kHz         (44.1 kHz signal, 1411-point STFT)
    cq (120)  log-frequency bands, 24 per octave from 36.7 Hz (D1) to 1.14 kHz (16 kHz signal, 6144-point STFT):
              fine enough in the low register to tell timpani pitches a semitone apart

All three come from one level normalisation (the recording brought to an RMS of -18 dBFS) and use log(power + 1e-5).
The high band is why the model needs a source recorded at 40 kHz or more.
"""
from __future__ import annotations

import numpy as np
import torch

SR = 44100
FLOOR = 1e-5
N_LO, N_HI, N_CQ = 128, 32, 120
N_BANDS = N_LO + N_HI + N_CQ          # 280
CQ_BPO, CQ_FMIN, CQ_NFFT = 24, 36.708, 6144


def _hz_to_mel(f):
    return 2595.0 * np.log10(1.0 + np.asarray(f) / 700.0)


def _mel_to_hz(m):
    return 700.0 * (10.0 ** (np.asarray(m) / 2595.0) - 1.0)


def mel_filterbank(sr: int, n_fft: int, n_mels: int, fmin: float, fmax: float) -> np.ndarray:
    """Slaney-normalised triangular mel filters, (n_mels, n_fft // 2 + 1) float32."""
    n_bins = n_fft // 2 + 1
    fft_freqs = np.linspace(0, sr / 2, n_bins)
    mel_pts = _mel_to_hz(np.linspace(_hz_to_mel(fmin), _hz_to_mel(fmax), n_mels + 2))
    fb = np.zeros((n_mels, n_bins), dtype=np.float32)
    for i in range(n_mels):
        lo, ctr, hi = mel_pts[i], mel_pts[i + 1], mel_pts[i + 2]
        up = (fft_freqs - lo) / max(ctr - lo, 1e-9)
        down = (hi - fft_freqs) / max(hi - ctr, 1e-9)
        fb[i] = np.maximum(0.0, np.minimum(up, down))
    fb *= (2.0 / (mel_pts[2:] - mel_pts[:-2]))[:, None].astype(np.float32)
    return fb


def logfreq_filterbank(sr: int = 16000, n_fft: int = CQ_NFFT, fmin: float = CQ_FMIN, bpo: int = CQ_BPO, n: int = N_CQ) -> np.ndarray:
    """Triangular log-frequency filters (each sums to 1), (n, n_fft // 2 + 1) float32. A band narrower than about two FFT bins
    reads the spectrum by linear interpolation at its centre instead."""
    freqs = np.arange(n_fft // 2 + 1) * sr / n_fft
    c = fmin * 2.0 ** (np.arange(-1, n + 1) / bpo)
    fb = np.zeros((n, len(freqs)), np.float32)
    for k in range(n):
        lo, ce, hi = c[k], c[k + 1], c[k + 2]
        w = np.maximum(0.0, np.minimum((freqs - lo) / (ce - lo), (hi - freqs) / (hi - ce)))
        if w.sum() < 1.0:
            j = ce * n_fft / sr
            j0 = int(np.floor(j))
            a = j - j0
            w = np.zeros_like(w)
            w[j0] = 1 - a
            w[j0 + 1] = a
        fb[k] = w / w.sum()
    return fb


_FILTERS: dict[str, torch.Tensor] = {}


def _filters(name: str) -> torch.Tensor:
    if not _FILTERS:
        _FILTERS["lo"] = torch.from_numpy(mel_filterbank(sr=16000, n_fft=512, n_mels=N_LO, fmin=20.0, fmax=7800.0))
        _FILTERS["hi"] = torch.from_numpy(mel_filterbank(sr=SR, n_fft=1411, n_mels=N_HI, fmin=7800.0, fmax=20000.0))
        _FILTERS["cq"] = torch.from_numpy(logfreq_filterbank())
    return _FILTERS[name]


def _logspec(x: np.ndarray, n_fft: int, hop: int, fb: torch.Tensor) -> np.ndarray:
    s = torch.stft(torch.from_numpy(np.ascontiguousarray(x, dtype=np.float32)), n_fft, hop_length=hop, win_length=n_fft,
                   window=torch.hann_window(n_fft), center=True, return_complex=True, pad_mode="constant")
    return torch.log(fb @ (s.abs() ** 2) + FLOOR).T.numpy()


def level_gain(x: np.ndarray) -> float:
    """The gain that brings x to an RMS of -18 dBFS."""
    rms = float(np.sqrt(np.mean(np.asarray(x, np.float64) ** 2))) + 1e-9
    return 10 ** (-18 / 20) / rms


def features(x44: np.ndarray) -> np.ndarray:
    """Mono audio at 44.1 kHz -> (T, 280) float16 features [lo | hi | cq] at 100 frames per second."""
    import soxr

    x44 = np.asarray(x44, np.float32)
    x = (x44 * level_gain(x44)).astype(np.float32)
    x16 = soxr.resample(x, SR, 16000).astype(np.float32)
    lo = _logspec(x16, 512, 160, _filters("lo"))
    hi = _logspec(x, 1411, SR // 100, _filters("hi"))
    cq = _logspec(x16, CQ_NFFT, 160, _filters("cq"))
    T = min(len(x16) // 160, len(lo), len(hi), len(cq))
    return np.concatenate([lo[:T], hi[:T], cq[:T]], 1).astype(np.float16)
