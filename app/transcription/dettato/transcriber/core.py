"""Running the transcriber over a whole recording.

The network sees 8 s windows. A song is covered by overlapping windows (every 2 s); each window's outputs are weighted by a Hann
taper and averaged per 20 ms frame. Besides the six probability maps the same pass can average two internal states for the later
stages: ``hidden`` (the 368-d input of the output heads, per frame and key) and ``stem`` (the stem output, the least processed
state). The same pass can also return a second average over every other window (every 4 s, the grouper's features), so the
network runs once per recording.
"""
from __future__ import annotations

import math

import numpy as np
import torch

from .. import hub, precision
from .decode import decode
from .frontend import N_KEYS, PITCH_LO, SR
from .model import LaneNet

HEADS = ("onset", "frame", "offset", "onset2", "frame2", "offset2")
WIN_S = 8.0


def _autocast(device):
    return precision.autocast(device)


class _HostBuffer:
    """A (T, 88, C) float16 sum kept in host memory while the windows are added on the device.

    Windows arrive in order of their start, so a frame that lies before the current window's first frame is final: only the
    frames the current and the next windows touch (``cap`` frames, ~50 MB) live on the device; everything before them is divided
    by its weight sum (the same float16 division as the on-device path) and copied to the host once. The host array is allocated
    at its exact size (a pinned allocation would be rounded up to a power of two)."""

    def __init__(self, T, C, device, cap):
        self.T, self.device, self.cap = T, device, cap
        self.host = torch.zeros(T, N_KEYS, C, dtype=torch.float16)
        self.act = torch.zeros(cap, N_KEYS, C, dtype=torch.float16, device=device)
        self.base = 0                                  # song frame of act[0]

    def add(self, a, b, x, ws):
        if b > self.base + self.cap:
            self.flush(a, ws)
        self.act[a - self.base:b - self.base] += x

    def flush(self, upto, ws):
        """Finish and move out the frames [base, upto)."""
        upto = min(upto, self.T)
        while self.base < upto:
            n = min(upto - self.base, self.cap)
            self.host[self.base:self.base + n] = (self.act[:n] / ws[self.base:self.base + n, None, None].half()).cpu()
            self.act[:self.cap - n] = self.act[n:].clone()
            self.act[self.cap - n:] = 0
            self.base += n


class _Average:
    """Running Hann-weighted sums of the six maps and some taps over the windows added to it (see ``Transcriber.run``)."""

    def __init__(self, T, device, taps, host_taps=False, cap=0):
        self.T, self.device, self.taps, self.host_taps, self.cap = T, device, tuple(taps), host_taps, cap
        self.P = {k: torch.zeros(T, N_KEYS, device=device) for k in HEADS}
        self.buf = {}
        self.wsum = torch.zeros(T, device=device)

    def add(self, a, b, g0, k, wt, pr, tp):
        for hd in HEADS:
            self.P[hd][a:b] += pr[hd][k, a - g0:b - g0] * wt[:, None]
        for name in self.taps:
            v = tp[name]
            x = (v[k, :, a - g0:b - g0, :N_KEYS].permute(1, 2, 0).float() * wt[:, None, None]).half()
            if self.host_taps:
                if name not in self.buf:
                    self.buf[name] = _HostBuffer(self.T, v.shape[1], self.device, self.cap)
                self.buf[name].add(a, b, x, self.wsum.clamp(min=1e-6))
            else:
                if name not in self.buf:
                    self.buf[name] = torch.zeros(self.T, N_KEYS, v.shape[1], device=self.device, dtype=torch.float16)
                self.buf[name][a:b] += x
        self.wsum[a:b] += wt

    def finish(self):
        ws = self.wsum.clamp(min=1e-6)
        maps = {hd: (self.P[hd] / ws[:, None]).cpu().numpy() for hd in HEADS}
        out = {}
        for name, buf in self.buf.items():
            if self.host_taps:
                buf.flush(self.T, ws)
                out[name] = buf.host
            else:
                buf /= ws[:, None, None].half()
                out[name] = buf
        return maps, out


