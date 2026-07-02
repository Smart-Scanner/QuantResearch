"""
sector_map.py - Map each stock's sector to an NSE sector-index name.
====================================================================
ADDITIVE module. It maps our custom sector names (stocks.SECTORS) to the
EXACT NSE index_name strings stored in the broker-free `index_bars` table
(populated by bhavcopy_history.fetch_ind_close_all, which preserves NSE's
"Index Name" column verbatim from ind_close_all_*.csv).

The output of get_sector_index_name(symbol) is fed directly to
pit_loader.load_index_series(index_name, as_of_date), which becomes the
engine's `sector_idx[symbol]` (a date-ascending pandas.Series of that
sector index's close). The locked engine uses it ONLY for relative-strength
(sector_rs). When no NSE sector index sensibly matches our sector, we map to
None; the engine then treats sector_rs as NEUTRAL (no look-ahead, no
fabrication).

DESIGN RULES (per task mandate)
-------------------------------
1. Only map where the NSE sectoral / thematic index CLEARLY matches the
   sector. No wild guessing. Ambiguous / no-good-fit sectors -> None.
2. The mapped strings are EXACT NSE ind_close_all "Index Name" values, so
   load_index_series can find rows in index_bars without normalization.
3. ROLLBACK-SAFE: READ-ONLY of stocks.SECTORS. No writes, no live imports
   beyond stocks (the sector map), no broker code.

WHY THESE NSE INDICES
---------------------
NSE publishes (in ind_close_all) sectoral indices: Nifty Auto, Nifty Bank,
Nifty Financial Services, Nifty FMCG, Nifty IT, Nifty Media, Nifty Metal,
Nifty Pharma, Nifty Healthcare Index, Nifty Consumer Durables,
Nifty Oil & Gas, Nifty Realty, Nifty Private Bank, Nifty PSU Bank; plus
thematic indices: Nifty Energy, Nifty Infrastructure, Nifty PSE,
Nifty Commodities, Nifty Consumption, Nifty Services Sector. We map only to
these where the fit is unambiguous.
"""

from __future__ import annotations

import logging

log = logging.getLogger("screener")


# ───────────────────── our-sector -> NSE index_name ─────────────────────
#
# Value is the EXACT NSE "Index Name" (as stored in index_bars), or None
# when no NSE sector index sensibly represents the sector. Rationale per
# entry is in the inline comment.

#
# REVIEWED + APPROVED against the REAL production PG `index_bars` (160 distinct
# NSE indices). Every non-None string below was confirmed to exist VERBATIM in
# index_bars (SELECT DISTINCT index_name). The original blind map (built when the
# subagent could only reach the local SQLite fallback) was upgraded here: 3 loose
# fits sharpened (Industrial/Power/Construction) + 11 sectors that DO have a
# dedicated NSE index were added (Chemicals/Defence/Cement/Insurance/Logistics/
# Retail/Telecom/Railways) + Hotels/Travel/Aviation folded into Nifty India
# Tourism. Coverage 73% -> ~91%. Remaining None sectors have no clean NSE index
# and are left NEUTRAL by the engine (safe, no fabrication).

SECTOR_TO_NSE_INDEX = {
    # --- direct, unambiguous NSE sectoral indices ---
    "Auto":               "Nifty Auto",                       # NSE Auto sectoral
    "Banking":            "Nifty Bank",                       # NSE Bank sectoral
    "Finance":            "Nifty Financial Services",         # NBFCs/AMCs/exchanges -> Fin Services
    "FMCG":               "Nifty FMCG",                       # NSE FMCG sectoral
    "IT":                 "Nifty IT",                         # NSE IT sectoral
    "Media":              "Nifty Media",                      # NSE Media sectoral
    "Metals":             "Nifty Metal",                      # NSE Metal sectoral
    "Mining":             "Nifty Metal",                      # COALINDIA/MOIL/etc. tracked under Metal
    "Pharma":             "Nifty Pharma",                     # NSE Pharma sectoral
    "Healthcare":         "Nifty Healthcare Index",           # hospitals/diagnostics -> Healthcare
    "Realty":             "Nifty Realty",                     # NSE Realty sectoral
    "Energy":             "Nifty Oil & Gas",                  # refiners/oil/gas distributors
    "Power":              "Nifty Power",                       # UPGRADED: dedicated Nifty Power (was Nifty Energy)
    "Consumer":           "Nifty Consumer Durables",          # durables/QSR/apparel -> Cons Durables
    "Infra":              "Nifty Infrastructure",             # ports/roads/infra
    "Construction":       "Nifty Construction",               # UPGRADED: dedicated Nifty Construction (was Infrastructure)
    "Industrial":         "Nifty Capital Goods",              # UPGRADED: capital-goods/industrials (was Commodities)
    "Agri":               "Nifty Commodities",                # fertilisers/agri-inputs -> Commodities (no NSE agri index)

    # --- ADDED: sectors with a dedicated NSE index present in index_bars ---
    "Chemicals":          "Nifty Chemicals",                  # ADDED: dedicated NSE Chemicals
    "Defence":            "Nifty India Defence",              # ADDED: dedicated NSE Defence thematic
    "Cement":             "Nifty Cement",                     # ADDED: dedicated NSE Cement
    "Insurance":          "Nifty Insurance",                  # ADDED: dedicated NSE Insurance
    "Logistics":          "Nifty Transportation & Logistics", # ADDED: dedicated NSE Transport & Logistics
    "Retail":             "Nifty Retail",                     # ADDED: dedicated NSE Retail
    "Telecom":            "Nifty Telecommunications",         # ADDED: dedicated NSE Telecom
    "Railways":           "Nifty India Railways PSU",         # ADDED: dedicated NSE Railways PSU
    "Hotels":             "Nifty India Tourism",              # ADDED (judgment): tourism thematic
    "Travel":             "Nifty India Tourism",              # ADDED (judgment): tourism thematic
    "Aviation":           "Nifty India Tourism",              # ADDED (judgment): tourism thematic

    # --- sectors with NO clean NSE sector index -> None (neutral sector_rs) ---
    "Building Materials": None,   # pipes/tiles/plywood mix, no dedicated NSE index
    "Diversified":        None,   # conglomerates span sectors
    "Education":          None,   # no NSE education index
    "Electronics":        None,   # EMS/electronics has no NSE sectoral index
    "Packaging":          None,   # no NSE packaging index
    "Paints":             None,   # distinct from bulk chemicals; no standalone NSE paints index
    "Paper":              None,   # no NSE paper index
    "Renewable":          None,   # renewables don't track Nifty Power well; no clean NSE index
    "Staffing":           None,   # no NSE staffing index
    "Textiles":           None,   # no NSE textiles index
}


