"""Symbol validation and caching utilities.

Decoupled from route modules to prevent circular imports.
Used by both routes/pages.py and routes/api.py.
"""

import logging
from cachetools.func import ttl_cache

log = logging.getLogger("symbol_utils")


@ttl_cache(maxsize=1, ttl=300)
def get_cached_active_universe():
    """Return the active universe as a frozen set (cached 5 min, single entry)."""
    import universe
    try:
        return frozenset(universe.get_active_universe())
    except Exception as exc:
        log.warning("[SYMBOL_UTILS] Failed to load active universe: %s", exc)
        return frozenset()


def check_symbol_exists(symbol: str) -> bool:
    """Outer un-cached helper to normalize input before lookup caching."""
    normalized = (symbol or "").upper().strip()
    if not normalized:
        return False
    return _check_symbol_exists_cached(normalized)


@ttl_cache(maxsize=500, ttl=300)
def _check_symbol_exists_cached(symbol: str) -> bool:
    """Check if a symbol exists in scan results or active universe.

    Checks (in order):
    1. Direct DB lookup via get_stock()
    2. Active universe membership
    """
    import db

    # Check 1: Direct DB scan result lookup
    try:
        if db.get_stock(symbol) is not None:
            return True
    except Exception as exc:
        log.warning("[SYMBOL_UTILS] get_stock failed for %s: %s", symbol, exc)

    # Check 2: Active universe membership
    try:
        active_univ = get_cached_active_universe()
        if symbol in active_univ:
            return True
    except Exception as exc:
        log.warning("[SYMBOL_UTILS] Universe check failed for %s: %s", symbol, exc)

    return False
