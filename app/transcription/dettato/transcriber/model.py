"""The transcriber network: a pitch-equivariant lane model.

audio (B, n) 16 kHz -> HCQT -> stem (20 ms frames x 1 semitone, 96 lanes from A0) -> lane blocks at 40 ms (ConvNeXt-style: depthwise
time conv + sparse pitch mixing at local and harmonic intervals), with axial attention (across pitch, across time) and a bidirectional
lane GRU after blocks 5, 10 and 15 -> back to 20 ms + refine blocks -> 1x1 heads on lanes 0-87.

Heads (logits, each (B, T, 88)): onset / frame / offset for the first voice of each key, onset2 / frame2 / offset2 for a second
voice on the same key (two instruments playing the same pitch).
"""
from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F

from .. import precision
from .frontend import HCQT, N_KEYS


class PitchMix(nn.Module):
    """Per-channel taps along the pitch axis at fixed semitone offsets, applied as one banded (P x P) matrix per channel."""

    def __init__(self, c, n_lanes, offsets):
        super().__init__()
        self.offsets = tuple(offsets)
        self.taps = nn.Parameter(torch.zeros(c, len(self.offsets)))
        S = torch.zeros(len(self.offsets), n_lanes, n_lanes)
        i = torch.arange(n_lanes)
        for k, o in enumerate(self.offsets):
            j = i + o
            ok = (j >= 0) & (j < n_lanes)
            S[k, i[ok], j[ok]] = 1.0
        self.register_buffer("S", S, persistent=False)

    def forward(self, x):
        W = torch.einsum("ck,kij->cij", self.taps.to(x.dtype), self.S.to(x.dtype))
        return torch.einsum("cij,bctj->bcti", W, x)


