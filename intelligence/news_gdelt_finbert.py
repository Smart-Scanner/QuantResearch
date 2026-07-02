"""
GDELT News Intelligence Engine (Decoupled from FinBERT)
---------------------------------------------------------
GDELT bulk article fetching + symbol mapping.
FinBERT scoring is delegated to intelligence.finbert_engine (Governance Rule #21).

FIXED (production-ready):
- _parse_age_hours: uses compact UTC format YYYYMMDDHHMMSS (no T separator)
- baseline_count: floor of 5× per-symbol avg, not 3×
- _safe_match_token: word-boundary regex prevents false-positives on Airtel/Adani/SBI
- GDELT rate limiting via controlled queue approach
"""

import os
import re
import time
import json
import logging
import threading
import requests
import db
from metrics.timer import timed
from datetime import datetime, timezone
from functools import lru_cache

log = logging.getLogger("screener")

# ──────────────────────────────────────────────────────────────
# FinBERT scoring — delegated to finbert_engine (Governance #21)
# ──────────────────────────────────────────────────────────────
from intelligence.finbert_engine import score_articles as _score_articles_finbert
from intelligence.finbert_engine import _keyword_sentiment


# ──────────────────────────────────────────────────────────────
# NSE Company Name Map
# ──────────────────────────────────────────────────────────────
NSE_NAME_MAP = {
    "reliance": "RELIANCE", "tcs": "TCS", "tata consultancy": "TCS",
    "hdfc bank": "HDFCBANK", "infosys": "INFY", "icici bank": "ICICIBANK",
    "state bank of india": "SBIN", "sbi": "SBIN",
    "bharti airtel": "BHARTIARTL",
    "kotak mahindra": "KOTAKBANK", "itc": "ITC", "larsen & toubro": "LT", "l&t": "LT",
    "axis bank": "AXISBANK", "bajaj finance": "BAJFINANCE", "asian paints": "ASIANPAINT",
    "maruti suzuki": "MARUTI", "hcl technologies": "HCLTECH", "sun pharma": "SUNPHARMA",
    "titan company": "TITAN", "wipro": "WIPRO", "ultratech cement": "ULTRACEMCO",
    "ntpc": "NTPC", "power grid": "POWERGRID", "nestle india": "NESTLEIND",
    "tech mahindra": "TECHM", "ongc": "ONGC", "tata steel": "TATASTEEL",
    "jsw steel": "JSWSTEEL", "hindalco": "HINDALCO",
    "adani enterprises": "ADANIENT", "adani ports": "ADANIPORTS",
    "bajaj finserv": "BAJAJFINSV", "grasim": "GRASIM",
    "cipla": "CIPLA", "dr reddy": "DRREDDY", "coal india": "COALINDIA",
    "bpcl": "BPCL", "eicher motors": "EICHERMOT", "divi laboratories": "DIVISLAB",
    "britannia": "BRITANNIA", "apollo hospitals": "APOLLOHOSP",
    "hero motocorp": "HEROMOTOCO", "sbi life": "SBILIFE", "dabur": "DABUR",
    "hdfc life": "HDFCLIFE", "bajaj auto": "BAJAJ-AUTO", "tata consumer": "TATACONSUM",
    "pidilite": "PIDILITIND", "siemens": "SIEMENS", "adani green": "ADANIGREEN",
    "havells": "HAVELLS", "ambuja cements": "AMBUJACEM", "dlf": "DLF",
    "godrej consumer": "GODREJCP", "trent": "TRENT", "vedanta": "VEDL",
    "bank of baroda": "BANKBARODA", "indusind bank": "INDUSINDBK",
    "icici prudential": "ICICIPRULI", "interglobe aviation": "INDIGO",
    "abb india": "ABB", "srf": "SRF", "info edge": "NAUKRI",
    "torrent pharma": "TORNTPHARM", "gail india": "GAIL", "pi industries": "PIIND",
    "marico": "MARICO", "tata power": "TATAPOWER", "colgate palmolive": "COLPAL",
    "mphasis": "MPHASIS", "power finance": "PFC", "rec limited": "RECLTD",
    "lupin": "LUPIN", "voltas": "VOLTAS", "polycab": "POLYCAB",
    "tvs motor": "TVSMOTOR", "sail": "SAIL", "mrf": "MRF",
    "federal bank": "FEDERALBNK", "cummins india": "CUMMINSIND",
    "petronet lng": "PETRONET", "nmdc": "NMDC", "jubilant foodworks": "JUBLFOOD",
    "oberoi realty": "OBEROIRLTY", "irctc": "IRCTC", "crompton greaves": "CROMPTON",
    "bharat electronics": "BEL", "hindustan aeronautics": "HAL",
    "nhpc": "NHPC", "sjvn": "SJVN", "irfc": "IRFC", "jsw energy": "JSWENERGY",
    "rail vikas nigam": "RVNL", "hindustan unilever": "HINDUNILVR",
    "zomato": "ZOMATO", "paytm": "PAYTM", "nykaa": "NYKAA",
    "avenue supermarts": "DMART", "tata motors": "TATAMOTORS",
    "mahindra & mahindra": "M&M", "persistent systems": "PERSISTENT",
    "coforge": "COFORGE", "ltimindtree": "LTIM", "l&t technology": "LTTS",
}


