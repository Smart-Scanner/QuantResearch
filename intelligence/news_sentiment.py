"""
News Sentiment Engine — V2 (P0.1 News Intelligence Recovery)
--------------------------------------------------------------
Architecture: 5-Layer Provider Hierarchy + Provider-Independent FinBERT

Layer 1 (Macro):     GDELT + NSE Announcements
Layer 2 (Stock):     Finnhub (Primary) + MarketAux (Enrichment)
Layer 3 (Discovery): Google News RSS
Layer 4 (Removed):   yfinance (removed — other layers sufficient)
Layer 5 (Global):    NewsAPI (dashboard macro headlines only)

All articles → Normalize → Deduplicate → FinBERT → News Impact → Scanner

Governance:
- Provider Independence: No provider is a hard dependency
- FinBERT Independence: Scanner continues if FinBERT fails
- Scheduler Isolation: Refresh failure ≠ scanner failure
- Cache-First: Scanner reads cache, refresh jobs populate cache
- Rate Limit: Configurable per-provider (not hardcoded)
"""

import os
import re
import time
import logging
import threading
import xml.etree.ElementTree as ET
import requests
from datetime import datetime, timedelta

from intelligence.news_gdelt_finbert import get_gdelt_sentiment
# yfinance removed — news uses GDELT, Finnhub, MarketAux, Google RSS
from intelligence import finbert_engine
from intelligence import news_cache

log = logging.getLogger("screener")

MARKETAUX_KEY = os.getenv("MARKETAUX_API_KEY", "")
NEWS_API_KEY  = os.getenv("NEWS_API_KEY", "")
FINNHUB_KEY   = os.getenv("FINNHUB_API_KEY", "")

# Day quota tracking
_quota_lock = threading.Lock()
_newsapi_calls = 0
_MARKETAUX_DAILY_CAP = 50
_NEWSAPI_DAILY_CAP = 80

# Reset counter daily (simple time-based reset)
_quota_reset_at = time.time() + 86400  # 24h from server start

# Finnhub rate limiter (configurable token bucket)
_finnhub_lock = threading.Lock()
_finnhub_last_call = 0.0
_FINNHUB_MIN_INTERVAL = 1.05  # seconds between calls (60/min safe)


def _check_reset():
    global _newsapi_calls, _quota_reset_at
    if time.time() > _quota_reset_at:
        _newsapi_calls = 0
        _quota_reset_at = time.time() + 86400


def _get_marketaux_calls_today() -> int:
    import db
    today_str = datetime.now().strftime("%Y-%m-%d")
    last_date = db.get_meta("marketaux_calls_date")
    if last_date != today_str:
        db.set_meta("marketaux_calls_date", today_str)
        db.set_meta("marketaux_calls_count", 0)
        return 0
    return db.get_meta("marketaux_calls_count", 0)


def _increment_marketaux_calls():
    import db
    today_str = datetime.now().strftime("%Y-%m-%d")
    count = _get_marketaux_calls_today()
    db.set_meta("marketaux_calls_date", today_str)
    db.set_meta("marketaux_calls_count", count + 1)


# ──────────────────────────────────────────────────────────────
# Layer 1 (Macro): NSE Announcements
# ──────────────────────────────────────────────────────────────
_nse_cache: dict = {}       # symbol -> list of announcements
_nse_cache_ts: float = 0
_NSE_CACHE_TTL = 1800       # 30 min
_nse_lock = threading.Lock()


def _fetch_nse_announcements() -> list:
    """
    Fetch latest NSE corporate announcements (single HTTP call).
    Returns list of {symbol, subject, date} dicts.
    Cached for 30 minutes.
    """
    global _nse_cache, _nse_cache_ts
    now = time.time()
    if now - _nse_cache_ts < _NSE_CACHE_TTL and _nse_cache:
        return list(_nse_cache.values())

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        url = "https://www.nseindia.com/api/corporate-announcements?index=equities&from_date=&to_date="
        session = requests.Session()
        session.get("https://www.nseindia.com", headers=headers, timeout=5)
        resp = session.get(url, headers=headers, timeout=8)
        if resp.status_code != 200:
            log.debug("NSE announcements HTTP %d", resp.status_code)
            news_cache.record_refresh_failure("nse", f"HTTP {resp.status_code}")
            return []
        data = resp.json()
        if not isinstance(data, list):
            return []

        new_cache = {}
        for item in data[:100]:
            sym = (item.get("symbol") or "").upper().strip()
            if not sym:
                continue
            entry = {
                "symbol": sym,
                "subject": item.get("desc", "")[:200],
                "date": (item.get("an_dt") or "")[:10],
            }
            if sym not in new_cache:
                new_cache[sym] = []
            new_cache[sym].append(entry)

        with _nse_lock:
            _nse_cache = new_cache
            _nse_cache_ts = time.time()

        news_cache.record_refresh_success("nse")
        log.info("NSE announcements fetched: %d events for %d symbols", len(data[:100]), len(new_cache))
        return list(new_cache.values())
    except Exception as exc:
        log.debug("NSE announcements fetch failed: %s", exc)
        news_cache.record_refresh_failure("nse", str(exc))
        return []


