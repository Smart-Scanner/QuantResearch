"""
levels.py - trade levels + display fields for scoring_v1 picks (broker-free).
============================================================================
The locked engine is a pure scorer/ranker — it produces NO price levels. This
module derives a real ENTRY band, STOP-LOSS and TARGETs from the same bhavcopy
point-in-time price df the engine scored, using ATR. The stop is 2*ATR, IDENTICAL
to exits.py (so entry-side risk and exit-side stop agree). The execution engine
fills on the entry band, so these must be sane.

Also computes the display-only fields the top_picks template renders (pct_1d/1w/1m,
ADX, weekly_trend, SMA20) so a scoring_v1 result dict has no empty columns
(MUST-FIX 3). SMA20 is exposed for the over-extension entry-quality filter.

All inputs are the engine's price_data[symbol] shape: lowercase OHLCV(+delivery_pct),
ascending DatetimeIndex. Pure functions; no DB, no look-ahead beyond the df given.
"""
from __future__ import annotations

import numpy as np

try:  # reuse the LOCKED engine's exact ATR/ADX (no re-implementation, no drift)
    from . import engine as _engine
except Exception:  # pragma: no cover
    import engine as _engine  # type: ignore

ATR_MULT_STOP = 2.0            # initial stop = entry - 2*ATR (matches exits.ATR_MULT default)
MAX_STOP_PCT = 0.08            # cap stop distance at 8% of price — 2*ATR on volatile
                               # small-caps yields ~11% stops + far targets; cap keeps the
                               # SL:target band realistic for a swing trade (R:R unchanged).
RR_TARGETS = (2.0, 3.0, 4.0)   # target1/2/3 at these R multiples of (entry-stop)
ENTRY_BAND_PCT = 0.01          # entry band = last close +/- 1%
OVEREXTENSION_MAX = 1.08       # skip entry if price > 8% above SMA20 (entry-quality filter)


def compute_levels(df) -> dict | None:
    """Entry band / stop / targets / RR from a PIT price df. None if not computable."""
    if df is None or len(df) < 20:
        return None
    close = df["close"]
    price = float(close.iloc[-1])
    try:
        atr = float(_engine._atr(df).iloc[-1])
    except Exception:
        return None
    if not (np.isfinite(price) and price > 0 and np.isfinite(atr) and atr > 0):
        return None
    # 2*ATR, but capped so volatile small-caps don't get ~11% stops / far targets.
    stop_dist = min(ATR_MULT_STOP * atr, MAX_STOP_PCT * price)
    stop = round(price - stop_dist, 2)
    risk = price - stop
    if risk <= 0:
        return None
    t1 = round(price + RR_TARGETS[0] * risk, 2)
    t2 = round(price + RR_TARGETS[1] * risk, 2)
    t3 = round(price + RR_TARGETS[2] * risk, 2)
    return {
        "price": round(price, 2),
        "entry_low": round(price * (1 - ENTRY_BAND_PCT), 2),
        "entry_high": round(price * (1 + ENTRY_BAND_PCT), 2),
        "stop_loss": stop,
        "target1": t1, "target2": t2, "target3": t3,
        "target_price": t1,
        "risk_reward": round((t1 - price) / risk, 2),
        "atr_pct": round(atr / price * 100, 2),
    }


def compute_display_fields(df) -> dict:
    """Display-only fields the top_picks template renders (MUST-FIX 3)."""
    close = df["close"]
    n = len(df)

    def _pct(k):
        return round((close.iloc[-1] / close.iloc[-1 - k] - 1) * 100, 2) if n > k else None

    try:
        adx = float(_engine._adx(df).iloc[-1]) if n > 20 else None
    except Exception:
        adx = None
    sma20 = float(close.rolling(20).mean().iloc[-1]) if n >= 20 else None
    sma50 = float(close.rolling(50).mean().iloc[-1]) if n >= 50 else None
    if sma20 and sma50:
        weekly_trend = "up" if sma20 > sma50 else ("down" if sma20 < sma50 else "flat")
    else:
        weekly_trend = "flat"
    return {
        "pct_1d": _pct(1), "pct_1w": _pct(5), "pct_1m": _pct(21),
        "adx": round(adx, 1) if adx is not None and np.isfinite(adx) else None,
        "sma20": round(sma20, 2) if sma20 else None,
        "weekly_trend": weekly_trend,
    }


def is_overextended(df) -> bool:
    """Entry-quality gate: True if last close is > OVEREXTENSION_MAX * SMA20.

    Score says WHAT to buy; this guards WHEN — skip names that have already run far
    above their 20-DMA. If SMA20 is unavailable, do NOT block (return False).
    """
    close = df["close"]
    if len(close) < 20:
        return False
    sma20 = float(close.rolling(20).mean().iloc[-1])
    price = float(close.iloc[-1])
    if not (np.isfinite(sma20) and sma20 > 0):
        return False
    return price > OVEREXTENSION_MAX * sma20
