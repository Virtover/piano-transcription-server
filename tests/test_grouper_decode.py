"""The grouper's final clustering (app/transcription/dettato/grouper/decode.py _agglo), changed from dettato's for long
recordings: the row-minimum cache must make exactly the merges of the original, ties included; above LARGE_GROUPS the
distances are computed as needed, in memory linear in the number of groups."""

import numpy as np
import pytest

pytest.importorskip("scipy")
pytest.importorskip("torch")      # the vendored package's __init__ imports it

from app.transcription.dettato.grouper import decode as D  # noqa: E402


def original_agglo(spaces, size, tau2, cap=600):
    """dettato 2.0.0's _agglo, as published."""
    spaces = [(m.astype(np.float64).copy(), w) for m, w in spaces if w > 0]
    size = size.astype(np.float64).copy()
    M = len(size)
    lab = np.arange(M)
    if M < 2:
        return lab
    Dm = sum(w * (1 - m @ m.T) for m, w in spaces)
    np.fill_diagonal(Dm, np.inf)
    alive = np.ones(M, bool)
    if M > cap:
        small = np.argsort(-size)[cap:]
        Dm[small] = np.inf
        Dm[:, small] = np.inf
    while True:
        k = int(np.argmin(Dm))
        i, j = divmod(k, M)
        if not np.isfinite(Dm[i, j]) or Dm[i, j] > tau2:
            break
        si, sj = size[i], size[j]
        for m, _ in spaces:
            m[i] = (si * m[i] + sj * m[j]) / (si + sj)
        size[i] = si + sj
        alive[j] = False
        lab[lab == j] = i
        Dm[j] = np.inf
        Dm[:, j] = np.inf
        d = sum(w * (1 - m @ m[i]) for m, w in spaces)
        d[~alive] = np.inf
        d[i] = np.inf
        Dm[i] = d
        Dm[:, i] = d
    return lab


def groups(rng, M, ties=False):
    ms = [rng.normal(size=(M, 6)), rng.normal(size=(M, 4))]
    if ties:
        ms = [np.repeat(m[: M // 3 + 1], 3, axis=0)[:M] for m in ms]
    ms = [m / np.linalg.norm(m, axis=1, keepdims=True) for m in ms]
    return [(ms[0], 1.0), (ms[1], 0.5)], rng.integers(1, 40, M).astype(float)


@pytest.mark.parametrize("seed", range(12))
def test_the_cached_clustering_makes_the_originals_merges(seed):
    rng = np.random.default_rng(seed)
    spaces, size = groups(rng, int(rng.integers(2, 400)), ties=seed % 3 == 0)
    tau, cap = float(rng.uniform(0.2, 1.4)), int(rng.integers(20, 500))
    assert np.array_equal(D._agglo(spaces, size, tau, cap), original_agglo(spaces, size, tau, cap))


def test_many_groups_take_the_large_path_and_give_a_valid_clustering(monkeypatch):
    rng = np.random.default_rng(5)
    spaces, size = groups(rng, 300)
    monkeypatch.setattr(D, "LARGE_GROUPS", 100)
    lab = D._agglo(spaces, size, 0.6, 80)
    expected = original_agglo(spaces, size, 0.6, 80)
    # A label is a surviving group's index; equal up to the last bits, so the number of groups is close.
    assert set(np.unique(lab)) <= set(range(300)) and all(lab[g] == g for g in np.unique(lab))
    assert abs(len(np.unique(lab)) - len(np.unique(expected))) <= max(2, len(np.unique(expected)) // 10)
