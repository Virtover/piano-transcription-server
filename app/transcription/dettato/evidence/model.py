"""The repairer: a note-level transformer that edits the transcriber's notes.

One token per transcribed note (pitch, a confidence-derived velocity, onset and duration within an 8 s window, a few context
features, and the note's 769-number evidence vector). A 9-layer transformer (rotary positions on onset time and pitch) predicts per
note: P(keep), an onset correction (5 ms bins over +-0.5 s), a duration ratio (log steps of 0.05 over +-4) and, in two insertion slots,
P(a note is missing here) with its pitch offset, onset offset and duration.
"""
from __future__ import annotations

import math

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

L_WIN = 8.0                                     # window length (s)
DON_STEP, DON_MAX = 0.005, 0.5                  # onset correction bins
N_DON = int(round(2 * DON_MAX / DON_STEP)) + 1  # 201
N_DUR = 128                                     # absolute duration bins, log-spaced 0.01 .. 16 s
DUR_EDGES = np.exp(np.linspace(np.log(0.01), np.log(16.0), N_DUR + 1))
DUR_CENT = np.sqrt(DUR_EDGES[:-1] * DUR_EDGES[1:]).astype(np.float32)
DR_STEP, DR_MAX = 0.05, 4.0                     # duration-ratio bins: log(new / old)
N_DR = int(round(2 * DR_MAX / DR_STEP)) + 1     # 161
INS_T_STEP, INS_T_MIN, INS_T_MAX = 0.01, -1.0, 3.0
N_INS_T = int(round((INS_T_MAX - INS_T_MIN) / INS_T_STEP)) + 1   # 401
INS_P_MAX = 24
N_INS_P = 2 * INS_P_MAX + 1                     # 49
N_SLOT = 2

CAND_DT = np.dtype([("inst", "i2"), ("pitch", "i2"), ("on", "f4"), ("off", "f4"), ("score", "f4"), ("kind", "i1")])


# ----------------------------------------------------------------------------- building blocks
class RMSNorm(nn.Module):
    def __init__(self, d, eps=1e-6):
        super().__init__()
        self.w = nn.Parameter(torch.ones(d))
        self.eps = eps

    def forward(self, x):
        return x * torch.rsqrt(x.float().pow(2).mean(-1, keepdim=True) + self.eps).to(x.dtype) * self.w


def fourier(x, n, lo, hi):
    """x [...] -> [..., 2n] sin / cos features with wavelengths geometric in [lo, hi]."""
    lam = torch.exp(torch.linspace(math.log(lo), math.log(hi), n, device=x.device))
    a = 2 * math.pi * x.float()[..., None] / lam
    return torch.cat([a.sin(), a.cos()], -1)


class MultiAxisRope(nn.Module):
    """Rotary embedding over continuous axes (onset seconds, pitch)."""

    def __init__(self, head_dim, axes):
        super().__init__()
        self.axes = axes
        self.used = 2 * sum(a[0] for a in axes)
        assert self.used <= head_dim
        for i, (n, lo, hi) in enumerate(axes):
            lam = torch.exp(torch.linspace(math.log(lo), math.log(hi), n))
            self.register_buffer(f"w{i}", 2 * math.pi / lam, persistent=False)

    def angles(self, positions):
        angs = [p.float()[..., None] * getattr(self, f"w{i}") for i, p in enumerate(positions)]
        a = torch.cat(angs, -1)[:, None]
        return a.cos(), a.sin()

    def apply(self, x, cs):
        cos, sin = cs
        u = self.used
        xr, xp = x[..., :u].float(), x[..., u:]
        x1, x2 = xr[..., 0::2], xr[..., 1::2]
        r1 = x1 * cos - x2 * sin
        r2 = x1 * sin + x2 * cos
        xr = torch.stack([r1, r2], -1).flatten(-2)
        return torch.cat([xr.to(x.dtype), xp], -1)


