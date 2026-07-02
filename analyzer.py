"""
Stock Analysis Engine — 25 Technical Indicators + 12 Intelligence Layers
=========================================================================
Layer 1:  20+ Technical Indicators (RSI, MACD, EMA, BB, Vol, ATR, Stoch, OBV,
          VWAP, ADX, CCI, Williams%R, MFI, Keltner, CMF, Ichimoku, Supertrend,
          EMA Deviation, BB Squeeze, Golden Cross, Momentum, Pivot S/R, Fib)
Layer 2:  Multi-Timeframe (1D/1W/1M) — via intelligence.mtf
Layer 3:  Support/Resistance + Trade Levels — via intelligence.support_resistance
Layer 4:  Fundamentals (P/E, ROE, EPS, D/E, Promoter) — via intelligence.fundamentals
Layer 5:  Indian Seasonal Intelligence — via intelligence.seasonal
Layer 6:  Order Book Proxy — via intelligence.order_book
Layer 7:  Sector Rotation (RRG) — via intelligence.sector_rotation
Layer 8:  GDELT + FinBERT News — via intelligence.news_gdelt_finbert
Layer 9:  News Sentiment Waterfall + Forex Factory — via intelligence.news_sentiment
Layer 10: World Markets — via intelligence.macro
Layer 11: FRED Macro — via intelligence.macro
Layer 12: Corporate Events — yfinance calendar
"""

import logging
import numpy as np
import pandas as pd
from collections import defaultdict

# ta library — primary
from ta.momentum import (RSIIndicator, StochasticOscillator, WilliamsRIndicator)
from ta.trend import (MACD, EMAIndicator, SMAIndicator, ADXIndicator, IchimokuIndicator,
                      CCIIndicator)
from ta.volatility import BollingerBands, AverageTrueRange, KeltnerChannel
from ta.volume import OnBalanceVolumeIndicator, MFIIndicator, ChaikinMoneyFlowIndicator

# jugaad_data — existing Phase 2 fallback
from jugaad_data.nse import stock_df
from datetime import date, timedelta

from stocks import SECTORS
from config import (
    DATA_LOOKBACK_DAYS, BENCHMARK_LOOKBACK_DAYS,
    MAX_RAW_SCORE, ATR_SL_MULTIPLIER,
    HC_MIN_SCORE, HC_MIN_SIGNALS_BULLISH, HC_RSI_RANGE,
    HC_DELIVERY_MIN, HC_ATR_RANGE, HC_RISK_MAX,
    HC_REQUIRE_MACD_BULLISH, HC_REQUIRE_VOLUME, HC_MIN_RISK_REWARD,
    BP_RSI_MAX, BP_VOLUME_MIN, BP_DELIVERY_MIN,
    BP_WEEK1_MAX_LOSS, BP_MACD_BULLISH, BP_TARGET_PCT,
)

# Intelligence package
from intelligence import run_all_layers
from intelligence.fundamentals import get_fundamentals_yf
from intelligence.yf_guard import yf_status as yf_guard_status

import time
import math
from metrics.timer import timed, _record as record_timing

log = logging.getLogger("screener")


# ── P0: NaN Source Tracing ──────────────────────────────────────────────
# Walks the result dict to find and log any remaining NaN/inf values
# with their full dotted path. This is the root-cause finder that tells
# us which analyzer component is producing bad data.

def trace_nan_sources(result: dict, symbol: str = "?", scan_id: str = "?"):
    """Walk result dict and log every NaN/inf field with full dotted path.

    Purpose: identify which analyzer component produces NaN so the
    source can be fixed. Runs after _safe() has already cleaned known
    spots, catching anything _safe() missed.
    """
    nan_fields = []

    def _walk(obj, path=""):
        if isinstance(obj, float):
            if math.isnan(obj) or math.isinf(obj):
                nan_fields.append(path or "root")
        elif hasattr(obj, "item"):
            try:
                native = obj.item()
                if isinstance(native, float) and (math.isnan(native) or math.isinf(native)):
                    nan_fields.append(path or "root")
            except Exception:
                pass
        elif isinstance(obj, dict):
            for k, v in obj.items():
                _walk(v, f"{path}.{k}" if path else k)
        elif isinstance(obj, (list, tuple)):
            for i, v in enumerate(obj):
                _walk(v, f"{path}[{i}]")

    _walk(result)

    if nan_fields:
        log.warning(
            "[NAN_TRACE] symbol=%s scan_id=%s nan_count=%d fields=%s",
            symbol, scan_id, len(nan_fields), nan_fields[:20]
        )
    return nan_fields


# ===================================================================
#  MARKET REGIME
# ===================================================================

def _calc_regime(df):
    df = df.sort_values("DATE").reset_index(drop=True)
    close = df["CLOSE"].astype(float)
    ret_1m = ((close.iloc[-1] / close.iloc[-22]) - 1) * 100 if len(close) >= 22 else 0
    ema20 = EMAIndicator(close, window=20).ema_indicator()
    ema50 = EMAIndicator(close, window=min(50, len(close) - 1)).ema_indicator()
    adx_val = ADXIndicator(
        df["HIGH"].astype(float), df["LOW"].astype(float), close, window=14
    ).adx().iloc[-1]
    curr, e20, e50 = close.iloc[-1], ema20.iloc[-1], ema50.iloc[-1]
    if curr > e20 > e50 and adx_val > 20:
        regime = "bullish"
    elif curr < e20 < e50 and adx_val > 20:
        regime = "bearish"
    else:
        regime = "sideways"
    if ret_1m <= -5:
        regime = "bearish"
    elif ret_1m <= -3 and regime == "sideways":
        regime = "bearish"
    elif ret_1m >= 5:
        regime = "bullish"
    return round(ret_1m, 2), regime


def get_nifty50_benchmark():
    # Flag-gated: source the Nifty 50 benchmark from the broker-free EOD store.
    # Default OFF → byte-identical to the Angel path below. Falls through on any miss.
    try:
        import bhavcopy_history
        if bhavcopy_history.USE_BHAVCOPY_HISTORY:
            idx_df = bhavcopy_history.get_index_history("Nifty 50", days=BENCHMARK_LOOKBACK_DAYS)
            if idx_df is not None and not idx_df.empty and len(idx_df) >= 30:
                nifty_1m, regime = _calc_regime(idx_df)
                try:
                    import db
                    db.set_meta("cached_nifty_1m", str(nifty_1m))
                    db.set_meta("cached_regime", regime)
                except Exception:
                    pass
                return nifty_1m, regime
    except Exception as exc:
        log.warning("Benchmark bhavcopy store failed: %s", exc)

    try:
        import live_feed
        df = live_feed.fetch_historical("NIFTYBEES", days=BENCHMARK_LOOKBACK_DAYS)
        if df is not None and not df.empty and len(df) >= 30:
            nifty_1m, regime = _calc_regime(df)
            # Cache for fallback during outages
            try:
                import db
                db.set_meta("cached_nifty_1m", str(nifty_1m))
                db.set_meta("cached_regime", regime)
            except Exception:
                pass
            return nifty_1m, regime
    except Exception as exc:
        log.warning("Benchmark Angel One failed: %s", exc)
    try:
        end_date = date.today()
        start_date = end_date - timedelta(days=BENCHMARK_LOOKBACK_DAYS)
        df = stock_df(symbol="NIFTYBEES", from_date=start_date, to_date=end_date)
        if not df.empty and len(df) >= 30:
            nifty_1m, regime = _calc_regime(df)
            try:
                import db
                db.set_meta("cached_nifty_1m", str(nifty_1m))
                db.set_meta("cached_regime", regime)
            except Exception:
                pass
            return nifty_1m, regime
    except Exception as exc:
        log.warning("Benchmark jugaad_data failed: %s", exc)

    # Fallback: use last cached benchmark (better than 'unknown')
    try:
        import db
        cached_1m = db.get_meta("cached_nifty_1m")
        cached_regime = db.get_meta("cached_regime")
        if cached_1m and cached_regime:
            log.info("Benchmark using cached values: %s%%, %s", cached_1m, cached_regime)
            return float(cached_1m), cached_regime
    except Exception:
        pass

    return 0, "unknown"


# ===================================================================
#  HELPER CALCULATIONS
# ===================================================================

