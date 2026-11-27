"""The evidence vector the repairer reads for every transcribed note (769 numbers).

For a note on key l from frame a to frame b (20 ms frames):

* 33 map numbers from the transcriber's six probability maps: its own voice's onset probability at a-2..a+2, frame probability at
  8 points over the note and at b-1+{1,3,6,12}, the highest frame probability in the 5 frames before a, the highest offset probability
  over [a+1, b+12) with its relative position, the offset probability at b-1; the other voice's onset (max a-1..a+1), frame at a and
  mean frame over the note; a second-voice flag; the first voice's onset probability (max a-1..a+1) on keys l-1, l+1, l-12, l+12,
  l+19, l+24; the log spread of the two hidden-state vectors below.
* 2 x 368 hidden-state numbers: the transcriber's last hidden state (the input of its output heads) at (a, l) and its mean over
  [a, b) on l, overlap-averaged like the maps; each vector standardised over its 368 dims and quantised to int8 steps of 1/32.
"""
from __future__ import annotations

import numpy as np
import torch

from ..transcriber import N_KEYS, PITCH_LO, SR

C_H = 368
N_MAP = 33
N_EV = N_MAP + 2 * C_H          # 769
Q = 32.0
MAX_S_1PASS = 250.0             # longer recordings: the tap buffers go to host memory (dettato.pipeline), or, in the standalone
                                # transcribe_with_evidence below, the hidden state is gathered in a second pass (bounded memory)


