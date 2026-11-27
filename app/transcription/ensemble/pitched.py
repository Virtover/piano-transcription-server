"""dettato's pitched pipeline (transcriber, evidence repairer, grouper) in memory that does not grow with the song.

The package keeps the transcriber's hidden and stem states for the whole song (three float16 tensors of T x 88 x 368:
about 10 MB per second of audio, 36 GB for an hour). Every later step reads them only near a note: the repairer at a
note and over it, the grouper within 1 s of its onset. So the one transcriber pass (a window every 4 s: then the repairer
and the grouper read the same average, as in dettato.cpp) fills a buffer that slides along the song. A segment is
finished as soon as its frames and a context after it are final: its notes decoded, their evidence gathered, the
repairer run on its 8 s windows, the grouper's features gathered at the repaired notes. Then the buffer moves on. Only
per-note data outlives a segment. The grouper's network and decode then run over the whole song's notes.

Every frame is added in the order and precision Transcriber.run adds it, and every time and index is computed from song
frame numbers (frames.py), so a segment's results are the package's at the same window step. The difference: a note
that lasts past its segment's context is cut at the context's end (counted in stats["notes_cut"]).

Memory: the buffer, BUFFER_FRAMES x 88 x 368 x 2 taps in float16 (about 0.5 GB), on the model's device, plus one
batch of windows' activations; on the host, per note about 1.2 KB.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

import numpy as np
import torch

from app.transcription.dettato.evidence.model import L_WIN
from app.transcription.dettato.grouper import retune
from app.transcription.dettato.grouper.core import _Song, assign_slots, decode as group_decode
from app.transcription.dettato.pipeline import MIN_END_S
from app.transcription.dettato.transcriber.core import HEADS, _autocast
from app.transcription.dettato.transcriber.frontend import N_KEYS, SR
from app.transcription.ensemble import frames
from app.transcription import stream_audio

HOP_FRAC = 0.5            # a window every 4 s (the package: every 2 s; dettato.cpp: every 4 s, the same scores)
BATCH = 2                 # windows per network call: larger batches are not faster (measured on an RTX 3060 Ti)
SEGMENT_S = 32.0          # a whole number of the repairer's 8 s windows
CONTEXT_S = 16.0          # final frames after a segment, for the ends of its notes
TAPS = ("hidden", "stem")


def buffer_frames(fps: int, window_frames: int, step_frames: int) -> int:
    left = int(L_WIN * fps)
    return left + int(SEGMENT_S * fps) + int(CONTEXT_S * fps) + window_frames + (BATCH + 1) * step_frames


def buffer_bytes(channels: int = 368) -> int:
    """The buffer's size: frames x keys x channels x taps x float16, plus the maps in float32."""
    fps, window, step = 50, 400, 200
    n = buffer_frames(fps, window, step)
    return n * N_KEYS * (channels * len(TAPS) * 2 + len(HEADS) * 4) + n * 4


class Cancelled(Exception):
    pass


class _Rolling:
    """Hann-weighted sums of the six maps and the taps over song frames [base, base + cap), normalised in place once
    final, exactly as Transcriber.run normalises its whole-song sums."""

    def __init__(self, cap, C, device):
        self.cap, self.base, self.normed = cap, 0, 0
        self.P = {k: torch.zeros(cap, N_KEYS, device=device) for k in HEADS}
        self.buf = {k: torch.zeros(cap, N_KEYS, C, device=device, dtype=torch.float16) for k in TAPS}
        self.wsum = torch.zeros(cap, device=device)

    def add(self, a, b, g0, k, wt, pr, tp):
        a_, b_ = a - self.base, b - self.base
        if a_ < 0 or b_ > self.cap or a < self.normed:
            raise RuntimeError(f"window frames {a}-{b} outside the buffer {self.base}+{self.cap} (normalised to {self.normed})")
        for hd in HEADS:
            self.P[hd][a_:b_] += pr[hd][k, a - g0:b - g0] * wt[:, None]
        for name, buf in self.buf.items():
            buf[a_:b_] += (tp[name][k, :, a - g0:b - g0, :N_KEYS].permute(1, 2, 0).float() * wt[:, None, None]).half()
        self.wsum[a_:b_] += wt

    def normalise(self, upto):
        """Divide the final frames [normed, upto) by their weight sums, once."""
        if upto <= self.normed:
            return
        s = slice(self.normed - self.base, upto - self.base)
        ws = self.wsum[s].clamp(min=1e-6)
        for hd in HEADS:
            self.P[hd][s] /= ws[:, None]
        for buf in self.buf.values():
            buf[s] /= ws[:, None, None].half()
        self.normed = upto

    def view(self, lo, hi):
        """Normalised frames [lo, hi): maps as numpy, taps as device tensors (views, no copy)."""
        assert self.base <= lo and hi <= self.normed
        s = slice(lo - self.base, hi - self.base)
        return {hd: self.P[hd][s].cpu().numpy() for hd in HEADS}, {k: b[s] for k, b in self.buf.items()}

    def advance(self, new_base):
        d = new_base - self.base
        if d <= 0:
            return
        for t in (*self.P.values(), *self.buf.values(), self.wsum):
            if d < self.cap:
                t[:self.cap - d] = t[d:].clone()
                t[self.cap - d:] = 0
            else:
                t.zero_()
        self.base = new_base


