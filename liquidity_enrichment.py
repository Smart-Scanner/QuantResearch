"""
liquidity_enrichment.py — Phase 5.6B/C: Background Liquidity Enrichment Worker

Detached from the boot startup path. The boot thread schedules this worker
which runs in the background enriching candidate symbols with 20-day
historical liquidity metrics from Angel One / yfinance.

Architecture:
  Boot → Master Sync → schedule liquidity worker → (background)
  Worker → Freeze candidates → Batch enrich → Check coverage → Trigger build

Safety:
  - Atomic single worker lock (SQL WHERE based, not read-then-write)
  - 2-hour crash recovery
  - Hard-capped ThreadPoolExecutor (max 4 workers)
  - Global rate limiter shared across all threads
  - Exponential backoff on API failures
  - Coverage-triggered universe build (80% threshold)
  - Exclusion percentage guard (max 10%)
"""

import logging
import time
import threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger("screener")

# Worker lock state
_worker_thread = None
_worker_lock = threading.Lock()

# ── Global Rate Limiter ───────────────────────────────────────
# Shared across all enrichment threads to prevent 429 storms.

class _RateLimiter:
    """Token-bucket rate limiter. Thread-safe."""
    def __init__(self, rps: float = 2.0):
        self._min_interval = 1.0 / rps if rps > 0 else 0.5
        self._last_call = 0.0
        self._lock = threading.Lock()

    def acquire(self):
        """Block until a request slot is available."""
        with self._lock:
            now = time.time()
            elapsed = now - self._last_call
            if elapsed < self._min_interval:
                wait = self._min_interval - elapsed
                time.sleep(wait)
            self._last_call = time.time()

_rate_limiter = None  # Initialized on worker start


def start_background_liquidity_worker():
    """Start the background liquidity worker if not already running.

    Startup protection:
    - If status = RUNNING and started_at < 2h, do not start worker
    - If older than 2h, mark stale, acquire lock, start replacement worker
    - Atomic lock acquisition via SQL WHERE (prevents dual-instance race)
    """
    import db

    with _worker_lock:
        current_status = db.get_meta("liquidity_worker_status")

        if current_status == "running":
            started_at = db.get_meta("liquidity_worker_started_at")
            if started_at:
                try:
                    started_dt = datetime.strptime(str(started_at)[:19], "%Y-%m-%d %H:%M:%S")
                    age_hours = (datetime.now() - started_dt).total_seconds() / 3600

                    if age_hours < 2:
                        log.info("[LiquidityWorker] Worker already running (%.1f hours) — skipping",
                                 age_hours)
                        return
                    else:
                        log.warning("[LiquidityWorker] Stale lock detected (%.1f hours old) — overriding",
                                    age_hours)
                except Exception:
                    pass
            else:
                log.info("[LiquidityWorker] Worker status=running but no timestamp — overriding")

        # Atomic lock acquisition via SQL WHERE — prevents dual Railway instance race
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        acquired = db.execute_db(
            """UPDATE scan_meta SET value = 'running', updated_at = ?
               WHERE key = 'liquidity_worker_status' AND value != 'running'""",
            (now,), fetch="rowcount"
        )

        # If no row was updated, either the key doesn't exist yet or another instance grabbed it
        if acquired == 0:
            # Try inserting (first-time case)
            try:
                db.execute_db(
                    """INSERT INTO scan_meta (key, value, updated_at) VALUES ('liquidity_worker_status', 'running', ?)
                       ON CONFLICT(key) DO NOTHING""",
                    (now,)
                )
                # Re-check if we actually own it
                check = db.get_meta("liquidity_worker_status")
                if check == "running":
                    log.info("[LiquidityWorker] Lock acquired via INSERT (first run)")
                else:
                    log.info("[LiquidityWorker] Another instance holds the lock — skipping")
                    return
            except Exception:
                log.info("[LiquidityWorker] Lock acquisition failed — another instance likely running")
                return
        else:
            log.info("[LiquidityWorker] Atomic lock acquired via UPDATE (%d rows)", acquired)

        db.set_meta("liquidity_worker_started_at", now)
        db.set_meta("liquidity_worker_error", "")

        # Initialize global rate limiter
        from config import LIQUIDITY_API_RPS
        global _rate_limiter
        _rate_limiter = _RateLimiter(rps=LIQUIDITY_API_RPS)

        # Launch background thread
        global _worker_thread
        _worker_thread = threading.Thread(
            target=_run_liquidity_enrichment,
            daemon=True,
            name="liquidity-worker"
        )
        _worker_thread.start()
        log.info("[LiquidityWorker] Launched background liquidity worker thread.")


