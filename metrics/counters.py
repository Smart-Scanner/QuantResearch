"""
Phase 11: Application-wide counters for observability.

Usage:
    from metrics import counters
    counters.inc("yf_calls")
    counters.inc("symbols_processed", by=50)
    counters.set_val("cache_hit_rate", 0.85)
    all_metrics = counters.get_all()
"""
import threading
import time
import logging

log = logging.getLogger("screener")

_lock = threading.Lock()
_counters: dict[str, int | float] = {}
_start_time = time.time()


def inc(key: str, by: int = 1):
    """Increment a counter."""
    with _lock:
        _counters[key] = _counters.get(key, 0) + by


def set_val(key: str, val):
    """Set a gauge value."""
    with _lock:
        _counters[key] = val


def get(key: str, default=0):
    """Get a single counter value."""
    with _lock:
        return _counters.get(key, default)


def get_all() -> dict:
    """Get all metrics with derived values."""
    with _lock:
        result = dict(_counters)

    # Derived metrics
    hits = result.get("cache_hits", 0)
    misses = result.get("cache_misses", 0)
    total = hits + misses
    result["cache_hit_rate"] = round(hits / total, 3) if total > 0 else 0.0

    fast_runs = result.get("fast_scan_runs", 0)
    fast_ms = result.get("fast_scan_total_ms", 0)
    result["avg_fast_scan_ms"] = round(fast_ms / fast_runs) if fast_runs > 0 else 0

    deep_runs = result.get("deep_scan_runs", 0)
    deep_ms = result.get("deep_scan_total_ms", 0)
    result["avg_deep_scan_ms"] = round(deep_ms / deep_runs) if deep_runs > 0 else 0

    result["uptime_seconds"] = round(time.time() - _start_time)

    return result


def reset():
    """Reset all counters (for testing)."""
    with _lock:
        _counters.clear()
