"""dettato's drum stage over a recording of any length, in bounded memory, with the package's result.

The package computes the features of the whole recording at once (a 6144-point STFT of an hour is about 9 GB) and runs
both networks over all of it in one call: VRAM grows about 3 MB per second, and from about 30 minutes cuDNN refuses the
GRU. The networks must still see the whole recording: their GRUs and attention span it, the router pools it (one
decision per recording, between the drum-kit and the orchestral model), and the CQ branch's GroupNorms take statistics
over it. Splitting it into chunks changes that (on one song the router went from 0.54 to 0.42, the other model).
So the same computation runs in pieces:

- features: the same STFTs in blocks with margins of more than half an FFT, which is exact; the level gain stays global;
- every GRU over a long sequence: chunk by chunk, each direction carrying its hidden state (a GRU is a recurrence);
- the CQ branch: its convolutions are local, so they run on chunks with a margin, and each GroupNorm normalises with
  statistics taken over the whole recording in an extra pass over the chunks;
- attention: PyTorch's memory-efficient kernels, whose memory grows with the length, not its square.

Like the package, the drums run only for a source recorded at 40 kHz or more: the model reads the 7.8-20 kHz band. The
server converts every recording to 44.1 kHz, so the source's own rate decides (source_rate).

A recording longer than LONG_S (an hour, more than the server's usual length limit) runs through the networks in blocks
of LONG_S with LONG_MARGIN_S of context each side, so their memory stays fixed for any length; the router's decision
then takes the adapter states pooled over all blocks, weighted by their length. Up to LONG_S nothing changes.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from app.transcription.dettato.drums.infer import MIN_SOURCE_RATE, SR
from app.transcription.dettato.drums.model import CQBranch
from app.transcription import stream_audio

DF = importlib.import_module("app.transcription.dettato.drums.features")   # the package re-exports a function by this name

FPS_IN = 100
FEATURE_BLOCK_S = 60.0
GRU_CHUNK = 4096           # GRU steps (50 fps) per call
CQ_CHUNK = 12000           # input frames (100 fps) per CQ chunk
CQ_MARGIN = 16             # more than the CQ branch's receptive field in time
LONG_S = 3600.0
LONG_MARGIN_S = 120.0


def features(reader, work_dir: Path | None = None) -> np.ndarray:
    """DF.features of reader's samples (44.1 kHz mono, an ArrayReader or a WAV on disk), computed in blocks:
    (T, 280) float16 at 100 fps; with work_dir, a file there (features.f16) read through np.memmap, and the scaled
    16 kHz copy a file too."""
    # DF.level_gain, from a mean in blocks
    gain = 10 ** (-18 / 20) / (float(np.sqrt(stream_audio.mean_square(reader))) + 1e-9)
    work = Path(work_dir) if work_dir is not None else None
    x16 = stream_audio.resample(reader, 16000, work / "drums16k.f32" if work else None, gain=gain)
    T = len(x16) // 160
    out = (np.lib.format.open_memmap(work / "features.npy", mode="w+", dtype=np.float16, shape=(T, DF.N_BANDS))
           if work else np.zeros((T, DF.N_BANDS), np.float16))
    m16 = DF.CQ_NFFT // 2 + 160
    m16 -= m16 % 160
    m44 = 3 * 441                     # the high band: 1411-point FFT, hop 441
    for f0 in range(0, T, int(FEATURE_BLOCK_S * FPS_IN)):
        f1 = min(T, f0 + int(FEATURE_BLOCK_S * FPS_IN))
        a16, b16 = max(0, f0 * 160 - m16), min(len(x16), f1 * 160 + m16)
        a44, b44 = max(0, f0 * 441 - m44), min(len(reader), f1 * 441 + m44)
        seg16, seg44 = np.asarray(x16[a16:b16], np.float32), reader.read(a44, b44) * gain
        lo = DF._logspec(seg16, 512, 160, DF._filters("lo"))[(f0 * 160 - a16) // 160:][: f1 - f0]
        cq = DF._logspec(seg16, DF.CQ_NFFT, 160, DF._filters("cq"))[(f0 * 160 - a16) // 160:][: f1 - f0]
        hi = DF._logspec(seg44, 1411, 441, DF._filters("hi"))[(f0 * 441 - a44) // 441:][: f1 - f0]
        k = min(len(lo), len(cq), len(hi))
        out[f0:f0 + k] = np.concatenate([lo[:k], hi[:k], cq[:k]], 1).astype(np.float16)
    if work:
        out.flush()
    return out


def _single(gru: nn.GRU, n_in: int, w: list) -> nn.GRU:
    """A one-layer, one-direction GRU sharing the given weights."""
    m = nn.GRU(n_in, gru.hidden_size, num_layers=1, bias=gru.bias, batch_first=True, bidirectional=False)
    with torch.no_grad():
        m.weight_ih_l0, m.weight_hh_l0 = w[0], w[1]
        if gru.bias:
            m.bias_ih_l0, m.bias_hh_l0 = w[2], w[3]
    return m.eval()


class ChunkedGRU(nn.Module):
    """An nn.GRU (batch_first) whose forward runs long sequences in chunks, carrying each direction's hidden state."""

    def __init__(self, gru: nn.GRU, chunk=GRU_CHUNK):
        super().__init__()
        self.gru, self.chunk = gru, chunk
        dirs = 2 if gru.bidirectional else 1
        self.singles = nn.ModuleList()
        for layer in range(gru.num_layers):
            n_in = gru.input_size if layer == 0 else gru.hidden_size * dirs
            for d in range(dirs):
                sfx = f"_l{layer}" + ("_reverse" if d else "")
                w = [getattr(gru, f"weight_ih{sfx}"), getattr(gru, f"weight_hh{sfx}")]
                if gru.bias:
                    w += [getattr(gru, f"bias_ih{sfx}"), getattr(gru, f"bias_hh{sfx}")]
                self.singles.append(_single(gru, n_in, w))

    def forward(self, x, hx=None):
        g = self.gru
        if x.shape[1] <= self.chunk or hx is not None or not g.batch_first:
            return g(x, hx)
        dirs = 2 if g.bidirectional else 1
        inp, h_last, i = x, [], 0
        for _ in range(g.num_layers):
            outs = []
            for d in range(dirs):
                one = self.singles[i]
                i += 1
                T = inp.shape[1]
                n = (T + self.chunk - 1) // self.chunk
                parts, h = [None] * n, None
                for c in (range(n) if d == 0 else reversed(range(n))):
                    seg = inp[:, c * self.chunk:min(T, (c + 1) * self.chunk)]
                    o, h = one(seg.flip(1) if d else seg, h)
                    parts[c] = o.flip(1) if d else o
                outs.append(torch.cat(parts, 1))
                h_last.append(h)
            inp = torch.cat(outs, -1) if dirs == 2 else outs[0]
        return inp, torch.cat(h_last, 0)


