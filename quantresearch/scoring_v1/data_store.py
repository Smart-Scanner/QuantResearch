"""
data_store.py — the shared "ready data layer" for the two-stage architecture.
================================================================================
ADDITIVE. Stage-1 (ingestion) WRITES here; Stage-2 (research) READS here with ZERO
external network. Personal-use; PG-only (bootstrap.require_pg upstream).

Tables / sources in the consolidated store (5 total; 3 already existed):
  * daily_bars        price/OHLCV/delivery/turnover   (exists)
  * index_bars        benchmark/index series          (exists)
  * universe_catalog  metadata + Dhan fundamentals     (exists; pe/pb/roe/roce/eps/
                      div_yield/industry_pe enriched via the bulk Dhan fetchdt)
  * earnings_store    raw Dhan-forecast {actuals,estimates} per ISIN   (NEW, here)
  * (fundamentals are read straight from universe_catalog — already Dhan-enriched,
     so we do NOT duplicate them into a separate table.)

earnings_store stores the RAW Dhan forecast JSON per ISIN (as_of-independent), so the
existing earnings map (earnings_adapter -> dhan_forecast.build_dhan_earnings) is applied
unchanged on read => v1 scores stay BYTE-IDENTICAL after the file-cache -> DB migration.
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger("screener")

_DDL_EARNINGS = """
CREATE TABLE IF NOT EXISTS earnings_store (
    isin       TEXT PRIMARY KEY,
    raw_json   TEXT,
    source     TEXT,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""


def ensure_tables() -> None:
    """Idempotent DDL — safe to call on every ingestion run."""
    import db
    db.execute_db(_DDL_EARNINGS, require_pg=True)


# ─── earnings_store (raw Dhan forecast per ISIN) ─────────────────────────────

def put_earnings_raw(isin: str, raw: dict, source: str = "dhan_forecast") -> bool:
    """Upsert the raw forecast JSON for an ISIN. Returns True on write."""
    if not isin or not raw:
        return False
    import db
    db.execute_db(
        "INSERT INTO earnings_store (isin, raw_json, source, fetched_at) "
        "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT (isin) DO UPDATE SET "
        "raw_json = EXCLUDED.raw_json, source = EXCLUDED.source, fetched_at = CURRENT_TIMESTAMP",
        (isin, json.dumps(raw), source), require_pg=True,
    )
    return True


_EARN_CACHE: dict | None = None  # None = cold; dict = warm (bulk-loaded for a scan)


def warm_earnings_cache(isins) -> int:
    """Bulk-load earnings_store for many ISINs into a process cache (ONE query/chunk),
    so per-symbol get_earnings_raw is local-instant during a scan. Byte-identical: the
    cached value is the same parsed raw the per-ISIN query would return."""
    global _EARN_CACHE
    import db
    _EARN_CACHE = {}
    syms = [i for i in (isins or []) if i]
    CHUNK = 800
    for i in range(0, len(syms), CHUNK):
        chunk = syms[i:i + CHUNK]
        ph = ",".join(["?"] * len(chunk))
        rows = db.execute_db(f"SELECT isin, raw_json FROM earnings_store WHERE isin IN ({ph})",
                            tuple(chunk), fetch="all", require_pg=True) or []
        for r in rows:
            try:
                _EARN_CACHE[r["isin"]] = json.loads(r["raw_json"]) if r.get("raw_json") else None
            except Exception:
                _EARN_CACHE[r["isin"]] = None
    return len(_EARN_CACHE)


def clear_earnings_cache() -> None:
    global _EARN_CACHE
    _EARN_CACHE = None


def get_earnings_raw(isin: str) -> dict | None:
    """Read the raw forecast JSON for an ISIN (Stage-2 path; local DB, no network)."""
    if not isin:
        return None
    if _EARN_CACHE is not None:           # warm: bulk cache hit (no per-ISIN query)
        return _EARN_CACHE.get(isin)
    import db
    row = db.execute_db("SELECT raw_json FROM earnings_store WHERE isin = ?",
                        (isin,), fetch="one", require_pg=True)
    if row and row.get("raw_json"):
        try:
            return json.loads(row["raw_json"])
        except Exception:
            return None
    return None


def earnings_coverage() -> dict:
    import db
    r = db.execute_db("SELECT COUNT(*) c FROM earnings_store", fetch="one", require_pg=True)
    return {"rows": (r.get("c") if r else 0)}


# ─── fundamentals (read from the already-Dhan-enriched universe_catalog) ──────

_FUND_COLS = ("pe", "pb", "roe", "roce", "eps", "div_yield", "industry_pe",
              "revenue", "free_cash_flow", "net_profit_margin",
              "debt_to_equity", "promoter_pct")


def get_fundamentals(symbol: str) -> dict | None:
    """Fundamentals for a symbol from universe_catalog (Dhan-enriched). Local DB read."""
    if not symbol:
        return None
    import db
    cols = ", ".join(_FUND_COLS)
    row = db.execute_db(
        f"SELECT symbol, company_name, isin, market_cap, sector, dhan_sid, {cols} "
        f"FROM universe_catalog WHERE UPPER(symbol) = ?",
        (symbol.upper().replace(".NS", ""),), fetch="one", require_pg=True,
    )
    return dict(row) if row else None
