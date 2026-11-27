"""Applying the repairer to a whole recording: 8 s windows of transcribed notes -> kept / moved / dropped notes + inserted notes."""
from __future__ import annotations

import numpy as np
import torch

from .. import hub
from .features import N_EV, evidence_from_run, transcribe_with_evidence
from .model import L_WIN, Repairer

MAX_WIN = 4096          # windows with more notes than this pass through unchanged
ONSET_TH = 0.25         # the transcriber's onset threshold: confidence [.25, 1] -> velocity 1..127
DEDUP_S = 0.03

NOTE_DT = np.dtype([("inst", "u1"), ("pitch", "u1"), ("vel", "u1"), ("on", "f4"), ("off", "f4")])


def conf_vel(conf):
    x = (np.asarray(conf, np.float64) - ONSET_TH) / (1.0 - ONSET_TH)
    return np.clip(np.round(1 + 126 * x), 1, 127).astype(np.uint8)


def input_notes(res):
    """Transcriber notes -> the repairer's note array sorted by (onset, pitch), and that sort order."""
    t = np.round(res["onset"], 4)
    off = np.round(res["offset"], 4)
    n = len(t)
    k = np.zeros(n, NOTE_DT)
    if not n:
        return k, np.zeros(0, np.int64)
    k["inst"] = 0
    k["pitch"] = np.asarray(res["pitch"], np.int64)
    k["vel"] = conf_vel(res["conf"])
    on = np.asarray(t, np.float64)
    k["on"], k["off"] = on, np.maximum(np.asarray(off, np.float64), on + 1e-3)
    o = np.lexsort((k["pitch"], k["inst"], k["on"]))
    return k[o], o


def _window_batch(k, ev, t0, device):
    n = len(k)
    b = dict(inst=k["inst"].astype(np.int64), pitch=k["pitch"].astype(np.int64), vel=k["vel"].astype(np.int64),
             on=(k["on"] - t0).astype(np.float32), dur=np.maximum(k["off"] - k["on"], 0).astype(np.float32))
    out = {f: torch.from_numpy(v[None]).to(device) for f, v in b.items()}
    out["mask"] = torch.ones(1, max(1, n), dtype=torch.bool, device=device)
    out["ev"] = torch.from_numpy(np.asarray(ev, np.float32)[None]).to(device)
    return out


def _dedup_idx(c, tol=DEDUP_S):
    """Same key within tol -> keep the higher score. Returns kept indices (sorted)."""
    if len(c) < 2:
        return np.arange(len(c))
    key = c["inst"].astype(np.int64) * 128 + c["pitch"]
    o = np.lexsort((c["on"], key))
    cs, ks = c[o], key[o]
    keep = np.ones(len(cs), bool)
    close = (ks[1:] == ks[:-1]) & (cs["on"][1:] - cs["on"][:-1] < tol)
    for j in np.nonzero(close)[0]:
        a, b = j, j + 1
        if not keep[a]:
            continue
        if cs["score"][a] >= cs["score"][b]:
            keep[b] = False
        else:
            keep[a] = False
    return np.sort(o[keep])


def _safe_dedup_idx(c, tol=DEDUP_S):
    """A kept note is never removed: an insertion is dropped when a kept note of the same pitch starts within tol, and the
    insertions among themselves go through _dedup_idx. (The transcriber's two voices can hold two notes on one key.)"""
    keep_m = c["kind"] == 0
    ins_i = np.nonzero(~keep_m)[0]
    if not len(ins_i):
        return np.arange(len(c))
    ok = np.ones(len(ins_i), bool)
    kp = {}
    for q in np.nonzero(keep_m)[0]:
        kp.setdefault(int(c["pitch"][q]), []).append(float(c["on"][q]))
    kp = {p: np.sort(v) for p, v in kp.items()}
    for j, q in enumerate(ins_i):
        v = kp.get(int(c["pitch"][q]))
        if v is not None:
            i = np.searchsorted(v, c["on"][q])
            ok[j] = not any(0 <= r < len(v) and abs(v[r] - c["on"][q]) < tol for r in (i - 1, i))
    ins_ok = ins_i[ok]
    ins_ok = ins_ok[_dedup_idx(c[ins_ok])]
    return np.sort(np.concatenate([np.nonzero(keep_m)[0], ins_ok]))