class LaneBlock(nn.Module):
    """Depthwise conv along time (kernel kt, dilation dt) + pitch mixing, BatchNorm, pointwise MLP, layer-scaled residual."""

    def __init__(self, c, kt=7, dt=1, exp=2, pmix=(), n_lanes=96):
        super().__init__()
        self.pmix = PitchMix(c, n_lanes, pmix)
        self.dw_t = nn.Conv2d(c, c, (kt, 1), padding=((kt // 2) * dt, 0), dilation=(dt, 1), groups=c)
        self.norm = nn.BatchNorm2d(c)
        self.pw1 = nn.Conv2d(c, c * exp, 1)
        self.pw2 = nn.Conv2d(c * exp, c, 1)
        self.gamma = nn.Parameter(torch.zeros(c))

    def forward(self, x):
        y = self.dw_t(x)
        y = y + self.pmix(y)
        y = self.pw2(F.gelu(self.pw1(self.norm(y))))
        return x + y * self.gamma[:, None, None]


class AxialAttn(nn.Module):
    """Self-attention along one axis of (B, C, T, P). axis="time": each lane over its frames (rotary positions); axis="pitch":
    the lanes of each frame (a learned bias per head for every pitch distance, so the layer is transposition-equivariant)."""

    def __init__(self, c, axis, heads=8, n_pos=128):
        super().__init__()
        self.axis, self.heads = axis, heads
        self.ln = nn.LayerNorm(c)
        self.qkv = nn.Linear(c, 3 * c)
        self.out = nn.Linear(c, c)
        self.gamma = nn.Parameter(torch.zeros(c))
        if axis == "pitch":
            self.rel_bias = nn.Parameter(torch.zeros(heads, 2 * n_pos - 1))
            idx = torch.arange(n_pos)
            self.register_buffer("rel_idx", (idx[None, :] - idx[:, None]) + n_pos - 1, persistent=False)

    @staticmethod
    def rope(x):
        N, H, L, D = x.shape
        half = D // 2
        freqs = 1.0 / (10000 ** (torch.arange(half, device=x.device, dtype=torch.float32) / half))
        t = torch.arange(L, device=x.device, dtype=torch.float32)
        ang = t[:, None] * freqs[None]
        cos, sin = ang.cos().to(x.dtype), ang.sin().to(x.dtype)
        x1, x2 = x[..., :half], x[..., half:2 * half]
        return torch.cat([x1 * cos - x2 * sin, x1 * sin + x2 * cos, x[..., 2 * half:]], -1)

    def forward(self, x):
        B, C, T, P = x.shape
        if self.axis == "time":
            s = x.permute(0, 3, 2, 1).reshape(B * P, T, C)
        else:
            s = x.permute(0, 2, 3, 1).reshape(B * T, P, C)
        h = self.ln(s)
        N, L, _ = h.shape
        qkv = self.qkv(h).view(N, L, 3, self.heads, C // self.heads).permute(2, 0, 3, 1, 4)
        q, k, v = qkv[0], qkv[1], qkv[2]
        mask = None
        if self.axis == "time":
            q, k = self.rope(q), self.rope(k)
        else:
            mask = self.rel_bias[:, self.rel_idx[:L, :L]].to(q.dtype)[None]
        a = F.scaled_dot_product_attention(q, k, v, attn_mask=mask)
        o = self.out(a.transpose(1, 2).reshape(N, L, C)) * self.gamma
        if self.axis == "time":
            o = o.view(B, P, T, C).permute(0, 3, 2, 1)
        else:
            o = o.view(B, T, P, C).permute(0, 3, 1, 2)
        return x + o


class LaneGRU(nn.Module):
    """Bidirectional GRU along time for every lane (shared weights), projected back to C channels. Residual.
    On CUDA it runs in bfloat16 (float32 parameters cast per call), as it was trained; in float32 where bfloat16 is not
    native (dettato.precision)."""

    def __init__(self, c, hidden=192, bf16=True):
        super().__init__()
        self.ln = nn.LayerNorm(c)
        self.gru = nn.GRU(c, hidden, batch_first=True, bidirectional=True)
        self.out = nn.Linear(2 * hidden, c)
        self.bf16 = bf16

    def forward(self, x):
        B, C, T, P = x.shape
        s = x.permute(0, 3, 2, 1).reshape(B * P, T, C)
        if self.bf16 and x.is_cuda and precision.bf16(x.device):
            inp = self.ln(s).to(torch.bfloat16)
            w = [p.to(torch.bfloat16) for p in self.gru._flat_weights]
            hx = inp.new_zeros(2, inp.shape[0], self.gru.hidden_size)
            h, _ = torch._VF.gru(inp, hx, w, True, 1, 0.0, False, True, True)
        else:
            with torch.autocast("cuda", enabled=False):
                h, _ = self.gru(self.ln(s).float())
        o = self.out(h.to(s.dtype))
        return x + o.view(B, P, T, C).permute(0, 3, 2, 1)


class Stem(nn.Module):
    """HCQT (B, Cin, T10, 3 P) -> (B, C, T, P): two 3x3 convs, then a centred stride-(2, 3) conv to 20 ms x 1 bin per semitone."""

    def __init__(self, cin, c, mid=64, t_stride=2):
        super().__init__()
        self.inorm = nn.BatchNorm2d(cin)
        self.c1 = nn.Conv2d(cin, mid, 3, padding=1)
        self.n1 = nn.BatchNorm2d(mid)
        self.c2 = nn.Conv2d(mid, mid, 3, padding=1)
        self.n2 = nn.BatchNorm2d(mid)
        kt = 3 if t_stride == 2 else 1
        self.down = nn.Conv2d(mid, c, (kt, 3), stride=(t_stride, 3), padding=(kt // 2, 1))
        self.n3 = nn.BatchNorm2d(c)

    def forward(self, x):
        x = self.inorm(x)
        x = F.gelu(self.n1(self.c1(x)))
        x = F.gelu(self.n2(self.c2(x)))
        return self.n3(self.down(x))


class LaneNet(nn.Module):
    def __init__(self, c=368, depth=15, exp=2, kt=7, dilations=(1, 2, 4, 8), mid=64, t_stride=2, attn_at=(5, 10, 15),
                 attn_axes=("pitch", "time"), attn_heads=8, gru_at=(5, 10, 15), gru_hidden=192, gru_bf16=True,
                 heads=("onset", "frame", "offset", "onset2", "frame2", "offset2"), harmonics=(0.5, 1, 2, 3, 4, 5, 6, 7),
                 extra_nffts=(512,), n_fft=2048, refine=2, n_lanes=88,
                 pmix=(-2, -1, 1, 2, -12, 12, -19, 19, -24, 24, -28, 28, -31, 31)):
        super().__init__()
        self.n_lanes = n_lanes
        self.front = HCQT(n_fft=n_fft, harmonics=harmonics, extra_nffts=extra_nffts)
        self.hop_out = 0.01 * t_stride
        self.stem = Stem(self.front.n_channels, c, mid, t_stride)
        self.layers = nn.ModuleList()
        for i in range(depth):
            self.layers.append(LaneBlock(c, kt=kt, dt=dilations[i % len(dilations)], exp=exp, pmix=pmix, n_lanes=n_lanes))
            if (i + 1) in attn_at:
                for ax in attn_axes:
                    self.layers.append(AxialAttn(c, ax, heads=attn_heads))
            if (i + 1) in gru_at:
                self.layers.append(LaneGRU(c, gru_hidden, bf16=gru_bf16))
        self.down = nn.Conv2d(c, c, (3, 1), stride=(2, 1), padding=(1, 0), groups=c)
        self.up_norm = nn.BatchNorm2d(c)
        self.refine = nn.ModuleList([LaneBlock(c, kt=kt, dt=1, exp=exp, pmix=pmix, n_lanes=n_lanes) for _ in range(refine)])
        self.head_names = tuple(heads)
        self.head = nn.Conv2d(c, len(self.head_names), 1)

    def forward(self, audio, taps: dict | None = None):
        """audio (B, n) -> {head: logits (B, T, 88)}. With ``taps`` (a dict), also stores taps["stem"] = the stem output
        (B, C, T, 96) and taps["hidden"] = the heads' input (B, C, T, 88)."""
        x = self.front(audio)
        h = self.stem(x)
        if taps is not None:
            taps["stem"] = h
        if self.n_lanes < h.shape[3]:
            h = h[:, :, :, :self.n_lanes]
        skip, T = h, h.shape[2]
        h = self.down(h)
        for layer in self.layers:
            h = layer(h)
        h = self.up_norm(h.repeat_interleave(2, dim=2)[:, :, :T] + skip)
        for layer in self.refine:
            h = layer(h)
        h = h[:, :, :, :N_KEYS]
        if taps is not None:
            taps["hidden"] = h
        o = self.head(h)
        return {n: o[:, i] for i, n in enumerate(self.head_names)}
