"""Drum transcription: 44.1 kHz audio -> drum hits on GM drum keys (timpani on keys 41-53, the key being the drum's pitch).

    from dettato.drums import DrumTranscriber
    drums = DrumTranscriber.from_pretrained("cuda")      # or "cpu"
    hits = drums.transcribe(audio_44k)                   # [{"onset", "offset", "pitch", "is_drum", "velocity"}, ...]
    drums.last_info                                      # {"router": 0.03, "model": "drum-kit"}

Two models and a router:

    drum-kit     ``DrumNet``: pop, rock, jazz, electronic music with a drum kit
    orchestral   three ``OrchestralExpert`` models, averaged: timpani by pitch and concert percussion

The router (the experts' mean, one value per recording, computed from the whole recording) picks the orchestral model when it
is at least 0.5, else the drum-kit model. Hits are the local peaks of each key's onset probability at or above the threshold
at 50 frames per second, at least 3 frames apart per key; the orchestral model adds its timpani hits, leaving out one that
repeats a key already hit within 40 ms. Every hit is written 100 ms long.
"""
from __future__ import annotations

import numpy as np
import torch

from .. import hub, precision
from .features import SR, features
from .model import KEYS, TIMPANI_KEYS, DrumNet, OrchestralExpert

MIN_SOURCE_RATE = 40000      # the drum model reads the 7.8-20 kHz band: sources recorded below this rate are not transcribed
FPS = 50                     # output frame rate


def peaks(prob: np.ndarray, th: float, gap: int = 3) -> tuple[np.ndarray, np.ndarray]:
    """(T, L) onset probabilities -> (frames, lanes) of the local maxima >= th, at least ``gap`` frames apart per lane."""
    T, L = prob.shape
    left = np.vstack([prob[:1], prob[:-1]])
    right = np.vstack([prob[1:], prob[-1:]])
    cand = (prob >= th) & (prob >= left) & (prob >= right)
    last = np.full(L, -10, np.int64)
    fr, ln = [], []
    for t in np.nonzero(cand.any(1))[0]:
        m = cand[t] & (t - last > gap - 1)
        for lane in np.nonzero(m)[0]:
            fr.append(int(t))
            ln.append(int(lane))
        last = np.where(m, t, last)
    return np.array(fr, int), np.array(ln, int)


def _split(state: dict, prefix: str) -> dict:
    return {k[len(prefix):]: v for k, v in state.items() if k.startswith(prefix)}