def calc_fibonacci(high_val, low_val, current):
    diff = high_val - low_val
    if diff <= 0:
        return {"level": "N/A", "score": 0, "support": None, "resistance": None}
    levels = {
        "0.236": high_val - diff * 0.236,
        "0.382": high_val - diff * 0.382,
        "0.5":   high_val - diff * 0.5,
        "0.618": high_val - diff * 0.618,
        "0.786": high_val - diff * 0.786,
    }
    nearest_support = nearest_resistance = None
    for _name, level in levels.items():
        if level <= current and (nearest_support is None or level > nearest_support[1]):
            nearest_support = (_name, level)
        if level >= current and (nearest_resistance is None or level < nearest_resistance[1]):
            nearest_resistance = (_name, level)
    fib_ratio = (high_val - current) / diff
    score = 0
    level_name = f"{fib_ratio:.3f}"
    if 0.55 <= fib_ratio <= 0.68:
        score, level_name = 15, "Golden Zone (0.618)"
    elif 0.45 <= fib_ratio <= 0.55:
        score, level_name = 12, "50% Retracement"
    elif 0.35 <= fib_ratio <= 0.45:
        score, level_name = 10, "38.2% Zone"
    elif 0.70 <= fib_ratio <= 0.85:
        score, level_name = 8, "Deep Retracement (78.6%)"
    elif fib_ratio > 0.85:
        score, level_name = 5, "Near Bottom"
    return {
        "level": level_name, "ratio": round(fib_ratio, 3), "score": score,
        "support": round(nearest_support[1], 2) if nearest_support else None,
        "resistance": round(nearest_resistance[1], 2) if nearest_resistance else None,
    }


def calc_support_resistance(high, low, close):
    try:
        h, l, c = float(high.iloc[-1]), float(low.iloc[-1]), float(close.iloc[-1])
        pivot = (h + l + c) / 3
        s1, r1 = 2 * pivot - h, 2 * pivot - l
        s2, r2 = pivot - (h - l), pivot + (h - l)
        return {k: round(v, 2) for k, v in
                {"pivot": pivot, "s1": s1, "s2": s2, "r1": r1, "r2": r2}.items()}
    except (IndexError, ValueError):
        return {"pivot": 0, "s1": 0, "s2": 0, "r1": 0, "r2": 0}


def calc_risk_score(rsi, atr_pct, dist_high, vol_ratio, pct_1m, below_ema200, adx):
    risk = 15  # R1-P0-Fix1: was 40 — remove inflated baseline
    if rsi > 75: risk += 20
    elif rsi > 65: risk += 12
    elif rsi < 25: risk += 8
    if atr_pct > 5: risk += 15
    elif atr_pct > 3.5: risk += 8
    if dist_high > -3: risk += 15
    elif dist_high > -8: risk += 8  # R1-P0-Fix5: was 5 — steeper near-high penalty
    if vol_ratio < 0.5: risk += 10
    if pct_1m > 20: risk += 18
    elif pct_1m > 12: risk += 12  # R1-P0-Fix5: was 10
    elif pct_1m > 8: risk += 6   # R1-P0-Fix5: new tier for moderate extension
    if pct_1m < -20: risk += 12
    elif pct_1m < -12: risk += 6
    if below_ema200: risk += 10
    if adx < 15: risk += 8
    return min(100, max(0, risk))


def detect_breakout(close, high, volume, avg_vol, bb_upper, atr):
    if len(close) < 20: return False
    current = float(close.iloc[-1])
    high_20 = float(high.tail(20).max())
    recent_range = float(high.tail(10).max()) - float(close.tail(10).min())
    tight = recent_range < (2.5 * atr)
    near_high = current >= high_20 * 0.98
    vol_confirm = float(volume.iloc[-1]) > avg_vol * 1.3
    bb_confirm = current >= bb_upper * 0.99
    return tight and near_high and vol_confirm and bb_confirm


def detect_vp_divergence(close, obv):
    if len(close) < 20 or len(obv) < 20: return False
    price_chg = (float(close.iloc[-1]) / float(close.iloc[-10]) - 1) * 100
    obv_val = float(obv.iloc[-10])
    obv_chg = (float(obv.iloc[-1]) - obv_val) / abs(obv_val) * 100 if abs(obv_val) > 0 else 0
    return price_chg < 2 and obv_chg > 8


def get_weekly_trend(close):
    if len(close) < 25: return "flat"
    wc = [float(close.iloc[-1]), float(close.iloc[-5]), float(close.iloc[-10]),
          float(close.iloc[-15]), float(close.iloc[-20])]
    up_weeks = sum(1 for i in range(len(wc) - 1) if wc[i] > wc[i + 1])
    return "up" if up_weeks >= 3 else "down" if up_weeks <= 1 else "flat"


# ===================================================================
#  AI SUMMARY
# ===================================================================

def generate_ai_summary(results, regime):
    if not results:
        return "No strong picks found in current scan."
    top5 = results[:5]
    hc_count = sum(1 for r in results if r.get("high_conviction"))
    sectors = defaultdict(int)
    for r in top5:
        sectors[r["sector"]] += 1
    dominant = max(sectors, key=sectors.get) if sectors else "Mixed"
    avg_rsi = sum((r.get("rsi") or 0) for r in top5) / len(top5)
    avg_delivery = sum((r.get("delivery_pct") or 0) for r in top5) / len(top5)

    lines = []
    regime_text = {
        "bullish": "BULLISH — favour momentum & breakout plays",
        "bearish": "BEARISH — favour defensive, high-delivery, oversold bounces only",
        "sideways": "SIDEWAYS — favour range-bound reversals & Fibonacci entries",
    }.get(regime, "UNKNOWN — use caution")
    lines.append(f"Market Regime: {regime_text}")

    if regime == "bearish":
        lines.append("WARNING: Market in downtrend. Reduce position sizes.")

    lines.append(f"Top 5 Avg RSI: {avg_rsi:.0f}")
    lines.append(f"Dominant Sector: {dominant}")
    lines.append(f"High Conviction Picks: {hc_count} stocks passed all filters")

    for i, r in enumerate(top5[:3]):
        risk = r.get("risk_score", 50)
        risk_label = "Low Risk" if risk < 40 else "Medium Risk" if risk < 65 else "High Risk"
        hc_tag = " [HC]" if r.get("high_conviction") else ""
        grade = r.get("grade", "")
        lines.append(
            f"#{i+1} {r['symbol']}{hc_tag} {grade} ({r['score']}/100) — "
            f"{risk_label} | Target: +{r.get('target_pct', 10)}% | "
            f"RRG: {r.get('sector_rotation', {}).get('quadrant', 'N/A')}"
        )
    return "\n".join(lines)


# ===================================================================
#  SECTOR STRENGTH
# ===================================================================

def apply_sector_strength(results):
    sector_scores = defaultdict(list)
    for r in results:
        sector_scores[r["sector"]].append(r["pct_1m"])
    sector_avg = {sec: (sum(vals) / len(vals)) for sec, vals in sector_scores.items() if vals}
    sorted_sectors = sorted(sector_avg.items(), key=lambda x: x[1], reverse=True)
    top_sectors = set(s[0] for s in sorted_sectors[:5] if s[1] > -5)
    heatmap = [
        {"sector": sec, "avg_return": round(avg, 2),
         "count": len(sector_scores[sec]), "strong": sec in top_sectors}
        for sec, avg in sorted_sectors
    ]
    for r in results:
        if r["sector"] in top_sectors:
            r["score"] = min(100, r["score"] + 3)
            r["signals"].append(("Strong Sector", f"{r['sector']} outperforming", "bullish"))
            r["sector_strong"] = True
        else:
            r["sector_strong"] = False
    results.sort(key=lambda x: (-x.get("high_conviction", False), -x["score"]))
    return heatmap


_jugaad_delivery_ok = True
_jugaad_delivery_fails = 0


def reset_delivery_state():
    global _jugaad_delivery_ok, _jugaad_delivery_fails
    _jugaad_delivery_ok = True
    _jugaad_delivery_fails = 0


# ===================================================================
#  GRADE CLASSIFIER
# ===================================================================

def classify_grade(score: int) -> str:
    if score >= 90:   return "🔥🔥 SUPER PICK"
    elif score >= 70: return "🔥 Strong Pick"
    elif score >= 50: return "⚡ Moderate"
    else:             return "📊 Weak"


