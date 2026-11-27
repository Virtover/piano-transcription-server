"""Tuning normalisation for the grouper's features.

A recording that sits clearly off A440 is resampled so that it does (pitch and time move together); the grouper reads the
transcriber's features on that audio and every time is mapped back afterwards (t_source = t_retuned x r).
"""
from __future__ import annotations

import numpy as np

SR = 16000
MIN_CENTS = 10.0          # retune only when the estimate is at least this far from A440 ...
MIN_STRENGTH = 0.12       # ... and the estimate is consistent enough (resultant length of the circular mean)


def tuning(y, n_fft=8192, hop=1024, H=6, fmin=65.0, fmax=1000.0, step_c=5.0, rel=0.5):
    """-> (offset from A440 in cents, in [-50, 50), strength in [0, 1]).

    Harmonic salience (harmonics 1..6, weights .85^(h-1)) of the square-root magnitude STFT on a 5-cent grid from 65 Hz to 1 kHz;
    per frame the local maxima above half the frame maximum, parabolically refined; their weighted circular mean modulo 100 c."""
    if len(y) < n_fft:
        return 0.0, 0.0
    win = np.hanning(n_fft).astype(np.float32)
    nfr = 1 + (len(y) - n_fft) // hop
    cents = np.arange(1200 * np.log2(fmin / 440), 1200 * np.log2(fmax / 440), step_c)
    f0 = 440 * 2 ** (cents / 1200)
    cs, ws = [], []
    for b0 in range(0, nfr, 512):
        idx = np.arange(b0, min(nfr, b0 + 512))
        fr = np.stack([y[i * hop:i * hop + n_fft] for i in idx]) * win
        S = np.abs(np.fft.rfft(fr, axis=1)).T.astype(np.float32) ** 0.5
        nb = S.shape[0]
        sal = np.zeros((len(cents), S.shape[1]), np.float32)
        for h in range(1, H + 1):
            fb = h * f0 * n_fft / SR
            ok = fb < nb - 2
            i0 = np.floor(fb[ok]).astype(int)
            w_ = (fb[ok] - i0)[:, None].astype(np.float32)
            sal[ok] += (0.85 ** (h - 1)) * ((1 - w_) * S[i0] + w_ * S[i0 + 1])
        a, b, c = sal[:-2], sal[1:-1], sal[2:]
        mx = sal.max(0, keepdims=True)
        pk = (b > a) & (b >= c) & (b > rel * mx) & (mx > 0)
        j, t = np.nonzero(pk)
        A, B, Cc = a[j, t], b[j, t], c[j, t]
        d = 0.5 * (A - Cc) / (A - 2 * B + Cc - 1e-12)
        cs.append((cents[j + 1] + np.clip(d, -1, 1) * step_c + 50) % 100 - 50)
        ws.append(B)
    c = np.concatenate(cs) if cs else np.zeros(0)
    w = np.concatenate(ws) if ws else np.zeros(0)
    if w.sum() <= 0:
        return 0.0, 0.0
    z = (w * np.exp(2j * np.pi * c / 100)).sum() / w.sum()
    return float(np.angle(z) * 100 / (2 * np.pi)), float(np.abs(z))


def ratio(y):
    """-> (offset in cents, strength, shift applied in cents, r). r = 1 when the recording is left as it is."""
    delta, strength = tuning(y)
    shift = -delta if (abs(delta) >= MIN_CENTS and strength >= MIN_STRENGTH) else 0.0
    return delta, strength, shift, 2.0 ** (shift / 1200.0)


def resample(y, r):
    """Play the audio r times faster (pitch x r): 16 kHz -> 16 kHz / r samples."""
    import soxr

    return soxr.resample(y, SR, SR / r, quality="HQ").astype(np.float32)
