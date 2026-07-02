"""
universe.py — Smart Stock Universe Manager (Phase 1.5)

Provides ACTIVE_UNIVERSE for the scanner based on layered filters:

  Layer 1 — NSE F&O universe (~200 most liquid stocks, always reliable data)
  Layer 2 — Curated sector universe (stocks.py _HARDCODED_UNIVERSE, ~573 stocks)
  Layer 3 — User portfolio holdings (OPEN positions in DB)
  Layer 4 — User custom watchlist stocks

ACTIVE_UNIVERSE = union of all enabled layers, deduplicated and sorted.

Environment Variables:
  FULL_UNIVERSE=0  (default) — curated ~573 stocks
  FULL_UNIVERSE=1            — all tokens from angel_tokens.json (2200+)

Output file: cache/active_universe.json
  Versioned structure with version=5, count, updated_at, symbols.
"""

import os
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Set

log = logging.getLogger("screener")

# Paths
ACTIVE_FILE = Path(__file__).parent / "cache" / "active_universe.json"

# Layer enable flags — all True by default
ENABLE_FNO_UNIVERSE = True
ENABLE_SECTOR_UNIVERSE = True
ENABLE_PORTFOLIO_STOCKS = True
ENABLE_CUSTOM_STOCKS = True

# ─── NSE F&O Universe (~200 most liquid, most reliable data) ───
# These are the stocks where Angel One / jugaad_data data quality
# is most consistent and volume ensures valid technical signals.

FNO_UNIVERSE: Set[str] = {
    # Nifty 50 core
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR",
    "SBIN", "BHARTIARTL", "KOTAKBANK", "ITC", "LT", "AXISBANK",
    "BAJFINANCE", "ASIANPAINT", "MARUTI", "HCLTECH", "SUNPHARMA",
    "TITAN", "WIPRO", "ULTRACEMCO", "NTPC", "POWERGRID", "NESTLEIND",
    "TECHM", "ONGC", "TATASTEEL", "JSWSTEEL", "INDUSINDBK", "HINDALCO",
    "ADANIENT", "ADANIPORTS", "BAJAJFINSV", "GRASIM", "CIPLA", "DRREDDY",
    "COALINDIA", "BPCL", "EICHERMOT", "DIVISLAB", "BRITANNIA",
    "APOLLOHOSP", "HEROMOTOCO", "SBILIFE", "DABUR", "HDFCLIFE",
    "BAJAJ-AUTO", "TATACONSUM", "PIDILITIND", "SIEMENS", "ADANIGREEN",
    # Nifty Next 50 / F&O liquids
    "HAVELLS", "AMBUJACEM", "DLF", "GODREJCP", "TRENT", "VEDL",
    "BANKBARODA", "IOC", "ICICIPRULI", "INDIGO", "ABB", "SRF",
    "NAUKRI", "TORNTPHARM", "GAIL", "PIIND", "HINDPETRO", "MARICO",
    "PERSISTENT", "TATAPOWER", "COLPAL", "CANBK", "BERGEPAINT",
    "MPHASIS", "PFC", "RECLTD", "IDFCFIRSTB", "LUPIN", "AUROPHARMA",
    "VOLTAS", "POLYCAB", "SHREECEM", "TVSMOTOR", "SAIL", "MRF",
    "FEDERALBNK", "CUMMINSIND", "CONCOR", "PETRONET", "ABCAPITAL",
    "NMDC", "JUBLFOOD", "COFORGE", "ALKEM", "ASTRAL", "MAXHEALTH",
    "OFSS", "IRCTC", "CROMPTON", "BHARATFORG", "LICHSGFIN", "AUBANK",
    "DEEPAKNTR", "DIXON", "ESCORTS", "BIOCON", "BALKRISIND",
    "LTTS", "KPITTECH", "SYNGENE", "BEL", "SUPREMEIND", "PAGEIND",
    "HINDCOPPER", "PHOENIXLTD", "ZYDUSLIFE", "UBL", "RAMCOCEM",
    "TATAELXSI", "BATAINDIA", "THERMAX", "SUNDARMFIN", "SUNTV",
    "MUTHOOTFIN", "M&MFIN", "YESBANK", "MANAPPURAM", "IEX",
    "NATIONALUM", "SONACOMS", "METROPOLIS", "FORTIS", "KAJARIACER",
    "CHOLAFIN", "HAL", "BDL", "MAZDOCK", "NHPC", "SJVN", "IRFC",
    "JSWENERGY", "CGPOWER", "KAYNES", "SUZLON", "GODREJPROP",
    "CANFINHOME", "ANGELONE", "PVRINOX", "BLUEDART", "DATAPATTNS",
    "LALPATHLAB", "AFFLE", "ZEEL", "ABFRL", "CHAMBLFERT",
    "IDEA", "M&M", "TATAMOTORS", "JINDALSTEL",
}


# ─── Public API ───

