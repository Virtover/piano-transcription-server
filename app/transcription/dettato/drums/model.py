"""The drum networks.

``DrumNet`` (188,764 parameters) reads the 160 mel bands of ``features.py`` (lo + hi) at 100 fps and writes, at 50 fps, one
onset logit and one velocity logit per drum key of the GM range 27-87:

    stem      2-D CNN over (time, band); time is halved once, the bands are pooled 8x
    collapse  a bidirectional GRU across the bands of every frame -> one 88-d vector per frame
    temporal  depthwise temporal conv block, residual bidirectional GRU, song-level content attention
    heads     one small bidirectional GRU per drum family (kick / snare / hats + ride / toms + crashes / shakers), each predicting
              only its own keys; every other key comes from a shared linear head; velocity from a low-rank linear head

``OrchestralExpert`` = a ``DrumNet`` + a pitch-resolving low band (the 120 ``cq`` bands) + an adapter (bidirectional GRU and
self-attention) that corrects the drum logits for concert percussion and adds 13 timpani logits, one per key 41-53 (the key of a
timpani hit is its pitch). Its router reads the adapter's states pooled over the whole recording and gives one number per
recording: the probability that it is orchestral music, which decides whether the expert or the plain ``DrumNet`` transcribes it.
"""
from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F

N_MEL = 160                              # lo + hi bands
N_CQ = 120
KEYS = list(range(27, 88))               # drum keys (GM numbering) of the drum logits
TIMPANI_KEYS = list(range(41, 54))       # keys of the timpani logits (key = pitch)


def _act(name):
    return {"relu": nn.ReLU, "silu": nn.SiLU, "gelu": nn.GELU}[name]()


class Res2d(nn.Module):
    """Residual separable block: depthwise 3x3 -> BN -> act -> pointwise -> BN, added to the input."""

    def __init__(self, c, act="relu"):
        super().__init__()
        self.dw = nn.Conv2d(c, c, 3, padding=1, groups=c)
        self.bn1 = nn.BatchNorm2d(c)
        self.pw1 = nn.Conv2d(c, c, 1)
        self.act = _act(act)
        self.bn2 = nn.BatchNorm2d(c)

    def forward(self, x):
        return x + self.bn2(self.pw1(self.act(self.bn1(self.dw(x)))))


class Stem(nn.Module):
    """A comma-separated layer spec: ``cN`` 3x3 conv to N channels (+ BN, act), ``r`` residual block, ``pTF`` max-pool (T, F)."""

    def __init__(self, cin, spec, act="relu"):
        super().__init__()
        layers, c, self.tdiv, self.fdiv = [], cin, 1, 1
        for tok in spec.split(","):
            kind, arg = tok.strip()[0], tok.strip()[1:]
            if kind == "c":
                n = int(arg)
                layers += [nn.Conv2d(c, n, 3, padding=1), nn.BatchNorm2d(n), _act(act)]
                c = n
            elif kind == "r":
                layers.append(Res2d(c, act))
            elif kind == "p":
                t, f = int(arg[0]), int(arg[1:])
                layers.append(nn.MaxPool2d((t, f)))
                self.tdiv *= t
                self.fdiv *= f
            else:
                raise ValueError(f"unknown stem layer {tok!r}")
        self.net = nn.Sequential(*layers)
        self.cout = c

    def forward(self, x):
        return self.net(x)


class BandGRU(nn.Module):
    """(B, C, T, F) -> (B, T, W): a bidirectional GRU scans the F bands of every frame; its two final states and its mean output
    are projected to W."""

    def __init__(self, c, w, m):
        super().__init__()
        self.rnn = nn.GRU(c, m, batch_first=True, bidirectional=True)
        self.proj = nn.Linear(4 * m, w)

    def forward(self, h):
        B, C, T, Fq = h.shape
        with torch.autocast(h.device.type, enabled=False):
            z = h.permute(0, 2, 3, 1).reshape(B * T, Fq, C).float()
            out, hn = self.rnn(z)
            z = torch.cat([hn[0], hn[1], out.mean(1)], -1)
        return self.proj(z).view(B, T, -1)