def _gn_apply(x, gn: nn.GroupNorm, mean, var):
    B, C, T, Fq = x.shape
    G = gn.num_groups
    v = x.float().reshape(B, G, C // G, T, Fq)
    v = (v - mean.float()[:, :, None, None, None]) / torch.sqrt(var.float() + gn.eps)[:, :, None, None, None]
    return v.reshape(B, C, T, Fq) * gn.weight[None, :, None, None] + gn.bias[None, :, None, None]


class LongCQ(nn.Module):
    """CQBranch.forward over a long input in chunks, with whole-input GroupNorm statistics."""

    def __init__(self, cq: CQBranch, chunk=CQ_CHUNK, margin=CQ_MARGIN):
        super().__init__()
        self.cq, self.chunk, self.margin = cq, chunk - chunk % 2, margin + margin % 2

    def forward(self, xc):
        cq = self.cq
        T = xc.shape[1]
        if T <= self.chunk + 2 * self.margin:
            return cq(xc)
        xc = xc.float()
        xc = (xc - xc.mean(dim=(1, 2), keepdim=True)) / 4.0
        lo = xc.amin(dim=(1, 2), keepdim=True)
        ch, mg = self.chunk, self.margin
        spans = [(c0, min(T, c0 + ch)) for c0 in range(0, T, ch)]
        stats = {}

        def shift(v, k):
            return torch.cat([v[..., k:], lo.expand(-1, v.shape[1], k)], -1)

        def run(c0, c1, upto):
            a, b = max(0, c0 - mg), min(T, c1 + mg)
            v = xc[:, a:b]
            z1 = cq.c1(torch.stack([v, shift(v, 14), shift(v, 24)], 1))
            k0 = c0 - a
            if upto == 1:
                return z1[:, :, k0:k0 + (c1 - c0)]
            z2 = cq.c2(F.silu(_gn_apply(z1, cq.n1, *stats["n1"])))
            h0, h1 = k0 // 2, k0 // 2 + (c1 - c0 + 1) // 2           # outputs at half rate; chunks start on even frames
            if upto == 2:
                return z2[:, :, h0:h1]
            z = F.silu(_gn_apply(z2, cq.n2, *stats["n2"]))
            z3 = cq.c3(z)
            if upto == 3:
                return z3[:, :, h0:h1]
            z = (F.silu(_gn_apply(z3, cq.n3, *stats["n3"])) + z)[:, :, h0:h1]
            Bz, C, Tz, Fq = z.shape
            return F.silu(cq.proj(z.permute(0, 2, 1, 3).reshape(Bz, Tz, C * Fq)))

        for level, gn in ((1, cq.n1), (2, cq.n2), (3, cq.n3)):
            s = ss = 0.0
            cnt = 0
            for c0, c1 in spans:
                v = run(c0, c1, level).float()
                v = v.reshape(v.shape[0], gn.num_groups, -1)
                s, ss, cnt = s + v.double().sum(-1), ss + (v.double() ** 2).sum(-1), cnt + v.shape[-1]
            mean = s / cnt
            stats[f"n{level}"] = (mean, ss / cnt - mean * mean)
        return torch.cat([run(c0, c1, 4) for c0, c1 in spans], 1)


def make_long(drums):
    """Patch a DrumTranscriber's networks in place, once at load: every GRU and the CQ branches take any length."""
    for net in (drums.kit, *drums.experts):
        for parent in list(net.modules()):
            for name, child in list(parent.named_children()):
                if isinstance(child, nn.GRU):
                    chunked = ChunkedGRU(child).to(next(child.parameters()).device)
                    for single in chunked.singles:
                        # its own contiguous copy of the weights, as cuDNN wants them
                        single.flatten_parameters()
                    setattr(parent, name, chunked)
                elif isinstance(child, CQBranch):
                    setattr(parent, name, LongCQ(child))
    return drums


@torch.no_grad()
def probabilities(drums, feats) -> dict:
    """DrumTranscriber.probabilities of feats (T, 280; an array or a file): in one call up to LONG_S, as the package;
    beyond it in blocks of LONG_S with LONG_MARGIN_S of context each side, the router from the adapter states pooled
    over every block (each block's mean over its input, weighted by its input's length)."""
    T = feats.shape[0] // 2 * 2
    block, margin = int(LONG_S * FPS_IN), int(LONG_MARGIN_S * FPS_IN)
    if T <= block + 2 * margin:
        return drums.probabilities(np.asarray(feats[:T], np.float32))
    pooled: list[list] = [[] for _ in drums.experts]
    hooks = [ex.gate.register_forward_hook(lambda m, inp, out, i=i: pooled[i].append(inp[0].detach().float().cpu()))
             for i, ex in enumerate(drums.experts)]
    parts: dict = {k: [] for k in ("kit", "kit_vel", "drum", "timpani", "vel")}
    weights = []
    try:
        for c0 in range(0, T, block):
            c1 = min(T, c0 + block)
            a, b = max(0, c0 - margin), min(T, c1 + margin)
            P = drums.probabilities(np.asarray(feats[a:b], np.float32))
            lo, n = (c0 - a) // 2, (c1 - c0) // 2
            for k in parts:
                parts[k].append(P[k][lo:lo + n])
            weights.append(b - a)
    finally:
        for hook in hooks:
            hook.remove()
    out = {k: np.concatenate(v) for k, v in parts.items()}
    wts = torch.tensor(weights, dtype=torch.float32)[:, None]
    routers = []
    for i, ex in enumerate(drums.experts):
        mean = (torch.cat(pooled[i]) * wts).sum(0, keepdim=True) / wts.sum()
        param = next(ex.gate.parameters())
        routers.append(float(torch.sigmoid(ex.gate(mean.to(param.device, param.dtype)).float())[0, 0]))
    out["router"] = float(np.mean(routers))
    return out


@torch.no_grad()
def transcribe(drums, x44, source_rate: int | None, work_dir: Path | None = None) -> tuple[list[dict], dict]:
    """Drum hits of a 44.1 kHz mono recording (an array, or a reader such as stream_audio.Pcm16Wav) whose source was at
    source_rate (None: unknown, taken as 44.1 kHz) -> (hits sorted by onset, info). No hits for a source below 40 kHz,
    as in the package. With work_dir, its large arrays are files there."""
    rate = int(source_rate) if source_rate else SR
    if rate < MIN_SOURCE_RATE:
        return [], {"skipped": f"source recorded at {rate} Hz (the drum model needs >= {MIN_SOURCE_RATE} Hz)"}
    reader = x44 if hasattr(x44, "read") else stream_audio.ArrayReader(x44, SR)
    if len(reader) < SR // 10:
        return [], {"router": None, "model": None}
    with stream_audio.scratch(work_dir, "drums-") as directory:
        P = probabilities(drums, features(reader, Path(directory) if work_dir is not None else None))
    on, keys, vel, which = drums.decode(P)
    # rounded as the package rounds them (DrumTranscriber.transcribe, then Dettato.transcribe)
    hits = [{"onset": round(float(a), 4), "offset": round(float(a) + 0.1, 4), "pitch": int(k),
             "velocity": max(1, min(127, int(round(round(float(np.clip(v, 0.05, 1.0)), 3) * 127))))}
            for a, k, v in zip(on, keys, vel)]
    return hits, {"router": P["router"], "model": which}
