"""
master_sync.py — Phase 5.5: Master Stock Registry Sync Job

Populates universe_catalog (Stock Master Registry) from:
  1. Angel ScripMaster (angel_tokens.json) — all NSE EQ symbols
  2. Dhan.co — market_cap, PE, PB, ROE, ROCE, EPS, sector, industry (via nse_bhavcopy)

Schedule: Every 14 days, Sunday, 18:00 IST
Mode: Upsert only — no truncate
Resume: Tracks last_synced_at per symbol
Retry: 3 attempts with exponential backoff

Logs:
  MASTER_SYNC_STARTED
  MASTER_SYNC_COMPLETED
  MASTER_SYNC_FAILED
"""

import os
import json
import time
import logging
from pathlib import Path
from datetime import datetime, timedelta

log = logging.getLogger("screener")

TOKEN_FILE = Path(__file__).parent / "cache" / "angel_tokens.json"


def is_master_sync_due() -> bool:
    """Check if 14 days have passed since last master sync."""
    import db
    from config import MASTER_SYNC_INTERVAL_DAYS

    last_sync = db.get_meta("master_sync_last_completed")
    if not last_sync:
        return True

    try:
        last_dt = datetime.strptime(str(last_sync)[:19], "%Y-%m-%d %H:%M:%S")
        age_days = (datetime.now() - last_dt).days
        return age_days >= MASTER_SYNC_INTERVAL_DAYS
    except Exception:
        return True