class EvidenceRepairer:
    """Edits the transcriber's notes using its own evidence: drops notes it judges wrong, corrects onsets and ends, inserts
    missing notes.

    >>> rep = EvidenceRepairer.from_pretrained("cuda")
    >>> notes = rep(transcriber, audio16k)       # dict: onset, offset, pitch, inserted (bool), score
    """

    def __init__(self, model: Repairer, keep_th=0.4, insert_th=0.6, device="cpu"):
        self.model = model.to(device).eval()
        self.device = torch.device(device)
        self.keep_th, self.insert_th = float(keep_th), float(insert_th)

    @classmethod
    def from_pretrained(cls, device="cpu"):
        cfg = hub.load_config("evidence")
        model = Repairer(**cfg["model"])
        model.load_state_dict(hub.load_state("evidence"))
        return cls(model, cfg["keep_th"], cfg["insert_th"], device)

    @torch.no_grad()
    def edits(self, k, ev):
        """Per input note: P(keep), repaired onset / end (NaN = its window passed through unchanged); insertion proposals
        [(onset, end, pitch, score)], all in seconds from the start of the recording."""
        n = len(k)
        pk, on_r, off_r = np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan)
        ins = []
        if not n:
            return pk, on_r, off_r, ins
        end, on = float(k["off"].max()), k["on"]
        for t0 in np.arange(0, end + L_WIN, L_WIN):
            lo, hi = int(np.searchsorted(on, t0)), int(np.searchsorted(on, t0 + L_WIN))
            if hi <= lo or hi - lo > MAX_WIN:
                continue
            c = self.model.candidates(_window_batch(k[lo:hi], ev[lo:hi], float(t0), self.device))[0]
            m = hi - lo
            kc = c[:m]
            ok = (kc["on"] >= 0) & (kc["on"] < L_WIN)      # a note whose corrected onset leaves the window is dropped
            pk[lo:hi] = np.where(ok, kc["score"], -1.0)
            on_r[lo:hi], off_r[lo:hi] = kc["on"] + t0, kc["off"] + t0
            for i in range(m, len(c)):
                x = c[i]
                if x["kind"] != 0 and 0 <= x["on"] < L_WIN:
                    ins.append((float(x["on"]) + t0, float(x["off"]) + t0, int(x["pitch"]), float(x["score"])))
        return pk, on_r, off_r, ins

    def apply(self, k, e):
        """Input notes + edits -> the repaired notes (dict of arrays sorted by onset, pitch)."""
        pk, on_r, off_r, ins = e
        has = ~np.isnan(pk)
        rows = []                                               # (on, off, pitch, score, kind)
        for j in range(len(k)):
            x = k[j]
            if not has[j]:
                rows.append((float(x["on"]), float(x["off"]), int(x["pitch"]), 1.0, 0))
            elif pk[j] >= self.keep_th:
                rows.append((on_r[j], off_r[j], int(x["pitch"]), pk[j], 0))
        rows += [(a, b, p, s, 1) for a, b, p, s in ins if s >= self.insert_th]
        c = np.zeros(len(rows), [("inst", "i2"), ("pitch", "i2"), ("on", "f8"), ("off", "f8"), ("score", "f4"), ("kind", "i1")])
        for i, r in enumerate(rows):
            c[i] = (0, r[2], r[0], r[1], r[3], r[4])
        if len(c):
            c = c[_safe_dedup_idx(c)]
        on = np.array([round(float(a), 4) for a in c["on"]], np.float64)
        off = np.array([round(max(float(b), float(a) + 0.01), 4) for a, b in zip(c["on"], c["off"])], np.float64)
        return dict(onset=on, offset=off, pitch=c["pitch"].astype(np.int64), inserted=c["kind"] == 1,
                    score=c["score"].astype(np.float32))

    def _run(self, res, ev):
        k, o = input_notes(res)
        e = self.edits(k, ev[o])
        rep = self.apply(k, e)
        info = dict(transcribed=len(k), kept=int((~rep["inserted"]).sum()), inserted=int(rep["inserted"].sum()))
        info["dropped"] = info["transcribed"] - info["kept"]
        return rep, res, info

    def __call__(self, transcriber, audio, batch=2):
        """audio: 16 kHz mono float32 -> (repaired notes, transcriber notes, info). Runs the transcriber itself."""
        res, _, ev = transcribe_with_evidence(transcriber, audio, batch=batch)
        return self._run(res, ev)

    def from_run(self, transcriber, maps, hidden):
        """The same from a transcriber pass already made: its maps and its hidden state (T, 88, 368 float16, on the device or in
        host memory), both averaged over every window (``Transcriber.run(audio, hop_frac=0.25, taps=("hidden",))``)."""
        res, ev = evidence_from_run(transcriber, maps, hidden)
        return self._run(res, ev)


__all__ = ["EvidenceRepairer", "N_EV"]