# ───────── Dhan-sector -> NSE-index FALLBACK (Option A; user-signed-off) ─────────
# Used ONLY when a symbol is ABSENT from stocks.SECTORS (strict order: Map A first,
# this Map B only on miss -> the two stock-sets are DISJOINT, so no symbol resolves
# via both). Source = universe_catalog.sector (Dhan), populated by the fixed
# enrich_market_cap_batch. Buckets verified: Healthcare=20/20 drug-makers -> Pharma;
# Capital Goods family -> Capital Goods; Capital Markets -> Nifty Capital Markets.
DHAN_SECTOR_TO_NSE_INDEX = {
    "Information Technology": "Nifty IT", "Automobiles": "Nifty Auto",
    "Chemicals": "Nifty Chemicals", "Financial Services": "Nifty Financial Services",
    "Banks": "Nifty Bank", "Construction": "Nifty Construction", "Realty": "Nifty Realty",
    "Media": "Nifty Media", "Metals & Mining": "Nifty Metal", "Steel": "Nifty Metal",
    "FMCG": "Nifty FMCG", "Food Products": "Nifty FMCG", "Beverages": "Nifty FMCG",
    "Power": "Nifty Power", "Utilities": "Nifty Power",
    "Logistics & Cargo": "Nifty Transportation & Logistics",
    "Transport": "Nifty Transportation & Logistics",
    "Transport Services": "Nifty Transportation & Logistics",
    "Telecom": "Nifty Telecommunications", "Oil & Gas": "Nifty Oil & Gas",
    "Petroleum Products": "Nifty Oil & Gas", "Aerospace & Defense": "Nifty India Defence",
    "Insurance": "Nifty Insurance", "Retail": "Nifty Retail",
    "Healthcare": "Nifty Pharma", "Healthcare Services": "Nifty Healthcare Index",
    "Capital Goods": "Nifty Capital Goods", "Industrial Products": "Nifty Capital Goods",
    "Capital Goods - Electrical Equipment": "Nifty Capital Goods",
    "Castings, Forgings & Fastners": "Nifty Capital Goods", "Cables": "Nifty Capital Goods",
    "Engineering Services": "Nifty Capital Goods", "Consumer Durables": "Nifty Consumer Durables",
    "Capital Markets": "Nifty Capital Markets", "Consumer Services": "Nifty Consumer Services",
    "Leisure Services": "Nifty India Tourism", "Aviation": "Nifty India Tourism",
    # deliberately None (no clean NSE index / ambiguous -> neutral sector-RS):
    "Consumer Goods": None, "Commercial Services": None, "Textiles": None,
    "Forest Materials": None, "Diversified": None, "Energy": None,
    "Diamond, Gems and Jewellery": None, "Printing & Stationery": None,
    "Packaging": None, "Trading": None, "Services": None, "Education": None,
}

_catalog_sector_cache = None
_conflict_mapa_cache = None
_conflict_warned = set()


def _conflict_mapa() -> dict:
    """symbol(upper) -> historical Map-A index, from the inert de-conflict audit CSV
    (sector_deconflict_pairs.csv). Used only by the runtime guard."""
    global _conflict_mapa_cache
    if _conflict_mapa_cache is None:
        _conflict_mapa_cache = {}
        try:
            import csv as _csv, os as _os
            p = _os.path.join(_os.path.dirname(__file__), "sector_deconflict_pairs.csv")
            if _os.path.exists(p):
                for row in _csv.DictReader(open(p, encoding="utf-8")):
                    _conflict_mapa_cache[row["symbol"].upper()] = row["map_a_index"]
        except Exception as exc:  # pragma: no cover
            log.debug("[sector_map] conflict-audit load failed: %s", exc)
    return _conflict_mapa_cache