# ===================================================================
#  FUNDAMENTAL AVAILABILITY CONTRACT (Release 1)
# ===================================================================
# A stock is considered Fundamental-Data-Available when the minimum
# required fundamental dataset used by the scoring engine is present.
# Normalization eligibility uses this contract and does NOT depend
# on only PE/PB fields.
#
# Scoring-relevant fields: pe, pb, roe, roa, revenue_growth,
# earnings_growth, debt_to_equity (the 7 fields that drive fund_score).
# If ALL of these are None, the data is genuinely missing from provider.

_FUND_SCORING_FIELDS = ("pe", "pb", "roe", "roa", "revenue_growth",
                        "earnings_growth", "debt_to_equity")


def _is_fundamental_data_missing(fundamentals: dict) -> bool:
    """Return True if fundamental data is genuinely unavailable from provider.

    Normalization Contract: This function returns True ONLY when the data
    source failed to provide ANY of the scoring-relevant fields.
    A stock with valid but poor fundamentals (all fields present, scoring 0)
    must NOT be considered missing.
    """
    if not fundamentals:
        return True
    return all(fundamentals.get(f) is None for f in _FUND_SCORING_FIELDS)


# ===================================================================
#  CORE ANALYSIS — 25 INDICATORS + 12 INTELLIGENCE LAYERS
# ===================================================================