def _run_liquidity_enrichment():
    """Main worker loop: freeze candidates, enrich in batches, trigger build."""
    import db
    from config import (
        LIQUIDITY_ENRICHMENT_BATCH_SIZE,
        LIQUIDITY_ENRICHMENT_WORKERS,
        LIQUIDITY_MIN_COVERAGE_PCT,
        LIQUIDITY_WORKER_SLEEP_MS,
        LIQUIDITY_MAX_RETRIES,
    )

    start_time = time.time()

    try:
        # 1. Determine target version
        current_version = db.get_latest_universe_version()
        # Parse version number and increment
        import re
        match = re.search(r"v(\d+)", current_version)
        version_num = int(match.group(1)) + 1 if match else 1
        target_version = f"UNIVERSE_v{version_num:03d}"

        # Build idempotency: skip if already built
        stage3_built = db.get_meta("stage3_built_version")
        if stage3_built == target_version:
            log.info("[LiquidityWorker] Version %s already built — skipping", target_version)
            _release_lock("completed")
            return

        log.info("[LiquidityWorker] Target version: %s", target_version)
        db.set_meta("building_universe_version", target_version)
        db.set_meta("universe_state", "ENRICHING")

        # 2. Classify instruments (heuristic fallback for unsynced symbols)
        try:
            classified = db.classify_instrument_types()
            log.info("[LiquidityWorker] Instrument classification: %d symbols updated", classified)
        except Exception as exc:
            log.warning("[LiquidityWorker] Instrument classification failed (non-fatal): %s", exc)

        # 3. Freeze candidate universe
        # Pre-freeze observability: log candidate breakdown
        try:
            pre_candidates = db.get_candidate_universe()
            pre_count = len(pre_candidates) if pre_candidates else 0
            # Count currently excluded symbols for diagnostics
            excluded_info = db.execute_db(
                "SELECT COUNT(*) as c FROM universe_catalog WHERE COALESCE(liquidity_excluded, FALSE) = TRUE",
                fetch="one"
            )
            excluded_count = int(excluded_info.get("c", 0)) if excluded_info else 0
            total_active = db.execute_db(
                "SELECT COUNT(*) as c FROM universe_catalog WHERE is_active = TRUE",
                fetch="one"
            )
            total_active_count = int(total_active.get("c", 0)) if total_active else 0
            log.info("[LiquidityWorker] candidate_count=%d eligible=%d excluded=%d",
                     total_active_count, pre_count, excluded_count)
        except Exception as exc:
            log.warning("[LiquidityWorker] Pre-freeze diagnostics failed (non-fatal): %s", exc)

        freeze_result = db.freeze_candidate_universe(target_version)
        frozen_count = freeze_result.get("frozen_count", 0)

        if frozen_count == 0:
            log.error("[LiquidityWorker] No candidates found — aborting enrichment")
            db.set_meta("universe_state", "WAITING_FOR_MARKETCAP")
            _release_lock("failed_no_candidates")
            return

        log.info("[LiquidityWorker] frozen=%d version=%s", frozen_count, target_version)

        # 4. Batch enrichment loop
        # Hard cap concurrency at 4 — no config override can exceed this
        max_workers = min(int(LIQUIDITY_ENRICHMENT_WORKERS), 4)
        batch_size = int(LIQUIDITY_ENRICHMENT_BATCH_SIZE)
        sleep_ms = int(LIQUIDITY_WORKER_SLEEP_MS)

        total_enriched = 0
        total_failed = 0
        total_skipped = 0
        batch_round = 0
        api_errors = 0
        api_throttles = 0

        while True:
            # Get next batch of pending symbols
            pending = db.get_liquidity_pending_symbols_v2(target_version, batch_size)

            if not pending:
                log.info("[LiquidityWorker] No more pending symbols after %d rounds", batch_round)
                break

            batch_round += 1
            log.info("[LiquidityWorker] BATCH %d: processing %d symbols (workers=%d)",
                     batch_round, len(pending), max_workers)

            # Process batch using thread pool
            batch_enriched = 0
            batch_failed = 0

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(_enrich_single_symbol, sym): sym
                    for sym in pending
                }

                for future in as_completed(futures):
                    sym = futures[future]
                    try:
                        result = future.result()
                        if result.get("success"):
                            batch_enriched += 1
                            total_enriched += 1
                        elif result.get("skipped"):
                            total_skipped += 1
                        else:
                            batch_failed += 1
                            total_failed += 1
                            failure_type = result.get("failure_type", "UNKNOWN")

                            if result.get("is_throttle"):
                                api_throttles += 1
                            else:
                                api_errors += 1

                            # Record failure in DB
                            db.increment_liquidity_sync_fail(sym, failure_type)

                    except Exception as exc:
                        log.debug("[LiquidityWorker] Future error for %s: %s", sym, exc)
                        batch_failed += 1
                        total_failed += 1
                        api_errors += 1
                        db.increment_liquidity_sync_fail(sym, "EXCEPTION")

            # Update progress metadata after each batch
            health = db.get_universe_health_metrics_v3(target_version)
            coverage_pct = health.get("liquidity_coverage_pct", 0)
            excluded_count = health.get("excluded_count", 0)
            total_candidates = health.get("total_candidates", 0)

            db.set_meta("liquidity_progress_pct", str(round(coverage_pct, 2)))
            db.set_meta("liquidity_enriched_count", str(total_enriched))
            db.set_meta("liquidity_failed_count", str(total_failed))
            db.set_meta("liquidity_api_errors", str(api_errors))
            db.set_meta("liquidity_api_throttles", str(api_throttles))
            db.set_meta("liquidity_permanent_exclusions", str(excluded_count))
            db.set_meta("liquidity_last_success_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

            runtime_min = (time.time() - start_time) / 60
            db.set_meta("liquidity_worker_runtime_minutes", str(round(runtime_min, 1)))

            log.info("[LiquidityWorker] BATCH %d DONE: enriched=%d failed=%d | "
                     "TOTAL: enriched=%d failed=%d excluded=%d coverage=%.1f%% runtime=%.1fmin",
                     batch_round, batch_enriched, batch_failed,
                     total_enriched, total_failed, excluded_count, coverage_pct, runtime_min)

            # Exclusion guard: if > 10% permanently excluded, flag DEGRADED
            if total_candidates > 0:
                exclusion_pct = (excluded_count / total_candidates) * 100
                if exclusion_pct > 10:
                    log.error("[LiquidityWorker] EXCLUSION GUARD: %.1f%% permanently excluded "
                              "(%d/%d) — flagging DEGRADED",
                              exclusion_pct, excluded_count, total_candidates)
                    db.set_meta("universe_state", "DEGRADED")

            # Check if coverage threshold reached → trigger build
            if coverage_pct >= LIQUIDITY_MIN_COVERAGE_PCT:
                if db.get_meta("stage3_built_version") != target_version:
                    log.info("[LiquidityWorker] Coverage %.1f%% >= %.1f%% — triggering universe build",
                             coverage_pct, LIQUIDITY_MIN_COVERAGE_PCT)
                    _trigger_universe_build(target_version, health)
                    # Continue enriching remaining symbols even after build
                else:
                    log.info("[LiquidityWorker] Build already triggered for %s — continuing enrichment",
                             target_version)

            # Rate limiting between batches
            time.sleep(sleep_ms / 1000.0)

        # 5. Final check — trigger build if not yet triggered and coverage is sufficient
        final_health = db.get_universe_health_metrics_v3(target_version)
        final_coverage = final_health.get("liquidity_coverage_pct", 0)

        if final_coverage >= LIQUIDITY_MIN_COVERAGE_PCT:
            if db.get_meta("stage3_built_version") != target_version:
                log.info("[LiquidityWorker] Final coverage %.1f%% — triggering build", final_coverage)
                _trigger_universe_build(target_version, final_health)
        else:
            log.warning("[LiquidityWorker] Final coverage %.1f%% < %.1f%% — universe NOT built",
                        final_coverage, LIQUIDITY_MIN_COVERAGE_PCT)
            db.set_meta("universe_state", "WAITING_FOR_LIQUIDITY")

        duration = time.time() - start_time
        log.info("[LiquidityWorker] COMPLETED: enriched=%d failed=%d skipped=%d "
                 "coverage=%.1f%% duration=%.1fs",
                 total_enriched, total_failed, total_skipped,
                 final_coverage, duration)

        _release_lock("completed")

    except Exception as exc:
        log.error("[LiquidityWorker] FATAL ERROR: %s", exc, exc_info=True)
        try:
            import db
            db.set_meta("liquidity_worker_error", str(exc))
        except Exception:
            pass
        _release_lock("failed")


def _enrich_single_symbol(symbol: str) -> dict:
    """Fetch 20-day OHLCV candles and compute liquidity metrics for one symbol.
    Returns dict with success/failure status.
    Uses global rate limiter to prevent 429 storms.
    """
    import db
    import live_feed
    from config import LIQUIDITY_MAX_RETRIES

    # Flag-gated: source the 20-day liquidity candles from the broker-free EOD
    # store instead of Angel. Default OFF → byte-identical to the Angel path.
    # Any miss / failure falls through to the existing Angel fetch below.
    try:
        import bhavcopy_history
        if bhavcopy_history.USE_BHAVCOPY_HISTORY:
            store_df = bhavcopy_history.get_history(symbol, days=30)
            if store_df is not None and not store_df.empty and len(store_df) >= 10:
                recent = store_df.tail(20)
                avg_volume = float(recent["VOLUME"].mean())
                recent_turnover = recent["VOLUME"] * recent["CLOSE"]
                avg_turnover = float(recent_turnover.mean())
                last_price = float(recent["CLOSE"].iloc[-1])
                db.update_liquidity_metrics(symbol, avg_volume, avg_turnover, last_price)
                return {"success": True}
    except Exception as exc:
        log.debug("[LiquidityWorker] bhavcopy store path failed for %s: %s — falling back", symbol, exc)

    for attempt in range(LIQUIDITY_MAX_RETRIES):
        try:
            # Acquire rate limiter slot before any API call
            if _rate_limiter:
                _rate_limiter.acquire()

            df = live_feed.fetch_historical(symbol, days=30)

            if df is None or df.empty:
                if attempt < LIQUIDITY_MAX_RETRIES - 1:
                    # Exponential backoff: 5s, 15s, 60s
                    backoff = [5, 15, 60][min(attempt, 2)]
                    time.sleep(backoff)
                    continue
                return {
                    "success": False,
                    "failure_type": "SYMBOL_NOT_SUPPORTED",
                    "is_throttle": False,
                }

            if len(df) < 10:
                return {"success": False, "failure_type": "INSUFFICIENT_DATA", "is_throttle": False}

            # Last 20 trading days
            recent = df.tail(20)
            avg_volume = float(recent["VOLUME"].mean())

            # Turnover = Volume × Close price
            recent_turnover = recent["VOLUME"] * recent["CLOSE"]
            avg_turnover = float(recent_turnover.mean())
            last_price = float(recent["CLOSE"].iloc[-1])

            # Update database
            db.update_liquidity_metrics(symbol, avg_volume, avg_turnover, last_price)

            return {"success": True}

        except Exception as exc:
            exc_str = str(exc).lower()

            # Detect API throttling (429)
            is_throttle = "429" in exc_str or "rate" in exc_str or "ab1019" in exc_str

            if attempt < LIQUIDITY_MAX_RETRIES - 1:
                backoff = [5, 15, 60][min(attempt, 2)]
                if is_throttle:
                    backoff *= 2  # Double backoff for throttles
                log.debug("[LiquidityWorker] %s attempt %d/%d failed: %s (backoff=%ds)",
                          symbol, attempt + 1, LIQUIDITY_MAX_RETRIES, exc, backoff)
                time.sleep(backoff)
                continue

            return {
                "success": False,
                "failure_type": "API_THROTTLE" if is_throttle else "EXCEPTION",
                "is_throttle": is_throttle,
            }

    return {"success": False, "failure_type": "MAX_RETRIES_EXCEEDED", "is_throttle": False}


def _trigger_universe_build(version: str, health_metrics: dict):
    """Trigger the universe builder for the given version."""
    import db

    try:
        from universe_builder import build_eligible_universe_v2
        build_eligible_universe_v2(version, health_metrics)
        db.set_meta("stage3_built_version", version)
    except Exception as exc:
        log.error("[LiquidityWorker] Universe build failed: %s", exc, exc_info=True)
        db.set_meta("universe_state", "DEGRADED")


def _release_lock(status: str):
    """Release the worker lock and update metadata."""
    try:
        import db
        db.set_meta("liquidity_worker_status", status)
        runtime = db.get_meta("liquidity_worker_runtime_minutes") or "0"
        log.info("[LiquidityWorker] Lock released: status=%s runtime=%s min", status, runtime)
    except Exception:
        pass
