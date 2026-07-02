"""
test_indicators.py - unit tests for the LOCKED engine's low-level indicators.

Each indicator is exercised against a SMALL, fixed fixture whose expected output
was hand-computed from the formula in engine.py (see derivations inline). We do
NOT modify the engine; tests adapt to it. Tolerances are used for floating point.

Indicators covered: _ema, _atr (Wilder/EWM alpha=1/n), _adx (Wilder), _obv, _cmf,
_max_drawdown.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quantresearch.scoring_v1 import engine as E

TOL = 1e-6


# ─────────────────────────────── _ema ───────────────────────────────────────
def test_ema_span3_hand_checked():
    """EMA span=3 => alpha = 2/(span+1) = 0.5, adjust=False.

    series = [1,2,3,4,5]
      e0 = 1
      e1 = 0.5*2 + 0.5*1     = 1.5
      e2 = 0.5*3 + 0.5*1.5   = 2.25
      e3 = 0.5*4 + 0.5*2.25  = 3.125
      e4 = 0.5*5 + 0.5*3.125 = 4.0625
    """
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    out = E._ema(s, 3)
    expected = [1.0, 1.5, 2.25, 3.125, 4.0625]
    assert np.allclose(out.values, expected, atol=TOL)


def test_ema_constant_series_is_constant():
    """EMA of a flat series equals that constant everywhere."""
    s = pd.Series([7.0] * 6)
    out = E._ema(s, 4)
    assert np.allclose(out.values, 7.0, atol=TOL)


# ─────────────────────────────── _atr ───────────────────────────────────────
def test_atr_n3_hand_checked():
    """ATR n=3 uses EWM(alpha=1/3, adjust=False) of True Range.

    high =[10,11,12,11,13]
    low  =[ 8, 9,10, 9,11]
    close=[ 9,10,11,10,12]

    TR[0] = high-low = 2  (prev close is NaN -> only h-l contributes)
    TR[1] = max(11-9, |11-9|,  |9-9|)   = 2
    TR[2] = max(12-10,|12-10|, |10-10|) = 2
    TR[3] = max(11-9, |11-11|, |9-11|)  = 2
    TR[4] = max(13-11,|13-10|, |11-10|) = 3

    EWM alpha=1/3: a0..a3 = 2 (constant input); a4 = (1/3)*3 + (2/3)*2 = 2.3333...
    """
    df = pd.DataFrame({
        "high":  [10, 11, 12, 11, 13],
        "low":   [8, 9, 10, 9, 11],
        "close": [9, 10, 11, 10, 12],
    }, dtype=float)
    atr = E._atr(df, 3)
    expected = [2.0, 2.0, 2.0, 2.0, 7.0 / 3.0]
    assert np.allclose(atr.values, expected, atol=TOL)


def test_atr_is_positive():
    rng = np.random.default_rng(0)
    n = 30
    base = 100 + np.cumsum(rng.normal(0, 0.5, n))
    df = pd.DataFrame({"high": base + 1, "low": base - 1, "close": base})
    atr = E._atr(df, 14).dropna()
    assert (atr > 0).all()


# ─────────────────────────────── _adx ───────────────────────────────────────
def test_adx_pure_uptrend_is_maximal():
    """A monotonically rising bar series has only +DM (no -DM), so DX == 100 at
    every step once smoothing settles -> ADX == 100."""
    n = 40
    df = pd.DataFrame({
        "high":  np.arange(10, 10 + n) + 1.0,
        "low":   np.arange(10, 10 + n) - 1.0,
        "close": np.arange(10, 10 + n) * 1.0,
    })
    adx = E._adx(df, 14)
    assert float(adx.iloc[-1]) == pytest.approx(100.0, abs=1e-6)


def test_adx_trend_exceeds_chop():
    """Strong trend ADX should comfortably exceed a low-amplitude chop's ADX."""
    n = 60
    up = pd.DataFrame({
        "high":  np.arange(10, 10 + n) + 1.0,
        "low":   np.arange(10, 10 + n) - 1.0,
        "close": np.arange(10, 10 + n) * 1.0,
    })
    rng = np.random.default_rng(1)
    base = 100 + np.cumsum(rng.normal(0, 0.01, n))
    chop = pd.DataFrame({"high": base + 0.5, "low": base - 0.5, "close": base})
    assert float(E._adx(up, 14).iloc[-1]) > float(E._adx(chop, 14).iloc[-1])


