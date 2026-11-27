"""The grouper network: one token per transcribed note, a transformer over the notes of a 24 s window.

Each token adds up
  - the note itself: pitch embedding + 11 scalars (log duration, the transcriber's onset / frame / offset probabilities of both
    voices around the note, voice slot, register);
  - three audio readers on the grouper's own front end: harmonic demixing (HDemix), the channel profile at the note's bin (Prof)
    and a lane trunk over the spectrogram plus the window's note rasters, read at each note (LaneFront);
  - the transcriber's own internal state at the note (three 368-d taps: its last hidden state at the onset and over the note,
    and its stem output over the first 200 ms), standardised over the window's notes.
Seven transformer blocks (rotary positions on onset time) follow. Heads per note: instrument logits (73 instruments), a 32-d
track embedding (unit vector; notes of one track point the same way), a keep score, and the note state that a small track head
turns into the track's instrument once the decode has formed the tracks.
"""
from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F

from . import front as FR
from .instruments import K


def rope_time(x, pos, base=10000.0):
    """x (B, H, N, D), pos (B, N) continuous positions -> rotary embedding along N."""
    D = x.shape[-1]
    half = D // 2
    freqs = 1.0 / (base ** (torch.arange(half, device=x.device, dtype=torch.float32) / half))
    ang = pos.float()[:, None, :, None] * freqs[None, None, None, :]
    cos, sin = ang.cos().to(x.dtype), ang.sin().to(x.dtype)
    x1, x2 = x[..., :half], x[..., half:2 * half]
    return torch.cat([x1 * cos - x2 * sin, x1 * sin + x2 * cos, x[..., 2 * half:]], -1)


