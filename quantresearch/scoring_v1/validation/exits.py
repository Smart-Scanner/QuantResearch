"""
exits.py - Point-in-time swing EXIT rules for the validation backtest.
======================================================================
Pure, 0-DB position-management logic. Given a held position and the price
series UP TO a given date, decide whether to exit and why. Used by the
walk-forward harness to realise outcomes of buys produced by the locked
scoring engine.

ROLLBACK-SAFETY: ADDITIVE. Imports nothing from the engine/adapter/live
screener and touches no DB. It only consumes a pandas price DataFrame
(the same shape pit_loader.load_price_df returns) plus a current rank.

POINT-IN-TIME MANDATE
---------------------
should_exit() is given `price_df_to_date` = the price history truncated to
the evaluation date (no rows after it). It NEVER peeks past the last row.
The caller advances the date one bar at a time and re-calls.

EXIT RULES (checked in priority order)
--------------------------------------
1. Initial hard stop : close <= entry_price - ATR_MULT * ATR14(at entry).
                       ATR_MULT configurable 1.5-2.0, default 2.0. The stop
                       level is anchored to the ATR as of the ENTRY bar so it
                       does not drift with later volatility (a fixed initial
                       risk line). The trailing logic below can only RAISE the
                       effective stop, never lower it.
2. Chandelier trail  : trail = highest_close_since_entry - CHANDELIER_MULT*ATR14
                       (ATR14 evaluated on the latest bar). Ratchets up only.
                       Exit if latest close <= max(initial_stop, trail).
3. EMA trail         : exit if latest close < EMA(EMA_TRAIL_SPAN) (default 22).
                       OR-combined with the chandelier breach (either trips it).
4. Time stop         : exit after MAX_HOLD_DAYS (=20) trading days held.
5. Momentum fade     : exit when current_rank falls out of the hold band
                       top-(TOP_N * HYSTERESIS_MULT) = top-50. Mirrors the
                       engine's hysteresis: a name held only while it stays in
                       the top-50; once it exits that band the thesis is stale.

Indicator math (ATR14 Wilder, EMA via ewm adjust=False) is kept IDENTICAL to
quantresearch.scoring_v1.engine so backtest exits agree with engine internals,
but the functions are re-implemented locally to preserve the 0-import contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


# ───────────────────────── tunable constants ─────────────────────────
ATR_PERIOD: int = 14                 # ATR lookback (Wilder)
ATR_MULT: float = 2.0                # initial hard-stop ATR multiple (1.5-2.0)
CHANDELIER_MULT: float = 3.0         # chandelier trail ATR multiple
EMA_TRAIL_SPAN: int = 22             # EMA trail span (close < EMA -> exit)
MAX_HOLD_DAYS: int = 20              # time stop (trading days)

# Engine hysteresis band (mirrors engine.TOP_N * engine.HYSTERESIS_MULT).
TOP_N: int = 25
HYSTERESIS_MULT: float = 2.0
HOLD_RANK_CUT: int = int(TOP_N * HYSTERESIS_MULT)   # = 50


# ───────────────────────── position record ─────────────────────────
@dataclass
class Position:
    """
    A held swing position. `entry_date` is a pandas Timestamp / date that
    exists in (or precedes) the price index; `entry_price` is the fill price;
    `entry_atr` is ATR14 as of the entry bar (anchors the initial stop). If
    `entry_atr` is None it is computed from price_df_to_date at the entry bar.
    `days_held` may be passed explicitly; otherwise it is inferred from the
    number of bars since entry_date.
    """
    symbol: str
    entry_date: object
    entry_price: float
    entry_atr: Optional[float] = None
    days_held: Optional[int] = None


# ───────────────────────── indicator helpers (engine-identical) ──────────────
def _atr(df, n: int = ATR_PERIOD):
    """Wilder ATR (matches engine._atr): TR ewm with alpha=1/n."""
    import pandas as pd  # local import keeps module import-light
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat(
        [(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False).mean()


def _ema(s, span: int):
    """EMA (matches engine._ema): ewm(span, adjust=False)."""
    return s.ewm(span=span, adjust=False).mean()


def _bars_since(df, entry_date) -> int:
    """Number of bars in df with index strictly AFTER entry_date (days held)."""
    import pandas as pd
    try:
        ed = pd.Timestamp(entry_date)
    except Exception:
        return len(df)
    return int((df.index > ed).sum())


def _slice_since_entry(df, entry_date):
    """Rows on/after the entry bar (inclusive), for highest-close tracking."""
    import pandas as pd
    try:
        ed = pd.Timestamp(entry_date)
    except Exception:
        return df
    sub = df[df.index >= ed]
    return sub if len(sub) else df


# ───────────────────────── main decision ─────────────────────────
def should_exit(
    position: Position,
    price_df_to_date,
    current_rank: Optional[int] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Decide whether to exit `position` as of the LAST bar of price_df_to_date.

    Parameters
    ----------
    position          : Position dataclass (entry price/date/atr).
    price_df_to_date  : pandas DataFrame, OHLCV, DatetimeIndex ASCENDING,
                        truncated to the evaluation date (no look-ahead).
    current_rank      : the symbol's rank from the latest scoring run as of
                        this date (1-based). If None, the momentum-fade rule
                        is skipped (e.g. symbol not scored that day).

    Returns
    -------
    (exit: bool, reason: str|None). reason is one of:
      'hard_stop', 'chandelier_trail', 'ema_trail', 'time_stop',
      'momentum_fade', or None when holding.

    Priority: hard_stop > chandelier_trail > ema_trail > time_stop >
    momentum_fade. (Price-protective stops dominate; rank fade is the softest.)
    """
    if price_df_to_date is None or len(price_df_to_date) == 0:
        return (False, None)

    df = price_df_to_date
    last_close = float(df["close"].iloc[-1])

    # --- ATR at entry (anchors the fixed initial stop) ---
    entry_atr = position.entry_atr
    if entry_atr is None:
        try:
            import pandas as pd
            ed = pd.Timestamp(position.entry_date)
            atr_series = _atr(df, ATR_PERIOD)
            at_entry = atr_series[atr_series.index <= ed]
            entry_atr = float(at_entry.iloc[-1]) if len(at_entry) else float(atr_series.iloc[-1])
        except Exception:
            entry_atr = None

    entry_price = float(position.entry_price)

    # --- latest ATR (drives the chandelier trail) ---
    try:
        latest_atr = float(_atr(df, ATR_PERIOD).iloc[-1])
    except Exception:
        latest_atr = entry_atr if entry_atr is not None else 0.0

    # ---------------- 1. initial hard stop ----------------
    initial_stop = None
    if entry_atr is not None and entry_atr > 0:
        initial_stop = entry_price - ATR_MULT * entry_atr
        if last_close <= initial_stop:
            return (True, "hard_stop")

    # ---------------- 2. chandelier trail (ratchet up) ----------------
    since = _slice_since_entry(df, position.entry_date)
    highest_close = float(since["close"].max()) if len(since) else last_close
    if latest_atr and latest_atr > 0:
        chandelier = highest_close - CHANDELIER_MULT * latest_atr
        # Trail can only RAISE the effective stop above the initial line.
        effective_trail = chandelier if initial_stop is None else max(initial_stop, chandelier)
        if last_close <= effective_trail and last_close <= highest_close:
            # Guard: only an exit if we have actually trailed up off entry,
            # i.e. the chandelier sits at/above the initial stop region.
            if initial_stop is None or chandelier >= initial_stop:
                return (True, "chandelier_trail")

    # ---------------- 3. EMA trail ----------------
    if len(df) >= 2:
        try:
            ema_val = float(_ema(df["close"], EMA_TRAIL_SPAN).iloc[-1])
            if last_close < ema_val:
                return (True, "ema_trail")
        except Exception:
            pass

    # ---------------- 4. time stop ----------------
    days_held = position.days_held
    if days_held is None:
        days_held = _bars_since(df, position.entry_date)
    if days_held >= MAX_HOLD_DAYS:
        return (True, "time_stop")

    # ---------------- 5. momentum fade (hysteresis band) ----------------
    if current_rank is not None and current_rank > HOLD_RANK_CUT:
        return (True, "momentum_fade")

    return (False, None)


