"""
News Cache Layer (Governance Rule #22)
---------------------------------------
Architecture: Background Refresh Job → News Cache → Scanner Reads Cache

Key Governance Rules:
- Provider-Specific TTLs (Admin Controlled, not hardcoded)
- Scheduler Isolation: refresh failure ≠ scanner failure
- News Refresh Telemetry: last_success, last_failure, consecutive_failures, health

The scanner never directly calls provider APIs.
It reads from this cache, which is populated by background refresh jobs.
"""

import time
import logging
import threading
from typing import Optional

log = logging.getLogger("screener")

# ──────────────────────────────────────────────────────────────
# Provider-Specific TTL Configuration (Admin Controlled)
# ──────────────────────────────────────────────────────────────
DEFAULT_TTL = {
    "finnhub": 1800,       # 30 min
    "marketaux": 3600,     # 60 min (preserve quota)
    "gdelt": 3600,         # 60 min (bulk fetch)
    "google_rss": 1800,    # 30 min
    "yahoo": 3600,         # 60 min (emergency)
    "nse": 1800,           # 30 min
}


def get_ttl(provider: str) -> int:
    """Get the cache TTL for a specific provider. Admin-configurable."""
    return DEFAULT_TTL.get(provider, 1800)


def set_ttl(provider: str, ttl_seconds: int):
    """Update the cache TTL for a specific provider at runtime."""
    DEFAULT_TTL[provider] = max(60, ttl_seconds)  # minimum 60s
    log.info("News cache TTL for '%s' updated to %d seconds", provider, ttl_seconds)


# ──────────────────────────────────────────────────────────────
# News Refresh Telemetry
# ──────────────────────────────────────────────────────────────
_refresh_telemetry_lock = threading.Lock()
_refresh_telemetry = {}  # provider -> {last_success, last_failure, consecutive_failures, health}


def _init_provider_telemetry(provider: str):
    """Initialize telemetry for a provider if not already present."""
    if provider not in _refresh_telemetry:
        _refresh_telemetry[provider] = {
            "last_success": None,
            "last_failure": None,
            "consecutive_failures": 0,
            "health": "unknown",
            "total_calls": 0,
            "total_failures": 0,
        }


def record_refresh_success(provider: str):
    """Record a successful news refresh for a provider."""
    with _refresh_telemetry_lock:
        _init_provider_telemetry(provider)
        t = _refresh_telemetry[provider]
        t["last_success"] = time.time()
        t["consecutive_failures"] = 0
        t["health"] = "healthy"
        t["total_calls"] += 1


def record_refresh_failure(provider: str, error: str = ""):
    """Record a failed news refresh for a provider."""
    with _refresh_telemetry_lock:
        _init_provider_telemetry(provider)
        t = _refresh_telemetry[provider]
        t["last_failure"] = time.time()
        t["consecutive_failures"] += 1
        t["total_calls"] += 1
        t["total_failures"] += 1

        if t["consecutive_failures"] >= 5:
            t["health"] = "critical"
        elif t["consecutive_failures"] >= 3:
            t["health"] = "degraded"
        else:
            t["health"] = "warning"

        log.warning("News refresh failure for %s (consecutive: %d): %s",
                     provider, t["consecutive_failures"], error)


def get_refresh_telemetry() -> dict:
    """Return telemetry for all providers (for Admin Diagnostics)."""
    with _refresh_telemetry_lock:
        return {k: dict(v) for k, v in _refresh_telemetry.items()}


def get_provider_health(provider: str) -> str:
    """Get health status of a specific provider."""
    with _refresh_telemetry_lock:
        _init_provider_telemetry(provider)
        return _refresh_telemetry[provider]["health"]


# ──────────────────────────────────────────────────────────────
# Thread-Safe In-Memory News Cache
# ──────────────────────────────────────────────────────────────
_cache_lock = threading.Lock()
_cache = {}  # key: (provider, symbol) -> {data, timestamp}

# Global article cache: all articles collected per symbol across providers
_symbol_articles_lock = threading.Lock()
_symbol_articles = {}  # symbol -> {articles: list, timestamp: float}
_SYMBOL_CACHE_TTL = 1800  # 30 min default


def put(provider: str, symbol: str, data: dict):
    """Store news data for a specific provider+symbol in the cache."""
    with _cache_lock:
        _cache[(provider, symbol)] = {
            "data": data,
            "timestamp": time.time(),
        }


def get(provider: str, symbol: str) -> Optional[dict]:
    """
    Retrieve cached news data for a provider+symbol.
    Returns None if cache miss or TTL expired.
    """
    with _cache_lock:
        entry = _cache.get((provider, symbol))
        if entry is None:
            return None
        ttl = get_ttl(provider)
        if time.time() - entry["timestamp"] > ttl:
            return None  # Expired
        return entry["data"]


def is_fresh(provider: str, symbol: str) -> bool:
    """Check if cached data for a provider+symbol is still valid."""
    return get(provider, symbol) is not None


def put_symbol_articles(symbol: str, articles: list):
    """Store the combined, deduplicated article list for a symbol."""
    with _symbol_articles_lock:
        _symbol_articles[symbol] = {
            "articles": articles,
            "timestamp": time.time(),
        }


def get_symbol_articles(symbol: str) -> Optional[list]:
    """Get cached combined articles for a symbol. Returns None if stale."""
    with _symbol_articles_lock:
        entry = _symbol_articles.get(symbol)
        if entry is None:
            return None
        if time.time() - entry["timestamp"] > _SYMBOL_CACHE_TTL:
            return None
        return entry["articles"]


def clear():
    """Clear all caches. Used for testing or forced refresh."""
    global _cache, _symbol_articles
    with _cache_lock:
        _cache = {}
    with _symbol_articles_lock:
        _symbol_articles = {}
    log.info("News cache fully cleared")


def get_cache_stats() -> dict:
    """Return cache statistics for Admin Diagnostics."""
    with _cache_lock:
        total_entries = len(_cache)
        providers = {}
        for (prov, _sym), entry in _cache.items():
            if prov not in providers:
                providers[prov] = {"count": 0, "fresh": 0, "stale": 0}
            providers[prov]["count"] += 1
            ttl = get_ttl(prov)
            if time.time() - entry["timestamp"] <= ttl:
                providers[prov]["fresh"] += 1
            else:
                providers[prov]["stale"] += 1

    with _symbol_articles_lock:
        symbol_count = len(_symbol_articles)

    return {
        "total_entries": total_entries,
        "symbol_articles_cached": symbol_count,
        "providers": providers,
        "telemetry": get_refresh_telemetry(),
    }
