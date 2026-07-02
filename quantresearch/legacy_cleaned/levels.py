"""
levels.py - structure/chart-based trade levels for the 'legacy_cleaned' engine.
================================================================================
ADDITIVE, STANDALONE. This module is for legacy_cleaned ONLY. It does NOT reuse,
import from, or modify scoring_v1/levels.py or legacy's levels (v1's clock is
running; legacy is the benchmark — both must stay byte-untouched).

Unlike the ATR-band approach in scoring_v1 (flat 2*ATR stop, uniform R-multiple
targets), this module derives levels from CHART STRUCTURE for 0-15 day momentum
swings, using only the STORED daily_bars DataFrame (broker-free, zero network):

  * STOP-LOSS  = recent SWING-LOW (lowest low over ~swing_lookback bars) minus a
                 buffer of buffer_atr * ATR  (NOT a flat multiple off entry).
  * TARGETS    = the next RESISTANCE levels ABOVE entry — swing-high pivots, the
                 prior consolidation top, and/or nearest round numbers above —
                 strictly increasing. Round numbers backfill if too few pivots.
  * R:R        = (first_target - entry) / (entry - stop), computed from the ACTUAL
                 structure, so it VARIES per stock (never a uniform 1:2).

Input df contract (same shape as engine price_data[symbol] / daily_bars load):
    columns : open, high, low, close, volume, delivery_pct   (lowercase)
    index   : DatetimeIndex, ASCENDING by date
Short history / NaNs are handled gracefully — always returns a best-effort dict,
never raises. Pure pandas/numpy.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ─── Wilder ATR (self-contained; do NOT import from scoring_v1) ───────────────

def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Wilder's Average True Range as a Series aligned to df.index.

    TR = max(high-low, |high-prev_close|, |low-prev_close|); ATR = Wilder EMA of
    TR (alpha = 1/n via ewm). Returns NaN-padded series; never raises on short df.
    """
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    close = pd.to_numeric(df["close"], errors="coerce")
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low).abs(),
         (high - prev_close).abs(),
         (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    # Wilder smoothing == EMA with alpha = 1/n (adjust=False), min_periods=1 so we
    # still get a best-effort value on short history.
    return tr.ewm(alpha=1.0 / max(int(n), 1), adjust=False, min_periods=1).mean()


# ─── pivot detection (inline; small +/-window local extrema) ─────────────────

def _swing_highs(high: np.ndarray, window: int = 3) -> list[int]:
    """Indices of local maxima: high[i] >= all highs within +/-window (strict vs
    the closest neighbour so flats don't all qualify). Edge bars are skipped."""
    n = len(high)
    out: list[int] = []
    for i in range(window, n - window):
        seg = high[i - window: i + window + 1]
        c = high[i]
        if not np.isfinite(c):
            continue
        if c >= np.nanmax(seg) and c > high[i - 1]:
            out.append(i)
    return out


def _swing_lows(low: np.ndarray, window: int = 3) -> list[int]:
    """Indices of local minima: low[i] <= all lows within +/-window."""
    n = len(low)
    out: list[int] = []
    for i in range(window, n - window):
        seg = low[i - window: i + window + 1]
        c = low[i]
        if not np.isfinite(c):
            continue
        if c <= np.nanmin(seg) and c < low[i - 1]:
            out.append(i)
    return out


def _round_levels_above(price: float, count: int = 3) -> list[float]:
    """Nearest 'round' numbers strictly above price, spaced by a price-scaled step
    (~1% of price rounded to a clean increment). Fallback when structural
    resistance is scarce so we can always offer >=2 ascending targets."""
    if not (np.isfinite(price) and price > 0):
        return []
    # choose a clean step by magnitude: sub-100 -> 5, sub-500 -> 10, sub-2000 -> 25,
    # else 50/100. This gives believable round-number resistances at any price scale.
    if price < 50:
        step = 2.5
    elif price < 100:
        step = 5.0
    elif price < 500:
        step = 10.0
    elif price < 2000:
        step = 25.0
    elif price < 5000:
        step = 50.0
    else:
        step = 100.0
    first = float(np.ceil((price + 1e-9) / step) * step)
    levels = [round(first + k * step, 2) for k in range(count)]
    return [float(x) for x in levels if x > price]


def _finite(x) -> bool:
    try:
        return bool(np.isfinite(x))
    except Exception:
        return False


def compute_levels(
    df: pd.DataFrame,
    atr_period: int = 14,
    swing_lookback: int = 10,
    buffer_atr: float = 0.5,
    over_ext_sma: int = 20,
    over_ext_mult: float = 1.08,
) -> dict:
    """Structure-based entry band / stop / targets / R:R for a 0-15 day swing.

    Parameters mirror the module docstring. Reads ONLY the passed daily_bars df
    (broker-free). Returns a best-effort dict on short history / NaNs; never raises.

    Returned keys:
        entry_low, entry_high : entry band (last close .. +1%, or a minor pullback
                                pivot at/below close if detected as entry_low).
        over_extended (bool)  : last close > over_ext_mult * SMA(close, over_ext_sma).
        stop_loss             : recent swing-low  -  buffer_atr * ATR.
        targets (list[float]) : 2-3 ascending resistance prices above entry_high.
        rr (float)            : reward/risk from entry_high, stop, first target.
        rr_str (str)          : "1 : X" form of rr.
        atr, swing_low, sma20 : observability.
        (plus target_price/target1..3 & risk_reward mirrors for caller convenience)
    """
    empty = {
        "entry_low": None, "entry_high": None, "over_extended": False,
        "stop_loss": None, "targets": [], "rr": None, "rr_str": None,
        "atr": None, "swing_low": None, "sma20": None,
        "target_price": None, "target1": None, "target2": None, "target3": None,
        "risk_reward": None,
    }
    if df is None or len(df) == 0:
        return empty

    try:
        close = pd.to_numeric(df["close"], errors="coerce")
        high = pd.to_numeric(df["high"], errors="coerce")
        low = pd.to_numeric(df["low"], errors="coerce")
    except Exception:
        return empty

    close_valid = close.dropna()
    if len(close_valid) == 0:
        return empty
    price = float(close_valid.iloc[-1])
    if not (_finite(price) and price > 0):
        return empty

    n = len(df)

    # ── ATR (best-effort; may be small on short history) ────────────────────
    try:
        atr_series = _atr(df, atr_period)
        atr = float(atr_series.iloc[-1])
    except Exception:
        atr = float("nan")
    if not (_finite(atr) and atr > 0):
        # degrade to a % proxy so stop buffer / round-target scaling still work
        atr = max(price * 0.01, 1e-6)

    # ── SMA + over-extension flag ────────────────────────────────────────────
    sma_win = max(int(over_ext_sma), 1)
    if len(close_valid) >= sma_win:
        sma20 = float(close_valid.rolling(sma_win).mean().iloc[-1])
    else:
        sma20 = float(close_valid.mean())  # best-effort on short history
    over_extended = bool(
        _finite(sma20) and sma20 > 0 and price > over_ext_mult * sma20
    )

    # ── numpy views for pivot scans ──────────────────────────────────────────
    high_arr = high.to_numpy(dtype=float)
    low_arr = low.to_numpy(dtype=float)

    # ── STOP: recent swing-low over ~swing_lookback bars, minus ATR buffer ───
    lb = min(max(int(swing_lookback), 1), n)
    recent_low_win = low.iloc[-lb:].dropna()
    if len(recent_low_win) > 0:
        swing_low = float(recent_low_win.min())
    else:
        swing_low = price  # no valid lows — degenerate; keep pipeline alive
    stop_loss = round(swing_low - buffer_atr * atr, 2)
    # guard: stop must be below entry ref; if structure put it at/above price
    # (e.g. brand-new low == last bar), fall back to a small ATR cushion.
    if stop_loss >= price:
        stop_loss = round(price - max(buffer_atr, 0.5) * atr, 2)

    # ── ENTRY band ───────────────────────────────────────────────────────────
    # entry_high default = last close +1%. If a MINOR pullback pivot (recent swing-
    # low at/below close within lookback) exists, use it as entry_low; else close.
    entry_high = round(price * 1.01, 2)
    entry_low = round(price, 2)
    lows_idx = _swing_lows(low_arr, window=3)
    # nearest recent swing-low that sits at/below current close = a minor pullback
    # support we'd happily enter near.
    for i in reversed(lows_idx):
        if i >= n - lb:  # only "recent" pivots
            piv = low_arr[i]
            if _finite(piv) and piv <= price:
                entry_low = round(float(piv), 2)
                break
    if entry_low > entry_high:  # safety: keep band ordered
        entry_low = round(price, 2)

    entry_ref = entry_high  # fills happen at top of band -> conservative risk/reward

    # ── TARGETS: next resistances ABOVE entry_high ───────────────────────────
    targets: list[float] = []

    # (1) swing-high pivots above entry
    highs_idx = _swing_highs(high_arr, window=3)
    pivot_highs = sorted(
        {round(float(high_arr[i]), 2) for i in highs_idx
         if _finite(high_arr[i]) and high_arr[i] > entry_high}
    )
    targets.extend(pivot_highs)

    # (2) prior consolidation top = the max high over a mid lookback window
    #     (excludes the most recent bars so it's a *prior* structure, not today).
    if n >= 8:
        window_hi = high.iloc[max(0, n - 60): max(1, n - 3)].dropna()
        if len(window_hi) > 0:
            cons_top = round(float(window_hi.max()), 2)
            if cons_top > entry_high:
                targets.append(cons_top)

    # (3) all-time / period high above entry (captures breakout target)
    hi_all = high.dropna()
    if len(hi_all) > 0:
        period_hi = round(float(hi_all.max()), 2)
        if period_hi > entry_high:
            targets.append(period_hi)

    # dedupe + strictly ascending
    targets = sorted(set(targets))

    # (4) backfill with round numbers if fewer than 2 structural levels
    if len(targets) < 2:
        for lvl in _round_levels_above(entry_high, count=3):
            if lvl > entry_high and lvl not in targets:
                targets.append(lvl)
        targets = sorted(set(targets))

    # keep 2-3, strictly increasing & above entry_high
    targets = [t for t in targets if t > entry_high]
    targets = targets[:3]
    # ensure strict monotonic (defensive; sorted+dedup already guarantees it)
    mono: list[float] = []
    for t in targets:
        t = float(t)
        if not mono or t > mono[-1]:
            mono.append(t)
    targets = mono

    # ── R:R from actual entry_ref, structure stop, first target ──────────────
    risk = entry_ref - stop_loss
    rr = None
    if targets and _finite(risk) and risk > 0:
        reward = targets[0] - entry_ref
        if _finite(reward) and reward > 0:
            rr = round(reward / risk, 2)
    rr_str = f"1 : {rr}" if rr is not None else None

    t1 = targets[0] if len(targets) >= 1 else None
    t2 = targets[1] if len(targets) >= 2 else None
    t3 = targets[2] if len(targets) >= 3 else None

    return {
        "entry_low": entry_low,
        "entry_high": entry_high,
        "over_extended": over_extended,
        "stop_loss": stop_loss,
        "targets": targets,
        "rr": rr,
        "rr_str": rr_str,
        "atr": round(atr, 2) if _finite(atr) else None,
        "swing_low": round(swing_low, 2) if _finite(swing_low) else None,
        "sma20": round(sma20, 2) if _finite(sma20) else None,
        # caller-convenience mirrors (do not conflict with the canonical keys above)
        "target_price": t1,
        "target1": t1, "target2": t2, "target3": t3,
        "risk_reward": rr,
    }


if __name__ == "__main__":  # tiny synthetic smoke test (no network, no DB)
    import numpy as _np

    _rng = _np.random.default_rng(7)
    _n = 80
    _dates = pd.date_range("2026-01-01", periods=_n, freq="B")
    # a noisy but clear uptrend with a mid consolidation + a recent pullback
    _base = _np.linspace(100, 160, _n)
    _wiggle = _np.sin(_np.linspace(0, 9, _n)) * 3 + _rng.normal(0, 1.2, _n)
    _closep = _base + _wiggle
    _closep[-1] = _closep[-2] * 0.995  # small pullback into last bar (entry-friendly)
    _high = _closep + _rng.uniform(0.5, 2.0, _n)
    _low = _closep - _rng.uniform(0.5, 2.0, _n)
    _openp = _closep - _rng.normal(0, 0.8, _n)
    _vol = _rng.integers(1_00_000, 5_00_000, _n)
    _df = pd.DataFrame(
        {"open": _openp, "high": _high, "low": _low, "close": _closep,
         "volume": _vol, "delivery_pct": _rng.uniform(30, 70, _n)},
        index=_dates,
    )

    _lv = compute_levels(_df)
    print("=== synthetic uptrend smoke ===")
    print(f"last close   : {float(_df['close'].iloc[-1]):.2f}")
    print(f"entry_low    : {_lv['entry_low']}")
    print(f"entry_high   : {_lv['entry_high']}")
    print(f"swing_low    : {_lv['swing_low']}")
    print(f"atr          : {_lv['atr']}")
    print(f"sma20        : {_lv['sma20']}")
    print(f"over_extended: {_lv['over_extended']}")
    print(f"stop_loss    : {_lv['stop_loss']}   (should be BELOW swing_low)")
    print(f"targets      : {_lv['targets']}   (should be ascending, all > entry_high)")
    print(f"rr           : {_lv['rr']}   ({_lv['rr_str']})   (should NOT be a flat 2.0)")

    # assertions to make the smoke self-checking
    assert _lv["stop_loss"] < _lv["swing_low"], "stop must sit below swing-low"
    assert _lv["targets"] == sorted(_lv["targets"]), "targets must be ascending"
    assert all(t > _lv["entry_high"] for t in _lv["targets"]), "targets above entry_high"
    assert len(_lv["targets"]) >= 2, "need at least 2 targets"
    assert _lv["rr"] is not None and _lv["rr"] > 0, "rr must be a positive float"
    print("smoke assertions: OK")

    # scenario 2: clean stair-step uptrend that pulled back to support, with a
    # clear overhead swing-high -> should give a HEALTHY, non-1:2 R:R.
    _n2 = 70
    _d2 = pd.date_range("2026-01-01", periods=_n2, freq="B")
    _c2 = _np.linspace(200, 240, _n2) + _np.sin(_np.linspace(0, 6, _n2)) * 6
    _c2[45:50] += 12          # an earlier spike -> leaves an overhead resistance
    _c2[-1] = _c2[-6] * 0.99  # pulled back to prior support for the entry
    _df2 = pd.DataFrame(
        {"open": _c2 - 0.5, "high": _c2 + _np.abs(_np.sin(_np.arange(_n2))) * 3 + 1,
         "low": _c2 - _np.abs(_np.cos(_np.arange(_n2))) * 3 - 1, "close": _c2,
         "volume": _np.full(_n2, 200000), "delivery_pct": _np.full(_n2, 55.0)},
        index=_d2,
    )
    _lv2 = compute_levels(_df2)
    print("\n=== scenario 2: pullback-to-support, overhead resistance ===")
    print(f"last close={float(_c2[-1]):.2f}  entry_high={_lv2['entry_high']}  "
          f"swing_low={_lv2['swing_low']}  stop={_lv2['stop_loss']}")
    print(f"targets={_lv2['targets']}  rr={_lv2['rr']} ({_lv2['rr_str']})")
    assert _lv2["stop_loss"] < _lv2["swing_low"]
    assert _lv2["rr"] is not None and _lv2["rr"] > 0
    assert abs(_lv2["rr"] - 2.0) > 1e-6, "R:R must be structure-driven, not a flat 2.0"

    # scenario 3: short history (5 bars) -> best-effort, must NOT raise.
    _d3 = pd.date_range("2026-01-01", periods=5, freq="B")
    _c3 = _np.array([100.0, 102.0, 101.0, 103.0, 104.0])
    _df3 = pd.DataFrame(
        {"open": _c3, "high": _c3 + 1, "low": _c3 - 1, "close": _c3,
         "volume": [1000] * 5, "delivery_pct": [50.0] * 5},
        index=_d3,
    )
    _lv3 = compute_levels(_df3)
    print("\n=== scenario 3: short history (5 bars, graceful) ===")
    print(f"entry_high={_lv3['entry_high']}  stop={_lv3['stop_loss']}  "
          f"targets={_lv3['targets']}  rr={_lv3['rr']}")
    assert _lv3["entry_high"] is not None, "short history must still return a band"
    print("\nALL SMOKE OK")