def get_nse_affected_symbols() -> set:
    """Return set of symbols with recent NSE announcements (from cache)."""
    with _nse_lock:
        return set(_nse_cache.keys())


# ──────────────────────────────────────────────────────────────
# Layer 2 (Stock - Primary): Finnhub
# ──────────────────────────────────────────────────────────────

def _fetch_finnhub_articles(symbol: str) -> list:
    """
    Finnhub per-stock news — 60/min, unlimited/day.
    Returns raw article list (not scored — scoring delegated to FinBERT).
    Rate limited via configurable token bucket.
    """
    if not FINNHUB_KEY:
        return []

    # Check cache first
    cached = news_cache.get("finnhub", symbol)
    if cached is not None:
        return cached

    # Rate limiter (configurable interval)
    global _finnhub_last_call
    with _finnhub_lock:
        elapsed = time.time() - _finnhub_last_call
        if elapsed < _FINNHUB_MIN_INTERVAL:
            time.sleep(_FINNHUB_MIN_INTERVAL - elapsed)
        _finnhub_last_call = time.time()

    try:
        now = datetime.now()
        from_dt = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        to_dt = now.strftime("%Y-%m-%d")
        url = (f"https://finnhub.io/api/v1/company-news"
               f"?symbol={symbol}.NS&from={from_dt}&to={to_dt}&token={FINNHUB_KEY}")
        resp = requests.get(url, timeout=6)
        
        if resp.status_code == 429:
            log.warning("Finnhub rate limit hit for %s", symbol)
            news_cache.record_refresh_failure("finnhub", "HTTP 429")
            return []
        
        data = resp.json()
        if not data or not isinstance(data, list):
            news_cache.put("finnhub", symbol, [])  # Cache empty result
            return []

        articles = [
            {
                "title": a.get("headline", ""),
                "source": "finnhub",
                "date": datetime.fromtimestamp(a.get("datetime", 0)).strftime("%Y-%m-%d") if a.get("datetime") else "",
                "age_hours": max(0, (time.time() - a.get("datetime", time.time())) / 3600) if a.get("datetime") else 12.0,
            }
            for a in data[:10] if a.get("headline")
        ]
        
        news_cache.put("finnhub", symbol, articles)
        news_cache.record_refresh_success("finnhub")
        return articles

    except Exception as exc:
        log.debug("Finnhub failed for %s: %s", symbol, exc)
        news_cache.record_refresh_failure("finnhub", str(exc))
        return []


# ──────────────────────────────────────────────────────────────
# Layer 2 (Stock - Enrichment): MarketAux
# ──────────────────────────────────────────────────────────────

def _fetch_marketaux_articles(symbol: str) -> list:
    """
    MarketAux per-stock news (50/day quota).
    Returns raw article list (scoring delegated to FinBERT).
    Triggered only for high-conviction candidates (tech_score > 80).
    """
    if not MARKETAUX_KEY:
        return []

    # Check cache first
    cached = news_cache.get("marketaux", symbol)
    if cached is not None:
        return cached

    try:
        calls_today = _get_marketaux_calls_today()
        if calls_today >= _MARKETAUX_DAILY_CAP:
            log.warning("MarketAux daily quota limit of %d reached.", _MARKETAUX_DAILY_CAP)
            return []

        _increment_marketaux_calls()

        url = (f"https://api.marketaux.com/v1/news/all"
               f"?symbols={symbol}.NSE&filter_entities=true"
               f"&language=en&api_token={MARKETAUX_KEY}&limit=5")
        resp = requests.get(url, timeout=6)

        if resp.status_code in (429, 402, 403):
            log.warning("MarketAux API returned error status %d. Capping quota.", resp.status_code)
            import db
            today_str = datetime.now().strftime("%Y-%m-%d")
            db.set_meta("marketaux_calls_count", _MARKETAUX_DAILY_CAP)
            news_cache.record_refresh_failure("marketaux", f"HTTP {resp.status_code}")
            return []

        data = resp.json().get("data", [])
        articles = [
            {
                "title": a.get("title", ""),
                "source": "marketaux",
                "date": a.get("published_at", "")[:10] if a.get("published_at") else "",
                "age_hours": 6.0,  # MarketAux doesn't provide exact timestamps for age calc
            }
            for a in data if a.get("title")
        ]

        news_cache.put("marketaux", symbol, articles)
        news_cache.record_refresh_success("marketaux")
        return articles

    except Exception as exc:
        log.debug("MarketAux failed for %s: %s", symbol, exc)
        news_cache.record_refresh_failure("marketaux", str(exc))
        return []