class Attention(nn.Module):
    def __init__(self, d, heads, rope=None):
        super().__init__()
        self.h = heads
        self.hd = d // heads
        self.q = nn.Linear(d, d, bias=False)
        self.kv = nn.Linear(d, 2 * d, bias=False)
        self.o = nn.Linear(d, d, bias=False)
        self.rope = rope

    def forward(self, x, mask=None, cs=None):
        B, N, D = x.shape
        q = self.q(x).view(B, N, self.h, self.hd).transpose(1, 2)
        k, v = self.kv(x).view(B, N, 2, self.h, self.hd).permute(2, 0, 3, 1, 4)
        if self.rope is not None and cs is not None:
            q = self.rope.apply(q, cs)
            k = self.rope.apply(k, cs)
        am = mask[:, None, None, :] if mask is not None else None
        o = F.scaled_dot_product_attention(q, k, v, attn_mask=am)
        return self.o(o.transpose(1, 2).reshape(B, N, D))


class SwiGLU(nn.Module):
    def __init__(self, d, hidden):
        super().__init__()
        self.w1 = nn.Linear(d, 2 * hidden, bias=False)
        self.w2 = nn.Linear(hidden, d, bias=False)

    def forward(self, x):
        a, b = self.w1(x).chunk(2, -1)
        return self.w2(F.silu(a) * b)


class Block(nn.Module):
    def __init__(self, d, heads, rope=None, ff_mult=3):
        super().__init__()
        self.n1 = RMSNorm(d)
        self.att = Attention(d, heads, rope)
        self.n2 = RMSNorm(d)
        self.ff = SwiGLU(d, int(round(d * ff_mult / 32)) * 32)

    def forward(self, x, mask=None, cs=None):
        x = x + self.att(self.n1(x), mask=mask, cs=cs)
        return x + self.ff(self.n2(x))