class DrumTranscriber:
    """The drum stage. ``last_info`` holds the router value and the model that ran for the last ``transcribe`` call."""

    def __init__(self, config: dict, state: dict, device: str | torch.device = "cpu"):
        assert config.get("model_type") == "dettato-drums", config.get("model_type")
        assert config["keys"] == KEYS and config["timpani_keys"] == TIMPANI_KEYS and config["sample_rate"] == SR
        self.config = config
        self.device = torch.device(device)
        th = config["thresholds"]
        self.th_kit, self.th_drum, self.th_timpani, self.th_router = th["drum_kit"], th["orchestral_drum"], th["timpani"], th["router"]
        self.kit = DrumNet(config["drum_kit"])
        self.kit.load_state_dict(_split(state, "drum_kit."))
        self.kit.to(self.device).eval()
        self.experts = []
        for i in range(config["orchestral"]["n_models"]):
            ex = OrchestralExpert(config["drum_kit"], **config["orchestral"]["adapter"])
            ex.load_state_dict(_split(state, f"orchestral.{i}."))
            self.experts.append(ex.to(self.device).eval())
        self.last_info: dict = {}

    @classmethod
    def from_pretrained(cls, device: str | torch.device = "cpu") -> "DrumTranscriber":
        """The released weights (``kobimusic/dettato-drums``)."""
        return cls(hub.load_config("drums"), hub.load_state("drums"), device)

    def n_params(self) -> int:
        return sum(p.numel() for m in [self.kit, *self.experts] for p in m.parameters())

    def _autocast(self):
        return precision.autocast(self.device)

    @torch.no_grad()
    def probabilities(self, feats: np.ndarray) -> dict:
        """(T, 280) features -> router value, the drum-kit model's onset / velocity probabilities and the orchestral models'
        (averaged) drum / timpani / velocity probabilities, all at 50 fps."""
        T = feats.shape[0] // 2 * 2
        x = torch.from_numpy(np.ascontiguousarray(feats[:T], dtype=np.float32))[None].to(self.device)
        with self._autocast():
            zk, vk = self.kit(x[..., :160])
        outs = []
        for ex in self.experts:
            with self._autocast():
                o = ex(x)
            base = o["base"][0].float()
            # the corrected drum logits are formed as the base logits plus the correction, as during training
            drum = torch.sigmoid(base + (o["drum"][0].float() - base))
            outs.append((float(torch.sigmoid(o["router"][0].float())), drum.cpu().numpy(),
                         torch.sigmoid(o["timpani"][0].float()).cpu().numpy(), torch.sigmoid(o["velocity"][0].float()).cpu().numpy()))
        n = min(o[1].shape[0] for o in outs)
        return {"router": float(np.mean([o[0] for o in outs])),
                "kit": torch.sigmoid(zk[0].float()).cpu().numpy(), "kit_vel": torch.sigmoid(vk[0].float()).cpu().numpy(),
                "drum": np.mean([o[1][:n] for o in outs], 0), "timpani": np.mean([o[2][:n] for o in outs], 0),
                "vel": np.mean([o[3][:n] for o in outs], 0)}

    def decode(self, P: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray, str]:
        """-> (onset seconds, key, velocity 0-1, which model: "drum-kit" / "orchestral")"""
        if P["router"] < self.th_router:
            fr, ln = peaks(P["kit"], self.th_kit)
            return fr / FPS, np.array([KEYS[lane] for lane in ln], np.int64), P["kit_vel"][fr, ln], "drum-kit"
        fd, ld = peaks(P["drum"], self.th_drum)
        on, ks, vs = list(fd / FPS), [KEYS[lane] for lane in ld], list(P["vel"][fd, ld])
        seen: dict[int, list] = {}
        for o, k in zip(on, ks):
            seen.setdefault(k, []).append(o)
        ft, lt = peaks(P["timpani"], self.th_timpani)
        for f, lane in zip(ft, lt):
            o, k = f / FPS, TIMPANI_KEYS[int(lane)]
            if any(abs(o - q) <= 0.04 for q in seen.get(k, [])):
                continue
            on.append(o)
            ks.append(k)
            vs.append(float(np.clip(P["timpani"][f, lane], 0.3, 1.0)))
        order = np.argsort(on, kind="stable")
        return np.array(on)[order], np.array(ks, np.int64)[order], np.array(vs)[order], "orchestral"

    def transcribe(self, audio: np.ndarray, sr: int = SR) -> list[dict]:
        """Mono audio (from a source recorded at >= 40 kHz) -> drum hits sorted by onset. Audio at another rate than 44.1 kHz is
        resampled first."""
        x = np.asarray(audio, np.float32)
        if sr != SR:
            import soxr

            x = soxr.resample(x, sr, SR, quality="HQ").astype(np.float32)
        if len(x) < SR // 10:
            self.last_info = {"router": None, "model": None}
            return []
        P = self.probabilities(features(x).astype(np.float32))
        on, keys, vel, which = self.decode(P)
        self.last_info = {"router": P["router"], "model": which}
        return [{"onset": round(float(a), 4), "offset": round(float(a) + 0.1, 4), "pitch": int(k), "is_drum": True,
                 "velocity": round(float(np.clip(v, 0.05, 1.0)), 3)} for a, k, v in zip(on, keys, vel)]