# ──────────────────────────────────────────────────────────────
# Layer 3 (Discovery): Google News RSS
# ──────────────────────────────────────────────────────────────

def _fetch_google_rss_articles(symbol: str) -> list:
    """
    Google News RSS for stock-specific headlines.
    Only called when higher layers yield < 2 articles.
    """
    cached = news_cache.get("google_rss", symbol)
    if cached is not None:
        return cached

    try:
        query = f"{symbol}+NSE+stock"
        url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
        resp = requests.get(url, timeout=6, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            news_cache.record_refresh_failure("google_rss", f"HTTP {resp.status_code}")
            return []

        root = ET.fromstring(resp.content)
        items_xml = root.findall(".//item")
        if not items_xml:
            news_cache.put("google_rss", symbol, [])
            return []

        articles = []
        for item in items_xml[:10]:
            title_el = item.find("title")
            if title_el is not None and title_el.text:
                articles.append({
                    "title": title_el.text.strip(),
                    "source": "google_rss",
                    "date": "",
                    "age_hours": 6.0,
                })

        news_cache.put("google_rss", symbol, articles)
        news_cache.record_refresh_success("google_rss")
        return articles

    except Exception as exc:
        log.debug("Google RSS failed for %s: %s", symbol, exc)
        news_cache.record_refresh_failure("google_rss", str(exc))
        return []


# Layer 4 removed — yfinance no longer used
def _fetch_yfinance_articles(symbol: str) -> list:
    """Stub — yfinance removed. Returns empty list."""
    return []


# ──────────────────────────────────────────────────────────────
# Master Orchestrator: 5-Layer → FinBERT → Impact
# ──────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────
# Per-symbol sentiment memo + warmup prewarm  (perf; SCORE-IDENTICAL)
# fetch_news_sentiment is a deterministic pure function of (the symbol's
# articles, the FinBERT model). To stop the 6 scan workers from re-running
# FinBERT inline (the per-symbol CPU bottleneck), we compute it ONCE per
# symbol — ideally pre-warmed in warmup_all, uncontended — and memoize the
# full (score, items, source_breakdown) tuple. The scan workers then read the
# cached value. Because _compute_news_sentiment is the exact pre-memo body and
# FinBERT is deterministic, the cached value is byte-identical to what the old
# inline path produced. Memo applies ONLY to the fast path (query_marketaux=
# False); deep/marketaux always computes live. clear_news_memo() runs at the
# start of each scan's warmup so news is fresh per scan.
# ──────────────────────────────────────────────────────────────
_news_memo = {}
_news_memo_lock = threading.Lock()


# Memo entries are (result_tuple, monotonic_ts). A TTL gives fresh-news-per-scan
# WITHOUT an explicit clear, so the double warmup_all() call per scan (scanner.py
# :457 and :1301) does NOT re-run the prewarm — the 2nd pass finds fresh entries
# and skips. Entries older than the TTL are treated as a miss and recomputed,
# so a later scan (>TTL apart) refreshes news. Within a single scan they're identical.
_NEWS_MEMO_TTL = float(os.getenv("NEWS_MEMO_TTL", "1200"))  # 20 min


def _memo_get_fresh(symbol):
    with _news_memo_lock:
        e = _news_memo.get(symbol)
        if e is not None and (time.monotonic() - e[1]) < _NEWS_MEMO_TTL:
            return e[0]
    return None


def _memo_put(symbol, result):
    with _news_memo_lock:
        _news_memo[symbol] = (result, time.monotonic())


def clear_news_memo():
    """Drop the per-symbol news memo (manual/testing use; not needed per-scan now
    that entries are TTL'd)."""
    with _news_memo_lock:
        _news_memo.clear()


def fetch_news_sentiment(symbol: str, query_marketaux: bool = False, scan_mode: str = "fast") -> tuple:
    """Memoized wrapper around _compute_news_sentiment — SCORE-IDENTICAL to the
    pre-memo behaviour. The fast path (query_marketaux=False) returns a cached
    per-symbol result when fresh (within _NEWS_MEMO_TTL); deep/marketaux always
    computes live so it is never served a fast-mode cached value."""
    if not query_marketaux:
        cached = _memo_get_fresh(symbol)
        if cached is not None:
            return cached
    result = _compute_news_sentiment(symbol, query_marketaux=query_marketaux)
    if not query_marketaux:
        _memo_put(symbol, result)
    return result


def prewarm_news_sentiment(symbols, max_workers: int = 3) -> int:
    """Pre-compute (and memoize) per-symbol news sentiment up front, so the scan
    workers don't run FinBERT inline. Uses the EXACT same _compute_news_sentiment
    path => cached scores are identical to inline computation. Skips symbols already
    fresh in the memo (so a repeated warmup within a scan is a near-instant no-op).
    Returns the number of symbols warmed."""
    syms = [s for s in symbols if _memo_get_fresh(s) is None]
    if not syms:
        return 0

    def _one(s):
        try:
            fetch_news_sentiment(s, query_marketaux=False)
        except Exception as exc:
            log.warning("prewarm_news_sentiment failed for %s: %s", s, exc)

    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max(1, int(max_workers))) as ex:
            list(ex.map(_one, syms))
    except Exception as exc:
        log.warning("prewarm_news_sentiment pool failed: %s — sequential fallback", exc)
        for s in syms:
            _one(s)
    return len(syms)