class Backbone(nn.Module):
    def __init__(self, d, layers, heads, rope_t=10, rope_p=4, ff_mult=3):
        super().__init__()
        self.rope = MultiAxisRope(d // heads, [(rope_t, 0.02, 40.0), (rope_p, 2.0, 96.0)])
        self.blocks = nn.ModuleList([Block(d, heads, self.rope, ff_mult) for _ in range(layers)])
        self.norm = RMSNorm(d)

    def forward(self, x, b):
        cs = self.rope.angles([b["on"].float(), b["pitch"].float()])
        for blk in self.blocks:
            x = blk(x, mask=b["mask"], cs=cs)
        return self.norm(x)


# ----------------------------------------------------------------------------- note embedding
def context_features(b, block=256):
    """Per-note context from its neighbours [B, N, 8]: gaps to the previous / next note on the same key, overlap with the next one,
    chord size, note density within 0.5 s, same-key notes within 0.25 s."""
    on, dur, mask = b["on"], b["dur"], b["mask"]
    off = on + dur
    key = b["inst"] * 128 + b["pitch"]
    big = 10.0
    N = on.shape[1]
    outs = []
    for a in range(0, N, block):
        z = slice(a, min(a + block, N))
        mq = mask[:, z]
        same_key = (key[:, z, None] == key[:, None, :]) & mask[:, None, :] & mq[:, :, None]
        same_inst = (b["inst"][:, z, None] == b["inst"][:, None, :]) & mask[:, None, :] & mq[:, :, None]
        dt = on[:, None, :] - on[:, z, None]
        ar = torch.arange(N, device=on.device)
        eye = (ar[None, :] == ar[z][:, None])[None]
        sk = same_key & ~eye
        prev_dt = torch.where(sk & (dt <= 0), -dt, torch.full_like(dt, big)).min(-1)
        prev_gap_on = prev_dt.values
        prev_off = torch.gather(off, 1, prev_dt.indices)
        onq = on[:, z]
        prev_gap_off = torch.where(prev_gap_on < big, onq - prev_off, torch.full_like(onq, big))
        nxt = torch.where(sk & (dt > 0), dt, torch.full_like(dt, big)).min(-1)
        next_gap_on = nxt.values
        next_overlap = torch.where(next_gap_on < big, off[:, z] - (onq + next_gap_on), torch.full_like(onq, -big))
        adt = dt.abs()
        chord = ((adt < 0.035) & same_inst).float().sum(-1)
        dens = ((adt < 0.5) & mask[:, None, :]).float().sum(-1)
        dens_inst = ((adt < 0.5) & same_inst).float().sum(-1)
        same_p_near = ((adt < 0.25) & sk).float().sum(-1)

        def sl(x):
            return torch.sign(x) * torch.log1p(x.abs() * 20)
        outs.append(torch.stack([sl(prev_gap_on), sl(prev_gap_off), sl(next_gap_on), sl(next_overlap),
                                 torch.log1p(chord), torch.log1p(dens), torch.log1p(dens_inst),
                                 torch.log1p(same_p_near)], -1))
    f = torch.cat(outs, 1)
    return f * mask[..., None]


def grid_features(b, n_per=2, subdiv=(1, 2, 3, 4, 6, 8)):
    """Parameter-free beat-grid estimate per window (comb filter over 72 periods 0.18-1.5 s) -> per-note phase features [B, N, 28]."""
    on, mask = b["on"].float(), b["mask"]
    dev = on.device
    P = torch.exp(torch.linspace(math.log(0.18), math.log(1.5), 72, device=dev))
    w = mask.float()
    ang = 2 * math.pi * on[:, :, None] / P[None, None, :]
    zr = (torch.cos(ang) * w[..., None]).sum(1)
    zi = (torch.sin(ang) * w[..., None]).sum(1)
    n = w.sum(1, keepdim=True).clamp(min=1)
    R = torch.sqrt(zr ** 2 + zi ** 2) / n
    phase = torch.atan2(zi, zr)
    feats = []
    Rm = R.clone()
    for _ in range(n_per):
        k = Rm.argmax(1)
        Pk = P[k][:, None]
        ph = phase.gather(1, k[:, None])
        rk = R.gather(1, k[:, None])
        base = 2 * math.pi * on / Pk - ph
        for m in subdiv:
            feats += [torch.sin(m * base), torch.cos(m * base)]
        feats += [rk.expand_as(on), torch.log(Pk).expand_as(on)]
        kk = torch.arange(len(P), device=dev)[None]
        Rm = Rm.masked_fill((kk - k[:, None]).abs() <= 3, -1.0)
    return torch.stack(feats, -1) * mask[..., None]


class NoteEmbed(nn.Module):
    def __init__(self, d, n_ev):
        super().__init__()
        self.inst = nn.Embedding(130, d)
        self.pitch = nn.Embedding(256, d)
        self.vel = nn.Embedding(16, d)
        self.cont = nn.Sequential(nn.Linear(24 + 24 + 24 + 2 + 8 + 28, d), nn.SiLU(), nn.Linear(d, d))
        self.norm = RMSNorm(d)
        self.evm = nn.Sequential(nn.Linear(n_ev, d), nn.SiLU(), nn.Linear(d, d))

    def forward(self, b):
        drum = (b["inst"] == 128).long()
        x = self.inst(b["inst"]) + self.pitch(b["pitch"] + 128 * drum) + self.vel(b["vel"] // 8)
        ld = torch.log(b["dur"].clamp(min=1e-4))
        c = [fourier(b["on"], 12, 0.01, 32.0), fourier(ld, 12, 0.04, 40.0), fourier(b["on"] + b["dur"], 12, 0.01, 32.0),
             (ld[..., None] + 3) / 3, b["dur"][..., None].clamp(max=8) / 2, context_features(b), grid_features(b)]
        x = x + self.cont(torch.cat(c, -1)) + self.evm(b["ev"].to(x.dtype))
        return self.norm(x)


# ----------------------------------------------------------------------------- the model
def _box_argmax(p, half):
    shp = p.shape
    k = torch.ones(1, 1, 2 * half + 1, device=p.device)
    s = F.conv1d(p.reshape(-1, 1, shp[-1]).float(), k, padding=half)[:, 0].view(shp)
    return s.argmax(-1), s.max(-1).values


class Repairer(nn.Module):
    def __init__(self, d=192, layers=9, heads=6, ff_mult=3, n_ev=769):
        super().__init__()
        self.emb = NoteEmbed(d, n_ev)
        self.bb = Backbone(d, layers, heads, ff_mult=ff_mult)
        self.keep = nn.Linear(d, 1)
        self.don = nn.Linear(d, N_DON)
        self.dur = nn.Linear(d, N_DR)
        self.ins = nn.ModuleList([nn.Linear(d, 1 + N_INS_P + N_INS_T + N_DUR) for _ in range(N_SLOT)])
        self._W = None

    def forward(self, b):
        h = self.bb(self.emb(b), b)
        return dict(keep=self.keep(h).squeeze(-1), don=self.don(h), dur=self.dur(h), ins=torch.stack([m(h) for m in self.ins], 2))

    def _dur_matrix(self, dev):
        if self._W is None or self._W.device != dev:
            c = torch.tensor(DUR_CENT, device=dev)
            tol = torch.clamp(0.2 * c[:, None], min=0.05)
            self._W = ((c[None, :] - c[:, None]).abs() <= tol).float()
        return self._W

    @torch.no_grad()
    def candidates(self, b, tol_on=0.05):
        """-> per window a CAND_DT array: the n input notes edited (kind 0: score = P(keep), corrected onset / end), then 2 n
        insertion proposals (kind 1: score = P(missing note))."""
        o = self(b)
        W = self._dur_matrix(b["on"].device)
        pk = torch.sigmoid(o["keep"].float())
        don_b, _ = _box_argmax(torch.softmax(o["don"].float(), -1), int(round(tol_on / DON_STEP)))
        cent = torch.tensor(DUR_CENT, device=pk.device)
        rb, _ = _box_argmax(torch.softmax(o["dur"].float(), -1), 3)
        dur_k = b["dur"].clamp(min=1e-4) * torch.exp(rb.float() * DR_STEP - DR_MAX)
        on_k = b["on"] + (don_b.float() * DON_STEP - DON_MAX)
        ins = o["ins"].float()
        P, T = N_INS_P, N_INS_T
        pe = torch.sigmoid(ins[..., 0])
        ip = ins[..., 1:1 + P].argmax(-1) - INS_P_MAX
        it, _ = _box_argmax(torch.softmax(ins[..., 1 + P:1 + P + T], -1), int(round(tol_on / INS_T_STEP)))
        idd = (torch.softmax(ins[..., 1 + P + T:], -1) @ W).argmax(-1)
        on_i = b["on"][..., None] + (it.float() * INS_T_STEP + INS_T_MIN)
        dur_i = cent[idd]
        pitch_i = (b["pitch"][..., None] + ip).clamp(0, 127)
        res = []
        mask = b["mask"].cpu().numpy()
        arrs = {k: v.cpu().numpy() for k, v in dict(pk=pk, on_k=on_k, dur_k=dur_k, pe=pe, on_i=on_i, dur_i=dur_i,
                                                     pitch_i=pitch_i, inst=b["inst"], pitch=b["pitch"]).items()}
        for i in range(mask.shape[0]):
            n = int(mask[i].sum())
            S = N_SLOT
            c = np.zeros(n + n * S, CAND_DT)
            c["inst"][:n] = arrs["inst"][i, :n]
            c["pitch"][:n] = arrs["pitch"][i, :n]
            c["on"][:n] = arrs["on_k"][i, :n]
            c["off"][:n] = arrs["on_k"][i, :n] + arrs["dur_k"][i, :n]
            c["score"][:n] = arrs["pk"][i, :n]
            c["kind"][:n] = 0
            c["inst"][n:] = np.repeat(arrs["inst"][i, :n], S)
            c["pitch"][n:] = arrs["pitch_i"][i, :n].reshape(-1)
            c["on"][n:] = arrs["on_i"][i, :n].reshape(-1)
            c["off"][n:] = (arrs["on_i"][i, :n] + arrs["dur_i"][i, :n]).reshape(-1)
            c["score"][n:] = arrs["pe"][i, :n].reshape(-1)
            c["kind"][n:] = 1
            res.append(c)
        return res
