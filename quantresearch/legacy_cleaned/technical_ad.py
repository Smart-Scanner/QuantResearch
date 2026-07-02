"""
legacy_cleaned — A+D TECHNICAL BLOCK (daily timeframe only; daily_bars is all we have).

Its OWN momentum sub-features — DELIBERATELY DIFFERENT from v1's compute_symbol_features
so the two engines don't re-create the 0.986 overlap. v1's technical leans on:
absolute 63-day momentum (m_rs_rank), 21+63d blend, EMA-stack, ADX-14, EMA50 slope,
5d/50d rvol. So this block instead uses:

  A) 12-1 momentum        : 12-month return EXCLUDING the last month (classic long
                            horizon) — a different horizon from v1's ~63d.
  D) relative-strength    : the stock's RS-line (close / benchmark) — 12-1 momentum
                            OF the RS-line, its 50d-MA slope, its 252d-high proximity,
                            and % of days above its MA (RS trend persistence).
  + a short 21-day absolute momentum kicker (recent thrust).

EXCLUDED on purpose: ADX and volume/delivery confirmation — v1 already leans on both
(ADX-14 in trend; rvol in momentum; delivery/OBV/CMF/volflow in smart-money). Using
them here would rebuild the overlap. RS-persistence is the trend confirmation instead.

All features: higher = better. Missing/short-history -> np.nan (engine treats absent).
Zero network; pure pandas/numpy on the passed-in daily df + benchmark series.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Sub-weights WITHIN the technical factor (sum 100). Momentum-forward, RS-led.
AD_SUBWEIGHTS = {
    "t_ad_rs_mom_12_1": 30,   # 12-1 momentum of the RS-line (relative, long horizon) — the core
    "t_ad_mom_12_1":    20,   # 12-1 ABSOLUTE momentum (long horizon; != v1's 63d)
    "t_ad_rs_52w_prox": 18,   # RS-line proximity to its 252d high (relative breakout)
    "t_ad_rs_slope":    14,   # slope of the RS-line 50d-MA (relative trend direction)
    "t_ad_mom_21":      10,   # 21d absolute momentum kicker (recent thrust)
    "t_ad_rs_persist":   8,   # % of last 50d the RS-line held above its MA (trend confirmation)
}


def compute_ad_technical(df, bench=None):
    """A+D momentum sub-features for one symbol (daily). Returns dict of t_ad_* (higher=better)."""
    c = df["close"]
    n = len(c)
    f = {}

    # A) 12-1 ABSOLUTE momentum: return from ~12 months ago to ~1 month ago (skip last month).
    f["t_ad_mom_12_1"] = (float(c.iloc[-22]) / float(c.iloc[-252]) - 1.0) if n > 252 else np.nan
    # short 21d kicker (recent thrust)
    f["t_ad_mom_21"] = (float(c.iloc[-1]) / float(c.iloc[-22]) - 1.0) if n > 22 else np.nan

    # D) RELATIVE-STRENGTH line vs benchmark (Nifty 500).
    rs = None
    if bench is not None:
        b = bench.reindex(df.index).ffill()
        rs = (c / b).replace([np.inf, -np.inf], np.nan).dropna()

    if rs is not None and len(rs) > 252:
        f["t_ad_rs_mom_12_1"] = float(rs.iloc[-22]) / float(rs.iloc[-252]) - 1.0
        rs_ma = rs.rolling(50).mean()
        ma_now = rs_ma.iloc[-1]
        f["t_ad_rs_slope"] = (float((ma_now - rs_ma.iloc[-22]) / ma_now)
                              if (len(rs) > 22 and ma_now and not np.isnan(ma_now)) else np.nan)
        w = min(252, len(rs))
        hi = float(rs.iloc[-w:].max())
        f["t_ad_rs_52w_prox"] = (float(rs.iloc[-1]) / hi) if hi else np.nan
        f["t_ad_rs_persist"] = (float((rs.iloc[-50:] > rs_ma.iloc[-50:]).mean())
                                if len(rs) >= 50 else np.nan)
    else:
        for k in ("t_ad_rs_mom_12_1", "t_ad_rs_slope", "t_ad_rs_52w_prox", "t_ad_rs_persist"):
            f[k] = np.nan
    return f