def _map_feats(own, other, L, A, B):
    on, fr, off = own
    T_ = on.shape[0]
    c = lambda x: np.clip(x, 0, T_ - 1)  # noqa: E731
    span = np.maximum(B - A, 1)
    f = [on[c(A + d), L] for d in (-2, -1, 0, 1, 2)]
    f += [fr[c(A + (j * span) // 8), L] for j in range(8)]
    f += [fr[c(B - 1 + k), L] for k in (1, 3, 6, 12)]
    f.append(np.stack([np.where(A - k >= 0, fr[c(A - k), L], 0.0) for k in range(1, 6)]).max(0))
    om, opos = np.zeros(len(L), np.float32), np.zeros(len(L), np.float32)
    for i in range(len(L)):
        seg = off[A[i] + 1:min(B[i] + 12, T_), L[i]]
        if len(seg):
            j = int(seg.argmax())
            om[i], opos[i] = seg[j], min((j + 1) / span[i], 2.0)
    f += [om, opos, off[c(B - 1), L]]
    o_on, o_fr, _ = other
    f.append(np.stack([o_on[c(A + d), L] for d in (-1, 0, 1)]).max(0))
    f.append(o_fr[c(A), L])
    f.append(np.stack([o_fr[c(A + (j * span) // 4), L] for j in range(4)]).mean(0))
    return f


def map_evidence(maps, L, A, B, S):
    """-> (N, 31) float32: every map number (the two log-spread numbers are appended by the caller)."""
    out = np.zeros((len(L), N_MAP - 2), np.float32)
    s1 = (maps["onset"], maps["frame"], maps["offset"])
    s2 = (maps["onset2"], maps["frame2"], maps["offset2"])
    on1 = maps["onset"]
    T_ = on1.shape[0]
    for slot, own, other in ((1, s1, s2), (2, s2, s1)):
        m = S == slot
        if not m.any():
            continue
        f = _map_feats(own, other, L[m], A[m], B[m])
        f.append(np.full(int(m.sum()), float(slot == 2), np.float32))
        Lm, Am = L[m], A[m]
        for dl in (-1, 1, -12, 12, 19, 24):
            l2 = Lm + dl
            ok = (l2 >= 0) & (l2 < N_KEYS)
            v = np.stack([on1[np.clip(Am + d, 0, T_ - 1), np.clip(l2, 0, N_KEYS - 1)] for d in (-1, 0, 1)]).max(0)
            f.append(np.where(ok, v, 0.0))
        out[m] = np.stack(f, 1)
    return out


@torch.no_grad()
def gather_hidden(H, L, A, B, device, chunk=4):
    """H (T, 88, C), on the device or in host memory -> per note H[A, L] and mean(H[A:B, L]) (float32 numpy), a few lanes at a time."""
    N = len(L)
    at = np.zeros((N, C_H), np.float32)
    mean = np.zeros((N, C_H), np.float32)
    if not N:
        return at, mean
    lanes = np.unique(L)
    for i in range(0, len(lanes), chunk):
        ls = lanes[i:i + chunk]
        hl = H[:, torch.as_tensor(ls, device=H.device)].to(device).float()   # H may live in host memory
        cs = torch.cat([torch.zeros(1, len(ls), C_H, device=device), hl.cumsum(0)], 0)
        for u, l in enumerate(ls):
            sel = np.nonzero(L == l)[0]
            a_, b_ = torch.as_tensor(A[sel], device=device), torch.as_tensor(B[sel], device=device)
            at[sel] = hl[a_, u].cpu().numpy()
            mean[sel] = ((cs[b_, u] - cs[a_, u]) / (b_ - a_).clamp(min=1)[:, None].float()).cpu().numpy()
    return at, mean


@torch.no_grad()
def gather_hidden_2pass(transcriber, audio, L, A, B, batch=2, hop_frac=0.25):
    """The same two vectors without holding the whole song's hidden state: a second pass over the windows that adds each window's
    weighted share at the notes it touches."""
    device = transcriber.device
    hop = transcriber.hop
    W, Fw, Tsong, starts, pad, g_pad = transcriber._layout(len(audio), hop_frac)
    w = np.hanning(Fw + 2)[1:-1].astype(np.float32)
    wsum = np.zeros(Tsong, np.float32)
    for s0 in starts:
        g0 = transcriber.frame_offset(s0, g_pad)
        a, b = max(0, g0), min(Tsong, g0 + Fw)
        if b > a:
            wsum[a:b] += w[a - g0:b - g0]
    wsum = np.maximum(wsum, 1e-6)
    N = len(L)
    at = torch.zeros(N, C_H, device=device)
    mean = torch.zeros(N, C_H, device=device)
    if not N:
        return at.cpu().numpy(), mean.cpu().numpy()
    Lt, At, Bt = (torch.as_tensor(v, device=device) for v in (L, A, B))
    wsum_t = torch.as_tensor(wsum, device=device)
    for ss, _, taps in transcriber.windows(audio, hop_frac, batch, taps=("hidden",)):
        H = taps["hidden"][:, :, :, :N_KEYS].float()
        for k, s0 in enumerate(ss):
            g0 = transcriber.frame_offset(s0, g_pad)
            lo, hi = max(0, g0), min(Tsong, g0 + Fw)
            if hi <= lo:
                continue
            sel = torch.nonzero((Bt > lo) & (At < hi)).flatten()
            if not len(sel):
                continue
            ul, inv = torch.unique(Lt[sel], return_inverse=True)
            wt = torch.as_tensor(w[lo - g0:hi - g0], device=device) / wsum_t[lo:hi]
            hs = H[k][:, lo - g0:hi - g0, :][:, :, ul].permute(1, 2, 0) * wt[:, None, None]
            cs = torch.cat([torch.zeros(1, len(ul), C_H, device=device), hs.cumsum(0)], 0)
            a_, b_ = At[sel], Bt[sel]
            in_at = (a_ >= lo) & (a_ < hi)
            if in_at.any():
                at[sel[in_at]] += hs[(a_[in_at] - lo), inv[in_at]]
            s_lo, s_hi = (torch.clamp(a_, lo, hi) - lo), (torch.clamp(b_, lo, hi) - lo)
            mean[sel] += cs[s_hi, inv] - cs[s_lo, inv]
    mean /= (Bt - At).clamp(min=1)[:, None].float()
    return at.cpu().numpy(), mean.cpu().numpy()


def _std_q(x):
    mu, sd = x.mean(1, keepdims=True), x.std(1, keepdims=True) + 1e-6
    z = np.clip((x - mu) / sd, -3.96, 3.96)
    return np.round(z * Q).astype(np.int8), np.log(sd[:, 0])


def _decode_maps(transcriber, maps):
    """-> notes, their (key, onset frame, end frame, voice) indices and the 31 map numbers of each."""
    hop = transcriber.hop
    res = transcriber.notes(maps)
    T_ = maps["onset"].shape[0]
    L = (res["pitch"] - PITCH_LO).astype(np.int64)
    A = np.clip(np.round(res["onset"] / hop).astype(np.int64), 0, T_ - 1)
    B = np.clip(np.round(res["offset"] / hop).astype(np.int64), A + 1, T_)
    S = res["slot"].astype(np.int64)
    return res, L, A, B, map_evidence(maps, L, A, B, S)


def _assemble(mf, at, mean):
    qa, la = _std_q(at)
    qm, lmn = _std_q(mean)
    evf = np.concatenate([mf, la[:, None], lmn[:, None]], 1).astype(np.float16)
    evh = np.concatenate([qa, qm], 1)
    return np.concatenate([evf.astype(np.float32), evh.astype(np.float32) / Q], 1)


def evidence_from_run(transcriber, maps, H):
    """The transcriber's maps and hidden state H (T, 88, C float16, on the device or in host memory), both averaged over all
    windows -> (notes dict as Transcriber.notes, ev (N, 769) float32 row-aligned with the notes)."""
    res, L, A, B, mf = _decode_maps(transcriber, maps)
    at, mean = gather_hidden(H, L, A, B, transcriber.device)
    return res, _assemble(mf, at, mean)


def transcribe_with_evidence(transcriber, audio, batch=2):
    """Run the transcriber (its own pass) and compute every note's evidence.
    -> (notes dict as Transcriber.notes, maps, ev (N, 769) float32 row-aligned with the notes).
    ``Dettato.transcribe`` does not call this: it runs the transcriber once for the repairer and the grouper together and uses
    ``evidence_from_run``."""
    one_pass = len(audio) <= MAX_S_1PASS * SR
    maps, buf = transcriber.run(audio, hop_frac=0.25, batch=batch, taps=("hidden",) if one_pass else ())
    if one_pass:
        res, ev = evidence_from_run(transcriber, maps, buf.pop("hidden"))
        return res, maps, ev
    res, L, A, B, mf = _decode_maps(transcriber, maps)
    at, mean = gather_hidden_2pass(transcriber, audio, L, A, B, batch)
    return res, maps, _assemble(mf, at, mean)


__all__ = ["transcribe_with_evidence", "evidence_from_run", "map_evidence", "N_EV", "C_H"]
