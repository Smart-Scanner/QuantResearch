"""
test_features.py - tests for engine.compute_symbol_features on tiny deterministic
DataFrames. We assert ranges and ORIENTATION (sign/direction) of key features.

Engine contract: all scored sub-features are "higher = better". We feed a clean
steady uptrend and verify the bullish features saturate high and the risk
features land in their expected sign/range. We do NOT modify the engine.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quantresearch.scoring_v1 import engine as E


def _uptrend_df(n=300, drift=0.0008, seed=42):
    """Deterministic gentle uptrend with full OHLCV + delivery_pct."""
    dates = pd.bdate_range("2023-01-01", periods=n)
    close = 100 * np.exp(np.cumsum(np.full(n, drift)))
    high = close * 1.01
    low = close * 0.99
    openp = (high + low) / 2.0
    vol = np.linspace(1e5, 2e5, n)
    deliv = np.full(n, 55.0)
    return pd.DataFrame(
        {"open": openp, "high": high, "low": low, "close": close,
         "volume": vol, "delivery_pct": deliv},
        index=dates,
    )


def _downtrend_df(n=300, drift=-0.0008):
    dates = pd.bdate_range("2023-01-01", periods=n)
    close = 100 * np.exp(np.cumsum(np.full(n, drift)))
    high = close * 1.01
    low = close * 0.99
    openp = (high + low) / 2.0
    vol = np.linspace(1e5, 2e5, n)
    return pd.DataFrame(
        {"open": openp, "high": high, "low": low, "close": close,
         "volume": vol, "delivery_pct": np.full(n, 30.0)},
        index=dates,
    )


@pytest.fixture(scope="module")
def up_features():
    return E.compute_symbol_features(_uptrend_df())


# ───────────────────────────── bounded ranges ───────────────────────────────
def test_m_52w_prox_in_unit_interval(up_features):
    v = up_features["m_52w_prox"]
    assert 0.0 <= v <= 1.0


def test_t_ema_stack_in_unit_interval_and_full_on_uptrend(up_features):
    v = up_features["t_ema_stack"]
    assert 0.0 <= v <= 1.0
    # clean uptrend -> price>ema20>ema50>ema100>ema200 -> full stack
    assert v == pytest.approx(1.0, abs=1e-9)


def test_t_persistence_in_unit_interval_and_high(up_features):
    v = up_features["t_persistence"]
    assert 0.0 <= v <= 1.0
    assert v == pytest.approx(1.0, abs=1e-9)


def test_v_atr_fit_in_unit_interval(up_features):
    v = up_features["v_atr_fit"]
    assert 0.0 <= v <= 1.0


def test_s_volflow_in_minus_one_to_one_and_max_on_uptrend(up_features):
    v = up_features["s_volflow"]
    assert -1.0 - 1e-9 <= v <= 1.0 + 1e-9
    # every recent day is an up day -> signed volume == total volume -> +1
    assert v == pytest.approx(1.0, abs=1e-9)


def test_t_adx_capped_at_50(up_features):
    # engine caps t_adx at 50
    assert up_features["t_adx"] <= 50.0 + 1e-9


# ───────────────────────────── orientation ──────────────────────────────────
def test_risk_features_sign(up_features):
    # v_gap_safety = -mean(|gap|) -> <= 0
    assert up_features["v_gap_safety"] <= 0.0
    # v_dd_stability = -max_drawdown -> <= 0 ; clean uptrend -> ~0
    assert up_features["v_dd_stability"] <= 1e-9


def test_delivery_passthrough(up_features):
    assert up_features["s_delivery"] == pytest.approx(55.0, abs=1e-9)


def test_history_days_recorded(up_features):
    assert up_features["_history_days"] == 300


def test_uptrend_momentum_positive(up_features):
    # 63-bar return on a rising series is positive
    assert up_features["_ret_63"] > 0.0


def test_downtrend_orientation_vs_uptrend():
    up = E.compute_symbol_features(_uptrend_df())
    dn = E.compute_symbol_features(_downtrend_df())
    # ema-stack collapses on a downtrend
    assert dn["t_ema_stack"] < up["t_ema_stack"]
    # signed volume flow turns negative on persistent down days
    assert dn["s_volflow"] < up["s_volflow"]
    # 63-bar return is negative on a falling series
    assert dn["_ret_63"] < 0.0
    # drawdown stability is more negative (worse) on a downtrend
    assert dn["v_dd_stability"] <= up["v_dd_stability"]


def test_missing_delivery_maps_to_nan():
    df = _uptrend_df().drop(columns=["delivery_pct"])
    f = E.compute_symbol_features(df)
    assert np.isnan(f["s_delivery"])


def test_earnings_none_yields_nan_block():
    f = E.compute_symbol_features(_uptrend_df(), earn=None)
    for k in ("e_growth", "e_accel", "e_margin", "e_surprise"):
        assert np.isnan(f[k])


def test_earnings_present_computes_growth_and_surprise():
    earn = {
        "rev_growth_yoy": 20.0, "pat_growth_yoy": 30.0,
        "pat_growth_yoy_prev": 10.0, "opm_latest": 18.0, "opm_yago": 15.0,
        "eps_actual": 12.0, "eps_consensus": 10.0, "days_since_result": 5,
    }
    f = E.compute_symbol_features(_uptrend_df(), earn=earn)
    assert f["e_growth"] == pytest.approx(0.5 * 20.0 + 0.5 * 30.0, abs=1e-9)
    assert f["e_accel"] == pytest.approx(30.0 - 10.0, abs=1e-9)
    assert f["e_margin"] == pytest.approx(18.0 - 15.0, abs=1e-9)
    assert f["e_surprise"] == pytest.approx((12.0 - 10.0) / abs(10.0), abs=1e-9)


def test_sector_rs_neutral_when_no_index():
    f = E.compute_symbol_features(_uptrend_df(), sec_idx=None, bench=None)
    assert np.isnan(f["r_rrg"])
    assert np.isnan(f["_sec_ret_21"])
