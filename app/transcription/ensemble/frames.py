"""The package's decode, evidence and grouper gather on a slice of the song, with its times in song frames.

A slice's arrays start at song frame f0. Every time is computed from song frame numbers exactly as the package computes it
on the whole song (u = t + refine with t the song frame, times = u * hop), and every index from those times exactly as the
package does (round(time / hop)), minus f0 only to read the slice. Note ends often sit on half frames, where computing
from slice-relative times would round some of them to the other frame.
"""
from __future__ import annotations

import numpy as np
import torch

from app.transcription.dettato.evidence.features import _assemble, gather_hidden, map_evidence
from app.transcription.dettato.grouper.core import PITCH_LO as G_PITCH_LO, SPAN_CAP, STEM_SPAN, _float_for
from app.transcription.dettato.transcriber.decode import _peaks, _refine
from app.transcription.dettato.transcriber.frontend import PITCH_LO


def decode(onset, frame, offset, hop, f0, T_song, on_th=0.25, off_th=0.3, fr_th=0.4, fr_low_frames=2, on_nms=3, min_dur=0.02,
           off_min_frames=2):
    """dettato.transcriber.decode.decode on a slice starting at song frame f0; the slice's last frame is the song's last
    frame only when f0 + len == T_song."""
    on, fr, off = onset, frame, offset
    T, L = on.shape
    at_end = f0 + T == T_song
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
            u0 = (t0 + f0) + _refine(on, t0, l)
            nxt = ons[i + 1] if i + 1 < len(ons) else T
            j = np.searchsorted(lows, t0 + 1)
            e_fr = lows[j] if j < len(lows) else T
            j = np.searchsorted(offs, t0 + off_min_frames)
            e_off = offs[j] if j < len(offs) else T
            if e_off < T and e_off <= min(e_fr, nxt):
                u1 = (e_off + f0) + _refine(off, e_off, l)
            elif e_fr <= nxt:
                u1 = (e_fr + f0) - 0.5
            else:
                u1 = (nxt + f0) + (_refine(on, nxt, l) if nxt < T else 0.0)
            on_s, off_s = u0 * hop, max(u1 * hop, u0 * hop + min_dur)
            notes.append((on_s, off_s, PITCH_LO + l))
    notes.sort()
    return notes


def notes(tr, maps, f0, T_song):
    """Transcriber.notes on a slice: times in song seconds."""
    th = tr.th
    args = dict(off_th=th["off_th"], fr_th=th["fr_th"], fr_low_frames=th["fr_low_frames"], on_nms=th.get("on_nms", 0))
    rows = []
    for slot, keys, on_th in ((1, ("onset", "frame", "offset"), th["on_th"]), (2, ("onset2", "frame2", "offset2"), th.get("v2_th"))):
        if on_th is None:
            continue
        on = maps[keys[0]]
        for u0, u1, p in decode(maps[keys[0]], maps[keys[1]], maps[keys[2]], tr.hop, f0, T_song, on_th=on_th, **args):
            t = int(min(max(round(u0 / tr.hop), 0), T_song - 1)) - f0
            t = min(max(t, 0), on.shape[0] - 1)
            rows.append((u0, u1, p, float(on[t, p - PITCH_LO]), slot))
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    a = np.array([r[:3] for r in rows], np.float64).reshape(-1, 3)
    return dict(onset=a[:, 0], offset=a[:, 1], pitch=a[:, 2].astype(np.int64), conf=np.array([r[3] for r in rows], np.float32),
                slot=np.array([r[4] for r in rows], np.int8))


def evidence(tr, maps, H, f0, T_song):
    """evidence_from_run on a slice -> (notes in song seconds, ev)."""
    hop = tr.hop
    res = notes(tr, maps, f0, T_song)
    L = (res["pitch"] - PITCH_LO).astype(np.int64)
    A = np.clip(np.round(res["onset"] / hop).astype(np.int64), 0, T_song - 1)
    B = np.clip(np.round(res["offset"] / hop).astype(np.int64), A + 1, T_song)
    n = maps["onset"].shape[0]
    Al, Bl = np.clip(A - f0, 0, n - 1), np.clip(B - f0, 1, n)
    Bl = np.maximum(Bl, Al + 1)
    S = res["slot"].astype(np.int64)
    mf = map_evidence(maps, L, Al, Bl, S)
    at, mean = gather_hidden(H, L, Al, Bl, tr.device)
    return res, _assemble(mf, at, mean)


@torch.no_grad()
def gather(P, H, S, notes, hop, f0, T_song):
    """dettato.grouper.core.gather on a slice: notes (N, 4) in song seconds; frame indices as the package computes them on
    the song, read from the slice."""
    dev = P["onset"].device
    T, L = P["onset"].shape
    N = len(notes)
    Cc = H.shape[2]
    if N == 0:
        return np.zeros((0, 12), np.float32), np.zeros((0, 3, Cc), np.int8), np.zeros((0, 3), np.float16)
    nt = torch.from_numpy(np.asarray(notes, _float_for(dev))).to(dev)
    lane = (nt[:, 2].round().long() - G_PITCH_LO).clamp(0, L - 1)
    t0 = (nt[:, 0] / hop).round().long().clamp(0, T_song - 1) - f0
    te = torch.maximum((nt[:, 1] / hop).ceil().long(), t0 + f0 + 1).clamp(max=T_song) - f0
    t0, te = t0.clamp(0, T - 1), te.clamp(1, T)

    def at(m, t):
        return m[t.clamp(0, T - 1), lane]

    def mx3(m, t):
        return torch.stack([at(m, t - 1), at(m, t), at(m, t + 1)]).amax(0)

    def span_mean(m):
        cs = torch.cat([torch.zeros(1, L, device=dev), m.cumsum(0)])
        return (cs[te, lane] - cs[t0, lane]) / (te - t0).clamp(min=1).float()

    meta = torch.stack([nt[:, 0].float(), nt[:, 1].float(), nt[:, 2].float(), nt[:, 3].float(),
                        mx3(P["onset"], t0), mx3(P["onset2"], t0), at(P["frame"], t0), at(P["frame2"], t0),
                        span_mean(P["frame"]), span_mean(P["frame2"]), mx3(P["offset"], te - 1), mx3(P["offset2"], te - 1)], 1)
    meta = meta.cpu().numpy().astype(np.float32)

    def rows(M, t):
        return M[t.to(M.device), lane.to(M.device)].to(dev)

    def span(M, cap):
        acc = torch.zeros(N, Cc, device=dev)
        cnt = torch.zeros(N, device=dev)
        lim = torch.minimum(te, t0 + cap)
        for k in range(cap):
            t = t0 + k
            m = (t < lim).float()
            acc += rows(M, t.clamp(max=T - 1)).float() * m[:, None]
            cnt += m
        return acc / cnt.clamp(min=1)[:, None]

    X = torch.stack([rows(H, t0).float(), span(H, SPAN_CAP), span(S, STEM_SPAN)], 1)
    sc = X.abs().amax(-1).clamp(min=1e-6) / 127.0
    feat = (X / sc[..., None]).round().clamp(-127, 127).to(torch.int8).cpu().numpy()
    return meta, feat, sc.cpu().numpy().astype(np.float16)
