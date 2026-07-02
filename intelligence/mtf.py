"""
Multi-Timeframe Trend Alignment Engine — Phase 3 (yfinance removed, uses cache only)
---------------------------------------------------------------------------
FIXED:
- ema50_w: clamped to max(5, ...) so minimum EMA window is always valid
- Weighted scoring: 1M=+3/-3, 1W=+2/-2, 1D=+1/-1 (monthly most reliable)
- Monthly UNKNOWN is not penalized (sparse data expected)

Phase 2 additions:
- In-memory cache (_mtf_cache) with TTL=1h
- cache_only=True param: returns {}, 0 immediately on cache miss (Fast Scan safe)
- yf_guard integration: circuit open → skip all yfinance downloads
- prefetch_mtf_batch(symbols, max_workers=5) for pre-scan warmup
- get_mtf_cache_stats() for observability
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from ta.trend import EMAIndicator
from metrics.timer import timed
from intelligence.yf_guard import yf_is_available
try:
    from intelligence.yf_guard import yf_record_failure, yf_record_success
except ImportError:
    def yf_record_failure(**kw): pass
    def yf_record_success(): pass

log = logging.getLogger("screener")

TIMEFRAMES = {
    "1D": ("6mo",  "1d"),
    "1W": ("2y",   "1wk"),
    "1M": ("5y",   "1mo"),
}

# Higher timeframes carry more weight: 1M > 1W > 1D
MTF_SCORE_MAP = {
    "1D": {"BULLISH": 1, "BEARISH": -1, "NEUTRAL": 0, "UNKNOWN": 0},
    "1W": {"BULLISH": 2, "BEARISH": -2, "NEUTRAL": 0, "UNKNOWN": 0},
    "1M": {"BULLISH": 3, "BEARISH": -3, "NEUTRAL": 0, "UNKNOWN": 0},
}

# ─── Cache ────────────────────────────────────────────────────────────────────
_MTF_TTL    = 3600           # 1 hour
_mtf_cache: dict = {}        # symbol → {data: (trends, score), ts: float}
_mtf_lock   = threading.Lock()

# Stats counters
_hit_count  = 0
_miss_count = 0


def _compute_mtf(symbol: str) -> tuple:
    """Internal: fetch daily OHLCV from Angel One, resample to weekly/monthly,
    compute EMA trend for each timeframe."""
    import pandas as pd
    import live_feed

    trends = {}

    # Fetch 365 days of daily data (enough for monthly EMA)
    df_daily = live_feed.fetch_historical(symbol, days=365)
    if df_daily is None or len(df_daily) < 20:
        return {"1D": "UNKNOWN", "1W": "UNKNOWN", "1M": "UNKNOWN"}, 0

    # Ensure DATE is datetime index for resampling
    df_daily = df_daily.copy()
    df_daily["DATE"] = pd.to_datetime(df_daily["DATE"])
    df_daily = df_daily.set_index("DATE").sort_index()

    # Build timeframe DataFrames by resampling
    tf_data = {
        "1D": df_daily,  # daily as-is
        "1W": df_daily.resample("W").agg({
            "OPEN": "first", "HIGH": "max", "LOW": "min",
            "CLOSE": "last", "VOLUME": "sum"
        }).dropna(),
        "1M": df_daily.resample("ME").agg({
            "OPEN": "first", "HIGH": "max", "LOW": "min",
            "CLOSE": "last", "VOLUME": "sum"
        }).dropna(),
    }

    for tf, df in tf_data.items():
        try:
            if df is None or len(df) < 5:
                trends[tf] = "UNKNOWN"
                continue
            close = df["CLOSE"].squeeze()
            last  = float(close.iloc[-1])
            ema20_w = min(20, len(close) - 1)
            if ema20_w < 3:
                trends[tf] = "UNKNOWN"
                continue
            ema20 = float(EMAIndicator(close, window=ema20_w).ema_indicator().iloc[-1])
            ema50_w = max(5, min(50, len(close) - 1))
            ema50 = float(EMAIndicator(close, window=ema50_w).ema_indicator().iloc[-1])
            if last > ema20 > ema50:
                trends[tf] = "BULLISH"
            elif last < ema20 < ema50:
                trends[tf] = "BEARISH"
            else:
                trends[tf] = "NEUTRAL"
        except Exception as exc:
            log.debug("MTF %s %s failed: %s", symbol, tf, exc)
            trends[tf] = "UNKNOWN"

    mtf_score = sum(
        MTF_SCORE_MAP[tf].get(state, 0)
        for tf, state in trends.items()
    )
    return trends, mtf_score


@timed("mtf_trend")
def get_mtf_trend(symbol: str, cache_only: bool = False) -> tuple:
    """
    Returns (trends_dict, mtf_score).
    trends_dict: {"1D": "BULLISH"|"BEARISH"|"NEUTRAL"|"UNKNOWN", ...}
    mtf_score: weighted sum — monthly most reliable (max +6 / min -6)

    Args:
        symbol:     NSE symbol without .NS
        cache_only: If True, return ({}, 0) on cache miss (Fast Scan mode).
    """
    global _hit_count, _miss_count
    sym = symbol.upper()

    # ── Memory cache ──────────────────────────────────────────────
    with _mtf_lock:
        entry = _mtf_cache.get(sym)
        if entry and (time.time() - entry["ts"]) < _MTF_TTL:
            _hit_count += 1
            return entry["data"]

    _miss_count += 1

    if cache_only:
        log.debug("MTF cache miss %s — cache_only=True, returning empty", sym)
        return {}, 0

    # ── Live fetch via Angel One historical ────────────────────────
    try:
        trends, mtf_score = _compute_mtf(sym)
    except Exception as exc:
        log.debug("MTF fetch failed for %s: %s", sym, exc)
        return {}, 0

    with _mtf_lock:
        _mtf_cache[sym] = {"data": (trends, mtf_score), "ts": time.time()}

    return trends, mtf_score


def prefetch_mtf_batch(symbols: list, max_workers: int = 5) -> None:
    """
    Pre-warm the MTF cache for a list of symbols concurrently.
    Skips symbols already cached. Uses Angel One historical data.
    Called during pre-scan warmup for FNO / shortlisted stocks.
    """
    to_fetch = []
    with _mtf_lock:
        for sym in symbols:
            sym = sym.upper()
            entry = _mtf_cache.get(sym)
            if not entry or (time.time() - entry["ts"]) >= _MTF_TTL:
                to_fetch.append(sym)

    if not to_fetch:
        log.debug("MTF prefetch: all %d symbols already cached", len(symbols))
        return

    log.info("MTF prefetch: warming %d symbols (workers=%d)", len(to_fetch), max_workers)

    def _safe_fetch(sym):
        try:
            result = _compute_mtf(sym)
            return sym, result
        except Exception as exc:
            log.debug("MTF prefetch failed for %s: %s", sym, exc)
            return sym, None

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_safe_fetch, sym): sym for sym in to_fetch}
        for future in as_completed(futures, timeout=60):
            try:
                sym, result = future.result()
                if result is not None:
                    with _mtf_lock:
                        _mtf_cache[sym] = {"data": result, "ts": time.time()}
            except Exception:
                pass

    log.info("MTF prefetch complete")


def get_mtf_cache_stats() -> dict:
    """Return cache observability stats for /api/health."""
    with _mtf_lock:
        size = len(_mtf_cache)
        if size > 0:
            oldest = min(e["ts"] for e in _mtf_cache.values())
            oldest_age = round(time.time() - oldest)
        else:
            oldest_age = 0
    total = _hit_count + _miss_count
    hit_rate = round(_hit_count / total * 100) if total > 0 else 0
    return {
        "mtf_cache_size": size,
        "mtf_cache_hit_count": _hit_count,
        "mtf_cache_miss_count": _miss_count,
        "mtf_cache_hit_rate_pct": hit_rate,
        "mtf_cache_oldest_entry_age_s": oldest_age,
    }