# ─────────────────────────────── _obv ───────────────────────────────────────
def test_obv_hand_checked():
    """OBV = cumsum( sign(close.diff()) * volume ), first diff -> 0.

    close =[10,11,10,12,11]  -> sign diff = [0, +1, -1, +1, -1]
    volume=[100,200,150,300,250]
    contributions = [0, +200, -150, +300, -250]
    cumsum        = [0,  200,   50,  350,  100]
    """
    df = pd.DataFrame({
        "close":  [10, 11, 10, 12, 11],
        "volume": [100, 200, 150, 300, 250],
    }, dtype=float)
    obv = E._obv(df)
    assert np.allclose(obv.values, [0.0, 200.0, 50.0, 350.0, 100.0], atol=TOL)


def test_obv_all_up_is_cumulative_volume():
    df = pd.DataFrame({
        "close":  [10, 11, 12, 13],
        "volume": [100, 100, 100, 100],
    }, dtype=float)
    obv = E._obv(df)
    # first diff -> 0, then +100 each step
    assert np.allclose(obv.values, [0.0, 100.0, 200.0, 300.0], atol=TOL)


# ─────────────────────────────── _cmf ───────────────────────────────────────
def test_cmf_hand_checked_n2():
    """CMF n=2: rolling-2 sum of money-flow-volume / rolling-2 sum of volume.

    mfv = ((c-l) - (h-c)) / (h-l) * v

    high =[11,12,11,13,12]
    low  =[ 9, 9, 8,10, 9]
    close=[10,11,10,12,11]
    vol  =[100,200,150,300,250]

    multiplier ((c-l)-(h-c))/(h-l):
      i0: ((10-9)-(11-10))/(11-9)   = 0
      i1: ((11-9)-(12-11))/(12-9)   = 1/3
      i2: ((10-8)-(11-10))/(11-8)   = 1/3
      i3: ((12-10)-(13-12))/(13-10) = 1/3
      i4: ((11-9)-(12-11))/(12-9)   = 1/3
    mfv = mult*vol = [0, 66.667, 50, 100, 83.333]

    CMF[1] = (0+66.667)/(100+200)            = 0.22222
    CMF[2] = (66.667+50)/(200+150)           = 0.33333
    CMF[3] = (50+100)/(150+300)              = 0.33333
    CMF[4] = (100+83.333)/(300+250)          = 0.33333
    CMF[0] = NaN (window not full)
    """
    df = pd.DataFrame({
        "high":   [11, 12, 11, 13, 12],
        "low":    [9, 9, 8, 10, 9],
        "close":  [10, 11, 10, 12, 11],
        "volume": [100, 200, 150, 300, 250],
    }, dtype=float)
    cmf = E._cmf(df, 2)
    assert np.isnan(cmf.iloc[0])
    assert cmf.iloc[1] == pytest.approx(0.2222222, abs=1e-5)
    assert cmf.iloc[2] == pytest.approx(0.3333333, abs=1e-5)
    assert cmf.iloc[3] == pytest.approx(0.3333333, abs=1e-5)
    assert cmf.iloc[4] == pytest.approx(0.3333333, abs=1e-5)


def test_cmf_in_minus_one_to_one():
    rng = np.random.default_rng(2)
    n = 50
    base = 100 + np.cumsum(rng.normal(0, 0.5, n))
    df = pd.DataFrame({
        "high":   base + 1.0,
        "low":    base - 1.0,
        "close":  base + rng.uniform(-0.9, 0.9, n),
        "volume": rng.integers(1e4, 1e5, n).astype(float),
    })
    cmf = E._cmf(df, 20).dropna()
    assert (cmf >= -1.0 - 1e-9).all() and (cmf <= 1.0 + 1e-9).all()


# ───────────────────────────── _max_drawdown ────────────────────────────────
def test_max_drawdown_hand_checked():
    """MDD over last n bars = -min(close/cummax - 1), returned positive.

    close=[10,12,9,11,8] (n=5)
      cummax = [10,12,12,12,12]
      ratio-1= [0, 0, -0.25, -0.0833, -0.3333]
      min     = -0.3333  -> returned 0.3333
    """
    close = pd.Series([10, 12, 9, 11, 8], dtype=float)
    assert E._max_drawdown(close, 5) == pytest.approx(1.0 / 3.0, abs=1e-9)


def test_max_drawdown_monotonic_up_is_zero():
    close = pd.Series([1, 2, 3, 4, 5, 6], dtype=float)
    assert E._max_drawdown(close, 6) == pytest.approx(0.0, abs=1e-12)