def get_active_universe(
    include_portfolio: bool = True,
    include_custom: bool = True,
) -> list:
    """
    Returns the ACTIVE_UNIVERSE as a sorted, deduplicated list.

    Priority layers (union — all included, no hierarchy):
      1. F&O universe (always, most liquid NSE stocks)
      2. _HARDCODED_UNIVERSE from stocks.py (curated 573 NSE stocks)
      3. Open portfolio positions (if include_portfolio=True)
      4. Custom watchlist stocks (if include_custom=True)
    """
    active: Set[str] = set()

    if ENABLE_FNO_UNIVERSE:
        active.update(FNO_UNIVERSE)

    if ENABLE_SECTOR_UNIVERSE:
        try:
            from stocks import _HARDCODED_UNIVERSE
            active.update(_HARDCODED_UNIVERSE)
        except Exception as exc:
            log.warning("Failed to load _HARDCODED_UNIVERSE from stocks.py: %s", exc)

    if include_portfolio and ENABLE_PORTFOLIO_STOCKS:
        try:
            import db
            positions = db.execute_db(
                "SELECT DISTINCT symbol FROM positions WHERE status='OPEN'",
                fetch="all"
            )
            if positions:
                active.update(r["symbol"] for r in positions if r.get("symbol"))
        except Exception as exc:
            log.debug("Portfolio universe layer failed (non-fatal): %s", exc)

    if include_custom and ENABLE_CUSTOM_STOCKS:
        try:
            import db
            customs = db.get_custom_stocks()
            active.update(s["symbol"] for s in customs if s.get("symbol"))
        except Exception as exc:
            log.debug("Custom stocks universe layer failed (non-fatal): %s", exc)

    return sorted(active)


def get_fast_scan_universe() -> list:
    """
    Return the universe to use for fast scans.

    Phase 5.5: If USE_UNIVERSE_ENGINE is active, prefer eligible_universe table.
    Default (FULL_UNIVERSE=0): curated ~573 stocks from get_active_universe().
    Override (FULL_UNIVERSE=1): all tokens from angel_tokens.json (2200+).

    Always appends open portfolio positions and custom stocks regardless of mode.
    """
    # Phase 5.5: Eligible universe takes priority
    from config import USE_UNIVERSE_ENGINE
    if USE_UNIVERSE_ENGINE:
        try:
            import db
            eligible = db.get_eligible_universe()
            if eligible and len(eligible) >= 100:
                symbols = [r["symbol"] for r in eligible]
                log.info("Universe Engine: %d eligible stocks from eligible_universe", len(symbols))
                return symbols
            log.warning("Universe Engine: eligible_universe too small (%d), falling back",
                        len(eligible) if eligible else 0)
        except Exception as exc:
            log.warning("Universe Engine fallback: %s", exc)

    if os.getenv("FULL_UNIVERSE", "0") == "1":
        log.info("FULL_UNIVERSE=1: loading all angel_tokens symbols")
        try:
            import db
            # Try loading the fully populated universe from the new catalog
            catalog_rows = db.execute_db("SELECT symbol FROM universe_catalog WHERE is_active = TRUE", fetch="all")
            if catalog_rows and len(catalog_rows) > 1000:
                full_catalog = [r["symbol"] for r in catalog_rows if r.get("symbol")]
                # Always append open portfolio positions and custom stocks
                positions = db.execute_db("SELECT DISTINCT symbol FROM positions WHERE status='OPEN'", fetch="all")
                if positions:
                    extras = {r["symbol"] for r in positions if r.get("symbol")}
                    full_catalog = list(dict.fromkeys(full_catalog + [s for s in extras if s not in set(full_catalog)]))
                customs = db.get_custom_stocks()
                if customs:
                    extras = {s["symbol"] for s in customs if s.get("symbol")}
                    full_catalog = list(dict.fromkeys(full_catalog + [s for s in extras if s not in set(full_catalog)]))
                    
                gate_limit = int(os.getenv("GATE_LIMIT", "574"))
                if len(full_catalog) > gate_limit:
                    log.info(f"Gate Enforced: Slicing catalog from {len(full_catalog)} down to {gate_limit}")
                    full_catalog = full_catalog[:gate_limit]
                    
                log.info(f"Gate 2 Active: Returning {len(full_catalog)} symbols from universe_catalog")
                return full_catalog
            
            from stocks import STOCK_UNIVERSE
            full = list(STOCK_UNIVERSE)
            # Still add portfolio + custom on top
            try:
                import db
                positions = db.execute_db(
                    "SELECT DISTINCT symbol FROM positions WHERE status='OPEN'",
                    fetch="all"
                )
                if positions:
                    extras = {r["symbol"] for r in positions if r.get("symbol")}
                    full = list(dict.fromkeys(full + [s for s in extras if s not in set(full)]))
            except Exception:
                pass
            return full
        except Exception as exc:
            log.warning("Full universe load failed, falling back to curated: %s", exc)

    return get_active_universe()