def run_master_sync():
    """
    Master Stock Registry Sync Job — Incremental Mode.

    Phase 1: Upsert ALL NSE EQ symbols into universe_catalog.
             This ensures new IPOs/delistings are captured immediately.
    Phase 2: Trigger Dhan enrichment to populate market_cap, PE, PB, ROE, etc.
    """
    import db
    from config import MASTER_SYNC_DAILY_BATCH_SIZE, MASTER_SYNC_INTERVAL_DAYS

    # ── Reentrant lock: prevent concurrent master sync runs ──
    current_status = db.get_meta("master_sync_status")
    if current_status == "running":
        # Stale lock recovery: if running for > 30 min, treat as crashed
        started_at = db.get_meta("master_sync_started_at")
        if started_at:
            try:
                started_dt = datetime.strptime(str(started_at)[:19], "%Y-%m-%d %H:%M:%S")
                age_min = (datetime.now() - started_dt).total_seconds() / 60
                if age_min < 30:
                    log.warning("[MasterSync] Another master sync is already running (%.0f min) — skipping", age_min)
                    return {"synced": 0, "failed": 0, "skipped": True}
                else:
                    log.warning("[MasterSync] Stale lock detected (%.0f min old) — overriding", age_min)
                    db.set_meta("master_sync_status", "stale_override")
            except Exception:
                pass
        else:
            log.warning("[MasterSync] Another master sync is already running — skipping")
            return {"synced": 0, "failed": 0, "skipped": True}

    scan_id = f"master_sync_{int(time.time())}"
    log.info("[MASTER_SYNC_STARTED] scan_id=%s (incremental mode)", scan_id)
    db.set_meta("master_sync_status", "running")
    db.set_meta("master_sync_started_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    db.set_meta("master_sync_scan_id", scan_id)
    db.log_scan_event(scan_id, "MASTER_SYNC_STARTED", "incremental")

    start_time = time.time()

    try:
        # Phase 1: Load ALL symbols from angel_tokens.json
        all_symbols = _load_nse_symbols()
        if not all_symbols:
            raise RuntimeError("No NSE symbols found in angel_tokens.json")

        log.info("[MasterSync] Phase 1: Found %d NSE EQ symbols — upserting all into catalog",
                 len(all_symbols))

        # Bulk upsert all symbols (lightweight — no yfinance call)
        # This ensures new symbols are in the catalog even without metadata
        phase1_data = []
        for sym in all_symbols:
            phase1_data.append({
                "symbol": sym,
                "company_name": sym,
                "market_cap": 0,
                "market_cap_bucket": "Unknown Cap",
                "sector": "",
                "industry": "",
                "is_active": True,
                "instrument_type": "EQ",
                "exchange": "NSE",
            })
            # Save in batches of 100 to avoid memory pressure
            if len(phase1_data) >= 100:
                db.upsert_universe_catalog(phase1_data, set_synced_at=False)
                phase1_data = []

        if phase1_data:
            db.upsert_universe_catalog(phase1_data, set_synced_at=False)

        log.info("[MasterSync] Phase 1 done: %d symbols in catalog", len(all_symbols))

        # Phase 1.5: Classify instrument types for unsynced symbols
        # Applies name heuristics ONLY for symbols without yfinance metadata
        try:
            classified = db.classify_instrument_types()
            log.info("[MasterSync] Phase 1.5: Classified %d instrument types (heuristic)", classified)
        except Exception as exc:
            log.warning("[MasterSync] Phase 1.5: Classification failed (non-fatal): %s", exc)

        # Catalog monitoring for Mission Control
        try:
            catalog_stats = db.execute_db(
                """SELECT
                     COUNT(*) as total,
                     SUM(CASE WHEN last_synced_at IS NOT NULL THEN 1 ELSE 0 END) as synced,
                     SUM(CASE WHEN last_synced_at IS NULL THEN 1 ELSE 0 END) as pending
                   FROM universe_catalog WHERE is_active = TRUE""",
                fetch="one"
            )
            if catalog_stats:
                db.set_meta("catalog_total", str(catalog_stats.get("total", 0)))
                db.set_meta("catalog_synced", str(catalog_stats.get("synced", 0)))
                db.set_meta("catalog_pending", str(catalog_stats.get("pending", 0)))
                log.info("[MasterSync] Catalog: total=%s synced=%s pending=%s",
                         catalog_stats.get("total", 0),
                         catalog_stats.get("synced", 0),
                         catalog_stats.get("pending", 0))
        except Exception:
            pass

        # Phase 2: Dhan enrichment (replaces yfinance Phase 2)
        # Dhan provides market_cap, PE, PB, ROE, ROCE, EPS, sector, industry
        synced = 0
        failed = 0
        try:
            from nse_bhavcopy import enrich_market_cap_batch
            dhan_result = enrich_market_cap_batch(max_symbols=len(all_symbols))
            synced = dhan_result.get("enriched", 0)
            log.info("[MasterSync] Phase 2: Dhan enrichment done — %d stocks enriched", synced)
        except Exception as exc:
            log.warning("[MasterSync] Phase 2: Dhan enrichment failed (non-fatal): %s", exc)
            failed = 1

        duration = time.time() - start_time
        log.info("[MASTER_SYNC_COMPLETED] %d synced via Dhan, %d failed, %.1f seconds",
                 synced, failed, duration)

        db.set_meta("master_sync_status", "completed")
        db.set_meta("master_sync_last_completed", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        db.set_meta("master_sync_synced_count", str(synced))
        db.set_meta("master_sync_failed_count", str(failed))
        db.set_meta("master_sync_duration_s", str(round(duration)))
        db.log_scan_event(scan_id, "MASTER_SYNC_COMPLETED",
                          f"synced={synced} failed={failed} duration={round(duration)}s source=dhan")

        return {"synced": synced, "failed": failed, "duration_s": round(duration)}

    except Exception as exc:
        log.error("[MASTER_SYNC_FAILED] %s", exc, exc_info=True)
        db.set_meta("master_sync_status", "failed")
        db.set_meta("master_sync_error", str(exc))
        db.log_scan_event(scan_id, "MASTER_SYNC_FAILED", str(exc))
        raise


def _get_stale_symbols(interval_days: int, max_batch: int) -> list:
    """Get symbols from universe_catalog where last_synced_at is NULL or older
    than interval_days. Returns at most max_batch symbols, prioritizing
    never-synced symbols first, then oldest-synced.
    """
    import db

    # Priority 1: Never-synced symbols (last_synced_at IS NULL)
    never_synced = db.execute_db(
        """SELECT symbol FROM universe_catalog
           WHERE is_active = TRUE AND last_synced_at IS NULL
           ORDER BY symbol LIMIT ?""",
        (max_batch,), fetch="all"
    ) or []

    result = [r.get("symbol") for r in never_synced if r.get("symbol")]

    if len(result) >= max_batch:
        return result[:max_batch]

    # Priority 2: Oldest-synced symbols
    remaining = max_batch - len(result)
    threshold = (datetime.now() - timedelta(days=interval_days)).strftime("%Y-%m-%d %H:%M:%S")

    oldest = db.execute_db(
        """SELECT symbol FROM universe_catalog
           WHERE is_active = TRUE AND last_synced_at IS NOT NULL
             AND last_synced_at < ?
           ORDER BY last_synced_at ASC LIMIT ?""",
        (threshold, remaining), fetch="all"
    ) or []

    result.extend(r.get("symbol") for r in oldest if r.get("symbol"))
    return result


def _get_sync_fail_count(symbol: str) -> int:
    """Get the current consecutive sync failure count for a symbol."""
    import db
    row = db.execute_db(
        "SELECT sync_fail_count FROM universe_catalog WHERE symbol=?",
        (symbol,), fetch="one"
    )
    if row and row.get("sync_fail_count") is not None:
        return int(row["sync_fail_count"])
    return 0


def _load_nse_symbols() -> list[str]:
    """Load all NSE EQ symbols from angel_tokens.json."""
    if TOKEN_FILE.exists():
        try:
            tokens = json.loads(TOKEN_FILE.read_text())
            # angel_tokens.json is {symbol: token} mapping
            return sorted(tokens.keys())
        except Exception as exc:
            log.warning("[MasterSync] Failed to load angel_tokens.json: %s", exc)

    # Fallback: try to refresh
    try:
        import live_feed
        live_feed.refresh_token_map()
        if TOKEN_FILE.exists():
            tokens = json.loads(TOKEN_FILE.read_text())
            return sorted(tokens.keys())
    except Exception:
        pass

    return []


def _fetch_symbol_metadata(symbol: str) -> dict:
    """Fetch metadata for a single symbol from universe_catalog (Dhan data).
    Returns dict with keys: symbol, company_name, market_cap, market_cap_bucket,
                            sector, industry, is_active, instrument_type, exchange, price
    Note: yfinance dependency removed. Dhan data is populated by enrich_market_cap_batch.
    """
    import db
    try:
        row = db.execute_db(
            """SELECT symbol, company_name, market_cap, sector, industry, price
               FROM universe_catalog WHERE symbol = ?""",
            (symbol,), fetch="one"
        )
        if row and row.get("market_cap") and row["market_cap"] > 0:
            mcap = row["market_cap"]
            return {
                "symbol": symbol,
                "company_name": row.get("company_name") or symbol,
                "market_cap": mcap,
                "market_cap_bucket": _classify_market_cap(mcap),
                "sector": row.get("sector") or "",
                "industry": row.get("industry") or "",
                "is_active": True,
                "instrument_type": "EQ",
                "exchange": "NSE",
                "price": row.get("price") or 0,
            }
    except Exception as exc:
        log.debug("[MasterSync] DB lookup failed for %s: %s", symbol, exc)
    return None


def _classify_market_cap(mcap_cr: float) -> str:
    """Classify market cap into buckets."""
    if mcap_cr >= 50000:
        return "Blue Chip"
    elif mcap_cr >= 20000:
        return "Large Cap"
    elif mcap_cr >= 5000:
        return "Mid Cap"
    elif mcap_cr >= 1000:
        return "Small Cap"
    elif mcap_cr > 0:
        return "Micro Cap"
    return "Unknown Cap"


def _detect_instrument_type(info: dict) -> str:
    """Detect if the instrument is an ETF, mutual fund, etc."""
    quote_type = (info.get("quoteType") or "").upper()
    if quote_type == "ETF":
        return "ETF"
    if quote_type == "MUTUALFUND":
        return "MF"
    return "EQ"