def _sync(device):
    if device.type == "cuda":
        torch.cuda.synchronize(device)


@torch.no_grad()
def _windows(tr, y, hop_frac, batch, taps):
    """Transcriber.windows over y, an array or a file read through np.memmap: the same windows, each cut from y with
    the same zero padding, without the padded copy of the whole song the original makes."""
    n = len(y)
    W, Fw, T, starts, pad, g_pad = tr._layout(n, hop_frac)
    for bi in range(0, len(starts), batch):
        ss = starts[bi:bi + batch]
        xs = np.zeros((len(ss), W), np.float32)
        for k, s0 in enumerate(ss):
            a = s0 - pad
            lo, hi = max(0, a), min(n, a + W)
            if hi > lo:
                xs[k, lo - a:hi - a] = y[lo:hi]
        x = torch.from_numpy(xs).to(tr.device)
        box = {}
        with _autocast(tr.device):
            res = tr.model(x, box)
        yield ss, {k: torch.sigmoid(res[k].float()) for k in HEADS}, {k: box[k] for k in taps}


class _FileSong(_Song):
    """The grouper's song inputs with the audio read from y (an array or a file) a window at a time, quantised to
    16 bits on the way exactly as _Song quantises the whole song."""

    def __init__(self, y, meta, feat, fsc):
        self.y, self.meta, self.feat, self.fsc = y, meta, feat, fsc

    def audio_slice(self, t_a, L):
        x = np.zeros(L, np.float32)
        s0 = int(round(t_a * SR))
        lo, hi = max(s0, 0), min(s0 + L, len(self.y))
        if hi > lo:
            q = np.clip(np.round(np.asarray(self.y[lo:hi], np.float32) * 32768.0), -32768, 32767).astype(np.int16)
            x[lo - s0:hi - s0] = np.asarray(q, np.float32) / 32768.0
        return x