class Block(nn.Module):
    def __init__(self, d, heads, ff):
        super().__init__()
        self.h = heads
        self.ln1 = nn.LayerNorm(d)
        self.qkv = nn.Linear(d, 3 * d)
        self.o = nn.Linear(d, d)
        self.ln2 = nn.LayerNorm(d)
        self.ff = nn.Sequential(nn.Linear(d, ff), nn.GELU(), nn.Linear(ff, d))

    def forward(self, x, pos, mask):
        B, N, D = x.shape
        q, k, v = self.qkv(self.ln1(x)).view(B, N, 3, self.h, D // self.h).permute(2, 0, 3, 1, 4)
        q, k = rope_time(q, pos), rope_time(k, pos)
        a = F.scaled_dot_product_attention(q, k, v, attn_mask=mask[:, None, None, :])
        x = x + self.o(a.transpose(1, 2).reshape(B, N, D))
        return x + self.ff(self.ln2(x))


class LayerNorm2d(nn.Module):
    def __init__(self, c, eps=1e-6):
        super().__init__()
        self.w, self.b, self.eps = nn.Parameter(torch.ones(c)), nn.Parameter(torch.zeros(c)), eps

    def forward(self, x):
        u = x.mean(1, keepdim=True)
        s = (x - u).pow(2).mean(1, keepdim=True)
        return (x - u) * torch.rsqrt(s + self.eps) * self.w[:, None, None] + self.b[:, None, None]


class PitchMix(nn.Module):
    """Per-channel taps along the lanes at local and harmonic semitone offsets."""

    def __init__(self, c, n_lanes, offsets=(-2, -1, 1, 2, -12, 12, -19, 19, -24, 24, -28, 28, -31, 31)):
        super().__init__()
        self.taps = nn.Parameter(torch.zeros(c, len(offsets)))
        S = torch.zeros(len(offsets), n_lanes, n_lanes)
        i = torch.arange(n_lanes)
        for k, o in enumerate(offsets):
            j = i + o
            ok = (j >= 0) & (j < n_lanes)
            S[k, i[ok], j[ok]] = 1.0
        self.register_buffer("S", S, persistent=False)

    def forward(self, x):
        W = torch.einsum("ck,kij->cij", self.taps.to(x.dtype), self.S.to(x.dtype))
        return torch.einsum("cij,bctj->bcti", W, x)


class LBlock(nn.Module):
    """Dilated depthwise time conv, harmonic lane mixing, LayerNorm, pointwise MLP, scaled residual."""

    def __init__(self, c, dil, n_lanes, exp=2):
        super().__init__()
        self.dw_t = nn.Conv2d(c, c, (7, 1), padding=(3 * dil, 0), dilation=(dil, 1), groups=c)
        self.pm = PitchMix(c, n_lanes)
        self.norm = LayerNorm2d(c)
        self.pw1, self.pw2 = nn.Conv2d(c, exp * c, 1), nn.Conv2d(exp * c, c, 1)
        self.gamma = nn.Parameter(torch.zeros(c))

    def forward(self, x):
        y = self.dw_t(x)
        y = y + self.pm(y)
        return x + self.pw2(F.gelu(self.pw1(self.norm(y)))) * self.gamma[:, None, None]


class LanePitchAttn(nn.Module):
    """At every frame the lanes attend to each other, with a learned bias per pitch distance (transposition-equivariant)."""

    def __init__(self, c, n_lanes, heads=4):
        super().__init__()
        self.h = heads
        self.ln, self.qkv, self.out = nn.LayerNorm(c), nn.Linear(c, 3 * c), nn.Linear(c, c)
        self.gamma = nn.Parameter(torch.zeros(c))
        self.rel_bias = nn.Parameter(torch.zeros(heads, 2 * n_lanes - 1))
        idx = torch.arange(n_lanes)
        self.register_buffer("rel_idx", (idx[None, :] - idx[:, None]) + n_lanes - 1, persistent=False)

    def forward(self, x):
        B, C, T, P = x.shape
        s = self.ln(x.permute(0, 2, 3, 1).reshape(B * T, P, C))
        q, k, v = self.qkv(s).view(B * T, P, 3, self.h, C // self.h).permute(2, 0, 3, 1, 4)
        a = F.scaled_dot_product_attention(q, k, v, attn_mask=self.rel_bias[:, self.rel_idx].to(q.dtype)[None])
        o = self.out(a.transpose(1, 2).reshape(B * T, P, C)) * self.gamma
        return x + o.view(B, T, P, C).permute(0, 3, 1, 2)


class LaneFront(nn.Module):
    """The normalised HCQT -> n_lanes semitone lanes at 80 ms (stem conv) + the notes' occupancy rasters -> lane blocks
    (dilations 1 2 4 8 ...) with lane attention after blocks `attn_at` -> each note reads its lane at its onset frame and
    averaged over its frames -> (B, N, d)."""

    def __init__(self, ch, d, c=128, depth=6, n_lanes=96, ts=8, n_attn=2, exp=2):
        super().__init__()
        self.P, self.ts = n_lanes, ts
        self.stem = nn.Conv2d(ch, c, (ts, 3), stride=(ts, 3), padding=(0, 1))
        self.occ = nn.Conv2d(4, c, 1)
        dils = [(1, 2, 4, 8)[i % 4] for i in range(depth)]
        self.blocks = nn.ModuleList([LBlock(c, dl, n_lanes, exp) for dl in dils])
        self.attn_at = set(range(depth - 1, -1, -max(1, depth // max(n_attn, 1)))[:n_attn]) if n_attn else set()
        self.attn = nn.ModuleDict({str(i): LanePitchAttn(c, n_lanes) for i in sorted(self.attn_at)})
        self.read = nn.Linear(2 * c, d)

    def forward(self, X, notes, mask):
        B = X.shape[0]
        Y = self.stem(X.to(next(self.stem.parameters()).dtype))
        Tq = Y.shape[2]
        R, on, off, lane = FR.rasters(notes, mask, Tq, self.P, 100.0 / self.ts)
        Y = Y + self.occ(R.to(Y.dtype))
        for i, blk in enumerate(self.blocks):
            Y = blk(Y)
            if i in self.attn_at:
                Y = self.attn[str(i)](Y)
        bi = torch.arange(B, device=X.device)[:, None]
        Yp = Y.permute(0, 2, 3, 1)
        at_on = Yp[bi, on, lane]
        cs = F.pad(Y.float(), (0, 0, 1, 0)).cumsum(2).permute(0, 2, 3, 1)
        mean = (cs[bi, off, lane] - cs[bi, on, lane]) / (off - on).clamp(min=1).float()[..., None]
        return self.read(torch.cat([at_on.float(), mean], -1))


def note_scalars(notes):
    """(B, N, 12) per-note scalars -> (B, N, 11) token features."""
    dur = (notes[..., 1] - notes[..., 0]).clamp(min=0.01)
    return torch.cat([torch.log(dur)[..., None], notes[..., 3:12], (notes[..., 2:3] / 64.0 - 1.0)], -1)


class Heads(nn.Module):
    def __init__(self, d, emb=32):
        super().__init__()
        self.inst = nn.Linear(d, K)
        self.emb = nn.Linear(d, emb)
        self.keep = nn.Linear(d, 1)
        # track head: (mean note state of a track + 6 register statistics) -> instrument logits; applied by the decode
        self.th = nn.Sequential(nn.Linear(d + 6, d), nn.GELU(), nn.Linear(d, d), nn.GELU(), nn.Linear(d, K))

    def forward(self, x):
        return {"inst": self.inst(x), "emb": F.normalize(self.emb(x).float(), dim=-1), "keep": self.keep(x)[..., 0], "h": x}


class NoteNet(nn.Module):
    def __init__(self, d=176, depth=7, heads=4, ff=352, emb=32, hd=10, hd_sus=4, lanes=128, lane_depth=6, lane_ts=8, lane_attn=2,
                 lane_exp=2, n_lanes=96, s15_taps=(0, 1, 2), s15_hid=256, s15_dim=368, pos_scale=10.0):
        super().__init__()
        self.pos_scale, self.s15_taps = pos_scale, tuple(s15_taps)
        self.front = FR.Front()
        ch = self.front.ch
        self.hd = FR.HDemix(d, self.front.lo, H=hd, sus=hd_sus)
        self.prof = FR.Prof(ch, d)
        self.lanes = LaneFront(ch, d, c=lanes, depth=lane_depth, n_lanes=n_lanes, ts=lane_ts, n_attn=lane_attn, exp=lane_exp)
        self.s15_ln = nn.LayerNorm(s15_dim)
        self.s15_net = nn.Sequential(nn.Linear(s15_dim * len(self.s15_taps), s15_hid), nn.GELU(), nn.Linear(s15_hid, d))
        self.pitch_emb = nn.Embedding(128, d)
        self.feat = nn.Sequential(nn.Linear(11, 64), nn.GELU(), nn.Linear(64, d))
        self.ln_in = nn.LayerNorm(d)
        self.blocks = nn.ModuleList([Block(d, heads, ff) for _ in range(depth)])
        self.ln_out = nn.LayerNorm(d)
        self.heads = Heads(d, emb)

    def tokens(self, batch):
        notes, mask = batch["notes"], batch["mask"]
        x = self.pitch_emb(notes[..., 2].round().long().clamp(0, 127)) + self.feat(note_scalars(notes))
        X, P = self.front(batch["audio"])
        xa = 0
        xa = xa + self.hd(P, notes, mask)
        xa = xa + self.prof(X, notes)
        xa = xa + self.lanes(X, notes, mask).to(x.dtype)
        x = x + xa
        f = batch["feat"][:, :, list(self.s15_taps)].float()
        # standardise every tap channel over the window's notes (what is constant over a recording carries no grouping cue)
        mk = mask[:, :, None, None].float()
        n = mk.sum(1, keepdim=True).clamp(min=1.0)
        mu = (f * mk).sum(1, keepdim=True) / n
        sd = (((f - mu) ** 2) * mk).sum(1, keepdim=True).div(n).sqrt().clamp(min=1e-3)
        f = self.s15_net(self.s15_ln((f - mu) / sd).flatten(2))
        return x + f

    def forward(self, batch):
        """batch: audio (B, L), notes (B, N, 12), mask (B, N), feat (B, N, 3, 368) -> per-note inst / emb / keep / h."""
        notes, mask = batch["notes"], batch["mask"]
        x = self.ln_in(self.tokens(batch))
        pos = notes[..., 0] * self.pos_scale
        for blk in self.blocks:
            x = blk(x, pos, mask)
        return self.heads(self.ln_out(x))