def save_active_universe(symbols: list) -> None:
    """
    Persist the resolved universe to cache/active_universe.json.
    Versioned structure (version=5) for visibility and debugging.
    """
    ACTIVE_FILE.parent.mkdir(exist_ok=True)
    payload = {
        "version": 5,
        "count": len(symbols),
        "updated_at": datetime.now().isoformat(),
        "symbols": symbols,
    }
    ACTIVE_FILE.write_text(json.dumps(payload, indent=2))
    log.debug("Active universe saved: %d symbols → %s", len(symbols), ACTIVE_FILE)


def get_universe_stats() -> dict:
    """
    Return a lightweight stats dict for /api/universe and /api/health.
    No DB calls — uses cached file if available.
    """
    cached_count = None
    cached_at = None
    if ACTIVE_FILE.exists():
        try:
            data = json.loads(ACTIVE_FILE.read_text())
            cached_count = data.get("count")
            cached_at = data.get("updated_at")
        except Exception:
            pass

    return {
        "active_count": cached_count or len(get_active_universe()),
        "fno_count": len(FNO_UNIVERSE),
        "curated_count": _curated_count(),
        "full_universe_enabled": os.getenv("FULL_UNIVERSE", "0") == "1",
        "last_updated": cached_at,
    }


def _curated_count() -> int:
    """Count of _HARDCODED_UNIVERSE without importing DB."""
    try:
        from stocks import _HARDCODED_UNIVERSE
        return len(_HARDCODED_UNIVERSE)
    except Exception:
        return 0

def get_universe_chunks(symbols: list) -> list[tuple[str, list[str]]]:
    """
    Groups symbols into chunks based on the universe_catalog.
    If universe_catalog is empty, falls back to structural Nifty50/F&O heuristics.
    NEVER uses alphabetical list splitting.
    """
    try:
        import db
        catalog_rows = db.execute_db("SELECT symbol, market_cap_bucket FROM universe_catalog WHERE is_active = TRUE", fetch="all")
        if catalog_rows:
            # We have an active catalog, use it!
            bucket_map = {row["symbol"]: row.get("market_cap_bucket", "Unknown Cap") for row in catalog_rows if row.get("symbol")}
            
            blue_chip = []
            large_cap = []
            mid_cap = []
            small_cap = []
            micro_cap = []
            unknown = []
            
            for s in symbols:
                bucket = bucket_map.get(s, "Unknown Cap").upper()
                if "BLUE" in bucket:
                    blue_chip.append(s)
                elif "LARGE" in bucket:
                    large_cap.append(s)
                elif "MID" in bucket:
                    mid_cap.append(s)
                elif "SMALL" in bucket:
                    small_cap.append(s)
                elif "MICRO" in bucket:
                    micro_cap.append(s)
                else:
                    unknown.append(s)
                    
            chunks = []
            def _add_chunk(name, arr):
                chunk_size = 100
                for i in range(0, len(arr), chunk_size):
                    slice_arr = arr[i:i+chunk_size]
                    if i == 0 and len(arr) <= chunk_size:
                        chunks.append((name, slice_arr))
                    else:
                        chunks.append((f"{name} Part {i//chunk_size + 1}", slice_arr))
                        
            if blue_chip: _add_chunk("Blue Chip", blue_chip)
            if large_cap: _add_chunk("Large Cap", large_cap)
            if mid_cap: _add_chunk("Mid Cap", mid_cap)
            if small_cap: _add_chunk("Small Cap", small_cap)
            if micro_cap: _add_chunk("Micro Cap", micro_cap)
            if unknown: _add_chunk("Unknown Cap", unknown)
            return chunks
    except Exception as exc:
        log.warning("Failed to group by universe_catalog: %s", exc)
        
    # FALLBACK: If catalog is empty or DB fails, we do NOT use alphabetical slicing.
    # We use FNO_UNIVERSE heuristic. The first 50 of FNO_UNIVERSE are basically Nifty50.
    # Next 100 are Large/Mid. The rest of the curated are Mid/Small.
    fno_list = list(FNO_UNIVERSE)
    nifty50_heuristic = set(fno_list[:50])
    fno_rest_heuristic = set(fno_list[50:])
    
    blue_chip = []
    large_mid_fno = []
    rest_universe = []
    
    for s in symbols:
        if s in nifty50_heuristic:
            blue_chip.append(s)
        elif s in fno_rest_heuristic:
            large_mid_fno.append(s)
        else:
            rest_universe.append(s)
            
    # Subdivide the rest_universe purely by count just to prevent timeouts, 
    # but label them accurately as 'Broader Market' rather than fake caps.
    chunks = []
    if blue_chip: chunks.append(("Blue Chip (Nifty 50 Proxy)", blue_chip))
    if large_mid_fno: chunks.append(("Large/Mid Cap (F&O Proxy)", large_mid_fno))
    
    # Split the rest into chunks of 100 so we don't blow up Angel One and get better thread distribution
    chunk_size = 100
    for i in range(0, len(rest_universe), chunk_size):
        chunk_slice = rest_universe[i:i+chunk_size]
        chunks.append((f"Broader Market Part {i//chunk_size + 1}", chunk_slice))
        
    return chunks