@timed("analyze_per_stock")
def fetch_and_analyze(symbol: str, nifty_1m: float = 0, regime: str = "unknown",
                      ext_df=None, query_marketaux: bool = False,
                      scan_mode: str = "fast") -> dict | None:
    """
    Full 12-layer analysis per stock.
    ext_df: pre-fetched OHLCV DataFrame from Angel One (skips jugaad fetch).
    scan_mode:
        'fast' (default): cache_only=True for all yfinance-backed layers.
                          Zero yfinance calls. query_marketaux forced False.
        'deep':           cache_only=False. yfinance allowed on cache miss.
                          Full MarketAux + MTF + Events fetched live.
    """
    cache_only = (scan_mode != "deep")
    if scan_mode == "fast":
        query_marketaux = False
    try:
        clean = symbol.replace(".NS", "")

        # ── DATA FETCH ─────────────────────────────────────────────
        _fetch_start = time.monotonic()  # Phase 0: measure provider latency
        _data_source = "UNKNOWN"
        _source_reason = "UNKNOWN"
        if ext_df is not None:
            df = ext_df
            _data_source = "ANGEL"
            _source_reason = "ANGEL_OK"
        else:
            end_date = date.today()
            start_date = end_date - timedelta(days=DATA_LOOKBACK_DAYS)
            df = stock_df(symbol=clean, from_date=start_date, to_date=end_date)
            _data_source = "JUGAAD"
            _source_reason = "ANGEL_UNAVAILABLE"
        _fetch_latency_ms = round((time.monotonic() - _fetch_start) * 1000)

        if df.empty or len(df) < 50:
            return None

        df = df.sort_values("DATE").reset_index(drop=True)
        close  = df["CLOSE"].astype(float)
        high   = df["HIGH"].astype(float)
        low    = df["LOW"].astype(float)
        volume = df["VOLUME"].astype(float)
        vwap   = df["VWAP"].astype(float) if "VWAP" in df.columns else close
        has_delivery = "DELIVERY %" in df.columns
        delivery_pct = None

        global _jugaad_delivery_ok, _jugaad_delivery_fails

        if has_delivery:
            delivery_pct = df["DELIVERY %"].astype(float)
        elif _jugaad_delivery_ok:
            try:
                ddf = stock_df(symbol=clean, from_date=date.today() - timedelta(days=15),
                               to_date=date.today())
                if not ddf.empty and "DELIVERY %" in ddf.columns:
                    ddf = ddf.sort_values("DATE").reset_index(drop=True)
                    dlv_vals = ddf["DELIVERY %"].astype(float).tolist()
                    pad_len = max(0, len(df) - len(dlv_vals))
                    delivery_pct = pd.Series([dlv_vals[0]] * pad_len + dlv_vals)
                    has_delivery = True
                    _jugaad_delivery_fails = 0
                else:
                    _jugaad_delivery_fails += 1
            except Exception:
                _jugaad_delivery_fails += 1
            if _jugaad_delivery_fails >= 3:
                _jugaad_delivery_ok = False
                log.warning("jugaad_data delivery blocked — disabling for this scan")

        if not has_delivery or delivery_pct is None:
            delivery_pct = pd.Series([35.0] * len(df))  # R1-P0-Fix4: was 50 — neutral-low, not "good"

        current_price = float(close.iloc[-1])
        sector = SECTORS.get(clean, "Other")

        # ── CHART DATA ─────────────────────────────────────────────
        chart_data = []
        for _, row in df.tail(30).iterrows():
            chart_data.append({
                "date": row["DATE"].strftime("%m/%d") if hasattr(row["DATE"], "strftime") else str(row["DATE"])[:5],
                "o": round(float(row.get("OPEN", row["CLOSE"])), 2),
                "h": round(float(row["HIGH"]), 2),
                "l": round(float(row["LOW"]), 2),
                "c": round(float(row["CLOSE"]), 2),
                "v": int(row.get("VOLUME", 0)),
            })

        # ════════════════════════════════════════════════════════════
        # LAYER 1 — 25 TECHNICAL INDICATORS
        # ════════════════════════════════════════════════════════════
        _start_tech = time.monotonic()
        score = 0
        smart_money_raw = 0  # R1-P1A: separate accumulator for smart money signals
        signals = []

        # ── RSI (14) ─────────────────────────────────────────────
        rsi = float(RSIIndicator(close, window=14).rsi().iloc[-1])
        # R1-P0-Fix2: RSI scoring correction — avoid rewarding falling knives
        if 28 <= rsi < 38:
            score += 22; signals.append(("RSI Oversold Bounce", f"RSI: {rsi:.1f}", "bullish"))
        elif 20 <= rsi < 28:
            score += 10; signals.append(("RSI Deep Oversold", f"RSI: {rsi:.1f} — caution", "neutral"))
        elif rsi < 20:
            score += 0; signals.append(("RSI Falling Knife ⚠️", f"RSI: {rsi:.1f} — no entry", "bearish"))
        elif 38 <= rsi < 45:
            score += 16; signals.append(("RSI Oversold Zone", f"RSI: {rsi:.1f}", "bullish"))
        elif rsi < 60:
            score += 8
        elif rsi > 78:
            score -= 10; signals.append(("RSI Overbought", f"RSI: {rsi:.1f}", "bearish"))
        elif rsi > 68:
            score -= 5; signals.append(("RSI Warm", f"RSI: {rsi:.1f}", "bearish"))

        # ── MACD ─────────────────────────────────────────────────
        macd_ind = MACD(close)
        macd_line = float(macd_ind.macd().iloc[-1])
        macd_sig_val = float(macd_ind.macd_signal().iloc[-1])
        macd_hist = float(macd_ind.macd_diff().iloc[-1])
        macd_hist_prev = float(macd_ind.macd_diff().iloc[-2]) if len(close) > 2 else 0
        if macd_line > macd_sig_val and macd_hist > macd_hist_prev:
            score += 20; signals.append(("MACD Bullish Crossover", "Histogram expanding", "bullish"))
        elif macd_line > macd_sig_val:
            score += 10; signals.append(("MACD Bullish", "Above signal", "bullish"))
        elif macd_hist > macd_hist_prev:
            score += 5; signals.append(("MACD Improving", "Histogram rising", "neutral"))
        else:
            score -= 8; signals.append(("MACD Bearish", "Below signal — penalized", "bearish"))  # R1-P0-Fix3: was 0 penalty

        # ── EMA Stack (9/21/50/200) ───────────────────────────────
        ema_9  = EMAIndicator(close, window=9).ema_indicator()
        ema_21 = EMAIndicator(close, window=21).ema_indicator()
        ema_50 = SMAIndicator(close, window=50).sma_indicator()  # using SMA50 as per original
        ema_200 = EMAIndicator(close, window=min(200, len(close)-1)).ema_indicator()
        e9, e21 = float(ema_9.iloc[-1]), float(ema_21.iloc[-1])
        e9_prev, e21_prev = float(ema_9.iloc[-2]), float(ema_21.iloc[-2])
        e50, e200 = float(ema_50.iloc[-1]), float(ema_200.iloc[-1])
        below_ema200 = current_price < e200

        if e9 > e21 and e9_prev <= e21_prev:
            score += 15; signals.append(("EMA 9/21 Crossover", "Fresh bullish cross", "bullish"))
        elif e9 > e21:
            score += 7; signals.append(("EMA Bullish", "9 > 21 EMA", "bullish"))

        if not below_ema200:
            score += 8; signals.append(("Above EMA 200", "Long-term uptrend", "bullish"))
        else:
            score -= 12; signals.append(("Below EMA 200", "Structural downtrend", "bearish"))

        if current_price > e50:
            score += 5; signals.append(("Above 50 SMA", "Uptrend confirmed", "bullish"))

        # ── Golden Cross (EMA50 > EMA200) ─────────────────────────
        if len(close) > 5:
            e50_prev  = float(SMAIndicator(close, window=50).sma_indicator().iloc[-2])
            e200_prev = float(EMAIndicator(close, window=min(200, len(close)-1)).ema_indicator().iloc[-2])
            if e50 > e200 and e50_prev <= e200_prev:
                score += 12; signals.append(("Golden Cross (50/200)", "Major bull signal 🌟", "bullish"))

        # ── Bollinger Bands + Squeeze ─────────────────────────────
        bb = BollingerBands(close, window=20, window_dev=2)
        bb_upper = float(bb.bollinger_hband().iloc[-1])
        bb_lower = float(bb.bollinger_lband().iloc[-1])
        bb_mid   = float(bb.bollinger_mavg().iloc[-1])
        bb_range = bb_upper - bb_lower
        bb_pct = (current_price - bb_lower) / bb_range if bb_range > 0 else 0.5
        # BB squeeze: width < 70% of 50-period avg
        bb_width = bb.bollinger_hband() - bb.bollinger_lband()
        bb_width_avg = float(bb_width.rolling(50).mean().iloc[-1]) if len(close) >= 50 else float(bb_width.mean())
        if bb_width_avg > 0 and float(bb_width.iloc[-1]) < bb_width_avg * 0.70:
            score += 12; signals.append(("BB Squeeze 🔥", "Breakout Imminent", "bullish"))
        if bb_pct < 0.15:
            score += 15; signals.append(("BB Lower Band", f"{bb_pct:.0%} — bounce zone", "bullish"))
        elif bb_pct < 0.35:
            score += 10; signals.append(("BB Lower Half", f"{bb_pct:.0%}", "neutral"))
        elif bb_pct > 0.90:
            score -= 5; signals.append(("BB Overbought", f"{bb_pct:.0%}", "bearish"))

        # ── Volume Surge ── [SMART MONEY] ─────────────────────────
        avg_vol = float(volume.rolling(20).mean().iloc[-1])
        vol_ratio = float(volume.iloc[-1] / avg_vol) if avg_vol > 0 else 1.0
        if vol_ratio > 3.0:
            smart_money_raw += 20; signals.append(("Volume Explosion 🚀", f"{vol_ratio:.1f}x avg", "bullish"))
        elif vol_ratio > 2.0:
            smart_money_raw += 15; signals.append(("Volume Surge", f"{vol_ratio:.1f}x avg", "bullish"))
        elif vol_ratio > 1.5:
            smart_money_raw += 10; signals.append(("High Volume", f"{vol_ratio:.1f}x", "bullish"))

        # ── ATR Sweet Spot ────────────────────────────────────────
        atr_val = float(AverageTrueRange(high, low, close, window=14).average_true_range().iloc[-1])
        atr_pct = (atr_val / current_price) * 100
        if 1.5 <= atr_pct <= 4.0:
            score += 10; signals.append(("Volatility Sweet Spot", f"ATR: {atr_pct:.1f}%", "bullish"))
        elif 1.0 <= atr_pct < 1.5 or 4.0 < atr_pct <= 5.5:
            score += 5; signals.append(("Moderate Volatility", f"ATR: {atr_pct:.1f}%", "neutral"))
        elif atr_pct > 7:
            score -= 5

        # ── Stochastic ────────────────────────────────────────────
        stoch = StochasticOscillator(high, low, close)
        stoch_k = float(stoch.stoch().iloc[-1])
        stoch_d = float(stoch.stoch_signal().iloc[-1])
        if stoch_k < 25 and stoch_k > stoch_d:
            score += 12; signals.append(("Stoch Oversold Cross", f"K={stoch_k:.0f}>D={stoch_d:.0f}", "bullish"))
        elif stoch_k < 35 and stoch_k > stoch_d:
            score += 7; signals.append(("Stoch Improving", f"K={stoch_k:.0f}", "bullish"))

        # ── Momentum (1W/2W/1M composite) ─────────────────────────
        pct_1w = float(((close.iloc[-1] / close.iloc[-5])  - 1) * 100) if len(close) >= 5  else 0
        pct_2w = float(((close.iloc[-1] / close.iloc[-10]) - 1) * 100) if len(close) >= 10 else 0
        pct_1m = float(((close.iloc[-1] / close.iloc[-22]) - 1) * 100) if len(close) >= 22 else 0
        mom_composite = pct_1w * 0.5 + pct_2w * 0.3 + pct_1m * 0.2
        if mom_composite > 5:
            score += 12; signals.append(("Strong Momentum", f"{mom_composite:.1f}%", "bullish"))
        elif -12 < pct_2w < -2 and pct_1w > 0 and pct_1w > pct_2w:
            score += 12; signals.append(("Reversal Pattern", f"2W:{pct_2w:+.1f}%→1W:{pct_1w:+.1f}%", "bullish"))
        elif -10 < pct_2w < 0 and pct_1w > pct_2w:
            score += 8; signals.append(("Reversal Building", f"1W: {pct_1w:+.1f}%", "bullish"))
        elif mom_composite < -5:
            score -= 6

        # ── OBV ── [SMART MONEY] ───────────────────────────────────
        obv = OnBalanceVolumeIndicator(close, volume).on_balance_volume()
        obv_slope = float((obv.iloc[-1] - obv.iloc[-5]) / abs(float(obv.iloc[-5])) * 100
                          if abs(float(obv.iloc[-5])) > 0 else 0)
        if obv_slope > 8:
            smart_money_raw += 10; signals.append(("OBV Surging", f"+{obv_slope:.1f}% accumulation", "bullish"))
        elif obv_slope > 3:
            smart_money_raw += 5; signals.append(("OBV Rising", f"+{obv_slope:.1f}%", "bullish"))

        # ── 52-Week Pullback ──────────────────────────────────────
        high_52w = float(high.max())
        low_52w  = float(low.min())
        dist_high = ((current_price - high_52w) / high_52w) * 100
        from_low  = ((current_price - low_52w) / low_52w) * 100
        if -35 <= dist_high <= -10:
            score += 15; signals.append(("52W Pullback Zone", f"{dist_high:.1f}% from high", "bullish"))
        elif -10 < dist_high < -3:
            score += 5
        if from_low < 15:
            score += 8; signals.append(("Near 52W Low", f"+{from_low:.1f}%", "bullish"))

        # ── VWAP ─────────────────────────────────────────────────
        curr_vwap = float(vwap.iloc[-1])
        vwap_position = ((current_price - curr_vwap) / curr_vwap) * 100 if curr_vwap > 0 else 0
        if -0.5 <= vwap_position <= 1.0:
            score += 10; signals.append(("VWAP Support", f"{vwap_position:+.1f}%", "bullish"))
        elif -2 < vwap_position < -0.5:
            score += 5; signals.append(("Below VWAP", f"{vwap_position:+.1f}%", "neutral"))

        # ── ADX ───────────────────────────────────────────────────
        adx_ind = ADXIndicator(high, low, close, window=14)
        adx = float(adx_ind.adx().iloc[-1])
        dip = float(adx_ind.adx_neg().iloc[-1])
        dim = float(adx_ind.adx_pos().iloc[-1])
        if adx > 30 and dim > dip:
            score += 15; signals.append(("Strong Uptrend", f"ADX: {adx:.0f}", "bullish"))
        elif adx > 20 and dim > dip:
            score += 8; signals.append(("Trending", f"ADX: {adx:.0f}", "bullish"))
        elif adx < 15:
            score -= 5; signals.append(("No Trend (Choppy)", f"ADX: {adx:.0f}", "bearish"))

        # ── CCI ───────────────────────────────────────────────────
        try:
            cci_val = float(CCIIndicator(high, low, close, window=20).cci().iloc[-1])
            if cci_val < -100:
                score += 10; signals.append(("CCI Oversold", f"CCI: {cci_val:.0f}", "bullish"))
            elif cci_val > 200:
                score -= 5
        except Exception:
            pass

        # ── Williams %R ───────────────────────────────────────────
        try:
            wr_val = float(WilliamsRIndicator(high, low, close, lbp=14).williams_r().iloc[-1])
            if wr_val < -80:
                score += 10; signals.append(("Williams %R Oversold", f"W%R: {wr_val:.1f}", "bullish"))
        except Exception:
            pass

        # ── MFI ── [SMART MONEY] ───────────────────────────────────
        try:
            mfi_val = float(MFIIndicator(high, low, close, volume, window=14).money_flow_index().iloc[-1])
            if mfi_val < 25:
                smart_money_raw += 10; signals.append(("MFI Oversold", f"MFI: {mfi_val:.1f}", "bullish"))
            elif mfi_val > 80:
                smart_money_raw -= 5
        except Exception:
            pass

        # ── Keltner Channel ───────────────────────────────────────
        try:
            kc = KeltnerChannel(high, low, close, window=20)
            if current_price < float(kc.keltner_channel_lband().iloc[-1]):
                score += 8; signals.append(("Below Keltner Lower", "Oversold zone", "bullish"))
        except Exception:
            pass

        # ── CMF (Chaikin Money Flow) ── [SMART MONEY] ──────────────
        try:
            cmf_val = float(ChaikinMoneyFlowIndicator(high, low, close, volume, window=20).chaikin_money_flow().iloc[-1])
            if cmf_val > 0.15:
                smart_money_raw += 8; signals.append(("CMF Positive", f"CMF: {cmf_val:.2f} — Accumulation", "bullish"))
            elif cmf_val < -0.15:
                smart_money_raw -= 5
        except Exception:
            pass

        # ── Ichimoku Cloud ────────────────────────────────────────
        try:
            ich = IchimokuIndicator(high, low, window1=9, window2=26, window3=52)
            span_a = float(ich.ichimoku_a().iloc[-1])
            span_b = float(ich.ichimoku_b().iloc[-1])
            if current_price > max(span_a, span_b):
                score += 10; signals.append(("Above Ichimoku Cloud ☁️", "Bullish signal", "bullish"))
            elif current_price < min(span_a, span_b):
                score -= 8; signals.append(("Below Ichimoku Cloud ☁️", "Bearish signal", "bearish"))
        except Exception:
            pass

        # ── Supertrend (manual ATR-based) ─────────────────────────
        try:
            hl2 = (float(high.iloc[-1]) + float(low.iloc[-1])) / 2
            lower_band = hl2 - 3 * atr_val
            if current_price > lower_band:
                score += 6; signals.append(("Supertrend Bullish ✅", "Price above lower band", "bullish"))
        except Exception:
            pass

        # ── EMA21 Deviation (Mean Reversion) ─────────────────────
        ema_dev = ((current_price - e21) / e21) * 100
        if -8 <= ema_dev <= -2:
            score += 8; signals.append(("EMA21 Pullback", f"{ema_dev:.1f}% below EMA21", "bullish"))
        elif ema_dev < -12:
            score += 5; signals.append(("Deep EMA Pullback", "Reversal setup", "bullish"))

        # ── RS vs Nifty ─────────────────────────────────────────
        rs_vs_nifty = pct_1m - nifty_1m
        if rs_vs_nifty > 8:
            score += 12; signals.append(("Crushing Nifty 💪", f"+{rs_vs_nifty:.1f}%", "bullish"))
        elif rs_vs_nifty > 3:
            score += 8; signals.append(("Outperforming Nifty", f"+{rs_vs_nifty:.1f}%", "bullish"))
        elif rs_vs_nifty > 0:
            score += 4

        # ── Delivery % ── [SMART MONEY] ───────────────────────────
        avg_delivery = float(delivery_pct.rolling(10).mean().iloc[-1]
                             if len(delivery_pct) >= 10 else delivery_pct.mean())
        curr_delivery = float(delivery_pct.iloc[-1])
        delivery_trend = curr_delivery - avg_delivery
        if np.isnan(avg_delivery): avg_delivery = 50.0
        if np.isnan(curr_delivery): curr_delivery = 50.0
        if np.isnan(delivery_trend): delivery_trend = 0.0
        if curr_delivery > 65 and delivery_trend > 8:
            smart_money_raw += 18; signals.append(("Delivery Surge 🚀", f"{curr_delivery:.0f}% (+{delivery_trend:.0f}%)", "bullish"))
        elif curr_delivery > 55 and delivery_trend > 3:
            smart_money_raw += 14; signals.append(("Strong Delivery", f"{curr_delivery:.0f}% (+{delivery_trend:.0f}%)", "bullish"))
        elif curr_delivery > 45 and delivery_trend > 0:
            smart_money_raw += 8; signals.append(("Good Delivery", f"{curr_delivery:.0f}%", "bullish"))
        elif curr_delivery > 35:
            smart_money_raw += 3

        # ── Fibonacci ─────────────────────────────────────────────
        fib = calc_fibonacci(high_52w, low_52w, current_price)
        if fib["score"] > 0:
            score += fib["score"]
            signals.append(("Fibonacci", fib["level"], "bullish" if fib["score"] >= 12 else "neutral"))

        # ── Breakout & VP Divergence ──────────────────────────────
        is_breakout = detect_breakout(close, high, volume, avg_vol, bb_upper, atr_val)
        vp_divergence = detect_vp_divergence(close, obv)
        if is_breakout:
            score += 15; signals.append(("Breakout! 🚀", "Breaking consolidation with volume", "bullish"))
        if vp_divergence:
            smart_money_raw += 10; signals.append(("Accumulation", "OBV rising while price flat", "bullish"))

        # ── Weekly Trend ──────────────────────────────────────────
        weekly_trend = get_weekly_trend(close)
        sr = calc_support_resistance(high, low, close)
        if weekly_trend == "up" and e9 > e21:
            score += 8; signals.append(("Weekly Uptrend", "Multi-TF aligned", "bullish"))
        elif weekly_trend == "down" and e9 < e21:
            score -= 8; signals.append(("Weekly Downtrend", "Multi-TF bearish", "bearish"))

        # ── Regime Adjustment ─────────────────────────────────────
        if regime == "bearish":
            score = int(score * 0.85)
            signals.append(("Bear Market", "Score reduced 15%", "bearish"))
        elif regime == "bullish" and not below_ema200:
            score += 5
            signals.append(("Bull Market Tailwind", "Market supportive", "bullish"))

        record_timing("technical_indicators", round((time.monotonic() - _start_tech) * 1000), True)

        # ════════════════════════════════════════════════════════════
        # LAYERS 2–12: INTELLIGENCE ENGINE
        # ════════════════════════════════════════════════════════════

        # Layer 4: Fundamentals (cache_only=True during scan → zero yfinance calls)
        fundamentals = get_fundamentals_yf(clean, cache_only=cache_only)
        # Fallback sector from SECTORS dict if yfinance returns Unknown
        if fundamentals.get("sector") == "Unknown":
            fundamentals["sector"] = sector

        # Layer 4B: Earnings Momentum Engine (R2) — uses cached quarterly data
        from intelligence.fundamentals import get_earnings_momentum
        earnings_mom = get_earnings_momentum(clean, fundamentals=fundamentals, cache_only=cache_only)

        # Check Technical Score from Layer 1 to see if we meet MarketAux criteria
        tech_score_100 = min(100, max(0, round((score / 220.0) * 100)))

        # Get news count from GDELT cache
        from intelligence.news_gdelt_finbert import get_gdelt_sentiment
        _, gdelt_articles, gdelt_spike = get_gdelt_sentiment(clean)
        news_count = len(gdelt_articles)

        # Decide if we query MarketAux (during scan loop or forced candidate check)
        should_query_mx = query_marketaux or (tech_score_100 > 80 and news_count < 3)

        # All other layers via orchestrator
        layers = run_all_layers(clean, df, current_price, fundamentals, query_marketaux=should_query_mx, cache_only=cache_only)

        # Add layer scores to total (for signals check)
        composite = layers.get("composite_layer_score", 0)
        score += composite

        # Add intelligence signals to main signals list
        # MTF
        mtf_trends = layers.get("mtf_trends", {})
        if all(v == "BULLISH" for v in mtf_trends.values() if v != "UNKNOWN"):
            signals.append(("All Timeframes Bullish 🟢", "1D/1W/1M aligned", "bullish"))

        # Seasonal
        seasonal = layers.get("seasonal", {})
        for reason in seasonal.get("reasons", [])[:2]:
            signals.append((reason, "Seasonal boost", "bullish"))

        # S/R
        supports    = layers.get("supports", [])
        resistances = layers.get("resistances", [])
        near_sup = any(abs(current_price - s) / current_price < 0.03 for s in supports)
        near_res = any(abs(current_price - r) / current_price < 0.03 for r in resistances)
        if near_sup:
            score += 14; signals.append(("At Strong Support 🛡️", "S/R zone", "bullish"))
        if near_res:
            score -= 10; signals.append(("At Resistance Zone ⚠️", "Overhead pressure", "bearish"))

        # RRG
        rot_quad = layers.get("sector_rotation", {}).get("quadrant", "UNKNOWN")
        if "LEADING" in rot_quad:
            signals.append((f"Sector RRG: {rot_quad}", "Best momentum", "bullish"))

        # News
        news_score = layers.get("news_sentiment", {}).get("score", 0)
        if news_score > 3:
            signals.append(("Positive News Sentiment 📰", f"Score: {news_score:+.1f}", "bullish"))
        elif news_score < -3:
            signals.append(("Negative News Sentiment ⚠️", f"Score: {news_score:+.1f}", "bearish"))

        # GDELT spike
        if gdelt_spike > 3:
            signals.append(("News Volume Spike 📢", f"{gdelt_spike:.1f}x normal", "bullish"))

        # Macro
        macro_bias = layers.get("macro_bias", 0)
        if macro_bias > 5:
            signals.append(("Macro Tailwinds 🌍", "Global markets supportive", "bullish"))
        elif macro_bias < -5:
            signals.append(("Macro Headwinds ⚠️", "Global market risk-off", "bearish"))

        # FF Regime
        ff_regime = layers.get("macro_event", {}).get("regime", "NEUTRAL")
        if ff_regime == "RISK_OFF":
            signals.append(("RISK_OFF Regime ⚠️", "Macro events bearish", "bearish"))
        elif ff_regime == "RISK_ON":
            signals.append(("RISK_ON Regime 🟢", "Macro events bullish", "bullish"))

        # Corporate events
        events = layers.get("events", [])
        if any("Earnings" in e.get("event", "") or "Result" in e.get("event", "") for e in events):
            signals.append(("Results Upcoming 📅", "Catalyst event near", "neutral"))

        # ── Skip clearly poor stocks ──────────────────────────────
        if score < -50:
            return None

        # ── COMPOSITE SCORING WEIGHTS ─────────────────────────────
        # R2: Weight Rebalance — earnings momentum added, tech/fund adjusted
        # R1-P1A: Smart money extracted as independent signal family
        raw_tech_score = score  # technical-only (smart money removed)

        # 1. Technical Structure (30% → max 30)
        #    Normalizer: ~200 is realistic max for pure technical indicators
        tech_score_30 = min(30.0, max(0.0, (raw_tech_score / 200.0) * 30.0))

        # 2. Earnings Momentum (15% → max 15) — R2: NEW
        earnings_mom_100 = earnings_mom.get("earnings_momentum_score", 0)
        earnings_mom_15 = min(15.0, max(0.0, earnings_mom_100 * 0.15))

        # 3. Fundamental Quality (10% → max 10)
        fund_score = fundamentals.get("fund_score", 0)
        fundamental_score_10 = min(10.0, max(0.0, (fund_score / 32.0) * 10.0))

        # 4. Smart Money (10% → max 10) — Delivery, OBV, CMF, MFI, Volume, VP Divergence
        #    Normalizer: 76 is actual max (Vol:20 + OBV:10 + MFI:10 + CMF:8 + Dlv:18 + VP:10)
        smart_money_100 = min(100.0, max(0.0, (smart_money_raw / 76.0) * 100.0))
        smart_money_10 = smart_money_100 * 0.10

        # 5. Sector Rotation (10% → max 10) — RRG quadrant + relative strength
        rot_data = layers.get("sector_rotation", {})
        rot_quadrant = rot_data.get("quadrant", "UNKNOWN")
        rot_score_map = {"LEADING": 90, "IMPROVING": 70, "WEAKENING": 35, "LAGGING": 15}
        sector_rot_100 = rot_score_map.get(rot_quadrant, 50)
        sector_rotation_10 = sector_rot_100 * 0.10

        # 6. News Sentiment (8% → max 8)
        ns_score = layers.get("news_sentiment", {}).get("score", 0.0)
        news_sentiment_8 = min(8.0, max(0.0, 4.0 + (ns_score / 15.0) * 4.0))

        # 7. News Spike (2% → max 2)
        news_spike_2 = 0.0
        if gdelt_spike > 1.0:
            news_spike_2 = min(2.0, (gdelt_spike - 1.0) * 0.4)

        # 8. Macro Score (5% → max 5)
        ff_score = layers.get("macro_event", {}).get("score", 0.0)
        macro_raw = ff_score + float(macro_bias)
        macro_score_5 = min(5.0, max(0.0, 2.5 + (macro_raw / 25.0) * 2.5))

        # 9. Catalyst Score (10% → max 10) — MarketAux + GDELT keywords
        news_items = layers.get("news_sentiment", {}).get("items", [])
        mx_items = [item for item in news_items if item.get("source") == "marketaux"]
        if mx_items:
            mx_avg = sum(item.get("score", 0.0) for item in mx_items) / len(mx_items)
            catalyst_score_10 = min(10.0, max(0.0, (mx_avg + 1.0) / 2.0 * 10.0))
        else:
            # GDELT keyword catalyst fallback
            pos_keywords = ["upgrade", "order win", "earnings beat", "acquisition", "buyback", "dividend", "revenue up", "expansion"]
            neg_keywords = ["downgrade", "penalty", "default", "bankruptcy", "earnings miss", "revenue down", "fraud"]
            bonus = 0.0
            for art in gdelt_articles:
                title = art.get("title", "").lower()
                if any(w in title for w in pos_keywords):
                    bonus += 1.5
                if any(w in title for w in neg_keywords):
                    bonus -= 1.5
            catalyst_score_10 = min(10.0, max(0.0, 5.0 + bonus))

        # ── Release 1: Fundamental-Only Dynamic Normalization ─────
        # Normalization Contract: Only remove weights for components that are
        # completely unavailable. Components available but scoring 0 retain weight.
        # Fundamental Availability Contract: Check the full set of fields used by
        # the fundamental scoring engine, not just PE/PB.
        max_available_weight = 100.0
        _fund_data_missing = _is_fundamental_data_missing(fundamentals)
        if _fund_data_missing:
            max_available_weight -= 10.0

        raw_sum = (
            tech_score_30 + earnings_mom_15 + fundamental_score_10 +
            smart_money_10 + sector_rotation_10 + news_sentiment_8 +
            news_spike_2 + macro_score_5 + catalyst_score_10
        )
        final_score = round((raw_sum / max_available_weight) * 100) if max_available_weight > 0 else 0
        score_100 = min(100, max(0, final_score))
        grade = classify_grade(score_100)

        # ── ATR-BASED TARGETS (legacy + new trade levels) ─────────
        trade = layers.get("trade", {})
        # Fallback to original ATR calculation
        atr_stop = current_price - (ATR_SL_MULTIPLIER * atr_val)

        # Calculative strategy to define the entry price range and stop loss
        s1 = sr.get("s1")
        s2 = sr.get("s2")
        pivot = sr.get("pivot")
        fib_s = fib.get("support")
        
        # 1. Stop Loss: strict technical SL placed below the nearest key support level or ATR stop
        sl_candidates = [atr_stop]
        
        # S1 support within 7% below current price
        if s1 and s1 < current_price and (current_price - s1) / current_price <= 0.07:
            sl_candidates.append(s1 * 0.99)
            
        # Fibonacci support within 7% below current price
        if fib_s and fib_s < current_price and (current_price - fib_s) / current_price <= 0.07:
            sl_candidates.append(fib_s * 0.99)
            
        # Select the closest structural support below current price
        valid_supports = [s for s in sl_candidates if s < current_price]
        if valid_supports:
            # Place the stop loss below support, maximizing R:R
            structural_sl = max(valid_supports)
            # Ensure it is at least 1.5% below current price
            if current_price - structural_sl < current_price * 0.015:
                structural_sl = min(valid_supports)
        else:
            structural_sl = atr_stop
            
        strict_sl = round(structural_sl, 2)
        # Guard: SL must always be below current price (at least 2% below)
        if strict_sl >= current_price:
            strict_sl = round(current_price * 0.98, 2)
        stop_loss_pct = round(((strict_sl - current_price) / current_price) * 100, 1)
        
        # 2. Target Price Strategy: based on structural resistances (R1, R2, Fib resistance)
        base_mult = 2.0
        if weekly_trend == "up" and e9 > e21:
            base_mult = 3.0
        elif weekly_trend == "down":
            base_mult = 1.8
        if adx > 25:
            base_mult += 0.5
            
        risk_distance = max(current_price - strict_sl, current_price * 0.02)  # Floor: at least 2% of CMP
        default_target = current_price + (base_mult * risk_distance)
        target_candidates = [default_target]
        
        r1 = sr.get("r1")
        r2 = sr.get("r2")
        fib_r = fib.get("resistance")
        
        if fib_r and fib_r > current_price * 1.02:
            target_candidates.append(fib_r)
        if r1 and r1 > current_price * 1.02:
            target_candidates.append(r1)
            
        realistic = [t for t in target_candidates if t <= default_target * 1.5]
        target_price = max(realistic) if realistic else default_target
        target_pct = round(((target_price - current_price) / current_price) * 100, 1)
        risk_reward = max(0, round((target_price - current_price) / risk_distance, 1)) if risk_distance > 0 else 0
        risk_score = calc_risk_score(rsi, atr_pct, dist_high, vol_ratio, pct_1m, below_ema200, adx)

        # 3. Target levels based on resistances:
        target1 = round(r1 if (r1 and r1 > current_price) else target_price, 2)
        target2 = round(r2 if (r2 and r2 > target1) else target1 * 1.08, 2)
        target3 = round(target2 * 1.10, 2)

        risk_dist = current_price - strict_sl
        rr1_val = round((target1 - current_price) / risk_dist, 1) if risk_dist > 0 else 1.5
        rr2_val = round((target2 - current_price) / risk_dist, 1) if risk_dist > 0 else 2.5
        rr3_val = round((target3 - current_price) / risk_dist, 1) if risk_dist > 0 else 3.5

        # 4. Entry Zone Strategy: buy near support or on breakout
        if is_breakout:
            breakout_level = r1 or fib_r or current_price
            if breakout_level > current_price * 0.95 and breakout_level < current_price * 1.05:
                entry_low = round(breakout_level * 0.995, 2)
                entry_high = round(breakout_level * 1.015, 2)
            else:
                entry_low = round(current_price * 0.995, 2)
                entry_high = round(current_price * 1.01, 2)
        else:
            pullback_supports = []
            if e9 and e9 < current_price and (current_price - e9) / current_price <= 0.03:
                pullback_supports.append(e9)
            if s1 and s1 < current_price and (current_price - s1) / current_price <= 0.03:
                pullback_supports.append(s1)
            if pivot and pivot < current_price and (current_price - pivot) / current_price <= 0.03:
                pullback_supports.append(pivot)
                
            if pullback_supports:
                entry_low = round(max(pullback_supports), 2)
                if entry_low >= current_price:
                    entry_low = round(current_price * 0.99, 2)
                entry_high = round(current_price * 1.005, 2)
            else:
                entry_low = round(current_price * 0.99, 2)
                entry_high = round(current_price * 1.005, 2)
                
        if entry_low > entry_high:
            entry_low, entry_high = entry_high, entry_low

        if regime == "bearish":
            booking_plan = "Book 100% at Target 1 (Bear Market defensive play)"
        elif weekly_trend == "up":
            booking_plan = "Book 50% at Target 1, trail 50% to Target 2 with SL at Cost"
        else:
            booking_plan = "Book 70% at Target 1, trail 30% with tight trailing SL"

        trade = {
            "entry_low": entry_low,
            "entry_high": entry_high,
            "stop_loss": strict_sl,
            "target1": target1,
            "rr1": rr1_val,
            "target2": target2,
            "rr2": rr2_val,
            "target3": target3,
            "rr3": rr3_val,
            "booking_plan": booking_plan,
            "risk_reward": risk_reward,
            # Maintain legacy keys for safety
            "target_1": target1,
            "target_2": target2,
        }

        # ── HIGH CONVICTION FLAG ──────────────────────────────────
        bullish_signals = sum(1 for s in signals if s[2] == "bullish")
        macd_is_bullish = macd_line > macd_sig_val

        # Golden Stock Composite Rule — R2: updated for new weight structure
        is_golden = (
            score_100 >= 80
            and tech_score_30 >= 21.0      # 70% of 30 max
            and fundamental_score_10 >= 6.0 # 60% of 10 max
            and earnings_mom_15 >= 8.0      # ~53% of 15 max — earnings momentum required
            and smart_money_10 >= 5.0       # 50% of 10 max
            and risk_reward >= 2.2
            and risk_score <= 45
        )

        hc_reasons = []
        hc_rejection_reasons = []

        if score_100 >= HC_MIN_SCORE:
            hc_reasons.append(f"Score {score_100} >= {HC_MIN_SCORE}")
        else:
            hc_rejection_reasons.append(f"Score {score_100} < {HC_MIN_SCORE}")

        if bullish_signals >= HC_MIN_SIGNALS_BULLISH:
            hc_reasons.append(f"Bullish Signals {bullish_signals} >= {HC_MIN_SIGNALS_BULLISH}")
        else:
            hc_rejection_reasons.append(f"Bullish Signals {bullish_signals} < {HC_MIN_SIGNALS_BULLISH}")

        if HC_RSI_RANGE[0] <= rsi <= HC_RSI_RANGE[1]:
            hc_reasons.append(f"RSI {rsi:.1f} in range {HC_RSI_RANGE}")
        else:
            hc_rejection_reasons.append(f"RSI {rsi:.1f} not in range {HC_RSI_RANGE}")

        if curr_delivery >= HC_DELIVERY_MIN:
            hc_reasons.append(f"Delivery {curr_delivery:.1f}% >= {HC_DELIVERY_MIN}%")
        else:
            hc_rejection_reasons.append(f"Delivery {curr_delivery:.1f}% < {HC_DELIVERY_MIN}%")

        if HC_ATR_RANGE[0] <= atr_pct <= HC_ATR_RANGE[1]:
            hc_reasons.append(f"ATR {atr_pct:.1f}% in range {HC_ATR_RANGE}")
        else:
            hc_rejection_reasons.append(f"ATR {atr_pct:.1f}% not in range {HC_ATR_RANGE}")

        if risk_score <= HC_RISK_MAX:
            hc_reasons.append(f"Risk Score {risk_score} <= {HC_RISK_MAX}")
        else:
            hc_rejection_reasons.append(f"Risk Score {risk_score} > {HC_RISK_MAX}")

        if risk_reward >= HC_MIN_RISK_REWARD:
            hc_reasons.append(f"Risk Reward {risk_reward:.2f} >= {HC_MIN_RISK_REWARD}")
        else:
            hc_rejection_reasons.append(f"Risk Reward {risk_reward:.2f} < {HC_MIN_RISK_REWARD}")

        if (not HC_REQUIRE_MACD_BULLISH or macd_is_bullish):
            hc_reasons.append("MACD Bullish check passed")
        else:
            hc_rejection_reasons.append("MACD not bullish")

        if vol_ratio >= HC_REQUIRE_VOLUME:
            hc_reasons.append(f"Volume Ratio {vol_ratio:.1f}x >= {HC_REQUIRE_VOLUME}x")
        else:
            hc_rejection_reasons.append(f"Volume Ratio {vol_ratio:.1f}x < {HC_REQUIRE_VOLUME}x")

        is_hc_base = len(hc_rejection_reasons) == 0

        if is_golden:
            hc_reasons.append("Is Golden Stock")
            is_hc_base = True

        high_conviction = is_hc_base

        bear_play = (
            regime == "bearish"
            and rsi < BP_RSI_MAX
            and vol_ratio >= BP_VOLUME_MIN
            and curr_delivery >= BP_DELIVERY_MIN
            and pct_1w >= BP_WEEK1_MAX_LOSS
            and (not BP_MACD_BULLISH or macd_is_bullish)
        )

        def _safe(v, default=0):
            if isinstance(v, float) and (np.isnan(v) or np.isinf(v)):
                return default
            return v

        _result = {
            # Core — R2: updated sub-scores with earnings momentum
            "model_version": "R2.1",  # Track for outcome comparison across releases
            "symbol": clean, "name": clean, "sector": fundamentals.get("sector", sector),
            "news_sentiment_score": round(news_sentiment_8, 2),
            "news_spike_score": round(news_spike_2, 2),
            "technical_score": round(tech_score_30, 2),
            "fundamental_score": round(fundamental_score_10, 2),
            "macro_score": round(macro_score_5, 2),
            "marketaux_catalyst_score": round(catalyst_score_10, 2),
            "smart_money_score": round(smart_money_10, 2),
            "smart_money_raw": round(smart_money_raw, 1),
            "smart_money_100": round(smart_money_100, 1),
            "sector_rotation_score": round(sector_rotation_10, 2),
            # R2: Earnings Momentum Engine
            "earnings_momentum_score": round(earnings_mom_15, 2),
            "earnings_momentum_100": earnings_mom.get("earnings_momentum_score", 0),
            "earnings_grade": earnings_mom.get("earnings_grade", "D"),
            "earnings_signals": earnings_mom.get("earnings_signals", []),
            "earnings_confidence": earnings_mom.get("confidence", 0),
            "marketaux_queried": should_query_mx,
            "price": round(current_price, 2),
            "score": score_100, "grade": grade,
            "signals": signals,
            # Indicators
            "rsi": _safe(round(rsi, 1)),
            "adx": _safe(round(adx, 1)),
            "macd_signal": "Bullish" if macd_line > macd_sig_val else "Bearish",
            "volume_ratio": _safe(round(vol_ratio, 1), 1.0),
            "atr_pct": _safe(round(atr_pct, 2)),
            "stoch_k": _safe(round(stoch_k, 1)),
            "stoch_d": _safe(round(stoch_d, 1)),
            "pct_1w": _safe(round(pct_1w, 2)),
            "pct_2w": _safe(round(pct_2w, 2)),
            "pct_1m": _safe(round(pct_1m, 2)),
            "bb_position": _safe(round(bb_pct * 100, 1)),
            "dist_from_high": _safe(round(dist_high, 1)),
            "rs_vs_nifty": _safe(round(rs_vs_nifty, 2)),
            # Targets (original)
            "target_price": _safe(round(target_price, 2)),
            "target_pct": _safe(target_pct),
            "stop_loss": _safe(round(atr_stop, 2)),
            "stop_loss_pct": _safe(stop_loss_pct),
            "risk_reward": _safe(risk_reward),
            "risk_score": _safe(risk_score),
            # Trade levels (new ATR + S/R based)
            "trade": trade,
            # Delivery
            "delivery_pct": _safe(round(curr_delivery, 1), 50.0) if has_delivery else None,
            "delivery_trend": _safe(round(delivery_trend, 1)),
            # Fib & S/R
            "fib_level": fib["level"],
            "fib_support": fib.get("support"),
            "fib_resistance": fib.get("resistance"),
            "vwap_position": _safe(round(vwap_position, 2)),
            "support_resistance": sr,
            "supports": layers.get("supports", []),
            "resistances": layers.get("resistances", []),
            # Intelligence layers
            "mtf_trends": layers.get("mtf_trends", {}),
            "mtf_score": layers.get("mtf_score", 0),
            "seasonal": layers.get("seasonal", {}),
            "order_book": layers.get("order_book", {}),
            "sector_rotation": layers.get("sector_rotation", {}),
            "gdelt": layers.get("gdelt", {}),
            "news_sentiment": layers.get("news_sentiment", {}),
            "macro_event": layers.get("macro_event", {}),
            "macro_bias": layers.get("macro_bias", 0),
            "events": layers.get("events", []),
            "fundamentals": fundamentals,
            "composite_layer_score": layers.get("composite_layer_score", 0),
            # Chart
            "chart_data": chart_data,
            # Flags
            "high_conviction": high_conviction,
            "hc_reasons": hc_reasons,
            "hc_rejection_reasons": hc_rejection_reasons,
            "is_golden": is_golden,
            "bear_play": bear_play,
            "is_breakout": is_breakout,
            "vp_divergence": vp_divergence,
            "weekly_trend": weekly_trend,
            "below_ema200": below_ema200,
            "high_52w": round(high_52w, 2),
            "low_52w":  round(low_52w, 2),
            "pullback_pct": round(abs(dist_high), 1),
            # Scan metadata
            "scan_mode": scan_mode,
            # Phase 0: Audit metadata (consumed by save_score_audit)
            "_score_components": {
                "technical": round(tech_score_30, 2),
                "earnings_momentum": round(earnings_mom_15, 2),
                "fundamental": round(fundamental_score_10, 2),
                "smart_money": round(smart_money_10, 2),
                "sector_rotation": round(sector_rotation_10, 2),
                "news_sentiment": round(news_sentiment_8, 2),
                "news_spike": round(news_spike_2, 2),
                "macro": round(macro_score_5, 2),
                "catalyst": round(catalyst_score_10, 2),
                # Phase 5, Section 36: Detailed per-indicator score breakdown
                "score_breakdown": {
                    "rsi": round(rsi, 1),
                    "macd_contribution": round(20 if (macd_line > macd_sig_val and macd_hist > macd_hist_prev) else (10 if macd_line > macd_sig_val else (5 if macd_hist > macd_hist_prev else -8)), 1),
                    "ema_stack": round(15 if (e9 > e21 and e9_prev <= e21_prev) else (7 if e9 > e21 else 0), 1),
                    "ema200_contribution": 8 if not below_ema200 else -12,
                    "bb_contribution": round(15 if bb_pct < 0.15 else (10 if bb_pct < 0.35 else (-5 if bb_pct > 0.90 else 0)), 1),
                    "volume_raw": round(smart_money_raw, 1),
                    "atr_pct": round(atr_pct, 2),
                    "stoch_k": round(stoch_k, 1),
                    "momentum_composite": round(mom_composite, 2),
                    "obv_slope": round(obv_slope, 2),
                    "52w_pullback": round(dist_high, 1),
                    "vwap_position": round(vwap_position, 2),
                    "adx": round(adx, 1),
                    "rs_vs_nifty": round(rs_vs_nifty, 2),
                    "delivery_pct": round(curr_delivery, 1),
                    "delivery_trend": round(delivery_trend, 1),
                    "fib_score": fib["score"],
                    "is_breakout": is_breakout,
                    "vp_divergence": vp_divergence,
                    "weekly_trend": weekly_trend,
                    "regime": regime,
                    "raw_tech_score": raw_tech_score,
                    "smart_money_100": round(smart_money_100, 1),
                    "fund_score": fund_score,
                    "sector_rot_quadrant": rot_quadrant,
                    "final_score_100": score_100,
                    "max_available_weight": max_available_weight,
                    "raw_sum": round(raw_sum, 2),
                },
            },
            "_data_source": _data_source,
            "_source_reason": _source_reason,
            "_provider_latency_ms": _fetch_latency_ms,
            "_data_staleness_hours": round(
                (time.time() - df["DATE"].iloc[-1].timestamp()) / 3600, 1
            ) if hasattr(df["DATE"].iloc[-1], "timestamp") else None,
        }

        # P0: Trace NaN sources for root-cause analysis before returning
        trace_nan_sources(_result, symbol=clean, scan_id=scan_mode)
        return _result

    except (KeyError, ValueError, IndexError, TypeError) as exc:
        log.debug("Analysis failed for %s: %s", symbol, exc)
        return None