@torch.no_grad()
def transcribe(models, x: np.ndarray, progress: Callable[[float], None] | None = None,
               cancelled: Callable[[], bool] | None = None, stats: dict | None = None,
               work_dir: Path | None = None) -> dict:
    """The transcriber pass over 16 kHz mono x (an array, or a float32 file read through np.memmap), with each segment
    repaired and its grouper features gathered. -> the song's state for group(): retuned audio, per-note grouper
    inputs, repairer counts, tuning. progress gets 0..1 over the pass. Raises Cancelled once cancelled() returns True
    (checked every batch of windows). With work_dir, the retuned audio is a file there (retuned.f32), not in memory."""
    tr, rp = models.transcriber, models.repairer
    dev = tr.device
    stats = stats if stats is not None else {}
    t_start = time.perf_counter()
    delta, strength, shift, r = retune.ratio(x)
    if shift:
        # as retune.resample, in blocks
        y = stream_audio.resample(stream_audio.ArrayReader(x, SR), SR / r,
                                  Path(work_dir) / "retuned.f32" if work_dir is not None else None)
    else:
        y = x
    stats["tuning_s"] = time.perf_counter() - t_start
    n = len(y)
    W, Fw, T, starts, pad, g_pad = tr._layout(n, HOP_FRAC)
    hop = tr.hop
    fps = int(round(1 / hop))
    seg_f, ctx_f, left_f = int(SEGMENT_S * fps), int(CONTEXT_S * fps), int(L_WIN * fps)
    step = int(round(W * HOP_FRAC))
    step_f = int(round(step / SR * fps))
    roll = _Rolling(buffer_frames(fps, Fw, step_f), tr.channels, dev)
    w = torch.from_numpy(np.hanning(Fw + 2)[1:-1].astype(np.float32)).to(dev)
    metas, feats, fscs = [], [], []
    rinfo = dict(transcribed=0, kept=0, inserted=0, dropped=0)
    seg = 0
    cut = 0
    t_seg = 0.0

    def finish(final):
        """Finish segment seg if its frames and context are final (< final). -> whether it did."""
        nonlocal seg, cut, t_seg
        c0 = seg * seg_f
        if c0 >= T:
            return False
        c1, e = min(T, c0 + seg_f), min(T, c0 + seg_f + ctx_f)
        if final < e:
            return False
        t0 = time.perf_counter()
        lo = max(0, c0 - left_f)
        roll.normalise(e)
        maps, taps = roll.view(lo, e)
        res, ev = frames.evidence(tr, maps, taps["hidden"], lo, T)
        on4 = np.round(res["onset"], 4)
        core = (on4 >= c0 * hop - 1e-9) & ((on4 < c1 * hop - 1e-9) if c1 < T else True)
        if e < T:
            cut += int((res["offset"][core] >= (e - 1) * hop).sum())
        rep, _, info = rp._run({k: v[core] for k, v in res.items()}, ev[core])
        for k in rinfo:
            rinfo[k] += info[k]
        notes = np.stack([rep["onset"], np.maximum(rep["offset"], rep["onset"] + MIN_END_S), rep["pitch"].astype(np.float64)], 1)
        own = np.stack([res["onset"], res["offset"], res["pitch"].astype(np.float64), res["slot"] - 1.0], 1).reshape(-1, 4)
        P = {k: torch.from_numpy(v).to(dev) for k, v in maps.items()}
        meta, feat, fsc = frames.gather(P, taps["hidden"], taps["stem"], assign_slots(notes, own), hop, lo, T)
        metas.append(meta)
        feats.append(feat)
        fscs.append(fsc)
        del maps, taps, P
        roll.advance(max(0, c1 - left_f))
        t_seg += time.perf_counter() - t0
        seg += 1
        return True

    t_pass = time.perf_counter()
    done = 0
    for ss, pr, tp in _windows(tr, y, HOP_FRAC, BATCH, TAPS):
        for k, s0 in enumerate(ss):
            g0 = tr.frame_offset(s0, g_pad)
            a, b = max(0, g0), min(T, g0 + Fw)
            if b > a:
                roll.add(a, b, g0, k, w[a - g0:b - g0], pr, tp)
        del pr, tp
        # frames before the next window's first frame are final
        final = T if ss[-1] >= starts[-1] else max(0, tr.frame_offset(ss[-1] + step, g_pad))
        while finish(final):
            pass
        done += len(ss)
        if progress:
            progress(done / len(starts))
        if cancelled and cancelled():
            raise Cancelled()
    while finish(T):
        pass
    _sync(dev)
    stats.update(pass_s=time.perf_counter() - t_pass - t_seg, segments_s=t_seg, segments=seg, notes_cut=cut,
                 windows=len(starts))
    del roll
    return dict(y=y, r=r, tuning=(delta, strength, shift, r), rinfo=rinfo, seconds=len(x) / SR,
                meta=np.concatenate(metas) if metas else np.zeros((0, 12), np.float32),
                feat=np.concatenate(feats) if feats else np.zeros((0, 3, tr.channels), np.int8),
                fsc=np.concatenate(fscs) if fscs else np.zeros((0, 3), np.float16))


@torch.no_grad()
def predict(models, state: dict):
    """The grouper network over the whole song's notes (GPU). -> its predictions, or None without notes."""
    if not len(state["meta"]):
        return None
    song = _FileSong(state["y"], state["meta"], state["feat"], state["fsc"])
    return models.grouper.predict(song)


def group(models, state: dict, pred) -> tuple[list[dict], dict]:
    """The grouper's decode (CPU only, numpy and scipy) -> (tracks in the original time axis, info). As Grouper.group."""
    gr = models.grouper
    meta = state["meta"]
    delta, strength, shift, r = state["tuning"]
    on, off, pitch = meta[:, 0].astype(np.float64), meta[:, 1].astype(np.float64), meta[:, 2].round().astype(np.int64)
    conf = np.where(meta[:, 3] > 0.5, meta[:, 5], meta[:, 4])
    groups = group_decode(pred, (on, off, pitch.astype(np.float64)), gr.th, **gr.params) if len(on) else []
    tracks = gr._tracks(on * r, off * r, pitch, conf, groups)
    info = dict(tuning_cents=round(float(delta), 2), notes_in=len(on), notes_out=int(sum(len(t["notes"]) for t in tracks)),
                **state["rinfo"])
    return tracks, info