class Transcriber:
    """Audio (16 kHz mono) -> notes, two voices per key, each with a confidence.

    >>> t = Transcriber.from_pretrained("cuda")
    >>> maps, _ = t.run(audio)
    >>> notes = t.notes(maps)          # dict of arrays: onset, offset, pitch, conf, slot
    """

    def __init__(self, model: LaneNet, thresholds: dict, device="cpu"):
        self.model = model.to(device).eval()
        self.device = torch.device(device)
        self.th = dict(thresholds)
        self.hop = model.hop_out

    @classmethod
    def from_pretrained(cls, device="cpu"):
        cfg = hub.load_config("transcriber")
        model = LaneNet(**cfg["model"])
        model.load_state_dict(hub.load_state("transcriber"))
        return cls(model, cfg["thresholds"], device)

    @property
    def channels(self) -> int:
        return self.model.head.in_channels

    # ------------------------------------------------------------------ windowed pass
    def _layout(self, n, hop_frac):
        W, Fw = int(round(WIN_S * SR)), int(round(WIN_S / self.hop))
        T = int(math.ceil(n / SR / self.hop))
        step = max(1, int(round(W * hop_frac)))
        pad = W // 2
        starts = list(range(0, n + pad, step))
        g_pad = int(round(pad / SR / self.hop))
        return W, Fw, T, starts, pad, g_pad

    def frame_offset(self, s0, g_pad):
        """Song frame of window frame 0 for a window starting at padded sample s0."""
        return int(round(s0 / SR / self.hop)) - g_pad

    @torch.no_grad()
    def windows(self, audio, hop_frac=0.25, batch=2, taps=(), pad_batch=False):
        """Yield (window starts, {head: sigmoid probs (b, Fw, 88)}, {tap: (b, C, Fw, lanes)}) batch by batch.
        pad_batch: fill the last batch with silent windows (one static batch shape)."""
        n = len(audio)
        W, Fw, T, starts, pad, g_pad = self._layout(n, hop_frac)
        x_all = np.concatenate([np.zeros(pad, np.float32), audio.astype(np.float32), np.zeros(pad + W, np.float32)])
        for bi in range(0, len(starts), batch):
            ss = starts[bi:bi + batch]
            xs = np.stack([x_all[s0:s0 + W] for s0 in ss])
            if pad_batch and len(ss) < batch:
                xs = np.concatenate([xs, np.zeros((batch - len(ss), W), np.float32)])
            x = torch.from_numpy(xs).to(self.device)
            box = {} if taps else None
            with _autocast(self.device):
                res = self.model(x, box)
            probs = {k: torch.sigmoid(res[k].float()) for k in HEADS}
            yield ss, probs, {k: box[k] for k in taps}

    @torch.no_grad()
    def run(self, audio, hop_frac=0.25, batch=2, taps=(), pad_batch=False, sub_hop_frac=None, sub_taps=(), buf_device=None):
        """-> (maps {head: (T, 88) float32 numpy}, {tap: (T, 88, C) float16 tensor}); with ``sub_hop_frac`` also a third
        element, the same pair for the sub-run (below).

        Each map / tap is the Hann-weighted mean over the windows covering the frame.

        sub_hop_frac: a second, coarser average taken from the SAME network outputs, over only the windows whose start (in
            padded samples) is a multiple of ``round(8 s * sub_hop_frac)``. Those are exactly the windows a separate run with
            ``hop_frac=sub_hop_frac`` would use, and they are accumulated in the same order, in the same precision and with their
            own weight sum, so the sub-average equals what that separate run returns (up to the batch size the windows ran in).
            It must be a multiple of ``hop_frac``. ``sub_taps`` names the taps it returns (it may hold taps ``taps`` lacks).
        buf_device: where the float16 tap buffers live (default: the model's device). "cpu" keeps them in host memory so a long
            recording does not need ~10 MB of GPU memory per second of audio: the windows are added on the device in a sliding
            ~50 MB working region and each frame is moved to the host once, when no later window can touch it (the result is the
            same, bit for bit)."""
        n = len(audio)
        W, Fw, T, starts, pad, g_pad = self._layout(n, hop_frac)
        w = torch.from_numpy(np.hanning(Fw + 2)[1:-1].astype(np.float32)).to(self.device)
        host = str(buf_device) == "cpu" and self.device.type != "cpu"
        cap = 2 * Fw                      # on-device working frames of a host-resident tap buffer
        acc = _Average(T, self.device, taps, host, cap)
        sub = None
        if sub_hop_frac is not None:
            sub_step = max(1, int(round(W * sub_hop_frac)))
            step = max(1, int(round(W * hop_frac)))
            if sub_step % step:
                raise ValueError("sub_hop_frac must be a multiple of hop_frac")
            sub = _Average(T, self.device, sub_taps, host, cap)
        want = tuple(dict.fromkeys(tuple(taps) + tuple(sub_taps)))
        for ss, pr, tp in self.windows(audio, hop_frac, batch, want, pad_batch):
            for k, s0 in enumerate(ss):
                g0 = self.frame_offset(s0, g_pad)
                a, b = max(0, g0), min(T, g0 + Fw)
                if b <= a:
                    continue
                wt = w[a - g0:b - g0]
                acc.add(a, b, g0, k, wt, pr, tp)
                if sub is not None and s0 % sub_step == 0:
                    sub.add(a, b, g0, k, wt, pr, tp)
        out = acc.finish()
        return out if sub is None else (*out, sub.finish())

    # ------------------------------------------------------------------ notes
    def notes(self, maps):
        """Decode both voices -> dict of arrays sorted by (onset, offset, pitch):
        onset / offset (s), pitch (MIDI), conf (the voice's onset probability at the onset frame), slot (1 / 2)."""
        th = self.th
        args = dict(off_th=th["off_th"], fr_th=th["fr_th"], fr_low_frames=th["fr_low_frames"], on_nms=th.get("on_nms", 0))
        rows = []
        for slot, keys, on_th in ((1, ("onset", "frame", "offset"), th["on_th"]), (2, ("onset2", "frame2", "offset2"), th.get("v2_th"))):
            if on_th is None:
                continue
            on = maps[keys[0]]
            for u0, u1, p in decode(maps[keys[0]], maps[keys[1]], maps[keys[2]], self.hop, on_th=on_th, **args):
                t = int(min(max(round(u0 / self.hop), 0), on.shape[0] - 1))
                rows.append((u0, u1, p, float(on[t, p - PITCH_LO]), slot))
        rows.sort(key=lambda r: (r[0], r[1], r[2]))
        a = np.array([r[:3] for r in rows], np.float64).reshape(-1, 3)
        return dict(onset=a[:, 0], offset=a[:, 1], pitch=a[:, 2].astype(np.int64), conf=np.array([r[3] for r in rows], np.float32),
                    slot=np.array([r[4] for r in rows], np.int8))
