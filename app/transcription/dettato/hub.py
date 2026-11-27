"""Model weights: from the copies the worker and RunPod images put in WEIGHTS, never from the network.

Changed from the original (which downloads from the Hugging Face Hub on first use): the images fetch each stage's repo at a
pinned revision and check every file's sha256 (build/dettato-weights.txt), so a running worker never fetches or loads an
unchecked file. Each stage has its own folder:

    transcriber  WEIGHTS/dettato
    evidence     WEIGHTS/dettato-evidence
    grouper      WEIGHTS/dettato-grouper
    drums        WEIGHTS/dettato-drums

DETTATO_WEIGHTS overrides WEIGHTS, for tests and development.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

WEIGHTS = Path("/opt/dettato")

REPOS = {
    "transcriber": "kobimusic/dettato",
    "evidence": "kobimusic/dettato-evidence",
    "grouper": "kobimusic/dettato-grouper",
    "drums": "kobimusic/dettato-drums",
}


def weights_dir() -> Path:
    return Path(os.environ.get("DETTATO_WEIGHTS") or WEIGHTS).expanduser()


def fetch(stage: str, filename: str, revision: str | None = None) -> str:
    """Local path of ``filename`` in the stage's folder."""
    path = weights_dir() / REPOS[stage].split("/", 1)[1] / filename
    if not path.is_file():
        raise FileNotFoundError(f"{path} is missing; the worker and RunPod images include it")
    return str(path)


def load_config(stage: str, filename: str = "config.json") -> dict:
    with open(fetch(stage, filename)) as fh:
        return json.load(fh)


def load_state(stage: str, filename: str = "model.safetensors") -> dict:
    # safetensors holds tensors only, never code.
    from safetensors.torch import load_file

    return load_file(fetch(stage, filename))