# ───────────────────────── self-check ─────────────────────────
# 0-DB module: no bootstrap/require_pg needed here (no PostgreSQL access).
if __name__ == "__main__":
    import pandas as pd
    import numpy as np

    def _mk(closes, highs=None, lows=None, start="2026-01-01"):
        idx = pd.bdate_range(start, periods=len(closes))
        closes = np.asarray(closes, float)
        highs = closes * 1.01 if highs is None else np.asarray(highs, float)
        lows = closes * 0.99 if lows is None else np.asarray(lows, float)
        return pd.DataFrame(
            {"open": closes, "high": highs, "low": lows,
             "close": closes, "volume": 1e6, "delivery_pct": 50.0},
            index=idx,
        )

    print("=== exits.py self-check ===")

    # A) Steady uptrend, just entered (few bars held) -> HOLD.
    #    Use a SHORT series: the harness truncates price_df to the eval date,
    #    so a freshly-opened position only sees ~a handful of bars. Anchor the
    #    entry near the END so days_held stays under MAX_HOLD_DAYS and the
    #    rising close stays above its own EMA(22).
    up = _mk(np.linspace(100, 120, 30))
    pos = Position("TEST", up.index[-4], entry_price=float(up["close"].iloc[-4]),
                   entry_atr=2.0)
    ex, why = should_exit(pos, up, current_rank=5)
    print(f"A uptrend hold      -> exit={ex} reason={why}")
    assert ex is False, f"steady uptrend just-entered should hold (got {why})"

    # B) Sharp drop below ATR hard stop on bar 2 -> hard_stop.
    crash = _mk([100, 99, 98, 70], highs=[101, 100, 99, 71], lows=[99, 98, 70, 65])
    posc = Position("TEST", crash.index[0], entry_price=100.0, entry_atr=3.0)
    exc, whyc = should_exit(posc, crash, current_rank=1)
    print(f"B crash hard stop   -> exit={exc} reason={whyc}")
    assert exc and whyc == "hard_stop", "deep gap-down must hit hard stop"

    # C) Time stop: 25 bars held, gently rising (no stop/EMA breach).
    longhold = _mk(np.linspace(100, 130, 26))
    post = Position("TEST", longhold.index[0], entry_price=100.0,
                    entry_atr=2.0, days_held=25)
    ext, whyt = should_exit(post, longhold, current_rank=3)
    print(f"C time stop         -> exit={ext} reason={whyt}")
    assert ext and whyt == "time_stop", "25 bars held must time-stop"

    # D) Momentum fade: healthy price but rank slipped out of top-50.
    posm = Position("TEST", up.index[0], entry_price=100.0,
                    entry_atr=2.0, days_held=3)
    exm, whym = should_exit(posm, up, current_rank=75)
    print(f"D momentum fade     -> exit={exm} reason={whym}")
    assert exm and whym == "momentum_fade", "rank>50 must momentum-fade"

    print("self-check OK")