# ──────────────────────────────────────────────────────────────
# GDELT Bulk Pull
# ──────────────────────────────────────────────────────────────
GDELT_QUERIES = [
    '("NSE" OR "Nifty" OR "India stock") (earnings OR profit OR revenue OR "order win" OR contract OR acquisition OR quarterly OR IPO OR merger OR buyback OR dividend)',
    '("BSE" OR "Sensex" OR "Indian market") (earnings OR profit OR quarterly OR growth OR results)',
]
GDELT_BASE = "https://api.gdeltproject.org/api/v2/doc/doc"

# Phase 9: GDELT circuit breaker
_gdelt_failures = 0
_gdelt_cooldown_until = 0.0
_GDELT_THRESHOLD = 3
_GDELT_COOLDOWN = 1800  # 30 min


def _gdelt_is_available() -> bool:
    if _gdelt_failures >= _GDELT_THRESHOLD:
        if time.time() < _gdelt_cooldown_until:
            return False
        # Cooldown expired, reset
        _gdelt_reset()
    return True


def _gdelt_record_failure():
    global _gdelt_failures, _gdelt_cooldown_until
    _gdelt_failures += 1
    if _gdelt_failures >= _GDELT_THRESHOLD:
        _gdelt_cooldown_until = time.time() + _GDELT_COOLDOWN
        log.warning("GDELT circuit breaker OPEN: %d failures. Cooldown %.0f min.",
                     _gdelt_failures, _GDELT_COOLDOWN / 60)


def _gdelt_reset():
    global _gdelt_failures, _gdelt_cooldown_until
    _gdelt_failures = 0
    _gdelt_cooldown_until = 0.0


