"""The grouper's own audio front end (no learned weights) and the per-note audio readers built on it.

Front:  audio -> X: the harmonic CQT (B, ch, T, 288), 10 ms frames, 3 bins per semitone from A0, with PCEN (a time-local gain
                   control per frequency channel) and a per-window standardisation, so recording level and EQ matter less;
                P: linear power on the extended log-frequency axis (B, NE, T), for harmonic demixing.
HDemix: every note lays a comb of H harmonics over its sounding frames; the combs of a window add up to an occupancy map and a
        note takes its share of the power in each comb bin (energy under chord mates is split, not counted twice). Per note:
        the log share-power of each harmonic at attack offsets and sustain points, relative to the note's own peak.
Prof:   mean / std of every channel at the note's bin (and the next bin up) over the note.
Note times are relative to the window; a window's audio starts PAD0 seconds before it.
"""
from __future__ import annotations

import math

import torch
import torch.nn as nn
import torch.nn.functional as F

from ..transcriber.frontend import HCQT

SR, HOP = 16000, 160
PAD0 = 0.5
HD_K = (-2, 0, 2, 4, 8, 12, 16, 24, 32, 48)                       # attack sample offsets (10 ms frames)


def pcen(X, s=0.025, alpha=0.98, delta=2.0, r=0.5, eps=1e-6, K=200):
    """PCEN on log-power maps (B, C, T, F): every frequency channel divided by its own causal slow level."""
    B, Cc, T, Fq = X.shape
    E = torch.exp(X.float()).permute(0, 1, 3, 2)
    k = s * (1 - s) ** torch.arange(K, device=X.device, dtype=torch.float32)
    n = 1 << int(math.ceil(math.log2(T + K)))
    M = torch.fft.irfft(torch.fft.rfft(E, n) * torch.fft.rfft(k, n), n)[..., :T].clamp(min=0)
    out = (E / (eps + M) ** alpha + delta) ** r - delta ** r
    return torch.log(out + 1e-4).permute(0, 1, 3, 2)


def standardise(X):
    """Per window and channel: standardise over the active region (the top 12 log units)."""
    act = X > (X.amax((2, 3), keepdim=True) - 12.0)
    cnt = act.float().sum((2, 3), keepdim=True).clamp(min=1.0)
    mu = (X * act).sum((2, 3), keepdim=True) / cnt
    sd = (((X - mu) ** 2) * act).sum((2, 3), keepdim=True).div(cnt).sqrt().clamp(min=0.5)
    return (X - mu) / sd


class Front(nn.Module):
    def __init__(self):
        super().__init__()
        self.h = HCQT()
        self.ch = self.h.n_channels
        self.lo, self.NE = self.h.lo, self.h.fb.shape[0]

    @torch.no_grad()
    def forward(self, audio):
        fr = self.h
        with torch.autocast("cuda", enabled=False):
            audio = audio.float()
            T = audio.shape[-1] // HOP
            P = torch.matmul(fr.fb, fr._pow(audio, fr.n_fft, fr.win)[:, :, :T])
            lf = torch.log(P + fr.log_eps)
            chans = []
            for s in fr.shifts:
                a = s - fr.lo
                x = lf[:, a:a + fr.n_bins]
                if x.shape[1] < fr.n_bins:
                    x = F.pad(x, (0, 0, 0, fr.n_bins - x.shape[1]), value=math.log(fr.log_eps))
                chans.append(x)
            for i, nf in enumerate(fr.extra):
                px = fr._pow(audio, nf, getattr(fr, f"win_x{i}"))[:, :, :T]
                chans.append(torch.log(torch.matmul(getattr(fr, f"fb_x{i}"), px) + fr.log_eps))
            X = torch.stack(chans, 1).transpose(2, 3)
            X = standardise(pcen(X))
        return X, P


def note_frames(notes, T, pad0=PAD0):
    f_on = ((notes[..., 0] + pad0) * 100).round().long().clamp(0, T - 1)
    f_off = torch.maximum(((notes[..., 1] + pad0) * 100).round().long().clamp(max=T), f_on + 1)
    lane = (notes[..., 2].round().long() - 21).clamp(0, 87)
    return f_on, f_off, lane