def _compute_news_sentiment(symbol: str, query_marketaux: bool = False) -> tuple:
    """
    Master news sentiment function (V2).
    Returns (score: float, items: list, source_breakdown: dict).
    Score range: -15 to +15.

    5-Layer Architecture:
    1. Macro: GDELT (cache) + NSE Events
    2. Stock: Finnhub (Primary) + MarketAux (Enrichment, if query_marketaux)
    3. Discovery: Google RSS (if above yield < 2 articles)
    4. Emergency: yfinance (if all others empty)

    All articles → finbert_engine → News Impact Pipeline → Scanner
    """
    all_articles = []

    # ── Layer 1: Macro (GDELT cache + NSE) ──
    gdelt_score, gdelt_articles, spike = get_gdelt_sentiment(symbol)
    if gdelt_articles:
        for art in gdelt_articles:
            art["source"] = art.get("source", "gdelt")
            if "age_hours" not in art:
                art["age_hours"] = art.get("age_h", 12.0)
        all_articles.extend(gdelt_articles)

    # ── Layer 2: Stock (Finnhub Primary) ──
    fh_articles = _fetch_finnhub_articles(symbol)
    all_articles.extend(fh_articles)

    # ── Layer 2: Stock (MarketAux Enrichment — only if gated) ──
    if query_marketaux:
        mx_articles = _fetch_marketaux_articles(symbol)
        all_articles.extend(mx_articles)

    # ── Layer 3: Discovery (Google RSS — only if above yield < 2 articles) ──
    if len(all_articles) < 2:
        rss_articles = _fetch_google_rss_articles(symbol)
        all_articles.extend(rss_articles)

    # ── Layer 4: (yfinance removed — 3 layers are sufficient) ──

    # ── Universal Pipeline: Normalize → Deduplicate → FinBERT → Impact ──
    result = finbert_engine.process_articles(all_articles)

    return result["score"], result["items"], result.get("source_breakdown", {})


# ──────────────────────────────────────────────────────────────
# Layer 5 (Global): NewsAPI macro headlines (dashboard only)
# ──────────────────────────────────────────────────────────────

def get_global_headlines() -> list:
    """
    NewsAPI global macro headlines (not per-stock).
    Used for macro context display in dashboard.
    """
    global _newsapi_calls
    with _quota_lock:
        _check_reset()
        if _newsapi_calls >= _NEWSAPI_DAILY_CAP or not NEWS_API_KEY:
            return []
        _newsapi_calls += 1

    try:
        url = (f"https://newsapi.org/v2/top-headlines"
               f"?category=business&language=en&pageSize=10&apiKey={NEWS_API_KEY}")
        data = requests.get(url, timeout=6).json()
        articles = data.get("articles", [])
        return [{"title": a.get("title", ""),
                 "source": a.get("source", {}).get("name", ""),
                 "published": a.get("publishedAt", "")[:10]}
                for a in articles[:10]]
    except Exception as exc:
        log.debug("NewsAPI global headlines failed: %s", exc)
        return []
