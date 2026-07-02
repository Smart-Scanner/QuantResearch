"""
Support & Resistance Engine + Smart Entry/Exit Calculator
----------------------------------------------------------
- Uses scipy argrelextrema on price pivots to detect S/R zones
- Clusters nearby levels within 0.5% tolerance
- ATR-based entry zone, stop-loss, targets T1/T2/T3 with R:R
"""

import numpy as np
import logging
from scipy.signal import argrelextrema
from ta.volatility import AverageTrueRange

log = logging.getLogger("screener")


def _cluster_levels(levels: list, tol: float = 0.005) -> list:
    """Merge nearby price levels within tol% of each other."""
    if not levels:
        return []
    levels = sorted(levels)
    clustered = []
    group = [levels[0]]
    for lv in levels[1:]:
        if abs(lv - group[-1]) / group[-1] < tol:
            group.append(lv)
        else:
            clustered.append(round(sum(group) / len(group), 2))
            group = [lv]
    clustered.append(round(sum(group) / len(group), 2))
    return clustered


def get_support_resistance(df, n: int = 10) -> tuple:
    """
    Detect S/R zones using local extrema on High/Low series.
    Returns (supports: list, resistances: list) — up to 3 each.
    """
    try:
        highs = df["HIGH"].values if "HIGH" in df.columns else df["High"].values
        lows  = df["LOW"].values  if "LOW"  in df.columns else df["Low"].values
        close = df["CLOSE"].values if "CLOSE" in df.columns else df["Close"].values
        current = float(close[-1])

        order = max(2, min(n, len(highs) // 8))   # stable on both short and long series
        if order < 2:
            return [], []

        res_idx = argrelextrema(highs, np.greater, order=order)[0]
        sup_idx = argrelextrema(lows,  np.less,    order=order)[0]

        raw_res = [float(highs[i]) for i in res_idx[-12:]]
        raw_sup = [float(lows[i])  for i in sup_idx[-12:]]

        res_clustered = _cluster_levels(raw_res)
        sup_clustered = _cluster_levels(raw_sup)

        # Keep levels within a wider band for volatile names
        resistances = [r for r in sorted(res_clustered, reverse=True)
                       if r > current * 0.98 and r < current * 1.60][:3]
        supports    = [s for s in sorted(sup_clustered, reverse=True)
                       if s < current * 1.02 and s > current * 0.55][:3]

        return supports, resistances

    except Exception as exc:
        log.debug("S/R detection failed: %s", exc)
        return [], []


def calculate_trade_levels(df, supports: list, resistances: list, price: float) -> dict:
    """
    ATR-based smart entry zone, stop-loss, and targets.
    Returns {entry_low, entry_high, stop_loss, target1, target2, target3,
             pct_t1, pct_t2, pct_sl, atr, rr_ratio}
    """
    try:
        high  = df["HIGH"]  if "HIGH"  in df.columns else df["High"]
        low   = df["LOW"]   if "LOW"   in df.columns else df["Low"]
        close = df["CLOSE"] if "CLOSE" in df.columns else df["Close"]

        atr = float(AverageTrueRange(high, low, close, window=14).average_true_range().iloc[-1])

        # Nearest support below current price
        below_sups = [s for s in supports if s < price]
        nearest_sup = max(below_sups) if below_sups else price * 0.96

        entry_low  = round(nearest_sup * 1.002, 2)
        # ATR-aware entry zone (smaller of 0.8% of price or 0.5×ATR)
        entry_high = round(price + min(price * 0.008, atr * 0.5), 2)

        risk = max(price - stop_loss, price * 0.01)

        t1 = round(price + risk * 2.0, 2)    # R:R 2:1
        t2 = round(price + risk * 3.5, 2)    # R:R 3.5:1
        t3 = round(price + risk * 5.0, 2)    # R:R 5:1

        # Cap T1 at nearest resistance ABOVE price (not just >2%)
        above_res = [r for r in resistances if r > price]
        if above_res:
            t1 = min(t1, min(above_res))

        return {
            "entry_low":  entry_low,
            "entry_high": entry_high,
            "stop_loss":  stop_loss,
            "target1":    t1,
            "target2":    t2,
            "target3":    t3,
            "pct_sl":  round(((price - stop_loss) / price) * 100, 2),
            "pct_t1":  round(((t1 - price) / price) * 100, 2),
            "pct_t2":  round(((t2 - price) / price) * 100, 2),
            "pct_t3":  round(((t3 - price) / price) * 100, 2),
            "atr":     round(atr, 2),
            "rr_ratio": f"2:1 / 3.5:1 / 5:1",
        }

    except Exception as exc:
        log.debug("Trade levels failed: %s", exc)
        return {}
