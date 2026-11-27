"""Probability maps -> notes (peak picking, no learned parameters)."""
from __future__ import annotations

import numpy as np

from .frontend import PITCH_LO


def _peaks(p, th):
    """(T, L) -> bool mask of local maxima >= th (plateaus: first frame)."""
    left = np.vstack([np.full((1, p.shape[1]), -1.0), p[:-1]])
    right = np.vstack([p[1:], np.full((1, p.shape[1]), -1.0)])
    return (p >= th) & (p > left) & (p >= right)


def _refine(p, t, l):
    """Sub-frame position of a peak at (t, l) from its two neighbours, clipped to +-0.5 frame."""
    T = p.shape[0]
    C = p[t, l]
    A = p[t - 1, l] if t > 0 else C
    Bv = p[t + 1, l] if t + 1 < T else C
    den = 2 * (C - min(A, Bv))
    if den <= 1e-6:
        return 0.0
    return float(np.clip((Bv - A) / den, -0.5, 0.5))


def decode(onset, frame, offset, hop, on_th=0.25, off_th=0.3, fr_th=0.4, fr_low_frames=2, on_nms=3, min_dur=0.02,
           off_min_frames=2):
    """Maps (T, 88) of one voice -> notes [(onset s, offset s, MIDI pitch)], sorted.

    A note starts at every onset peak >= on_th (peaks within on_nms frames of a higher one are dropped). It ends at the first of:
    an offset peak >= off_th (from onset + off_min_frames), the frame probability staying below fr_th for fr_low_frames frames,
    the next onset on the same key."""
    on, fr, off = onset, frame, offset
    T, L = on.shape
    pk_on = _peaks(on, on_th)
    pk_off = _peaks(off, off_th)
    low = fr < fr_th
    run = low.copy()
    for k in range(1, fr_low_frames):
        run[:T - k] &= low[k:]
    notes = []
    for l in range(L):
        ons = np.nonzero(pk_on[:, l])[0]
        if len(ons) == 0:
            continue
        if on_nms > 0 and len(ons) > 1:
            keep = np.ones(len(ons), bool)
            for oi in np.argsort(-on[ons, l]):
                if not keep[oi]:
                    continue
                close = np.abs(ons - ons[oi]) <= on_nms
                close[oi] = False
                keep &= ~close
            ons = ons[keep]
        offs = np.nonzero(pk_off[:, l])[0]
        lows = np.nonzero(run[:, l])[0]
        for i, t0 in enumerate(ons):
            u0 = t0 + _refine(on, t0, l)
            nxt = ons[i + 1] if i + 1 < len(ons) else T
            j = np.searchsorted(lows, t0 + 1)
            e_fr = lows[j] if j < len(lows) else T
            j = np.searchsorted(offs, t0 + off_min_frames)
            e_off = offs[j] if j < len(offs) else T
            if e_off < T and e_off <= min(e_fr, nxt):
                u1 = e_off + _refine(off, e_off, l)
            elif e_fr <= nxt:
                u1 = e_fr - 0.5
            else:
                u1 = nxt + (_refine(on, nxt, l) if nxt < T else 0.0)
            on_s, off_s = u0 * hop, max(u1 * hop, u0 * hop + min_dur)
            notes.append((on_s, off_s, PITCH_LO + l))
    notes.sort()
    return notes