class HDemix(nn.Module):
    def __init__(self, d, lo, H=10, sus=4, hid=160):
        super().__init__()
        self.H, self.sus, self.lo = H, sus, lo
        hs = [int(round(36 * math.log2(h))) for h in range(1, H + 1)]
        self.register_buffer("shift", torch.tensor(hs, dtype=torch.long), persistent=False)
        self.prior = nn.Parameter(torch.log(torch.expm1(1.0 / torch.arange(1, H + 1, dtype=torch.float32))))
        self.nfeat = H * (len(HD_K) + sus) + H
        self.enc = nn.Sequential(nn.Linear(self.nfeat, hid), nn.GELU(), nn.Linear(hid, d))

    def raw(self, P, notes, mask):
        B, NE, T = P.shape
        N = notes.shape[1]
        H = self.H
        f_on, f_off, lane = note_frames(notes, T)
        bins = (3 * lane - self.lo)[..., None] + self.shift                             # (B, N, H) on the extended axis
        valid_b = (bins < NE).float()
        bins = bins.clamp(0, NE - 1)
        pi = F.softplus(self.prior)
        T1 = T + 1
        flat_on = (bins * T1 + f_on[..., None]).reshape(B, -1)
        flat_off = (bins * T1 + f_off[..., None]).reshape(B, -1)
        dur = (f_off - f_on).float()
        kf = torch.tensor(HD_K, device=P.device)
        fr_ = torch.tensor([0.25, 0.5, 0.75, 0.95], device=P.device) if self.sus == 4 else torch.linspace(0.25, 0.95, self.sus, device=P.device)
        tk = torch.cat([f_on[..., None] + kf, f_on[..., None] + (dur[..., None] * fr_).long()], -1).clamp(0, T - 1)
        Kt = tk.shape[-1]
        act = ((tk >= f_on[..., None]) & (tk < f_off[..., None])).float()
        idx = (bins[..., :, None] * T + tk[..., None, :]).reshape(B, -1)
        Pg = torch.gather(P.reshape(B, -1), 1, idx).view(B, N, H, Kt)
        mk = mask[..., None].float() * valid_b
        own = pi[None, None, :].expand(B, N, H)                                          # each note's claim on its comb bins
        w = own * mk
        O = torch.zeros(B, NE * T1, device=P.device).scatter_add(1, flat_on, w.reshape(B, -1))
        O = O.scatter_add(1, flat_off, -w.reshape(B, -1)).view(B, NE, T1).cumsum(2)[:, :, :T]       # occupancy
        Og = torch.gather(O.reshape(B, -1), 1, idx).view(B, N, H, Kt)
        o4 = own[..., None]
        share = o4 / (Og + o4 * (1 - act[:, :, None, :])).clamp(min=1e-6)
        lE = torch.log(Pg * share.clamp(max=1.0) + 1e-7) * valid_b[..., None]
        lE = lE - lE.amax((2, 3), keepdim=True)
        return torch.cat([lE.clamp(min=-20.0).reshape(B, N, -1) / 10.0, valid_b], -1)

    def forward(self, P, notes, mask):
        return self.enc(self.raw(P, notes, mask))


class Prof(nn.Module):
    def __init__(self, ch, d):
        super().__init__()
        self.net = nn.Sequential(nn.Linear(4 * ch, 64), nn.GELU(), nn.Linear(64, d))

    def forward(self, X, notes):
        B, ch, T, Fq = X.shape
        f_on, f_off, lane = note_frames(notes, T)
        fb = 3 * lane
        cs = torch.cumsum(F.pad(X, (0, 0, 1, 0)), 2).permute(0, 2, 3, 1)
        cs2 = torch.cumsum(F.pad(X * X, (0, 0, 1, 0)), 2).permute(0, 2, 3, 1)
        bi = torch.arange(B, device=X.device)[:, None]
        Ln = (f_off - f_on).clamp(min=1).float()[..., None]
        prof = []
        for off in (0, 1):
            fbb = (fb + off).clamp(0, Fq - 1)
            m1 = (cs[bi, f_off, fbb] - cs[bi, f_on, fbb]) / Ln
            m2 = (cs2[bi, f_off, fbb] - cs2[bi, f_on, fbb]) / Ln
            prof += [m1, ((m2 - m1 * m1).clamp(min=0) + 1e-4).sqrt()]
        return self.net(torch.cat(prof, -1))


def note_conf(notes):
    """The onset probability of the note's own voice."""
    return torch.where(notes[..., 3] > 0.5, notes[..., 5], notes[..., 4])


def rasters(notes, mask, Tq, P, fps, pad0=PAD0):
    """Note occupancy on a (Tq frames at fps) x (P lanes from A0) grid: onsets, sustain, onset confidence, second-voice onsets
    -> (B, 4, Tq, P), plus each note's onset / end frame and lane."""
    B, N = mask.shape
    on = ((notes[..., 0] + pad0) * fps).long().clamp(0, Tq - 1)
    off = torch.maximum(((notes[..., 1] + pad0) * fps).long().clamp(max=Tq), on + 1)
    lane = (notes[..., 2].round().long() - 21).clamp(0, P - 1)
    mk = mask.float()
    R = torch.zeros(B, 4, (Tq + 1) * P, device=notes.device)
    io, ie = on * P + lane, off * P + lane
    R[:, 0].scatter_add_(1, io, mk)
    R[:, 1].scatter_add_(1, io, mk)
    R[:, 1].scatter_add_(1, ie, -mk)
    R[:, 2].scatter_add_(1, io, mk * note_conf(notes))
    R[:, 3].scatter_add_(1, io, mk * (notes[..., 3] > 0.5).float())
    R = R.view(B, 4, Tq + 1, P)
    return torch.stack([R[:, 0], R[:, 1].cumsum(1), R[:, 2], R[:, 3]], 1)[:, :, :Tq], on, off, lane
