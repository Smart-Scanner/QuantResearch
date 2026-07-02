"""
test_parity.py - parity + determinism tests for the LOCKED engine.score_universe.

(a) PARITY: port the engine's __main__ synthetic universe (seed 7) and assert the
    engine runs, returns a non-empty ranked DataFrame with the expected columns,
    composite descending, and ranks 1..N unique.
(b) DETERMINISM: rebuild the same synthetic input and confirm identical
    composite_z and rank.

We never modify the engine; this test mirrors its own smoke harness verbatim.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quantresearch.scoring_v1 import engine as E

REQUIRED_COLS = {
    "score", "rank", "composite_z", "data_integrity",
    "signal_agreement", "drivers", "weaknesses",
}


def _build_synthetic(seed=7, n_syms=80, periods=300):
    """Verbatim port of engine.py __main__ synthetic universe (default seed 7)."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-01", periods=periods)

    def make_walk(drift):
        p = 100 * np.exp(np.cumsum(rng.normal(drift, 0.02, len(dates))))
        h = p * (1 + rng.uniform(0, 0.02, len(dates)))
        l = p * (1 - rng.uniform(0, 0.02, len(dates)))
        return pd.DataFrame(
            {"open": (h + l) / 2, "high": h, "low": l, "close": p,
             "volume": rng.integers(1e5, 1e6, len(dates)).astype(float),
             "delivery_pct": rng.uniform(20, 80, len(dates))},
            index=dates,
        )

    pdata = {f"S{i:02d}": make_walk(rng.normal(0.0003, 0.0004)) for i in range(n_syms)}
    bench = pd.Series(
        100 * np.exp(np.cumsum(rng.normal(0.0002, 0.01, len(dates)))), index=dates
    )
    sidx = {
        s: pd.Series(
            100 * np.exp(np.cumsum(rng.normal(0.0002, 0.012, len(dates)))), index=dates
        )
        for s in pdata
    }
    earn = {
        s: {
            "rev_growth_yoy": rng.normal(15, 20), "pat_growth_yoy": rng.normal(12, 25),
            "pat_growth_yoy_prev": rng.normal(8, 20), "opm_latest": rng.normal(18, 5),
            "opm_yago": rng.normal(16, 5), "eps_actual": rng.normal(10, 3),
            "eps_consensus": rng.normal(9, 3),
            "days_since_result": int(rng.integers(0, 120)),
        }
        for s in pdata
    }
    return pdata, bench, sidx, earn


# ─────────────────────────────── parity ─────────────────────────────────────
def test_score_universe_runs_and_has_expected_shape():
    pdata, bench, sidx, earn = _build_synthetic()
    out = E.score_universe(pdata, bench, sidx, earn, mode="tuned")

    assert isinstance(out, pd.DataFrame)
    assert not out.empty
    assert len(out) == len(pdata)  # all 80 have >= 126 bars
    assert REQUIRED_COLS.issubset(set(out.columns))


def test_composite_is_descending():
    pdata, bench, sidx, earn = _build_synthetic()
    out = E.score_universe(pdata, bench, sidx, earn, mode="tuned")
    vals = out["composite_z"].values
    assert (vals[:-1] >= vals[1:]).all()


def test_ranks_are_unique_one_to_n():
    pdata, bench, sidx, earn = _build_synthetic()
    out = E.score_universe(pdata, bench, sidx, earn, mode="tuned")
    n = len(out)
    assert sorted(out["rank"].tolist()) == list(range(1, n + 1))


def test_score_in_zero_to_hundred():
    pdata, bench, sidx, earn = _build_synthetic()
    out = E.score_universe(pdata, bench, sidx, earn, mode="tuned")
    assert (out["score"] >= 0.0).all() and (out["score"] <= 100.0).all()


def test_equal_mode_also_runs():
    pdata, bench, sidx, earn = _build_synthetic()
    out = E.score_universe(pdata, bench, sidx, earn, mode="equal")
    assert isinstance(out, pd.DataFrame)
    assert not out.empty
    assert REQUIRED_COLS.issubset(set(out.columns))


def test_eligibility_floor_drops_short_history():
    """Symbols with < MIN_HISTORY_DAYS bars are excluded by the engine floor."""
    pdata, bench, sidx, earn = _build_synthetic(n_syms=5)
    # truncate two symbols below the 126-bar floor
    short_keys = list(pdata)[:2]
    for k in short_keys:
        pdata[k] = pdata[k].iloc[: E.MIN_HISTORY_DAYS - 1]
    out = E.score_universe(pdata, bench, sidx, earn, mode="tuned")
    for k in short_keys:
        assert k not in out.index
    assert len(out) == 3


# ─────────────────────────────── determinism ────────────────────────────────
def test_determinism_composite_and_rank_identical():
    p1, b1, s1, e1 = _build_synthetic(seed=7)
    p2, b2, s2, e2 = _build_synthetic(seed=7)
    out1 = E.score_universe(p1, b1, s1, e1, mode="tuned")
    out2 = E.score_universe(p2, b2, s2, e2, mode="tuned")

    # align by index before comparing (both should already be identically ordered)
    out2 = out2.reindex(out1.index)
    assert np.allclose(out1["composite_z"].values, out2["composite_z"].values, atol=1e-12)
    assert (out1["rank"].values == out2["rank"].values).all()


def test_drivers_and_weaknesses_are_strings():
    pdata, bench, sidx, earn = _build_synthetic()
    out = E.score_universe(pdata, bench, sidx, earn, mode="tuned")
    assert out["drivers"].map(lambda x: isinstance(x, str)).all()
    assert out["weaknesses"].map(lambda x: isinstance(x, str)).all()