def _guard_conflict(symbol: str, dhan_index):
    """Warn ONCE if a symbol now resolves via the Dhan fallback but was a known
    Map-A conflict — i.e. it was REMOVED from stocks.SECTORS, which would silently
    change its sector-RS (14% factor) index. Future-proofing; no-op today (those
    symbols are still in stocks.SECTORS and resolve via Map A)."""
    key = (symbol or "").upper()
    if key in _conflict_warned:
        return
    a = _conflict_mapa().get(key)
    if a and a != dhan_index:
        _conflict_warned.add(key)
        log.warning("[sector_map] %s now resolves via Dhan fallback -> %s, but was historically "
                    "mapped -> %s (removed from stocks.SECTORS?). Verify sector-RS.",
                    symbol, dhan_index, a)


def _catalog_sectors() -> dict:
    """symbol(upper) -> Dhan sector from universe_catalog (loaded once, cached)."""
    global _catalog_sector_cache
    if _catalog_sector_cache is None:
        try:
            import db
            rows = db.execute_db(
                "SELECT symbol, sector FROM universe_catalog "
                "WHERE sector IS NOT NULL AND sector <> ''", fetch="all"
            ) or []
            _catalog_sector_cache = {r["symbol"].upper(): r["sector"] for r in rows}
        except Exception as exc:  # pragma: no cover
            log.warning("[sector_map] catalog sector load failed: %s", exc)
            _catalog_sector_cache = {}
    return _catalog_sector_cache


def _import_sectors():
    """Lazy READ-ONLY import of the our-sector map (symbol -> sector name)."""
    try:
        import stocks
        return getattr(stocks, "SECTORS", {}) or {}
    except Exception as exc:  # pragma: no cover - defensive
        log.error("[sector_map] could not import stocks.SECTORS: %s", exc)
        return {}


def get_sector_index_name(symbol: str):
    """
    Return the EXACT NSE index_name for `symbol`'s sector, or None.

    None means: unknown symbol, unknown sector, or a sector we deliberately
    left unmapped (engine then treats sector_rs as neutral). The returned
    string, when not None, is suitable as a direct argument to
    pit_loader.load_index_series(...).
    """
    if not symbol:
        return None
    sectors = _import_sectors()
    # Map A FIRST: if the symbol is labelled in stocks.SECTORS, that is authoritative
    # (even a deliberate None). Strict order -> a labelled symbol NEVER uses Dhan.
    if symbol in sectors:
        return SECTOR_TO_NSE_INDEX.get(sectors[symbol], None)
    # Map B FALLBACK (only on miss): Dhan catalog sector -> NSE index.
    ds = _catalog_sectors().get(symbol.upper())
    if ds:
        b = DHAN_SECTOR_TO_NSE_INDEX.get(ds, None)
        _guard_conflict(symbol, b)  # warn if this symbol was a known Map-A conflict (future-proofing)
        return b
    return None


def coverage_summary():
    """
    Build a human-review coverage report of the mapping.

    Returns a dict:
        {
          "total_symbols": int,
          "mapped_symbols": int,
          "unmapped_symbols": int,
          "sectors_total": int,
          "sectors_mapped": int,
          "sectors_unmapped": int,
          "per_sector": { sector: {"nse_index": str|None, "count": int} },
          "unmapped_sectors": [sector, ...],
          "unmapped_symbol_list": [symbol, ...],
        }
    """
    sectors = _import_sectors()
    per_sector = {}
    for symbol, sector in sectors.items():
        nse = SECTOR_TO_NSE_INDEX.get(sector, None)
        bucket = per_sector.setdefault(
            sector, {"nse_index": nse, "count": 0}
        )
        bucket["count"] += 1

    mapped_symbols = sum(
        b["count"] for b in per_sector.values() if b["nse_index"]
    )
    unmapped_symbols = sum(
        b["count"] for b in per_sector.values() if not b["nse_index"]
    )
    unmapped_sectors = sorted(
        s for s, b in per_sector.items() if not b["nse_index"]
    )
    unmapped_symbol_list = sorted(
        sym for sym, sec in sectors.items()
        if not SECTOR_TO_NSE_INDEX.get(sec, None)
    )

    return {
        "total_symbols": len(sectors),
        "mapped_symbols": mapped_symbols,
        "unmapped_symbols": unmapped_symbols,
        "sectors_total": len(per_sector),
        "sectors_mapped": sum(1 for b in per_sector.values() if b["nse_index"]),
        "sectors_unmapped": sum(1 for b in per_sector.values() if not b["nse_index"]),
        "per_sector": per_sector,
        "unmapped_sectors": unmapped_sectors,
        "unmapped_symbol_list": unmapped_symbol_list,
    }


if __name__ == "__main__":  # pragma: no cover - manual review aid
    import json
    print(json.dumps(coverage_summary(), indent=2, sort_keys=True))
