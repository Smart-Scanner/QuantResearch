"""
Intelligence Package — 12-Layer Analysis Orchestrator
======================================================
All 12 layers wired together. analyzer.py calls run_all_layers() once per stock.

Pre-scan warmup (call these in scanner.py before the stock loop):
    from intelligence import warmup_all
    warmup_all(all_symbols_set)

Per-stock (called inside fetch_and_analyze):
    from intelligence import run_all_layers
    layers = run_all_layers(symbol, df, current_price, fundamentals)
"""

import logging
import os
import threading
import time
from metrics.timer import timed
# yf_guard stubs — yfinance removed
try:
    from intelligence.yf_guard import yf_is_available
except ImportError:
    def yf_is_available(): return False

from intelligence.seasonal import get_seasonal_score
from intelligence.order_book import get_order_book_proxy
from intelligence.sector_rotation import (
    get_sector_rotation_score, scan_sector_rotation, sector_rotation_cache
)
from intelligence.macro import (
    get_macro_market_bias, scan_world_markets, macro_snapshot, world_snapshot,
    get_world_snapshot, get_macro_snapshot,
)
from intelligence.macro_events import scan_macro_events, get_macro_event_score, get_ff_regime, get_ff_events
from intelligence.news_gdelt_finbert import build_article_cache, get_gdelt_sentiment
from intelligence.news_sentiment import fetch_news_sentiment, get_global_headlines
from intelligence.fundamentals import get_fundamentals_yf
from intelligence.mtf import get_mtf_trend
from intelligence.support_resistance import get_support_resistance, calculate_trade_levels

# ─── Events cache ─────────────────────────────────────────────────────────────
_events_cache: dict = {}    # symbol → {data: list, ts: float}
_EVENTS_TTL = 6 * 3600      # 6 hours
_events_lock = threading.Lock()

log = logging.getLogger("screener")


# ──────────────────────────────────────────────────────────────
# Pre-Scan Warmup — call ONCE before scan loop
# ──────────────────────────────────────────────────────────────
def warmup_all(all_symbols: set = None):
    """
    Pre-warms all cached global intelligence data.
    Called once at scan start in scanner.py.
    """
    log.info("[Intelligence] Starting pre-scan warmup...")

    # News memo is TTL'd (fresh-per-scan without an explicit clear), so the two
    # warmup_all() calls per scan don't redo the prewarm — no clear here.

    # World markets + FRED macro
    try:
        scan_world_markets()
    except Exception as exc:
        log.warning("[Intelligence] World markets warmup failed: %s", exc)

    # Sector rotation (RRG)
    try:
        scan_sector_rotation()
    except Exception as exc:
        log.warning("[Intelligence] Sector rotation warmup failed: %s", exc)

    # Forex Factory macro events
    try:
        scan_macro_events()
    except Exception as exc:
        log.warning("[Intelligence] Forex Factory warmup failed: %s", exc)

    # GDELT + FinBERT article cache (most important warmup step)
    if all_symbols:
        try:
            build_article_cache(set(all_symbols))
        except Exception as exc:
            log.warning("[Intelligence] GDELT/FinBERT cache build failed: %s", exc)

        # Per-symbol news sentiment pre-warm: compute (and memoize) each symbol's
        # FinBERT news score ONCE here, uncontended, so the parallel scan workers
        # read the cached value instead of re-running FinBERT inline (the per-symbol
        # CPU bottleneck). SCORE-IDENTICAL: same _compute_news_sentiment path,
        # deterministic model.
        try:
            from intelligence.news_sentiment import prewarm_news_sentiment
            _pw_workers = int(os.getenv("NEWS_PREWARM_WORKERS", "3"))
            _n = prewarm_news_sentiment(set(all_symbols), max_workers=_pw_workers)
            log.info("[Intelligence] News sentiment pre-warmed for %d symbols", _n)
        except Exception as exc:
            log.warning("[Intelligence] News sentiment pre-warm failed: %s", exc)

    log.info("[Intelligence] Pre-scan warmup complete")