def fetch_gdelt_india_bulk(hours_back: int = 48) -> list:
    """
    Pull up to 1000 Indian business news articles from GDELT.
    Returns list of dicts: {url, title, seendate}
    Zero API key. Zero cost. Single GET per query.
    """
    # [NEW DB CACHE LOGIC] Check DB first to avoid multiple Railway workers hitting 429
    try:
        cached_raw = db.get_meta("gdelt_cache")
        if cached_raw:
            cached_data = json.loads(cached_raw)
            cache_age = time.time() - cached_data.get("timestamp", 0)
            if cache_age < 900:  # 15 minutes valid
                log.info("GDELT fetched %d articles from DB Cache (age: %.1f min)", len(cached_data.get("articles", [])), cache_age / 60)
                return cached_data.get("articles", [])
    except Exception as e:
        log.warning("GDELT DB Cache read failed: %s", e)

    if not _gdelt_is_available():
        log.info("GDELT circuit breaker open — returning empty (cached data used if available)")
        return []

    articles = []
    seen_urls = set()
    query_failures = 0
    for query in GDELT_QUERIES:
        try:
            # Controlled rate limiting between queries
            if query != GDELT_QUERIES[0]:
                time.sleep(6)  # GDELT requires 1 request per 5 seconds

            params = {
                "query": query,
                "mode": "ArtList",
                "maxrecords": 250,
                "format": "json",
                "timespan": f"{hours_back}H",
                "sort": "DateDesc",
            }
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            resp = requests.get(GDELT_BASE, params=params, headers=headers, timeout=20)
            
            if resp.status_code == 429:
                log.debug("GDELT rate limited (429). Waiting 6 seconds and retrying...")
                time.sleep(6)
                resp = requests.get(GDELT_BASE, params=params, headers=headers, timeout=20)
                
            if resp.status_code != 200:
                log.debug("GDELT query failed with status %d: %s", resp.status_code, resp.text[:100])
                query_failures += 1
                continue
            data = resp.json()
            for art in data.get("articles", []):
                url = art.get("url", "")
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                articles.append({
                    "url": url,
                    "title": art.get("title", ""),
                    "seendate": art.get("seendate", ""),
                })
        except Exception as exc:
            log.debug("GDELT query failed: %s", exc)
            query_failures += 1

    if query_failures >= len(GDELT_QUERIES):
        _gdelt_record_failure()
    elif articles:
        _gdelt_reset()  # Success — reset failure count

    log.info("GDELT fetched %d unique articles from API", len(articles))
    
    # Save to DB cache if successful
    if articles:
        try:
            db.set_meta("gdelt_cache", json.dumps({"timestamp": time.time(), "articles": articles}))
        except Exception as e:
            log.warning("GDELT DB Cache write failed: %s", e)

    return articles


# ──────────────────────────────────────────────────────────────
# Helpers — FIXED timestamp parser + recency + safe token match
# ──────────────────────────────────────────────────────────────