class ConvBlock(nn.Module):
    """Depthwise temporal conv (kernel k) -> BN -> SiLU -> pointwise, residual. On (B, T, W)."""

    def __init__(self, w, k=15, d=1):
        super().__init__()
        self.dw = nn.Conv1d(w, w, k, padding=d * (k // 2), dilation=d, groups=w)
        self.bn = nn.BatchNorm1d(w)
        self.pw = nn.Conv1d(w, w, 1)

    def forward(self, x):
        h = x.transpose(1, 2)
        return x + self.pw(F.silu(self.bn(self.dw(h)))).transpose(1, 2)


class GRUBlock(nn.Module):
    """LayerNorm -> bidirectional GRU (h per direction) -> linear back to W, residual."""

    def __init__(self, w, h=48):
        super().__init__()
        self.rnn = nn.GRU(w, h, batch_first=True, bidirectional=True)
        self.out = nn.Linear(2 * h, w)
        self.norm = nn.LayerNorm(w)

    def forward(self, x):
        with torch.autocast(x.device.type, enabled=False):
            y, _ = self.rnn(self.norm(x.float()))
        return x + self.out(y)


class SongAttention(nn.Module):
    """Self-attention over the whole input, content only (no positions), residual."""

    def __init__(self, w, da=32, heads=2):
        super().__init__()
        self.n = nn.LayerNorm(w)
        self.qkv = nn.Linear(w, 3 * da)
        self.o = nn.Linear(da, w)
        self.da, self.heads = da, heads

    def forward(self, x):
        B, T, _ = x.shape
        q, k, v = self.qkv(self.n(x)).view(B, T, 3, self.heads, self.da // self.heads).permute(2, 0, 3, 1, 4)
        a = F.scaled_dot_product_attention(q, k, v).transpose(1, 2).reshape(B, T, self.da)
        return x + self.o(a)


def _temporal_block(spec, w):
    t = spec["t"]
    if t == "tb":
        return ConvBlock(w, spec.get("k", 15), spec.get("d", 1))
    if t == "gru":
        return GRUBlock(w, spec.get("h", 48))
    if t == "attn":
        return SongAttention(w, spec.get("da", 32), spec.get("heads", 2))
    raise ValueError(f"unknown temporal block {t!r}")


class FamilyHeads(nn.Module):
    """Onset logits: one bidirectional GRU + linear head per key family (each predicts only its family's keys); the other keys
    come from a shared linear head."""

    def __init__(self, w, K, groups, h=24):
        super().__init__()
        self.groups = groups
        self.towers = nn.ModuleList([nn.GRU(w, h, batch_first=True, bidirectional=True) for _ in groups])
        self.heads = nn.ModuleList([nn.Linear(2 * h + w, len(g)) for g in groups])
        self.rest = nn.Linear(w, K)
        self.register_buffer("idx", torch.tensor([l for g in groups for l in g]))

    def forward(self, h):
        z = self.rest(h).float()
        with torch.autocast(h.device.type, enabled=False):
            hf = h.float()
            parts = [hd(torch.cat([tw(hf)[0], hf], -1)) for tw, hd in zip(self.towers, self.heads)]
        return z.index_copy(-1, self.idx, torch.cat(parts, -1))


class DrumNet(nn.Module):
    """(B, T, 160) mel features at 100 fps -> (onset logits, velocity logits), each (B, T/2, 61) at 50 fps."""

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        K = len(KEYS)
        self.stem = Stem(1, cfg["stem"], cfg.get("act", "relu"))
        assert self.stem.tdiv == 2, "the stem must halve time once"
        w = cfg["w"]
        assert cfg.get("collapse", "fgru") == "fgru"
        self.collapse = BandGRU(self.stem.cout, w, cfg["collapse_m"])
        blocks = []
        for spec in cfg["temporal"]:
            blocks += [_temporal_block(spec, w) for _ in range(spec.get("n", 1))]
        self.temporal = nn.ModuleList(blocks)
        groups = [[KEYS.index(k) for k in g] for g in cfg["towers"]]
        self.on = FamilyHeads(w, K, groups, cfg.get("tower_h", 24))
        self.vel = nn.Sequential(nn.Linear(w, 16), nn.Linear(16, K))

    def embed(self, x):
        """(B, T, 160) -> frame embeddings (B, T/2, W)."""
        return self.collapse(self.stem(((x + 5.0) / 3.0)[:, None]))

    def trunk(self, c):
        """frame embeddings -> (trunk states, onset logits, velocity logits)"""
        h = c
        for b in self.temporal:
            h = b(h)
        return h, self.on(h), self.vel(h)

    def embed_long(self, x, chunk=8000, margin=200):
        """``embed`` on a long input in windows of ``chunk`` frames with ``margin`` frames of context each side (more than the
        stem's receptive field): bounded memory, the same result up to float rounding."""
        T = x.shape[1]
        parts, s = [], 0
        while s < T:
            a, b = max(0, s - margin), min(T, s + chunk + margin)
            h = self.embed(x[:, a:b])
            lo = (s - a) // 2
            parts.append(h[:, lo:lo + (min(s + chunk, T) - s) // 2])
            s += chunk
        return torch.cat(parts, 1)

    def forward(self, x, long_above=9000):
        c = self.embed_long(x) if x.shape[1] > long_above else self.embed(x)
        _, z, v = self.trunk(c)
        return z, v


class CQBranch(nn.Module):
    """(B, T, 120) log-frequency bands at 100 fps -> (B, T/2, d). The input is stacked with itself shifted up a fifth (14 bands)
    and an octave (24 bands), so a timpani's principal tone and its strongest overtones meet in one column."""

    def __init__(self, d=128, c=32):
        super().__init__()
        self.c1 = nn.Conv2d(3, 24, (5, 7), padding=(2, 3))
        self.c2 = nn.Conv2d(24, c, (3, 5), stride=(2, 1), padding=(1, 2))
        self.c3 = nn.Conv2d(c, c, (3, 5), padding=(1, 2))
        self.n1, self.n2, self.n3 = nn.GroupNorm(4, 24), nn.GroupNorm(4, c), nn.GroupNorm(4, c)
        self.proj = nn.Linear(c * N_CQ, d)

    def forward(self, xc):
        xc = xc.float()
        xc = (xc - xc.mean(dim=(1, 2), keepdim=True)) / 4.0          # level-free
        lo = xc.amin(dim=(1, 2), keepdim=True)

        def shift(k):
            return torch.cat([xc[..., k:], lo.expand(-1, xc.shape[1], k)], -1)

        z = torch.stack([xc, shift(14), shift(24)], 1)                # (B, 3, T, F)
        z = F.silu(self.n1(self.c1(z)))
        z = F.silu(self.n2(self.c2(z)))
        z = F.silu(self.n3(self.c3(z))) + z
        B, C, T, Fq = z.shape
        return F.silu(self.proj(z.permute(0, 2, 1, 3).reshape(B, T, C * Fq)))


class OrchestralExpert(nn.Module):
    """A ``DrumNet`` plus the cq branch and an adapter. (B, T, 280) -> per frame at 50 fps: the DrumNet's own onset logits, the
    adapter's correction to them, timpani logits (13), velocity logits, and one router logit per input."""

    def __init__(self, base_cfg, d=192, h=128, layers=2, heads=4, cq_d=128):
        super().__init__()
        self.base = DrumNet(base_cfg)
        K = len(KEYS)
        self.cq = CQBranch(cq_d)
        self.inp = nn.Linear(base_cfg["w"] + cq_d + K, d)
        self.rnn = nn.GRU(d, h, num_layers=layers, batch_first=True, bidirectional=True)
        self.heads = heads
        self.qkv = nn.Linear(2 * h, 3 * 2 * h)
        self.ao = nn.Linear(2 * h, 2 * h)
        self.ln = nn.LayerNorm(2 * h)
        self.delta = nn.Linear(2 * h, K)
        self.timp = nn.Linear(2 * h, len(TIMPANI_KEYS))
        self.gate = nn.Sequential(nn.Linear(2 * h, 64), nn.SiLU(), nn.Linear(64, 1))

    def forward(self, x, chunk=8000, margin=200):
        T = x.shape[1] // 2 * 2
        x = x[:, :T]
        xm = x[..., :N_MEL]
        c = self.base.embed_long(xm, chunk, margin) if T > chunk + 2 * margin else self.base.embed(xm)
        h0, z0, v0 = self.base.trunk(c)
        cq = self.cq(x[..., N_MEL:])
        n = min(h0.shape[1], cq.shape[1])
        h0, z0, v0, cq = h0[:, :n], z0[:, :n], v0[:, :n], cq[:, :n]
        a = F.silu(self.inp(torch.cat([h0.float(), cq, torch.sigmoid(z0.float())], -1)))
        r, _ = self.rnn(a)
        B, Tn, D = r.shape
        q, k, v = self.qkv(r).view(B, Tn, 3, self.heads, D // self.heads).permute(2, 0, 3, 1, 4)
        att = F.scaled_dot_product_attention(q, k, v).transpose(1, 2).reshape(B, Tn, D)
        r = self.ln(r + self.ao(att))
        router = self.gate(r.float().mean(1))[:, 0]                   # one decision per input (a whole recording)
        return {"base": z0.float(), "drum": z0.float() + self.delta(r).float(), "timpani": self.timp(r).float(),
                "velocity": v0.float(), "router": router}
