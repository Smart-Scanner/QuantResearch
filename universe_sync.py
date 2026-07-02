"""
universe_sync.py — Phase 1: Build Clean EQ Universe from Angel ScripMaster
============================================================================
Reads angel_tokens.json → filters out ETF/NAV/MF/Rights/Warrants → deduplicates
NSE+BSE → writes clean EQ-only universe to DB.

Run frequency: Once at boot + once every 2 months (or manually triggered).
"""

import json
import logging
import re
from pathlib import Path
from datetime import datetime

log = logging.getLogger("screener")

TOKEN_FILE = Path(__file__).parent / "cache" / "angel_tokens.json"

# ─── Filtering Constants ────────────────────────────────────────────────────

# Exact symbols known to be non-equity (whitelist approach for tricky names)
_KNOWN_NON_EQ = frozenset({
    "LIQUIDBEES", "NIFTYBEES", "BANKBEES", "GOLDBEES", "JUNIORBEES",
    "CPSEETF", "MOM100", "MOM30IETF", "MOMENTUM",
})

# Symbols that LOOK like non-equity but are actually valid EQ stocks
_FALSE_POSITIVE_EQ = frozenset({
    "NAVKARCORP", "NAVINFLUOR", "NAVNETEDUL", "NAVIGATIONCORP",
    "FUNDSMITH",  # not real but protect pattern
})

# Suffix/pattern based filtering for symbol names
_NON_EQ_SUFFIX_PATTERNS = [
    # INAV / NAV tracking (must check BEFORE general NAV)
    re.compile(r'.*INAV$', re.IGNORECASE),
    # SETF (Nippon/SBI ETF tracking values)
    re.compile(r'^SETF', re.IGNORECASE),
    # ETF patterns
    re.compile(r'.*BEES$', re.IGNORECASE),
    re.compile(r'.*ETF\d*$', re.IGNORECASE),
    re.compile(r'.*IETF$', re.IGNORECASE),
    # NAV suffix (but NOT NAVKARCORP etc)
    re.compile(r'.*NAV$', re.IGNORECASE),
    # Rights / Warrants / Debentures
    re.compile(r'.*-RE$', re.IGNORECASE),
    re.compile(r'.*-RR$', re.IGNORECASE),
    re.compile(r'.*-RI$', re.IGNORECASE),
    re.compile(r'.*-WR$', re.IGNORECASE),
    re.compile(r'.*-W\d$', re.IGNORECASE),
    re.compile(r'.*-DB$', re.IGNORECASE),
    re.compile(r'.*-BD$', re.IGNORECASE),
    re.compile(r'.*-PP$', re.IGNORECASE),
    re.compile(r'.*-NCD$', re.IGNORECASE),
    # SME / Suspended
    re.compile(r'.*-SM$', re.IGNORECASE),
    re.compile(r'.*-BE$', re.IGNORECASE),
    re.compile(r'.*-BZ$', re.IGNORECASE),
    re.compile(r'.*-BT$', re.IGNORECASE),
    re.compile(r'.*-SME$', re.IGNORECASE),
]


def _classify_symbol(symbol: str) -> str:
    """
    Classify a symbol into instrument type.
    Returns: 'EQ', 'ETF', 'NAV', 'RIGHTS', 'WARRANT', 'DEBT', 'SME', 'FILTERED'
    """
    sym = symbol.upper().strip()

    # 1. Check known non-EQ list first
    if sym in _KNOWN_NON_EQ:
        if "ETF" in sym or "BEES" in sym:
            return "ETF"
        return "NAV"

    # 2. Protect known false-positive EQ stocks
    if sym in _FALSE_POSITIVE_EQ:
        return "EQ"

    # 3. Pattern matching
    for pattern in _NON_EQ_SUFFIX_PATTERNS:
        if pattern.match(sym):
            # Determine type from pattern
            if "INAV" in sym or "NAV" == sym[-3:]:
                return "NAV"
            if "ETF" in sym or "BEES" in sym or "SETF" in sym or "IETF" in sym:
                return "ETF"
            if any(x in sym for x in ["-RE", "-RR", "-RI", "RIGHTS"]):
                return "RIGHTS"
            if any(x in sym for x in ["-WR", "-W1", "-W2"]):
                return "WARRANT"
            if any(x in sym for x in ["-DB", "-BD", "-NCD", "-PP"]):
                return "DEBT"
            if any(x in sym for x in ["-SM", "-SME"]):
                return "SME"
            if any(x in sym for x in ["-BE", "-BZ", "-BT"]):
                return "SUSPENDED"
            return "FILTERED"

    return "EQ"