# ──────────────────────────────────────────────────────────────
# Per-Stock Corporate Events
# ──────────────────────────────────────────────────────────────
@timed("corporate_events")
def get_upcoming_events(symbol: str, cache_only: bool = False) -> list:
    """Corporate events cache. yfinance removed — returns cached data or empty."""
    sym = symbol.upper()
    # Level 1: Memory cache
    with _events_lock:
        entry = _events_cache.get(sym)
        if entry and (time.time() - entry["ts"]) < _EVENTS_TTL:
            return entry["data"]

    # No live source (yfinance removed) — return empty
    return []


def invalidate_events_cache(symbol: str) -> None:
    """Clear memory cache for a symbol's corporate events."""
    with _events_lock:
        _events_cache.pop(symbol.upper(), None)
    log.debug("events cache invalidated for %s", symbol)


# ──────────────────────────────────────────────────────────────
# Per-Stock Layer Runner
# ──────────────────────────────────────────────────────────────
def run_all_layers(
    symbol: str, df, current_price: float, fundamentals: dict,
    query_marketaux: bool = False, cache_only: bool = False,
) -> dict:
    """
    Run all intelligence layers for a stock.
    Returns merged dict with composite_layer_score.

    df: OHLCV DataFrame (columns: DATE/OPEN/HIGH/LOW/CLOSE/VOLUME)
    fundamentals: dict from get_fundamentals_yf() — passed in to avoid double-fetch
    cache_only: if True, use cached data only (Fast Scan mode)
    """
    result = {}

    # ── Layer 3: Support & Resistance + Trade Levels ──────────────
    try:
        supports, resistances = get_support_resistance(df)
        trade = calculate_trade_levels(df, supports, resistances, current_price)
        result["supports"] = supports
        result["resistances"] = resistances
        result["trade"] = trade
    except Exception as exc:
        log.debug("S/R failed for %s: %s", symbol, exc)
        result["supports"] = []
        result["resistances"] = []
        result["trade"] = {}

    # ── Layer 2: Multi-Timeframe ───────────────────────────────────
    try:
        mtf_trends, mtf_score = get_mtf_trend(symbol, cache_only=cache_only)
        result["mtf_trends"] = mtf_trends
        result["mtf_score"] = mtf_score
    except Exception as exc:
        log.debug("MTF failed for %s: %s", symbol, exc)
        result["mtf_trends"] = {}
        result["mtf_score"] = 0

    # ── Layer 5: Seasonal Intelligence ────────────────────────────
    try:
        seasonal_score, active_seasons, seasonal_reasons = get_seasonal_score(
            fundamentals.get("sector", ""),
            fundamentals.get("industry", "")
        )
        result["seasonal"] = {
            "score": seasonal_score,
            "active": active_seasons,
            "reasons": seasonal_reasons,
        }
    except Exception as exc:
        log.debug("Seasonal failed for %s: %s", symbol, exc)
        result["seasonal"] = {"score": 0, "active": [], "reasons": []}

    # ── Layer 6: Order Book Proxy ──────────────────────────────────
    try:
        ob_data = get_order_book_proxy(symbol, fundamentals)
        result["order_book"] = ob_data
    except Exception as exc:
        log.debug("OrderBook failed for %s: %s", symbol, exc)
        result["order_book"] = {"ob_score": 0, "signals": [], "ob_to_mcap": None}

    # ── Layer 7: Sector Rotation (RRG) ────────────────────────────
    try:
        rot_score, rot_quad = get_sector_rotation_score(fundamentals.get("sector", ""))
        result["sector_rotation"] = {"score": rot_score, "quadrant": rot_quad}
    except Exception as exc:
        log.debug("SectorRot failed for %s: %s", symbol, exc)
        result["sector_rotation"] = {"score": 0, "quadrant": "UNKNOWN"}

    # ── Layer 8: GDELT + FinBERT News (from pre-built cache) ──────
    try:
        gdelt_score, gdelt_articles, spike = get_gdelt_sentiment(symbol)
        result["gdelt"] = {
            "score": gdelt_score,
            "articles": gdelt_articles,
            "spike": spike,
        }
    except Exception as exc:
        log.debug("GDELT failed for %s: %s", symbol, exc)
        result["gdelt"] = {"score": 0, "articles": [], "spike": 1.0}

    # -- Layer 9: Full News Waterfall (includes GDELT + Finnhub + RSS + MX) --
    try:
        _scan_mode = "deep" if not cache_only else "fast"
        news_score, news_items, news_sources = fetch_news_sentiment(
            symbol, query_marketaux=query_marketaux, scan_mode=_scan_mode
        )
        result["news_sentiment"] = {
            "score": news_score, "items": news_items, "source_breakdown": news_sources,
        }
    except Exception as exc:
        log.debug("News failed for %s: %s", symbol, exc)
        result["news_sentiment"] = {"score": 0, "items": [], "source_breakdown": {}}

    # ── Layer 9b: Forex Factory macro events ──────────────────────
    try:
        ff_score, ff_regime = get_macro_event_score(fundamentals.get("sector", ""))
        result["macro_event"] = {"score": ff_score, "regime": ff_regime}
    except Exception as exc:
        log.debug("FF failed for %s: %s", symbol, exc)
        result["macro_event"] = {"score": 0, "regime": "NEUTRAL"}

    # ── Layer 10/11: Macro + World Markets ────────────────────────
    try:
        macro_bias = get_macro_market_bias()
        result["macro_bias"] = macro_bias
    except Exception as exc:
        log.debug("Macro bias failed for %s: %s", symbol, exc)
        result["macro_bias"] = 0

    # ── Layer 12: Corporate Events ────────────────────────────────
    try:
        events = get_upcoming_events(symbol, cache_only=cache_only)
        result["events"] = events
    except Exception as exc:
        log.debug("Events failed for %s: %s", symbol, exc)
        result["events"] = []

    # ── Composite Layer Score ──────────────────────────────────────
    mtf_s      = result.get("mtf_score", 0) * 3
    fund_s     = fundamentals.get("fund_score", 0)
    seasonal_s = result.get("seasonal", {}).get("score", 0)
    ob_s       = result.get("order_book", {}).get("ob_score", 0)
    rot_s      = result.get("sector_rotation", {}).get("score", 0)
    news_s     = result.get("news_sentiment", {}).get("score", 0)
    gdelt_s    = result.get("gdelt", {}).get("score", 0)
    ff_s       = result.get("macro_event", {}).get("score", 0)
    macro_s    = result.get("macro_bias", 0)

    # GDELT is primary news signal; news_sentiment includes GDELT so use it, not both
    composite = mtf_s + fund_s + seasonal_s + ob_s + rot_s + news_s + ff_s + macro_s

    # RISK_OFF regime dampener: reduce all scores 20%
    ff_regime_str = result.get("macro_event", {}).get("regime", "NEUTRAL")
    if ff_regime_str == "RISK_OFF":
        composite = round(composite * 0.8)

    result["composite_layer_score"] = composite

    return result


__all__ = [
    "run_all_layers",
    "warmup_all",
    "get_gdelt_sentiment",
    "fetch_news_sentiment",
    "get_global_headlines",
    "get_fundamentals_yf",
    "get_upcoming_events",
    "invalidate_events_cache",
    "scan_world_markets",
    "scan_sector_rotation",
    "scan_macro_events",
    "sector_rotation_cache",
    "macro_snapshot",
    "world_snapshot",
    "get_world_snapshot",
    "get_macro_snapshot",
    "get_ff_regime",
    "get_ff_events",
]