def _parse_age_hours(seendate: str) -> float:
    """
    Parse GDELT seendate — compact UTC format YYYYMMDDHHMMSS or ISO format.
    Falls back to 12h if parsing fails.
    """
    if not seendate:
        return 12.0
    try:
        # Strip non-digits to handle any format
        clean = re.sub(r"\D", "", seendate)
        if len(clean) >= 14:
            dt = datetime.strptime(clean[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        elif len(clean) >= 8:
            dt = datetime.strptime(clean[:8], "%Y%m%d").replace(tzinfo=timezone.utc)
        else:
            return 12.0
        return max((datetime.now(timezone.utc) - dt).total_seconds() / 3600, 0.0)
    except Exception:
        return 12.0


def _recency_weight(age_hours: float) -> float:
    """Exponential decay with floor: 0h→1.0, 12h→0.5, 48h→0.2, floor=0.15"""
    return max(0.15, 1 / (1 + age_hours / 12))


def _safe_match_token(text: str, term: str) -> bool:
    """
    Word-boundary regex match to prevent false-positives.
    'sbi' won't match 'sbilife'; 'airtel' won't match 'bhartiairtel'.
    """
    return re.search(rf"\b{re.escape(term)}\b", text.lower()) is not None


def score_headlines_finbert(headlines: list) -> list:
    """
    Batch-score list of headline strings.
    Delegates to finbert_engine for provider-independent scoring.
    Returns list of floats: positive=+conf, negative=-conf, neutral=0.
    """
    if not headlines:
        return []
    # Build article dicts for the engine
    articles = [{"title": h, "source": "gdelt", "age_hours": 12.0} for h in headlines]
    scored = _score_articles_finbert(articles)
    return [a.get("sentiment", _keyword_sentiment(a["title"])) for a in scored]


# ──────────────────────────────────────────────────────────────
# Article → Symbol Mapping — with strict token boundary check
# ──────────────────────────────────────────────────────────────

def _map_article_to_symbols(title: str, all_symbols: set) -> list:
    """
    Map article title to NSE symbols via word-boundary-safe name matching.
    Uses _safe_match_token() to avoid false positives (Airtel/Adani/SBI).
    """
    matches = []
    title_l = title.lower()

    # Direct symbol match — must be whole word
    for sym in all_symbols:
        if _safe_match_token(title_l, sym.lower()):
            matches.append(sym)

    # Company name match (longer names first = more specific)
    for name in sorted(NSE_NAME_MAP.keys(), key=len, reverse=True):
        sym = NSE_NAME_MAP[name]
        if sym not in all_symbols:
            continue
        if sym in matches:
            continue
        if _safe_match_token(title_l, name):
            matches.append(sym)

    return matches[:3]  # max 3 stocks per article


# ──────────────────────────────────────────────────────────────
# Per-Scan Article Cache
# ──────────────────────────────────────────────────────────────
_article_cache: dict = {}   # symbol → {score, articles, spike, ...}
_cache_lock = threading.Lock()
_cache_built_at: float = 0
_CACHE_TTL = 3600  # 1 hour


@timed("gdelt_bulk_fetch")
def build_article_cache(all_symbols: set):
    """
    Called ONCE at scan start. Builds per-symbol article cache.
    4-signal scoring: sentiment avg + spike + freshness-weighted confidence + negative penalty.
    """
    global _article_cache, _cache_built_at

    now = time.time()
    if now - _cache_built_at < _CACHE_TTL and _article_cache:
        log.info("Article cache fresh (%.0f min old), reusing", (now - _cache_built_at) / 60)
        return

    log.info("Building GDELT + FinBERT article cache...")
    articles = fetch_gdelt_india_bulk(hours_back=48)

    if not articles:
        log.warning("No GDELT articles — news layer will use fallback")
        return

    # Batch score all headlines at once
    headlines = [a["title"] for a in articles]
    scores = score_headlines_finbert(headlines)

    # Map articles to symbols with enriched metadata
    sym_articles: dict = {}
    for art, score in zip(articles, scores):
        age_h = _parse_age_hours(art.get("seendate", ""))
        weight = _recency_weight(age_h)
        mapped = _map_article_to_symbols(art["title"], all_symbols)
        for sym in mapped:
            if sym not in sym_articles:
                sym_articles[sym] = []
            sym_articles[sym].append({
                "raw_score": score,
                "weighted_score": score * weight,
                "title": art["title"],
                "age_hours": round(age_h, 1),
                "weight": round(weight, 3),
                "negative": score < -0.3,
            })

    # Spike baseline: floor at 1.0, use 5× per-symbol avg for stability
    baseline_count = max(1.0, len(articles) / max(1, len(all_symbols)) * 5)

    new_cache = {}
    for sym, arts in sym_articles.items():
        n = len(arts)

        # Signal 1: Sentiment average (raw FinBERT scores)
        avg_sent = sum(a["raw_score"] for a in arts) / n

        # Signal 2: Volume spike (articles vs baseline)
        spike = n / baseline_count

        # Signal 3: Freshness-weighted confidence
        fw_conf = sum(a["weighted_score"] for a in arts) / n

        # Signal 4: Negative headline penalty (-1 per strongly negative headline)
        neg_count = sum(1 for a in arts if a["negative"])
        neg_penalty = -min(neg_count * 1.5, 6.0)

        # Combine into a single score (-15 to +15 range)
        sent_score   = round(avg_sent * 8.0, 2)    # ±8 max
        spike_bonus  = min(5.0, round((spike - 1) * 3, 1)) if spike > 1.5 else 0
        fresh_bonus  = round(fw_conf * 3.0, 2)      # ±3 freshness layer
        total_score  = round(sent_score + spike_bonus + fresh_bonus + neg_penalty, 2)

        new_cache[sym] = {
            "score": total_score,
            "sentiment": round(avg_sent, 3),
            "spike": round(spike, 2),
            "freshness": round(fw_conf, 3),
            "neg_penalty": round(neg_penalty, 1),
            "articles": [
                {"title": a["title"], "score": round(a["raw_score"], 3), "age_h": a["age_hours"]}
                for a in sorted(arts, key=lambda x: x["age_hours"])[:5]
            ],
        }

    with _cache_lock:
        _article_cache = new_cache
        _cache_built_at = time.time()

    log.info("Article cache built: %d symbols with GDELT+FinBERT data", len(_article_cache))


def get_gdelt_sentiment(symbol: str) -> tuple:
    """
    Returns (score, articles, spike) from pre-built cache. O(1) — no API call.
    """
    with _cache_lock:
        data = _article_cache.get(symbol)
    if data is None:
        return 0, [], 1.0
    return data["score"], data["articles"], data["spike"]
