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


HEADS = ("kit", "kit_vel", "drum", "timpani", "vel")


@torch.no_grad()
def _probabilities(drums, feats) -> dict:
    """DrumTranscriber.probabilities, with less host memory and the same numbers: the features go to the device as
    they are stored (float16 to float32 is exact either way) instead of as a float32 copy on the host, and the three
    orchestral experts' outputs are averaged as they come, in the order np.mean adds them, instead of side by side."""
    T = feats.shape[0] // 2 * 2
    dev = drums.device
    x = torch.from_numpy(np.ascontiguousarray(feats[:T])).to(dev)[None].float()
    with drums._autocast():
        zk, vk = drums.kit(x[..., :160])
    out = {"kit": torch.sigmoid(zk[0].float()).cpu().numpy(), "kit_vel": torch.sigmoid(vk[0].float()).cpu().numpy()}
    del zk, vk
    sums, routers, n = None, [], None
    for ex in drums.experts:
        with drums._autocast():
            o = ex(x)
        base = o["base"][0].float()
        parts = (torch.sigmoid(base + (o["drum"][0].float() - base)), torch.sigmoid(o["timpani"][0].float()),
                 torch.sigmoid(o["velocity"][0].float()))
        routers.append(float(torch.sigmoid(o["router"][0].float())))
        del o, base
        n = min(n, parts[0].shape[0]) if n is not None else parts[0].shape[0]
        sums = list(parts) if sums is None else [a[:n] + b[:n] for a, b in zip(sums, parts)]
    del x
    for name, total in zip(("drum", "timpani", "vel"), sums):
        # Divided on the host: CUDA divides by a number as a product with its inverse, a rounding apart.
        out[name] = total[:n].cpu().numpy() / np.float32(len(drums.experts))
    out["router"] = float(np.mean(routers))
    return out


def _store(work: Path | None, name: str, shape, dtype=np.float32):
    if work is None:
        return np.zeros(shape, dtype)
    return np.lib.format.open_memmap(work / f"{name}.npy", mode="w+", dtype=dtype, shape=shape)


@torch.no_grad()
def probabilities(drums, feats, work: Path | None = None) -> dict:
    """The drum probabilities of feats (T, 280; an array or a file): in one call up to LONG_S, as the package; beyond it
    in blocks of LONG_S with LONG_MARGIN_S of context each side, written into arrays (files in work, if given), the
    router from the adapter states pooled over every block (each block's mean over its input, weighted by its input's
    length)."""
    T = feats.shape[0] // 2 * 2
    block, margin = int(LONG_S * FPS_IN), int(LONG_MARGIN_S * FPS_IN)
    if T <= block + 2 * margin:
        return _probabilities(drums, feats)
    pooled: list[list] = [[] for _ in drums.experts]
    hooks = [ex.gate.register_forward_hook(lambda m, inp, out, i=i: pooled[i].append(inp[0].detach().float().cpu()))
             for i, ex in enumerate(drums.experts)]
    out: dict = {}
    weights = []
    try:
        for c0 in range(0, T, block):
            c1 = min(T, c0 + block)
            a, b = max(0, c0 - margin), min(T, c1 + margin)
            P = _probabilities(drums, feats[a:b])
            lo, n = (c0 - a) // 2, (c1 - c0) // 2
            for k in HEADS:
                if k not in out:
                    out[k] = _store(work, k, (T // 2, P[k].shape[1]))
                piece = P[k][lo:lo + n]
                out[k][c0 // 2:c0 // 2 + len(piece)] = piece
            weights.append(b - a)
            del P
    finally:
        for hook in hooks:
            hook.remove()
    wts = torch.tensor(weights, dtype=torch.float32)[:, None]
    routers = []
    for i, ex in enumerate(drums.experts):
        mean = (torch.cat(pooled[i]) * wts).sum(0, keepdim=True) / wts.sum()
        param = next(ex.gate.parameters())
        routers.append(float(torch.sigmoid(ex.gate(mean.to(param.device, param.dtype)).float())[0, 0]))
    out["router"] = float(np.mean(routers))
    return out


def _peaks(prob, th: float, gap: int = 3, block: int = int(LONG_S * 50)) -> tuple[np.ndarray, np.ndarray]:
    """dettato.drums.infer.peaks over prob (an array or a file) in blocks of rows: the same hits, as each frame is
    compared with its neighbours (one row of overlap) and each lane's last hit carries over."""
    T, L = prob.shape
    last = np.full(L, -10, np.int64)
    fr, ln = [], []
    for a in range(0, T, block):
        b = min(T, a + block)
        lo, hi = max(0, a - 1), min(T, b + 1)
        seg = np.asarray(prob[lo:hi], np.float32)
        left = np.vstack([seg[:1], seg[:-1]])
        right = np.vstack([seg[1:], seg[-1:]])
        cand = ((seg >= th) & (seg >= left) & (seg >= right))[a - lo:a - lo + (b - a)]
        for t in np.nonzero(cand.any(1))[0]:
            g = int(t) + a
            m = cand[t] & (g - last > gap - 1)
            for lane in np.nonzero(m)[0]:
                fr.append(g)
                ln.append(int(lane))
            last = np.where(m, g, last)
    return np.array(fr, int), np.array(ln, int)


def decode(drums, P: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray, str]:
    """DrumTranscriber.decode with the hit picking in blocks (_peaks), for probabilities that may be files."""
    from app.transcription.dettato.drums.infer import FPS
    from app.transcription.dettato.drums.model import KEYS, TIMPANI_KEYS

    if P["router"] < drums.th_router:
        fr, ln = _peaks(P["kit"], drums.th_kit)
        return fr / FPS, np.array([KEYS[lane] for lane in ln], np.int64), np.asarray(P["kit_vel"][fr, ln]), "drum-kit"
    fd, ld = _peaks(P["drum"], drums.th_drum)
    on, ks, vs = list(fd / FPS), [KEYS[lane] for lane in ld], list(np.asarray(P["vel"][fd, ld]))
    seen: dict[int, list] = {}
    for o, k in zip(on, ks):
        seen.setdefault(k, []).append(o)
    ft, lt = _peaks(P["timpani"], drums.th_timpani)
    for f, lane in zip(ft, lt):
        o, k = f / FPS, TIMPANI_KEYS[int(lane)]
        if any(abs(o - q) <= 0.04 for q in seen.get(k, [])):
            continue
        on.append(o)
        ks.append(k)
        vs.append(float(np.clip(P["timpani"][f, lane], 0.3, 1.0)))
    order = np.argsort(on, kind="stable")
    return np.array(on)[order], np.array(ks, np.int64)[order], np.array(vs)[order], "orchestral"


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
        work = Path(directory) if work_dir is not None else None
        P = probabilities(drums, features(reader, work), work)
        on, keys, vel, which = decode(drums, P)
        router = P["router"]
        del P                                   # its files close before the folder goes
    # rounded as the package rounds them (DrumTranscriber.transcribe, then Dettato.transcribe)
    hits = [{"onset": round(float(a), 4), "offset": round(float(a) + 0.1, 4), "pitch": int(k),
             "velocity": max(1, min(127, int(round(round(float(np.clip(v, 0.05, 1.0)), 3) * 127))))}
            for a, k, v in zip(on, keys, vel)]
    return hits, {"router": router, "model": which}
