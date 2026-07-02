"""
data_ingest.py — STAGE 1 (ingestion): the daily slow-I/O job.
================================================================================
ADDITIVE. Runs once daily (06:00 IST, wired in app.py). Does ALL the network work
so STAGE 2 (research) can run with ZERO external fetch.

Pipeline:
  1. candidates  = symbols with >=126 daily bars (pit_loader).
  2. BASIC GATE  = market_cap >= 1000cr AND price > 50 (cheap; from universe_catalog).
                   This only shrinks the fetch-set; per-engine gates run in Stage 2.
  3. EARNINGS    = for every gated ISIN, persist the raw Dhan forecast into the shared
                   earnings_store (DB). Seeds from the existing file-cache first (fast,
                   no network), then network-fetches the misses. Idempotent.
  4. FUNDAMENTALS= already Dhan-enriched into universe_catalog by the bulk fetchdt
                   pipeline (nse_bhavcopy); optionally refreshed best-effort here.
  5. PRICE/INDEX = daily_bars / index_bars (bhavcopy append — existing pipeline).

Reuses dhan_forecast's retry/cache pattern. Personal-use; PG-only.
"""
from __future__ import annotations

import os
import glob
import json
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger("screener")


def _basic_gated(as_of: str) -> list[dict]:
    """candidates ∩ {mcap>=1000cr AND price>50}. Returns [{symbol, isin}]."""
    import db
    from . import adapter
    cands = set(s.upper() for s in adapter.pit_loader.list_symbols_with_history(as_of))
    rows = db.execute_db(
        "SELECT symbol, isin FROM universe_catalog "
        "WHERE market_cap >= 1000 AND price > 50 AND instrument_type = 'EQ' AND is_active "
        "AND isin IS NOT NULL AND isin <> ''",
        fetch="all", require_pg=True,
    ) or []
    return [{"symbol": r["symbol"], "isin": r["isin"]}
            for r in rows if (r["symbol"] or "").upper() in cands]


def seed_earnings_from_filecache() -> int:
    """Copy any existing cache/dhan_forecast/*.json into earnings_store (no network).
    Guarantees the store's raw == the file-cache raw (byte-identical migration)."""
    from . import data_store, dhan_forecast
    data_store.ensure_tables()
    n = 0
    for fp in glob.glob(os.path.join(dhan_forecast._CACHE_DIR, "*.json")):
        isin = os.path.splitext(os.path.basename(fp))[0]
        try:
            raw = json.loads(open(fp, encoding="utf-8").read())
            if raw and raw.get("actuals") and data_store.put_earnings_raw(isin, raw, "filecache_seed"):
                n += 1
        except Exception:
            continue
    log.info("[ingest] seeded %d earnings rows from file-cache", n)
    return n


def run_ingestion(as_of: str | None = None, force: bool = False,
                  refresh_price: bool = False, refresh_fundamentals: bool = False,
                  max_workers: int = 12) -> dict:
    """Stage-1 daily ingestion. Idempotent. Returns coverage stats.

    refresh_price       -> bhavcopy_history.append_latest() (daily_bars).
    refresh_fundamentals-> nse_bhavcopy.enrich_market_cap_batch() (universe_catalog).
    Both default OFF so a bare call just (re)builds earnings_store; the 6 AM job
    passes both True for a full "fetch everything" refresh.
    """
    import db
    from . import bootstrap, data_store, dhan_forecast
    bootstrap.require_pg()
    data_store.ensure_tables()
    t0 = time.time()

    # 5) PRICE — refresh daily_bars from the latest NSE bhavcopy (broker-free).
    if refresh_price:
        try:
            import bhavcopy_history
            bhavcopy_history.append_latest()
            log.info("[ingest] bhavcopy_history.append_latest() done")
        except Exception as exc:
            log.warning("[ingest] price append skipped: %s", exc)

    if as_of is None:
        row = db.execute_db("SELECT MAX(date) d FROM daily_bars", fetch="one", require_pg=True)
        as_of = str(row["d"])[:10] if row and row.get("d") else None

    # 4/5) fundamentals (universe_catalog) + price (daily_bars) — existing pipelines.
    if refresh_fundamentals:
        try:
            import nse_bhavcopy
            nse_bhavcopy.enrich_market_cap_batch()  # bulk Dhan fetchdt -> universe_catalog
        except Exception as exc:
            log.warning("[ingest] catalog fundamentals refresh skipped: %s", exc)

    # Surveillance (ASM/GSM): warm the DAILY cache in STAGE-1 so the Stage-2 scans
    # (scoring_v1 + legacy_cleaned) read it cache-only and stay ZERO external network.
    # Additive: same ASM/GSM data whether cached or fetched -> no scoring change.
    try:
        from . import asm_gsm
        asm_gsm.get_surveillance(force_refresh=True)
        log.info("[ingest] asm_gsm surveillance cache warmed")
    except Exception as exc:
        log.warning("[ingest] asm_gsm warm skipped: %s", exc)

    # 1-3) basic gate + earnings_store population
    gated = _basic_gated(as_of)
    have = {r["isin"] for r in (db.execute_db("SELECT isin FROM earnings_store", fetch="all", require_pg=True) or [])}
    # seed from file-cache first (fast, no network) so re-runs/byte-identical are cheap
    seeded = seed_earnings_from_filecache()
    have = {r["isin"] for r in (db.execute_db("SELECT isin FROM earnings_store", fetch="all", require_pg=True) or [])}

    todo = [r["isin"] for r in gated if force or r["isin"] not in have]
    fetched = 0
    if todo:
        with_ex = ThreadPoolExecutor(max_workers=max_workers)
        futs = {with_ex.submit(dhan_forecast.fetch_and_store, isin): isin for isin in todo}
        try:
            for fut in as_completed(futs, timeout=900):
                try:
                    if fut.result():
                        fetched += 1
                except Exception:
                    pass
        except Exception as exc:
            log.warning("[ingest] earnings fetch deadline/err: %s", exc)
        finally:
            with_ex.shutdown(wait=False, cancel_futures=True)

    store_rows = data_store.earnings_coverage()["rows"]
    fund_rows = db.execute_db(
        "SELECT COUNT(*) c FROM universe_catalog WHERE market_cap>=1000 AND price>50 "
        "AND instrument_type='EQ' AND is_active AND pe IS NOT NULL", fetch="one", require_pg=True)["c"]
    out = {
        "as_of": as_of, "basic_gated": len(gated),
        "earnings_seeded_from_cache": seeded, "earnings_network_fetched": fetched,
        "earnings_store_rows": store_rows,
        "fundamentals_in_catalog(pe!=null)": fund_rows,
        "elapsed_s": round(time.time() - t0, 1),
    }
    log.info("[ingest] DONE %s", out)
    return out


if __name__ == "__main__":
    import json as _j
    print(_j.dumps(run_ingestion(), indent=2, default=str))