def sync_universe(force: bool = False) -> dict:
    """
    Build clean EQ-only universe from angel_tokens.json.

    Steps:
    1. Load all symbols from angel_tokens.json
    2. Classify each symbol (EQ, ETF, NAV, MF, etc.)
    3. Upsert ALL into universe_catalog with correct instrument_type
    4. Deactivate delisted symbols
    
    Returns: dict with counts
    """
    import db

    log.info("[UniverseSync] Starting universe sync...")

    # 1. Load angel_tokens
    if not TOKEN_FILE.exists():
        log.error("[UniverseSync] angel_tokens.json not found at %s", TOKEN_FILE)
        # Try to refresh
        try:
            import live_feed
            live_feed.refresh_token_map()
        except Exception as exc:
            log.error("[UniverseSync] Failed to refresh tokens: %s", exc)
            return {"error": str(exc)}

    try:
        tokens = json.loads(TOKEN_FILE.read_text())
    except Exception as exc:
        log.error("[UniverseSync] Failed to read angel_tokens.json: %s", exc)
        return {"error": str(exc)}

    log.info("[UniverseSync] Loaded %d symbols from angel_tokens.json", len(tokens))

    # 2. Classify all symbols
    classified = {"EQ": [], "ETF": [], "NAV": [], "RIGHTS": [], "WARRANT": [],
                  "DEBT": [], "SME": [], "SUSPENDED": [], "FILTERED": []}

    for symbol, token in tokens.items():
        inst_type = _classify_symbol(symbol)
        classified.setdefault(inst_type, []).append(symbol)

    eq_count = len(classified.get("EQ", []))
    non_eq_summary = {k: len(v) for k, v in classified.items() if k != "EQ" and v}

    log.info("[UniverseSync] Classification: EQ=%d | %s",
             eq_count,
             " | ".join(f"{k}={v}" for k, v in sorted(non_eq_summary.items())))

    # 3. Upsert into universe_catalog (ALL symbols, with correct type)
    batch = []
    for inst_type, symbols in classified.items():
        for symbol in symbols:
            batch.append({
                "symbol": symbol,
                "company_name": symbol,  # Will be enriched later by bhavcopy
                "market_cap": 0,
                "market_cap_bucket": "Unknown Cap",
                "sector": "",
                "industry": "",
                "is_active": inst_type == "EQ",  # Only EQ stocks are active
                "instrument_type": inst_type,
                "exchange": "NSE",
            })

            # Upsert in batches of 100
            if len(batch) >= 100:
                db.upsert_universe_catalog(batch, set_synced_at=False)
                batch = []

    if batch:
        db.upsert_universe_catalog(batch, set_synced_at=False)

    # 4. Deactivate symbols NOT in angel_tokens (delisted)
    all_token_symbols = set(tokens.keys())
    try:
        existing = db.execute_db(
            "SELECT symbol FROM universe_catalog WHERE is_active = TRUE",
            fetch="all"
        )
        deactivated = 0
        if existing:
            for row in existing:
                sym = row.get("symbol", "")
                if sym and sym not in all_token_symbols:
                    db.execute_db(
                        "UPDATE universe_catalog SET is_active = FALSE WHERE symbol = ?",
                        (sym,)
                    )
                    deactivated += 1
        if deactivated:
            log.info("[UniverseSync] Deactivated %d delisted symbols", deactivated)
    except Exception as exc:
        log.warning("[UniverseSync] Deactivation check failed (non-fatal): %s", exc)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    result = {
        "total_loaded": len(tokens),
        "eq_count": eq_count,
        "non_eq": non_eq_summary,
        "synced_at": now,
    }

    # Update meta
    db.set_meta("universe_sync_completed_at", now)
    db.set_meta("universe_sync_eq_count", str(eq_count))
    db.set_meta("universe_sync_total", str(len(tokens)))

    log.info("[UniverseSync] ✅ Complete: %d EQ stocks in universe_catalog (total=%d)",
             eq_count, len(tokens))

    return result


def get_eq_symbols() -> list:
    """Get all active EQ symbols from universe_catalog."""
    import db
    rows = db.execute_db(
        "SELECT symbol FROM universe_catalog WHERE is_active = TRUE AND instrument_type = 'EQ' ORDER BY symbol",
        fetch="all"
    )
    return [r["symbol"] for r in rows] if rows else []
