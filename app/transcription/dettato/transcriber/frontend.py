"""Harmonic constant-Q-like front end: 16 kHz audio -> log power on a log-frequency axis, one channel per harmonic."""
from __future__ import annotations

import math

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

SR = 16000
HOP = 160            # 10 ms spectrogram hop
N_KEYS = 88          # lanes: lane i = MIDI pitch 21 + i
PITCH_LO = 21


def logfreq_filterbank(n_bins, n_fft, fmin=27.5, bps=3, min_width_bins=2.0):
    """Triangular filters on a log-frequency grid (``bps`` bins per semitone from ``fmin``), each normalised to sum 1."""
    fft_freqs = np.linspace(0, SR / 2, n_fft // 2 + 1)
    centers = fmin * 2.0 ** (np.arange(n_bins) / (12.0 * bps))
    fb = np.zeros((n_bins, n_fft // 2 + 1), dtype=np.float32)
    for i in range(n_bins):
        ctr = centers[i]
        lo, hi = ctr * 2 ** (-1 / (12 * bps)), ctr * 2 ** (1 / (12 * bps))
        width = max(hi - lo, min_width_bins * SR / n_fft)
        lo, hi = ctr - width / 2, ctr + width / 2
        up = (fft_freqs - lo) / max(ctr - lo, 1e-9)
        down = (hi - fft_freqs) / max(hi - ctr, 1e-9)
        v = np.maximum(0.0, np.minimum(up, down))
        if v.sum() > 0:
            fb[i] = v / v.sum()
    return fb


class HCQT(nn.Module):
    """audio (B, n) -> (B, C, T, F) log power, T = n // HOP, F = n_bins (3 per semitone from A0).

    Channels: one per harmonic h (the spectrum shifted so that harmonic h of a note lands on the note's own bin) from the long
    window, plus plain channels from shorter windows (sharper onsets)."""

    def __init__(self, n_fft=2048, n_bins=288, bps=3, fmin=27.5, harmonics=(0.5, 1, 2, 3, 4, 5, 6, 7), extra_nffts=(512,),
                 log_eps=1e-5):
        super().__init__()
        self.n_fft, self.n_bins, self.bps, self.log_eps = n_fft, n_bins, bps, log_eps
        self.harmonics = tuple(harmonics)
        self.shifts = [int(round(12 * bps * math.log2(h))) for h in self.harmonics]
        lo, hi = min(0, min(self.shifts)), max(0, max(self.shifts))
        self.lo, self.hi = lo, hi
        fb = logfreq_filterbank(n_bins + hi - lo, n_fft, fmin * 2 ** (lo / (12 * bps)), bps)
        self.register_buffer("fb", torch.from_numpy(fb), persistent=False)
        self.register_buffer("win", torch.hann_window(n_fft), persistent=False)
        self.extra = tuple(extra_nffts)
        for i, nf in enumerate(self.extra):
            self.register_buffer(f"fb_x{i}", torch.from_numpy(logfreq_filterbank(n_bins, nf, fmin, bps)), persistent=False)
            self.register_buffer(f"win_x{i}", torch.hann_window(nf), persistent=False)
        self.n_channels = len(self.harmonics) + len(self.extra)

    def _pow(self, audio, n_fft, win):
        spec = torch.stft(audio, n_fft, hop_length=HOP, win_length=n_fft, window=win, center=True, return_complex=True,
                          pad_mode="constant")
        return spec.real ** 2 + spec.imag ** 2

    @torch.autocast("cuda", enabled=False)
    def forward(self, audio):
        audio = audio.float()
        T = audio.shape[-1] // HOP
        p = self._pow(audio, self.n_fft, self.win)[:, :, :T]
        lf = torch.log(torch.matmul(self.fb, p) + self.log_eps)
        chans = []
        for s in self.shifts:
            a = s - self.lo
            x = lf[:, a:a + self.n_bins]
            if x.shape[1] < self.n_bins:
                x = F.pad(x, (0, 0, 0, self.n_bins - x.shape[1]), value=math.log(self.log_eps))
            chans.append(x)
        for i, nf in enumerate(self.extra):
            px = self._pow(audio, nf, getattr(self, f"win_x{i}"))[:, :, :T]
            chans.append(torch.log(torch.matmul(getattr(self, f"fb_x{i}"), px) + self.log_eps))
        return torch.stack(chans, 1).transpose(2, 3)
