"""
Stock scanner -- Phase 1 (Angel One) + Phase 2 (jugaad_data fallback).
DB is single source of truth. No shared mutable state for results.

Phase 4: Event-driven architecture:
  1. refresh_news_pipeline() detects news spikes + NSE announcements
  2. _shortlist_for_deep_scan() picks candidates with hard cap
  3. run_full_scan() runs fast scan, then deep scan on shortlisted
"""

import time
import logging
import threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from metrics.timer import timed, _record as record_timing

IST = timezone(timedelta(hours=5, minutes=30))


def now_ist():
    return datetime.now(IST)


from universe import get_fast_scan_universe, save_active_universe
from config import MAX_WORKERS, BATCH_SIZE, BATCH_DELAY, DATA_LOOKBACK_DAYS, CACHE_TTL_HOURS
from analyzer import (
    fetch_and_analyze, get_nifty50_benchmark,
    apply_sector_strength, generate_ai_summary, reset_delivery_state,
)
from intelligence.news_gdelt_finbert import build_article_cache, _article_cache, _cache_lock
from intelligence.news_sentiment import _fetch_nse_announcements, get_nse_affected_symbols
import live_feed
import db

log = logging.getLogger("screener")


# ===================================================================
#  SCAN STATE — DB-backed (Phase 6 + Phase 0A Hardening)
# ===================================================================
# Phase 0A: ScanState class is now a backward-compat wrapper.
# scanner.py uses the new module-level functions directly.

from db import (
    scan_state, acquire_scan_lock, transition_scan_state,
    update_scan_progress, is_scan_active, get_scan_cancel_requested,
    save_state_transition,
)
from scan_context import ScanContext
from events import ACTOR_SYSTEM, ACTOR_USER, ACTOR_AUTO_SCAN

# Phase 0B: Graceful shutdown event — shared across all daemon threads
_shutdown_event = threading.Event()


# ===================================================================
#  MARKETAUX BACKGROUND WORKER (Phase 8)
# ===================================================================
import queue as _queue_mod

_marketaux_queue: _queue_mod.Queue = _queue_mod.Queue(maxsize=200)
_marketaux_thread: threading.Thread | None = None
_marketaux_overflow_count = 0

# ===================================================================
#  HEARTBEAT WORKER (Phase B)
# ===================================================================
def _scan_heartbeat_worker(scan_id: str, stop_event: threading.Event):
    """Background daemon: emits heartbeat for active scan every 30s."""
    log.info("[HEARTBEAT] Started for scan_id=%s", scan_id)
    while not stop_event.is_set():
        try:
            db.update_scan_heartbeat(scan_id)
        except Exception as exc:
            log.warning("[HEARTBEAT] Update failed: %s", exc)
        stop_event.wait(30)
    log.info("[HEARTBEAT] Stopped for scan_id=%s", scan_id)



def _marketaux_worker():
    """Background daemon: pulls symbols from queue, enriches with MarketAux.
    Phase 0B: Checks _shutdown_event for graceful termination.
    """
    while not _shutdown_event.is_set():
        try:
            sym = _marketaux_queue.get(timeout=5)
        except _queue_mod.Empty:
            continue
        try:
            nifty_1m = live_feed.get_nifty_1m()
            regime = db.get_meta("market_regime", "unknown")
            df = live_feed.fetch_historical(sym, days=DATA_LOOKBACK_DAYS)
            if df is not None and not df.empty:
                new_r = fetch_and_analyze(sym, nifty_1m, regime, ext_df=df, query_marketaux=True, scan_mode="deep")
                if new_r:
                    db.save_results([new_r])
                    log.info("MarketAux BG: Enriched %s (score=%s)", sym, new_r.get("score", "?"))
        except Exception as exc:
            log.warning("MarketAux BG: Failed for %s: %s", sym, exc)
        finally:
            _marketaux_queue.task_done()


def enqueue_marketaux(symbols: list):
    """Non-blocking enqueue for MarketAux enrichment. Drops gracefully if full."""
    global _marketaux_overflow_count
    for sym in symbols:
        try:
            _marketaux_queue.put_nowait(sym)
        except _queue_mod.Full:
            _marketaux_overflow_count += 1
            log.warning("MarketAux queue full (overflow #%d), dropping: %s", _marketaux_overflow_count, sym)


def start_marketaux_worker():
    """Start background MarketAux worker thread (idempotent)."""
    global _marketaux_thread
    if _marketaux_thread is not None and _marketaux_thread.is_alive():
        return
    _marketaux_thread = threading.Thread(target=_marketaux_worker, daemon=True, name="marketaux-bg")
    _marketaux_thread.start()
    log.info("MarketAux background worker started")


def get_marketaux_queue_depth() -> int:
    return _marketaux_queue.qsize()


def get_marketaux_overflow_count() -> int:
    return _marketaux_overflow_count


# ===================================================================
#  CACHE CHECK
# ===================================================================

def has_valid_cache() -> bool:
    """Check if DB has valid (non-stale) scan results."""
    db.init_db()
    if db.get_result_count() == 0:
        return False
    timestamp = db.get_meta("timestamp")
    if timestamp:
        try:
            cached_time = datetime.fromisoformat(timestamp)
            # Handle naive vs aware datetime comparison
            if cached_time.tzinfo is None:
                age_hours = (now_ist().replace(tzinfo=None) - cached_time).total_seconds() / 3600
            else:
                age_hours = (now_ist() - cached_time).total_seconds() / 3600
            if age_hours > CACHE_TTL_HOURS:
                return False
        except (ValueError, TypeError):
            pass
    return True


# ===================================================================
#  EVENT-DRIVEN: News pipeline + shortlisting (Phase 4)
# ===================================================================

_prev_article_counts: dict = {}  # symbol -> article count from previous refresh


@timed("news_pipeline")
def refresh_news_pipeline(all_symbols: set) -> dict:
    """
    Refresh all news sources BEFORE scan. Detects two event signals:
      1. NSE corporate announcements (1 HTTP call)
      2. GDELT news volume spikes (1 HTTP call + FinBERT scoring)

    Returns {"spikes": set, "announcements": set}
    """
    global _prev_article_counts

    # 1. NSE announcements
    try:
        _fetch_nse_announcements()
    except Exception as exc:
        log.warning("NSE announcements fetch failed: %s", exc)
    nse_affected = get_nse_affected_symbols()

    # 2. Rebuild GDELT article cache and detect spikes
    # Snapshot previous counts before rebuild
    with _cache_lock:
        prev_counts = {sym: len(data.get("articles", [])) for sym, data in _article_cache.items()}

    try:
        build_article_cache(all_symbols)
    except Exception as exc:
        log.warning("GDELT cache rebuild failed: %s", exc)

    # Compare new vs previous article counts to find spikes
    spikes = set()
    with _cache_lock:
        for sym, data in _article_cache.items():
            new_count = len(data.get("articles", []))
            old_count = prev_counts.get(sym, 0)
            # Spike: >2x previous count AND at least 3 articles
            if new_count >= 3 and old_count > 0 and new_count > old_count * 2:
                spikes.add(sym)
            # Also flag if GDELT spike ratio > 2
            if data.get("spike", 1.0) > 2.0:
                spikes.add(sym)

    _prev_article_counts = {
        sym: len(data.get("articles", []))
        for sym, data in _article_cache.items()
    }

    log.info(
        "News pipeline: %d NSE announcements, %d GDELT spikes",
        len(nse_affected), len(spikes),
    )

    # Phase 7: Flag event-driven symbols for deep scan
    for sym in spikes:
        db.mark_deep_scan_needed(sym, reason="news_spike")
    for sym in nse_affected:
        db.mark_deep_scan_needed(sym, reason="corp_event")

    return {"spikes": spikes, "announcements": nse_affected}


def _shortlist_for_deep_scan(
    fast_results: list,
    event_signals: dict,
    hard_cap: int = 100,
    soft_target: int = 50,
) -> list:
    """
    Build shortlist of candidates for deep scan from fast scan results + event signals.

    Tiered selection:
      Tier 1: Event-driven (NSE announcements | GDELT spikes) -- highest priority
      Tier 2: Breakouts + score>=60 + vol_ratio>=2.0
      Tier 3: Score>=40 -- fill to soft_target only

    Hard cap enforced: never returns more than hard_cap candidates.
    """
    spikes = event_signals.get("spikes", set())
    announcements = event_signals.get("announcements", set())
    # Phase 7: Also include DB-flagged symbols needing deep scan
    db_flagged = set(db.get_symbols_needing_deep_scan(limit=hard_cap))
    event_syms = spikes | announcements | db_flagged

    candidates = []
    seen = set()

    # Tier 1: Event-driven + DB-flagged
    for r in fast_results:
        sym = r.get("symbol", "")
        if sym in event_syms and sym not in seen:
            candidates.append(sym)
            seen.add(sym)
            if len(candidates) >= hard_cap:
                break
    # Also add DB-flagged symbols not in fast_results
    if len(candidates) < hard_cap:
        for sym in db_flagged - seen:
            candidates.append(sym)
            seen.add(sym)
            if len(candidates) >= hard_cap:
                break

    # Tier 2: Breakouts + high score + high volume
    if len(candidates) < hard_cap:
        for r in sorted(fast_results, key=lambda x: x.get("score", 0), reverse=True):
            sym = r.get("symbol", "")
            if sym in seen:
                continue
            score = r.get("score", 0)
            vol = r.get("volume_ratio", 1.0) or 1.0
            is_breakout = r.get("is_breakout", False)
            if (is_breakout and score >= 60) or (score >= 60 and vol >= 2.0):
                candidates.append(sym)
                seen.add(sym)
                if len(candidates) >= hard_cap:
                    break

    # Tier 3: Score >= 40 -- fill to soft_target only
    if len(candidates) < soft_target:
        for r in sorted(fast_results, key=lambda x: x.get("score", 0), reverse=True):
            sym = r.get("symbol", "")
            if sym in seen:
                continue
            if r.get("score", 0) >= 40:
                candidates.append(sym)
                seen.add(sym)
                if len(candidates) >= soft_target:
                    break

    # Hard cap enforcement -- no exceptions
    final = candidates[:hard_cap]
    log.info(
        "Deep scan shortlist: %d candidates (hard_cap=%d) "
        "[T1_events=%d, T2_breakout=%d, T3_score=%d]",
        len(final), hard_cap,
        len(event_syms & seen),
        sum(1 for s in seen if s not in event_syms),
        max(0, len(final) - len(event_syms & seen)),
    )
    return final


# ===================================================================
#  FULL SCAN -- writes directly to DB
# ===================================================================


@timed("full_scan")
def run_full_scan(context: ScanContext = None, resume_from_scan_id: str = None):
    """Run full stock scan. Writes results to DB incrementally.

    Phase 0A: Uses ScanContext for execution ownership.
    Phase 0B: Guaranteed terminal state via finally block.
    Phase 1: Full context propagation (correlation_id, versions, config_snapshot).
    """
    # Phase 1: Create context if not provided (auto-scan / legacy callers)
    if context is None:
        context = ScanContext.create(
            trigger_source="auto",
            user_id="system",
            mode="manual",
        )

    scan_id = context.scan_id
    correlation_id = context.correlation_id

    # Phase 5: Flush any deferred writes from previous cycle
    try:
        flushed = db.flush_deferred_writes()
        if flushed:
            log.info("[%s] Flushed %d deferred writes from DLQ", correlation_id[:12], flushed)
            
        # P0.1E: Flush governance artifacts (mandatory for audit)
        gov_flushed = db.flush_governance_writes()
        if gov_flushed:
            log.info("[%s] Flushed %d governance artifacts from DLQ", correlation_id[:12], gov_flushed)
    except Exception as exc:
        log.warning("[%s] DLQ flush failed: %s", correlation_id[:12], exc)

    try:
        live_feed.reset_login_circuit_breaker()
    except Exception:
        pass

    # Phase 6, Section 39: Configuration drift check at scan ingress
    try:
        from config import check_config_drift
        from events import CONFIG_DRIFT_DETECTED
        _drift = check_config_drift()
        if _drift:
            log.warning(
                "[%s] CONFIG DRIFT DETECTED — %d variable(s) changed: %s",
                correlation_id[:12], len(_drift), list(_drift.keys())
            )
            # Persist drift details for audit trail
            import json as _json
            db.set_meta("config_drift", _json.dumps({
                "scan_id": scan_id,
                "drift": {k: {dk: str(dv) for dk, dv in v.items()} for k, v in _drift.items()},
                "detected_at": datetime.now(IST).isoformat(),
            }))
            # Emit event via state transition audit trail
            save_state_transition(
                scan_id, "running", "running",
                reason=f"config_drift: {list(_drift.keys())}",
                actor=ACTOR_SYSTEM,
                correlation_id=correlation_id,
            )
        else:
            log.info("[%s] Config drift check passed — no changes from baseline", correlation_id[:12])
    except Exception as exc:
        log.debug("[%s] Config drift check failed (non-fatal): %s", correlation_id[:12], exc)

    # ── Phase 5.5: Universe Engine Feature Flag ────────────────────
    from config import USE_UNIVERSE_ENGINE
    if USE_UNIVERSE_ENGINE:
        log.info("[%s] Phase 5.5: Universe Engine ACTIVE — routing to parallel scan", correlation_id[:12])
        _run_parallel_scan(context)
        return

    # ── Legacy scan path (USE_UNIVERSE_ENGINE=0) ──────────────────
    # P0 Governance: Lock active universe version
    universe_version = db.get_meta("active_universe_version")
    if not universe_version:
        log.error("[%s] EMERGENCY FALLBACK: No active universe version found.", correlation_id[:12])
        raise RuntimeError("EMERGENCY FALLBACK: No active universe version found. Scan aborted.")
        
    eligible_rows = db.get_eligible_universe(universe_version)
    if not eligible_rows:
        log.warning("[%s] Frozen universe %s has 0 members — falling back to universe_catalog EQ", correlation_id[:12], universe_version)
        try:
            fallback_rows = db.execute_db(
                """SELECT symbol FROM universe_catalog 
                   WHERE is_active = TRUE 
                   AND instrument_type = 'EQ'
                   ORDER BY symbol""",
                fetch="all"
            )
            eligible_rows = [{"symbol": r["symbol"]} for r in fallback_rows] if fallback_rows else []
            universe_version = "FALLBACK_EQ"
            log.info("[%s] FALLBACK universe: %d EQ stocks", correlation_id[:12], len(eligible_rows))
        except Exception as exc:
            log.error("[%s] FALLBACK universe query failed: %s", correlation_id[:12], exc)
            eligible_rows = []
    if not eligible_rows:
        log.error("[%s] No stocks available even after fallback. Scan aborted.", correlation_id[:12])
        return
        
    all_symbols = [row["symbol"] for row in eligible_rows]
    total = len(all_symbols)

    # Persist the resolved universe for debugging / transparency
    try:
        save_active_universe(all_symbols)
    except Exception as exc:
        log.debug("save_active_universe failed (non-fatal): %s", exc)

    # Phase 0A: Atomic lock acquisition via ScanContext
    lock_acquired = scan_state.start(total, mode=context.trigger_source, context=context)
    if lock_acquired is None:
        # Lock not acquired — another scan is running (Section 32: TOCTOU prevention)
        log.warning("[%s] Scan rejected — another scan is already active", correlation_id[:12])
        return

    # Lock universe version into scan_runs (P0 Governance)
    db.execute_db("UPDATE scan_runs SET universe_version = ? WHERE scan_id = ?", (universe_version, scan_id))

    db.clear_meta_cache()  # Phase 1: ensure fresh metadata during scan
    log.info("[%s] Scan: %d stocks... (scan_id=%s)", correlation_id[:12], total, scan_id[:20])
    start_time = time.monotonic()
    
    db.log_scan_event(scan_id, "SCAN_STARTED", f"Scanning {total} stocks")
    db.log_scan_event(scan_id, "SCAN_LOCK_ACQUIRED", "Scan lock acquired")

    # Phase 0B: Track whether we reached a terminal state
    _reached_terminal = False
    
    # Phase B: Start Heartbeat
    heartbeat_stop = threading.Event()
    heartbeat_thread = threading.Thread(
        target=_scan_heartbeat_worker, args=(scan_id, heartbeat_stop), daemon=True
    )
    heartbeat_thread.start()

    try:
        # ── PRE-SCAN INTELLIGENCE WARMUP ────────────────────────────
        # Warms: GDELT+FinBERT article cache, world markets,
        # sector rotation (RRG), Forex Factory macro events.
        # All results cached globally — O(1) per-stock lookup.
        try:
            from intelligence import warmup_all
            warmup_all(set(all_symbols))
        except Exception as exc:
            log.warning("[%s] Intelligence warmup failed (continuing): %s", correlation_id[:12], exc)

        # Reset delivery enrichment flag
        reset_delivery_state()

        # Benchmark
        nifty_1m, regime = get_nifty50_benchmark()
        db.set_meta("nifty50_1m", nifty_1m)
        db.set_meta("market_regime", regime)
        log.info("[%s] Nifty 1M: %+.2f%% | Regime: %s", correlation_id[:12], nifty_1m, regime.upper())

        results = []
        failed_symbols = []
        scored_set = set()

        # ── PHASE 1: Angel One historical (primary — fresh data) ──
        log.info("[%s] Phase 1: Angel One (%d stocks)...", correlation_id[:12], total)

        # Phase 3: Log phase transition
        save_state_transition(scan_id, "running", "running",
                              reason="phase1_started", actor=ACTOR_SYSTEM,
                              correlation_id=correlation_id)

        # Phase 6: Chunk Execution Architecture
        import universe

        chunks = universe.get_universe_chunks(all_symbols)
        
        # --- PHASE F: INTRA-CHUNK RESUME LOGIC ---
        resume_states = {}
        if resume_from_scan_id:
            try:
                resume_states = db.get_chunk_run_states(resume_from_scan_id)
                log.info("[%s] Resuming from %s, found %d chunk states.", correlation_id[:12], resume_from_scan_id, len(resume_states))
            except Exception as e:
                log.warning("[%s] Failed to fetch resume states: %s", correlation_id[:12], e)

        filtered_chunks = []
        for c_name, c_symbols in chunks:
            if c_name in resume_states:
                status, processed = resume_states[c_name]
                if status == "COMPLETED":
                    log.info("[%s] Skipping completed chunk: %s", correlation_id[:12], c_name)
                    continue
                elif processed > 0:
                    log.info("[%s] Resuming chunk: %s from offset %d", correlation_id[:12], c_name, processed)
                    filtered_chunks.append((c_name, c_symbols[processed:]))
                else:
                    filtered_chunks.append((c_name, c_symbols))
            else:
                filtered_chunks.append((c_name, c_symbols))
        chunks = filtered_chunks
        # ----------------------------------------

        
        def _process_chunk_worker(chunk_name, chunk_symbols, scan_id, correlation_id, nifty_1m, regime, total):
            from data_provider import provider_manager
            chunk_results = []
            chunk_failed = 0
            chunk_processed = 0
            failed_syms = []
            scored_syms = set()
            
            provider = provider_manager.acquire_active_provider(role="RESEARCH")
            log.info("[%s] Worker acquired provider: %s for %s", correlation_id[:12], provider.name, chunk_name)
            
            try:
                for sym in chunk_symbols:
                    if db.check_scan_status(scan_id) not in ("running",):
                        break
                    if get_scan_cancel_requested():
                        break

                    sym_start_time = time.monotonic()
                    try:
                        df = live_feed.fetch_historical(sym, days=DATA_LOOKBACK_DAYS)
                        api_duration = time.monotonic() - sym_start_time
                        
                        anal_start = time.monotonic()
                        if df is not None and not df.empty and len(df) >= 50:
                            r = fetch_and_analyze(sym, nifty_1m, regime, ext_df=df)
                            anal_duration = time.monotonic() - anal_start
                            if r:
                                chunk_results.append(r)
                                scored_syms.add(sym)
                                db.log_scan_event(scan_id, "SYMBOL_COMPLETED", f"Sym: {sym}, Chunk: {chunk_name}, API: {api_duration:.2f}s, Anal: {anal_duration:.2f}s")
                        else:
                            failed_syms.append(sym)
                            chunk_failed += 1
                            db.log_scan_event(scan_id, "SYMBOL_FAILED", f"Sym: {sym}, Chunk: {chunk_name}, Reason: Empty df")
                    except Exception as exc:
                        failed_syms.append(sym)
                        chunk_failed += 1
                        db.log_scan_event(scan_id, "SYMBOL_FAILED", f"Sym: {sym}, Chunk: {chunk_name}, Error: {str(exc)}")
                    
                    chunk_processed += 1
                    time.sleep(0.5)  # 0.5s between Angel calls (~2 req/s — within rate limit)
            finally:
                provider_manager.release_provider(provider.name)
                
            return chunk_name, chunk_symbols, chunk_results, failed_syms, chunk_processed, chunk_failed

        global_i = 0
        _reached_terminal = False
        
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_to_chunk = {}
            for chunk_name, chunk_symbols in chunks:
                if not chunk_symbols:
                    continue
                    
                chunk_run_id = db.start_chunk_run(scan_id, chunk_name, len(chunk_symbols))
                db.log_scan_event(scan_id, "CHUNK_STARTED", f"Chunk: {chunk_name} ({len(chunk_symbols)} symbols)")
                log.info("[%s] Queuing chunk: %s (%d symbols)", correlation_id[:12], chunk_name, len(chunk_symbols))
                
                future = executor.submit(_process_chunk_worker, chunk_name, chunk_symbols, scan_id, correlation_id, nifty_1m, regime, total)
                future_to_chunk[future] = (chunk_name, chunk_run_id, len(chunk_symbols))
                
            for future in as_completed(future_to_chunk):
                chunk_name, chunk_run_id, chunk_total = future_to_chunk[future]
                
                current_status = db.check_scan_status(scan_id)
                if current_status not in ("running",):
                    log.error("[%s] SCANNER_ABORT_DETECTED: Status changed to %s", correlation_id[:12], current_status)
                    db.log_scan_event(scan_id, "SCANNER_ABORT_DETECTED", f"Scan status changed to {current_status}")
                    _reached_terminal = True
                    break

                if get_scan_cancel_requested():
                    log.warning("[%s] Scan cancelled by user at %d/%d", correlation_id[:12], global_i, total)
                    db.log_scan_event(scan_id, "SCAN_CANCELLED", "User cancelled scan")
                    transition_scan_state(
                        scan_id=scan_id, from_status="running", to_status="cancelled",
                        reason="user_cancelled", actor=ACTOR_USER,
                        correlation_id=correlation_id,
                    )
                    _reached_terminal = True
                    break
                    
                try:
                    c_name, c_symbols, c_results, c_failed_syms, c_processed, c_failed = future.result()
                    
                    for r in c_results:
                        results.append(r)
                        scored_set.add(r['symbol'])
                    for f in c_failed_syms:
                        failed_symbols.append(f)
                        
                    global_i += c_processed
                    scan_state.set_progress(global_i)
                    if global_i % 50 == 0 or global_i == total:
                        log.info("[%s] Phase 1: %d/%d done, %d scored", correlation_id[:12], global_i, total, len(results))
                        
                    if c_failed > 0 and c_processed == 0:
                        chunk_status = "FAILED"
                    else:
                        chunk_status = "COMPLETED"
                    
                    db.end_chunk_run(chunk_run_id, chunk_status, c_processed, f"{c_failed} failed")
                    db.log_scan_event(scan_id, f"CHUNK_{chunk_status}", f"Chunk: {c_name}, Processed: {c_processed}")
                    
                    if c_results:
                        db.save_results(c_results, scan_id=scan_id)
                        
                except Exception as exc:
                    db.end_chunk_run(chunk_run_id, "FAILED", 0, f"Worker exception: {str(exc)}")
                    db.log_scan_event(scan_id, "CHUNK_FAILED", f"Chunk: {chunk_name}, Error: {str(exc)}")
                    
        if _reached_terminal:
            return

        scan_state.update(phase="phase1_done")
        log.info("[%s] Phase 1 done: %d scored, %d failed", correlation_id[:12], len(results), len(failed_symbols))

        # Phase 4, Section 37: Data quality gate — abort if too many symbols failed
        _degraded_data = False
        if total > 0:
            _fail_pct = len(failed_symbols) / total
            if _fail_pct > 0.05 and len(failed_symbols) > 5:
                # Check if this will improve in Phase 2 (jugaad_data fallback)
                # Only abort if we're missing critical mass (>5% AND more than 5 symbols)
                log.warning(
                    "[%s] Data quality check: %.1f%% symbols failed Phase 1 (%d/%d). "
                    "Will attempt jugaad_data fallback before final quality decision.",
                    correlation_id[:12], _fail_pct * 100, len(failed_symbols), total
                )

        # ── PHASE 2: jugaad_data fallback (has delivery %) ──
        if failed_symbols:
            log.info("[%s] Phase 2: jugaad_data fallback (%d stocks)...", correlation_id[:12], len(failed_symbols))
            jugaad_scored = 0
            for batch_start in range(0, len(failed_symbols), BATCH_SIZE):
                batch = failed_symbols[batch_start:batch_start + BATCH_SIZE]
                if batch_start > 0:
                    time.sleep(BATCH_DELAY)

                batch_results = []
                with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                    futures = {
                        executor.submit(fetch_and_analyze, sym, nifty_1m, regime): sym
                        for sym in batch
                    }
                    for future in as_completed(futures):
                        sym = futures[future]
                        try:
                            r = future.result()
                            if r:
                                results.append(r)
                                batch_results.append(r)
                                scored_set.add(sym)
                                jugaad_scored += 1
                        except Exception:
                            pass

                # Save batch to DB
                if batch_results:
                    db.save_results(batch_results, scan_id=scan_id)

                batch_num = batch_start // BATCH_SIZE + 1
                log.info("[%s] Phase 2 batch %d: +%d scored", correlation_id[:12], batch_num, jugaad_scored)

                # First batch 0 → jugaad blocked, skip
                if batch_num >= 1 and jugaad_scored == 0:
                    log.warning("[%s] Phase 2: jugaad_data blocked — skipping", correlation_id[:12])
                    break

            log.info("[%s] Phase 2 done: +%d from jugaad_data", correlation_id[:12], jugaad_scored)

        # Phase 4, Section 37: Final data quality gate (post-fallback)
        _final_failed = total - len(results)
        if total > 0 and _final_failed > 5:
            _final_fail_pct = _final_failed / total
            if _final_fail_pct > 0.05:
                # Hard abort — too many symbols have no price data at all
                _abort_reason = (
                    f"data_quality_abort: {_final_failed}/{total} symbols "
                    f"({_final_fail_pct:.1%}) failed both Angel One and yfinance"
                )
                log.error("[%s] %s", correlation_id[:12], _abort_reason)
                transition_scan_state(
                    scan_id=scan_id, from_status="running", to_status="failed",
                    reason=_abort_reason, actor=ACTOR_SYSTEM,
                    correlation_id=correlation_id,
                    error_message=_abort_reason,
                )
                _reached_terminal = True
                return

        # Phase 4: Check if non-critical feeds degraded (GDELT, MarketAux)
        # Intelligence warmup failures are soft — we continue but flag
        try:
            _warmup_meta = db.get_meta("intelligence_warmup_status", {})
            if isinstance(_warmup_meta, dict) and _warmup_meta.get("gdelt_failed"):
                _degraded_data = True
                log.warning("[%s] Non-critical feed degraded: GDELT unavailable", correlation_id[:12])
        except Exception:
            pass

        # Persist degraded_data flag to scan_runs
        if _degraded_data:
            try:
                db.execute_db(
                    "UPDATE scan_runs SET degraded_data=? WHERE scan_id=?",
                    (True, scan_id)
                )
                log.warning("[%s] Scan flagged as degraded_data=True", correlation_id[:12])
            except Exception as exc:
                log.debug("[%s] Failed to set degraded_data: %s", correlation_id[:12], exc)

        # ── POST-SCAN MARKETAUX: ENQUEUE TO BACKGROUND WORKER (Phase 8) ──
        results.sort(key=lambda x: x.get("score", 0), reverse=True)
        top30_syms = [
            r["symbol"] for r in results[:30]
            if not r.get("marketaux_queried", False)
        ]
        if top30_syms:
            enqueue_marketaux(top30_syms)
            log.info("[%s] MarketAux: Enqueued %d top symbols for background enrichment", correlation_id[:12], len(top30_syms))

        # ── FINALIZE ──
        db.log_scan_event(scan_id, "FINALIZE_STARTED", "")
        # Apply sector strength (modifies results in-place)
        try:
            db.log_scan_event(scan_id, "SECTOR_STRENGTH_STARTED", "")
            heatmap = apply_sector_strength(results)
            db.set_meta("heatmap", heatmap)
            db.log_scan_event(scan_id, "SECTOR_STRENGTH_COMPLETED", "")
        except Exception as exc:
            log.warning("[%s] Heatmap failed: %s", correlation_id[:12], exc)

        try:
            db.log_scan_event(scan_id, "AI_SUMMARY_STARTED", "")
            summary = generate_ai_summary(results, regime)
            db.set_meta("summary", summary)
            db.log_scan_event(scan_id, "AI_SUMMARY_COMPLETED", "")
        except Exception as exc:
            log.warning("[%s] Summary failed: %s", correlation_id[:12], exc)

        # Final save — all results with sector strength applied
        db.log_scan_event(scan_id, "SAVE_RESULTS_STARTED", "")
        db.save_results(results, scan_id=scan_id)
        db.log_scan_event(scan_id, "SAVE_RESULTS_COMPLETED", "")
        
        # Phase 5: Snapshot Governance (Immutable Research Freeze)
        db.log_scan_event(scan_id, "SNAPSHOT_STARTED", "")
        for r in results:
            if r.get("high_conviction") or r.get("score", 0) >= 65:
                try:
                    db.save_research_snapshot_v2(r.get("symbol"), r, context)
                except Exception as exc:
                    log.exception("[%s] Failed to save research snapshot for %s: %s", correlation_id[:12], r.get("symbol"), exc)
        db.log_scan_event(scan_id, "SNAPSHOT_COMPLETED", "")

        # Release 4: Submit signals to Execution Engine (real-time paper trading)
        # Replaces the legacy 11 AM batch snapshot — orders are now PENDING immediately
        try:
            from execution_engine import submit_order
            import recommendation_engine as _re
            # W4 (ADR-001/002/003): when RE2_RO_EXEC is ON, execution consumes the projected
            # Recommendation Object trade levels (Display == Execution), and REJECTED ROs are
            # fail-closed (not submitted). Flag default OFF → unchanged legacy behavior.
            _exec_ro = _re.RO_EXEC_ENABLED
            _exec_gen = None
            if _exec_ro:
                from datetime import datetime, timezone
                _exec_gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            _exec_submitted = 0
            _exec_failclosed = 0
            for r in results:
                if r.get("high_conviction") or r.get("score", 0) >= 65:
                    r_exec = r
                    if _exec_ro and r.get("symbol"):
                        try:
                            _proj = _re.projection.project_result_copy(r, scan_id, _exec_gen)
                            if _proj.get("_ro_status") == "REJECTED":
                                _exec_failclosed += 1
                                continue  # ADR-003 fail-closed: do not submit ineligible setups
                            # expose RO entry band at top-level (submit_order reads top-level keys)
                            _tr = _proj.get("trade") or {}
                            if _tr.get("entry_low") is not None:
                                _proj["entry_low"] = _tr["entry_low"]
                            if _tr.get("entry_high") is not None:
                                _proj["entry_high"] = _tr["entry_high"]
                            r_exec = _proj
                        except Exception:
                            r_exec = r  # fail-safe → legacy levels, never break the scan
                    if submit_order(r_exec, {"scan_id": scan_id, "correlation_id": correlation_id}):
                        _exec_submitted += 1
            if _exec_submitted > 0 or _exec_failclosed > 0:
                log.info("[%s] Execution Engine: %d signals submitted as PENDING orders (ro_exec=%s, fail_closed=%d)",
                         correlation_id[:12], _exec_submitted, _exec_ro, _exec_failclosed)
        except Exception as exc:
            log.warning("[%s] Execution Engine signal submission failed (non-fatal): %s", correlation_id[:12], exc)

        db.log_scan_event(scan_id, "FINALIZE_COMPLETED", "")
                    
        db.set_meta("last_scan", now_ist().strftime("%Y-%m-%d %H:%M IST"))
        db.set_meta("timestamp", now_ist().isoformat())

        elapsed = time.monotonic() - start_time
        hc_count = sum(1 for r in results if r.get("high_conviction"))
        log.info("[%s] Done in %.0fs! %d scored, %d HC", correlation_id[:12], elapsed, len(results), hc_count)

        # Phase F: Structured scan performance telemetry
        _rate = round(len(results) / elapsed, 2) if elapsed > 0 else 0
        log.info(
            "[SCAN PERF] scan_id=%s | correlation=%s | symbols=%d | results_saved=%d | failed=%d | "
            "duration=%dms | rate=%.2f/sec",
            scan_id[:20], correlation_id[:12], total, len(results), total - len(results),
            round(elapsed * 1000), _rate
        )

        # Persist timing metrics baseline
        try:
            import json
            from metrics.timer import get_report
            db.set_meta("perf_baseline", json.dumps({
                "captured_at": datetime.now().isoformat(),
                "scan_duration_min": round(elapsed / 60, 2),
                "symbol_count": len(results),
                "operations": get_report()
            }))
            log.info("[%s] Timing baseline persisted to scan_meta", correlation_id[:12])
        except Exception as exc:
            log.warning("[%s] Failed to persist timing baseline: %s", correlation_id[:12], exc)

        # Phase 0: Trust & Observability — audit trail
        try:
            from config import SCAN_VERSION
            _scan_start_str = datetime.fromtimestamp(start_time + time.time() - time.monotonic()).strftime("%Y-%m-%d %H:%M:%S")
            _scan_end_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Score audit: one row per stock per scan
            db.save_score_audit(results, scan_id, SCAN_VERSION)

            # Scan audit: one row per scan run
            db.save_scan_audit(
                scan_id=scan_id,
                start_time=_scan_start_str,
                end_time=_scan_end_str,
                duration_ms=round(elapsed * 1000),
                stocks_scanned=total,
                stocks_succeeded=len(results),
                stocks_failed=total - len(results),
                data_source="ANGEL",  # primary source used
                scan_version=SCAN_VERSION,
                scan_mode=context.trigger_source,
            )
        except Exception as exc:
            log.warning("[%s] Phase 0: audit trail failed (non-fatal): %s", correlation_id[:12], exc)

        # ── R1 EVIDENCE COLLECTION (Append-Only, Schema-Frozen) ──────
        try:
            import csv as _csv
            from datetime import date as _obs_date
            from pathlib import Path as _Path

            _R1_DEPLOY_DATE = "2026-06-08"
            _today_str = now_ist().strftime("%Y-%m-%d")
            _obs_day = (_obs_date.today() - _obs_date.fromisoformat(_R1_DEPLOY_DATE)).days + 1
            _release = "R1.0"
            _audit_dir = _Path(__file__).parent / "release_audits"
            _audit_dir.mkdir(parents=True, exist_ok=True)

            # Scan status classification
            _fail_count = total - len(results)
            if _fail_count == 0:
                _scan_status = "SUCCESS"
            elif len(results) > total * 0.5:
                _scan_status = "PARTIAL"
            else:
                _scan_status = "FAILED"

            # Pre-compute score percentiles
            _scores = sorted([r.get("score", 0) for r in results]) if results else []
            def _pct(p):
                if not _scores: return 0
                idx = int(len(_scores) * p / 100)
                return _scores[min(idx, len(_scores) - 1)]

            _hc_count = sum(1 for r in results if r.get("high_conviction"))
            _golden_count = sum(1 for r in results if r.get("is_golden"))
            _top_sym = results[0].get("symbol", "") if results else ""
            _top_score = results[0].get("score", 0) if results else 0

            # Store scan_id for trade_outcomes.csv to reference
            db.set_meta("current_scan_id", scan_id)

            _manifest_rows = []  # (artifact_name, rows_written)

            # ── Artifact 1: daily_release1_snapshot.csv ──
            _snap_path = _audit_dir / "daily_release1_snapshot.csv"
            _snap_header = not _snap_path.exists()
            with open(_snap_path, "a", newline="", encoding="utf-8") as f:
                w = _csv.writer(f)
                if _snap_header:
                    w.writerow([
                        "Date", "Scan ID", "Release Version", "Observation Day",
                        "Scan Status", "Stocks Attempted", "Stocks Successfully Analyzed",
                        "Stocks Failed", "HC Count", "Golden Count",
                        "P50", "P75", "P90", "P95", "P99",
                        "Max Score", "Top Symbol", "Top Score",
                    ])
                w.writerow([
                    _today_str, scan_id, _release, _obs_day,
                    _scan_status, total, len(results), _fail_count,
                    _hc_count, _golden_count,
                    _pct(50), _pct(75), _pct(90), _pct(95), _pct(99),
                    _scores[-1] if _scores else 0, _top_sym, _top_score,
                ])
            _manifest_rows.append(("daily_release1_snapshot.csv", 1))
            log.info("[R1 Evidence] daily_release1_snapshot.csv appended (Day %d)", _obs_day)

            # ── Artifact 2: daily_top20_snapshot.csv ──
            _top20_path = _audit_dir / "daily_top20_snapshot.csv"
            _top20_header = not _top20_path.exists()
            _ranked = sorted(results, key=lambda x: x.get("score", 0), reverse=True)[:20]
            with open(_top20_path, "a", newline="", encoding="utf-8") as f:
                w = _csv.writer(f)
                if _top20_header:
                    w.writerow([
                        "Date", "Scan ID", "Release Version", "Observation Day",
                        "Rank", "Symbol", "Score", "HC", "Golden",
                        "Risk", "RR", "Sector", "Sector Rotation Score",
                    ])
                for rank, r in enumerate(_ranked, 1):
                    w.writerow([
                        _today_str, scan_id, _release, _obs_day,
                        rank, r.get("symbol", ""), r.get("score", 0),
                        1 if r.get("high_conviction") else 0,
                        1 if r.get("is_golden") else 0,
                        r.get("risk_score", 0), r.get("risk_reward", 0),
                        r.get("sector", ""), r.get("sector_rotation_score", 0),
                    ])
            _manifest_rows.append(("daily_top20_snapshot.csv", len(_ranked)))
            log.info("[R1 Evidence] daily_top20_snapshot.csv appended (%d rows)", len(_ranked))

            # ── Artifact 3: daily_open_trades_mtm.csv ──
            _mtm_path = _audit_dir / "daily_open_trades_mtm.csv"
            _mtm_header = not _mtm_path.exists()
            _open_trades = db.get_open_paper_trades()
            # Build price lookup from scan results
            _price_map = {r.get("symbol", ""): r.get("price", 0) for r in results}
            _mtm_written = 0
            with open(_mtm_path, "a", newline="", encoding="utf-8") as f:
                w = _csv.writer(f)
                if _mtm_header:
                    w.writerow([
                        "Date", "Scan ID", "Release Version", "Observation Day",
                        "Date Opened", "Symbol", "Entry Price", "Current Price",
                        "Unrealized Return %", "HC Flag (Entry)", "Score (Entry)",
                    ])
                for t in _open_trades:
                    _sym = t.get("symbol", "")
                    _entry_p = t.get("entry_price", 0)
                    _curr_p = _price_map.get(_sym, _entry_p)
                    _unreal = round(((_curr_p - _entry_p) / _entry_p) * 100, 2) if _entry_p > 0 else 0
                    w.writerow([
                        _today_str, scan_id, _release, _obs_day,
                        t.get("entry_date", ""), _sym, _entry_p, _curr_p,
                        _unreal, t.get("high_conviction", 0), t.get("score_at_entry", 0),
                    ])
                    _mtm_written += 1
            _manifest_rows.append(("daily_open_trades_mtm.csv", _mtm_written))
            log.info("[R1 Evidence] daily_open_trades_mtm.csv appended (%d open trades)", _mtm_written)

            # ── Artifact 4: daily_hc_funnel_snapshot.csv ──
            _funnel_path = _audit_dir / "daily_hc_funnel_snapshot.csv"
            _funnel_header = not _funnel_path.exists()
            from config import (HC_MIN_SCORE, HC_RSI_RANGE, HC_DELIVERY_MIN,
                                HC_ATR_RANGE, HC_RISK_MAX, HC_MIN_RISK_REWARD,
                                HC_REQUIRE_MACD_BULLISH, HC_REQUIRE_VOLUME,
                                HC_MIN_SIGNALS_BULLISH)
            # Sequential funnel attrition
            _universe = len(results)
            _pool = results[:]
            _pool = [r for r in _pool if HC_RSI_RANGE[0] <= (r.get("rsi") or 0) <= HC_RSI_RANGE[1]]
            _after_rsi = len(_pool)
            _pool = [r for r in _pool if (r.get("delivery_pct") or 50.0) >= HC_DELIVERY_MIN]
            _after_dlv = len(_pool)
            _pool = [r for r in _pool if HC_ATR_RANGE[0] <= (r.get("atr_pct") or 0) <= HC_ATR_RANGE[1]]
            _after_atr = len(_pool)
            _pool = [r for r in _pool if (r.get("risk_score") or 0) <= HC_RISK_MAX]
            _after_risk = len(_pool)
            _pool = [r for r in _pool if (r.get("risk_reward") or 0) >= HC_MIN_RISK_REWARD]
            _after_rr = len(_pool)
            if HC_REQUIRE_MACD_BULLISH:
                _pool = [r for r in _pool if r.get("macd_signal") == "Bullish"]
            _after_vol = len([r for r in _pool if (r.get("volume_ratio") or 1.0) >= HC_REQUIRE_VOLUME])
            _pool = [r for r in _pool if (r.get("volume_ratio") or 1.0) >= HC_REQUIRE_VOLUME]
            _after_score = len([r for r in _pool if (r.get("score") or 0) >= HC_MIN_SCORE])
            with open(_funnel_path, "a", newline="", encoding="utf-8") as f:
                w = _csv.writer(f)
                if _funnel_header:
                    w.writerow([
                        "Date", "Scan ID", "Release Version", "Observation Day",
                        "HC Threshold Used", "Universe", "After RSI", "After Delivery",
                        "After ATR", "After Risk", "After RR",
                        "After Volume", "After Score", "Final HC",
                    ])
                w.writerow([
                    _today_str, scan_id, _release, _obs_day,
                    HC_MIN_SCORE, _universe, _after_rsi, _after_dlv,
                    _after_atr, _after_risk, _after_rr,
                    _after_vol, _after_score, _hc_count,
                ])
            _manifest_rows.append(("daily_hc_funnel_snapshot.csv", 1))
            log.info("[R1 Evidence] daily_hc_funnel_snapshot.csv appended (Day %d)", _obs_day)

            # ── Manifest: Artifact Health Check ──
            _manifest_path = _audit_dir / "manifest.csv"
            _manifest_hdr = not _manifest_path.exists()
            with open(_manifest_path, "a", newline="", encoding="utf-8") as f:
                w = _csv.writer(f)
                if _manifest_hdr:
                    w.writerow(["Date", "Scan ID", "Artifact Name", "Rows Written"])
                for _art_name, _art_rows in _manifest_rows:
                    w.writerow([_today_str, scan_id, _art_name, _art_rows])
            log.info("[R1 Evidence] manifest.csv updated (%d artifacts validated)", len(_manifest_rows))

        except Exception as _ev_exc:
            log.warning("[R1 Evidence] Evidence collection failed (non-fatal): %s", _ev_exc)
        # ── END R1 EVIDENCE COLLECTION ───────────────────────────────


        # Subscribe all to live feed
        live_feed.subscribe([r["symbol"] for r in results])

        # Clean up stale detail cache files (72h retention)
        try:
            from cache_layer import cleanup_detail_cache
            cleanup_detail_cache()
        except Exception:
            pass

        # Phase 0A: Mark completed via atomic transition
        transition_scan_state(
            scan_id=scan_id, from_status="running", to_status="completed",
            reason="scan_completed", actor=ACTOR_SYSTEM,
            correlation_id=correlation_id,
        )
        _reached_terminal = True
        db.log_scan_event(scan_id, "SCAN_COMPLETED", "")

    except Exception as e:
        log.error("[%s] Fatal scan error: %s", correlation_id[:12], e, exc_info=True)
        db.log_scan_event(scan_id, "SCAN_FAILED", str(e))
    finally:
        # Stop the heartbeat worker thread
        if 'heartbeat_stop' in locals():
            heartbeat_stop.set()

        # Phase 0B: GUARANTEED terminal state — if we haven't reached one yet,
        # force it now. This catches edge cases where exceptions bypass the
        # normal completion path.
        if not _reached_terminal:
            log.warning("[%s] Finally block: scan did not reach terminal state — forcing FAILED", correlation_id[:12])
            transition_scan_state(
                scan_id=scan_id, from_status="running", to_status="failed",
                reason="finally_block_recovery", actor=ACTOR_SYSTEM,
                correlation_id=correlation_id,
                error_message="scan_exited_without_terminal_state",
            )
        db.clear_meta_cache()  # Phase 1: ensure fresh metadata after scan


# ═══════════════════════════════════════════════════════════════
# Phase 5.5: Parallel Scan Engine
# ═══════════════════════════════════════════════════════════════

import queue as _q

def _run_parallel_scan(context: ScanContext):
    """
    Phase 5.5/5.6B/C: Queue-based parallel scan with 2 persistent workers.

    Architecture:
      1. Load eligible universe from active version (or resume from checkpoint)
      2. Safety gate: abort if eligible_universe < 500
      3. Acquire scan lock (heartbeat-based)
      4. Split into batches of SCAN_BATCH_SIZE
      5. Create persistent workers that pull from batch queue
      6. Progressive publish every PROGRESSIVE_PUBLISH_INTERVAL stocks
      7. Update resume checkpoint after each batch
      8. Finalize (sector strength, AI summary, evidence)
      9. Release lock + cleanup resume state
    """
    from config import (
        SCAN_BATCH_SIZE, MAX_SCAN_WORKERS, PROGRESSIVE_PUBLISH_INTERVAL,
        DATA_LOOKBACK_DAYS, SCAN_DURATION_ALERT_MINUTES,
    )

    scan_id = context.scan_id
    correlation_id = context.correlation_id

    log.info("[%s] ═══ Phase 5.6B/C: Parallel Scan Engine ═══", correlation_id[:12])

    # ── Safety Gate: Check eligible_universe count ────────────
    # If eligible_universe is too small (enrichment not yet complete),
    # fall back to candidate universe (EQ stocks only, no ETF/NAV/MF)
    _use_fallback_universe = False
    try:
        eu_count_row = db.execute_db(
            "SELECT COUNT(*) as c FROM eligible_universe",
            fetch="one"
        )
        eu_count = int(eu_count_row.get("c", 0)) if eu_count_row else 0

        if eu_count < 500:
            log.warning("[%s] eligible_universe count=%d < 500. Attempting candidate fallback...",
                      correlation_id[:12], eu_count)
            _use_fallback_universe = True
    except Exception as exc:
        log.warning("[%s] Safety gate check failed (non-fatal): %s", correlation_id[:12], exc)
        _use_fallback_universe = True

    # ── 1. Load or resume universe ──────────────────────────────
    resume = db.get_pending_resume()
    start_batch = 0
    active_universe_version = db.get_meta("active_universe_version")

    if resume and resume.get("status") == "running":
        resume_version = resume.get("universe_version")
        if active_universe_version and resume_version != active_universe_version:
            log.warning(
                "[%s] [RESUME_VERSION_MISMATCH] resume=%s active=%s",
                correlation_id[:12], resume_version, active_universe_version
            )
            old_scan_id = resume.get("scan_id")
            if old_scan_id:
                try:
                    db.clear_scan_resume_state(old_scan_id)
                    db.transition_scan_state(old_scan_id, "running", "failed", reason="stale_resume_abandoned", actor=ACTOR_SYSTEM)
                except Exception as exc:
                    log.warning("[%s] Failed to clear stale resume %s: %s", correlation_id[:12], old_scan_id, exc)
            resume = None
        else:
            universe_version = resume_version
            eligible_rows = db.get_eligible_universe(universe_version)
            eligible = [r["symbol"] for r in eligible_rows] if eligible_rows else []
            start_batch = resume.get("current_batch_index", 0)
            log.info("[%s] RESUMING scan from batch %d, universe=%s (%d stocks)",
                     correlation_id[:12], start_batch, universe_version, len(eligible))

    if not resume or resume.get("status") != "running":
        if _use_fallback_universe:
            # FALLBACK: Use candidate universe directly (EQ only, no ETF/NAV/MF)
            log.warning("[%s] FALLBACK: Building scan universe from universe_catalog (EQ only)", correlation_id[:12])
            try:
                fallback_rows = db.execute_db(
                    """SELECT symbol FROM universe_catalog 
                       WHERE is_active = TRUE 
                       AND instrument_type = 'EQ'
                       ORDER BY symbol""",
                    fetch="all"
                )
                eligible = [r["symbol"] for r in fallback_rows] if fallback_rows else []
                universe_version = "FALLBACK_EQ"
                log.info("[%s] FALLBACK universe: %d EQ stocks (filtered ETF/NAV/MF)",
                         correlation_id[:12], len(eligible))
            except Exception as exc:
                log.error("[%s] FALLBACK universe query failed: %s", correlation_id[:12], exc)
                eligible = []
                universe_version = "FALLBACK_FAILED"
        else:
            # Normal path: use eligible_universe
            universe_version = active_universe_version
            if not universe_version:
                log.error("[%s] EMERGENCY FALLBACK: No active universe version found.", correlation_id[:12])
                scan_state.complete(success=False, error_message="EMERGENCY FALLBACK: No active universe version.")
                raise RuntimeError("EMERGENCY FALLBACK: No active universe version found. Scan aborted.")
                
            eligible_rows = db.get_eligible_universe(universe_version)
            if not eligible_rows:
                # Instead of crashing, fall back to universe_catalog EQ stocks
                log.warning("[%s] Frozen universe %s has 0 members — falling back to universe_catalog EQ", correlation_id[:12], universe_version)
                try:
                    fallback_rows = db.execute_db(
                        """SELECT symbol FROM universe_catalog 
                           WHERE is_active = TRUE 
                           AND instrument_type = 'EQ'
                           ORDER BY symbol""",
                        fetch="all"
                    )
                    eligible = [r["symbol"] for r in fallback_rows] if fallback_rows else []
                    universe_version = "FALLBACK_EQ"
                    log.info("[%s] FALLBACK universe: %d EQ stocks", correlation_id[:12], len(eligible))
                except Exception as exc:
                    log.error("[%s] FALLBACK universe query also failed: %s", correlation_id[:12], exc)
                    eligible = []
            else:
                eligible = [row["symbol"] for row in eligible_rows]

    MIN_UNIVERSE_SIZE = 100  # Lowered from 500 — enrichment may still be in progress
    if not eligible or len(eligible) < MIN_UNIVERSE_SIZE:
        # P0.1A: Forensic log for resume corruption detection
        if resume and resume.get("status") == "running":
            _resume_ver = resume.get("universe_version", "UNKNOWN")
            _resume_cnt = len(eligible) if eligible else 0
            _active_ver = active_universe_version or "UNKNOWN"
            try:
                _active_rows = db.get_eligible_universe(_active_ver) if _active_ver != "UNKNOWN" else []
                _active_cnt = len(_active_rows) if _active_rows else 0
            except Exception:
                _active_cnt = -1
            log.error("[RESUME_CORRUPT] scan_id=%s resume_version=%s resume_count=%d active_version=%s active_count=%d",
                      scan_id, _resume_ver, _resume_cnt, _active_ver, _active_cnt)
            
            # Clear the stale resume state that caused this
            old_scan_id = resume.get("scan_id")
            if old_scan_id:
                try:
                    db.clear_scan_resume_state(old_scan_id)
                    db.transition_scan_state(old_scan_id, "running", "failed", reason="stale_resume_abandoned", actor=ACTOR_SYSTEM)
                except Exception:
                    pass

        log.error("[%s] Universe too small (count=%d, min=%d). Scan blocked.", correlation_id[:12], len(eligible) if eligible else 0, MIN_UNIVERSE_SIZE)
        scan_state.complete(success=False, error_message="Universe too small: %d < %d" % (len(eligible) if eligible else 0, MIN_UNIVERSE_SIZE))
        return

    total = len(eligible)
    log.info("[%s] Universe: %d stocks, version=%s, batch_size=%d, workers=%d",
             correlation_id[:12], total, universe_version, SCAN_BATCH_SIZE, MAX_SCAN_WORKERS)

    # Persist the resolved universe for debugging
    try:
        save_active_universe(eligible)
    except Exception:
        pass

    # ── 2. Acquire scan lock ─────────────────────────────────────
    lock_acquired = scan_state.start(total, mode=context.trigger_source, context=context)
    if lock_acquired is None:
        log.warning("[%s] Scan rejected — another scan is already active", correlation_id[:12])
        return

    # Lock universe version into scan_runs (P0 Governance)
    db.execute_db("UPDATE scan_runs SET universe_version = ? WHERE scan_id = ?", (universe_version, scan_id))

    if not db.acquire_scan_lock_v2(scan_id, context.correlation_id):
        log.warning("[%s] Scan lock held by another owner — aborting", correlation_id[:12])
        return

    db.clear_meta_cache()
    log.info("[%s] Scan lock acquired, starting parallel scan against Universe %s", correlation_id[:12], universe_version)
    
    # Store universe metadata in scan_runs for forensic audit
    try:
        db.execute_db("UPDATE scan_runs SET universe_version=? WHERE scan_id=?", (universe_version, scan_id))
        # Phase 5.6B/C: Record candidate + eligible counts for forensic analysis
        candidate_count_meta = db.get_meta("candidate_frozen_count") or "0"
        db.execute_db(
            """UPDATE scan_runs SET candidate_count=?
               WHERE scan_id=?""",
            (total, scan_id)
        )
        log.info("[%s] Scan universe audit: version=%s eligible=%d candidates=%s",
                 correlation_id[:12], universe_version, total, candidate_count_meta)
    except Exception as exc:
        log.warning("[%s] Failed to store universe metadata in scan_runs: %s", correlation_id[:12], exc)

    start_time = time.monotonic()

    db.log_scan_event(scan_id, "SCAN_STARTED",
                      f"Parallel scan: {total} stocks, {universe_version}")

    # Phase 0B: Track terminal state
    _reached_terminal = False

    # Phase B: Start heartbeat
    heartbeat_stop = threading.Event()
    heartbeat_thread = threading.Thread(
        target=_scan_heartbeat_worker, args=(scan_id, heartbeat_stop), daemon=True
    )
    heartbeat_thread.start()

    try:
        # ── 3. Pre-scan intelligence warmup ──────────────────────
        try:
            from intelligence import warmup_all
            warmup_all(set(eligible))
        except Exception as exc:
            log.warning("[%s] Intelligence warmup failed (continuing): %s",
                        correlation_id[:12], exc)

        reset_delivery_state()

        nifty_1m, regime = get_nifty50_benchmark()
        db.set_meta("nifty50_1m", nifty_1m)
        db.set_meta("market_regime", regime)
        log.info("[%s] Nifty 1M: %+.2f%% | Regime: %s",
                 correlation_id[:12], nifty_1m, regime.upper())

        # ── 4. Split into batches ────────────────────────────────
        batches = [eligible[i:i + SCAN_BATCH_SIZE]
                   for i in range(0, total, SCAN_BATCH_SIZE)]
        total_batches = len(batches)

        log.info("[%s] Created %d batches of %d stocks each",
                 correlation_id[:12], total_batches, SCAN_BATCH_SIZE)

        # Create batch records in DB
        db.create_scan_batches(scan_id, batches)

        # Save resume state
        db.save_scan_resume_state(scan_id, universe_version, total_batches, start_batch)

        # ── 5. Create batch queue + persistent workers ───────────
        batch_queue = _q.Queue()
        results_queue = _q.Queue()

        # Load pending batches into queue
        for idx in range(start_batch, total_batches):
            batch_queue.put((idx, batches[idx]))

        # Sentinel values to stop workers
        for _ in range(MAX_SCAN_WORKERS):
            batch_queue.put(None)

        # Start persistent workers (Rule 7: created once, stay alive)
        workers = []
        for w_id in range(MAX_SCAN_WORKERS):
            t = threading.Thread(
                target=_persistent_scan_worker,
                args=(f"worker-{w_id}", scan_id, batch_queue, results_queue,
                      nifty_1m, regime, context, PROGRESSIVE_PUBLISH_INTERVAL),
                daemon=True,
                name=f"scan-worker-{w_id}",
            )
            t.start()
            workers.append(t)
            log.info("[%s] Started persistent worker-%d", correlation_id[:12], w_id)

        # ── 6. Main thread: consume results, track progress ─────
        all_results = []
        completed_batches = start_batch
        global_processed = 0
        alert_fired = False

        while completed_batches < total_batches:
            try:
                batch_result = results_queue.get(timeout=120)
            except _q.Empty:
                # Check if workers are still alive
                alive = sum(1 for t in workers if t.is_alive())
                if alive == 0:
                    log.error("[%s] All workers dead — breaking", correlation_id[:12])
                    break

                # Check 2: Stale batch recovery — re-queue batches stuck RUNNING > 5 min
                recovered = db.recover_stale_batches(scan_id, stale_threshold_seconds=300)
                if recovered:
                    for r_idx in recovered:
                        if r_idx < len(batches):
                            batch_queue.put((r_idx, batches[r_idx]))
                            log.info("[%s] Re-queued recovered stale batch %d",
                                     correlation_id[:12], r_idx)

                log.warning("[%s] Waiting for batch results (workers alive: %d)",
                            correlation_id[:12], alive)
                continue

            if batch_result is None:
                continue

            batch_idx, batch_results = batch_result
            all_results.extend(batch_results)
            completed_batches += 1
            global_processed += len(batches[batch_idx])

            # Mark batch complete
            db.complete_batch(scan_id, batch_idx, len(batch_results))

            # Update resume checkpoint
            db.save_scan_resume_state(scan_id, universe_version,
                                      total_batches, completed_batches)

            # ── Progressive Save: results appear on frontend immediately ──
            if batch_results:
                try:
                    db.save_results(batch_results,
                                   scan_id=scan_id,
                                   meta={"last_scan": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
                                         "scan_id": scan_id,
                                         "universe_version": universe_version})
                except Exception as prog_exc:
                    log.warning("[%s] Progressive save failed (non-fatal): %s",
                                correlation_id[:12], prog_exc)

            # Update live progress
            scan_state.set_progress(global_processed)
            update_scan_progress(scan_id, processed_count=global_processed)

            # Refresh lock heartbeat
            db.refresh_scan_lock_heartbeat(scan_id)

            elapsed = time.monotonic() - start_time
            rate = global_processed / elapsed * 60 if elapsed > 0 else 0
            remaining_batches = total_batches - completed_batches
            eta_seconds = (remaining_batches * (elapsed / completed_batches)) if completed_batches > 0 else 0

            # ── Professional progress message for frontend ──
            if remaining_batches > 0:
                eta_min = int(eta_seconds / 60)
                eta_str = f"~{eta_min} min remaining" if eta_min >= 1 else "< 1 min remaining"
                progress_msg = (f"Analysing {total} stocks • "
                                f"Batch {completed_batches}/{total_batches} complete • "
                                f"{len(all_results)} results found • {eta_str}")
            else:
                progress_msg = (f"Finalising analysis • {len(all_results)} stocks scored • "
                                f"Generating report...")
            db.set_meta("scan_progress_message", progress_msg)

            log.info("[%s] Batch %d/%d complete: +%d results | %d/%d total | "
                     "%.1f stocks/min | %.0fs elapsed",
                     correlation_id[:12], completed_batches, total_batches,
                     len(batch_results), global_processed, total, rate, elapsed)

            # Performance alert (Rule 12)
            if not alert_fired and elapsed > SCAN_DURATION_ALERT_MINUTES * 60:
                log.warning("[SCAN_PERFORMANCE_ALERT] scan_id=%s exceeded %d min "
                            "(%.0fs elapsed, %d/%d processed)",
                            scan_id, SCAN_DURATION_ALERT_MINUTES, elapsed,
                            global_processed, total)
                db.log_scan_event(scan_id, "SCAN_PERFORMANCE_ALERT",
                                  f"Exceeded {SCAN_DURATION_ALERT_MINUTES}min")
                alert_fired = True

        # ── 7. Wait for workers to finish ────────────────────────
        for t in workers:
            t.join(timeout=30)

        elapsed = time.monotonic() - start_time
        log.info("[%s] All workers finished: %d results in %.1fs",
                 correlation_id[:12], len(all_results), elapsed)

        # ── 8. Finalize ──────────────────────────────────────────
        if all_results:
            # Apply sector strength
            try:
                heatmap = apply_sector_strength(all_results)  # modifies all_results in-place, returns heatmap
                db.set_meta("heatmap", heatmap)  # save heatmap separately
            except Exception:
                pass

            # Generate AI summary
            try:
                generate_ai_summary(all_results, regime)
            except Exception:
                pass

            # Final save (ensures all results are persisted)
            db.save_results(all_results,
                           scan_id=scan_id,
                           meta={"last_scan": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
                                 "scan_id": scan_id,
                                 "universe_version": universe_version})

            # Subscribe to live feed
            live_feed.subscribe([r["symbol"] for r in all_results if r.get("symbol")])

            # RE-3 P0 (RE-2A §3): shadow-build canonical Recommendation Objects.
            # Flag-gated (RE2_RO_BUILD, default OFF) and fully exception-isolated — this
            # is a non-consuming SHADOW step and must NEVER affect the scan.
            try:
                import recommendation_engine
                if recommendation_engine.RO_BUILD_ENABLED:
                    summary = recommendation_engine.shadow_build_results(
                        all_results, scan_id, persist=True)
                    log.info("[RE3-P0] shadow RO build: %s", summary)
            except Exception as _ro_exc:
                log.warning("[RE3-P0] shadow RO build skipped (non-fatal): %s", _ro_exc)

        # Record performance metrics
        db.set_meta("scan_duration_s", str(round(elapsed)))
        db.set_meta("scan_stocks_per_min", str(round(len(all_results) / elapsed * 60) if elapsed > 0 else 0))
        db.set_meta("scan_universe_version", universe_version)
        db.set_meta("scan_worker_count", str(MAX_SCAN_WORKERS))
        db.set_meta("scan_batch_size", str(SCAN_BATCH_SIZE))

        # Check for FAILED_PERMANENTLY batches → COMPLETED_WITH_ERRORS
        failed_batches = db.execute_db(
            "SELECT COUNT(*) as cnt FROM scan_batches WHERE scan_id = ? AND status = 'FAILED_PERMANENTLY'",
            (scan_id,), fetch="one"
        )
        failed_count = (failed_batches or {}).get("cnt", 0)

        if failed_count > 0:
            # RC2-A: 'completed_with_errors' is NOT a registered terminal in
            # VALID_TRANSITIONS, so transition_scan_state silently rejected it and the
            # scan stuck in 'running' (current_scan_state never reset). Use the valid
            # 'completed' terminal; the failed-batch count is preserved via final_reason
            # and the scan_failed_batches meta below.
            final_status = "completed"
            final_reason = f"parallel_scan_completed_with_{failed_count}_failed_batches"
            log.warning("[%s] Scan completed with %d FAILED_PERMANENTLY batches",
                        correlation_id[:12], failed_count)
        else:
            final_status = "completed"
            final_reason = "parallel_scan_completed"

        # Phase 0A: Mark terminal state
        # RC2-A: capture the transition result. A rejected/raced transition returns
        # False; we must NOT then mark the scan terminal, otherwise the finally-block
        # recovery is skipped and current_scan_state stays stuck in 'running'.
        _reached_terminal = transition_scan_state(
            scan_id=scan_id, from_status="running", to_status=final_status,
            reason=final_reason, actor=ACTOR_SYSTEM,
            correlation_id=correlation_id,
        )
        db.set_meta("scan_failed_batches", str(failed_count))
        db.log_scan_event(scan_id, f"SCAN_{final_status.upper()}",
                          f"Parallel: {len(all_results)} results, {elapsed:.0f}s, "
                          f"{universe_version}, failed_batches={failed_count}")

    except Exception as e:
        log.error("[%s] Parallel scan fatal error: %s", correlation_id[:12], e, exc_info=True)
        db.log_scan_event(scan_id, "SCAN_FAILED", str(e))
    finally:
        # Stop heartbeat
        heartbeat_stop.set()

        # Guaranteed terminal state
        if not _reached_terminal:
            log.warning("[%s] Finally: forcing FAILED state", correlation_id[:12])
            recovered = False
            try:
                recovered = transition_scan_state(
                    scan_id=scan_id, from_status="running", to_status="failed",
                    reason="finally_block_recovery", actor=ACTOR_SYSTEM,
                    correlation_id=correlation_id,
                    error_message="parallel_scan_exited_without_terminal_state",
                )
            except Exception as e:
                log.error("[%s] Finally block transition failed: %s", correlation_id[:12], e)
            # RC2-A: if recovery did not cleanly reach a terminal — whether it raised OR
            # returned False (rejected/raced) — force current_scan_state back to idle so
            # the UI/lock never stays stuck in RUNNING.
            if not recovered:
                db.execute_db("UPDATE current_scan_state SET status='idle', phase='' WHERE id=1")

        # Cleanup
        try:
            db.clear_scan_resume_state(scan_id)
        except Exception: pass
        
        try:
            db.release_scan_lock_v2(scan_id)
        except Exception: pass
        
        try:
            db.clear_meta_cache()
        except Exception: pass

        log.info("[%s] ═══ Parallel Scan Engine: DONE ═══", correlation_id[:12])


def _persistent_scan_worker(worker_id: str, scan_id: str,
                             batch_queue: _q.Queue, results_queue: _q.Queue,
                             nifty_1m: float, regime: str,
                             context: ScanContext,
                             publish_interval: int = 25):
    """
    Phase 5.5, Rule 7: Persistent worker that pulls batches from queue.

    - Created once, stays alive for entire scan lifecycle
    - Pulls batch from queue, processes symbols, sends results back
    - Progressive publish every `publish_interval` stocks within a batch
    - Stops on None sentinel
    """
    correlation_id = context.correlation_id
    log.info("[%s][%s] Worker started", correlation_id[:12], worker_id)

    while True:
        item = batch_queue.get()
        if item is None:
            log.info("[%s][%s] Received shutdown sentinel", correlation_id[:12], worker_id)
            break

        batch_idx, symbols = item
        log.info("[%s][%s] Processing batch %d (%d symbols)",
                 correlation_id[:12], worker_id, batch_idx, len(symbols))

        # Claim batch in DB
        db.claim_next_batch(scan_id, batch_idx, worker_id)

        batch_results = []
        batch_publish_buffer = []

        for sym_idx, sym in enumerate(symbols):
            # Cancel check
            if get_scan_cancel_requested():
                log.warning("[%s][%s] Cancel requested — stopping", correlation_id[:12], worker_id)
                break

            try:
                df = live_feed.fetch_historical(sym, days=DATA_LOOKBACK_DAYS)
                if df is not None and not df.empty and len(df) >= 50:
                    r = fetch_and_analyze(sym, nifty_1m, regime, ext_df=df)
                    if r:
                        # ── Phase 5.7: Immutable First Analysis Lock ──
                        try:
                            # Only overlay if recommendation lock is ACTIVE
                            locked_thesis = db.get_locked_thesis(sym)
                            is_active = (locked_thesis is not None and locked_thesis.get("thesis_status") == "ACTIVE")

                            first = db.get_first_analysis(sym)
                            if first is None:
                                # First time — lock this analysis permanently
                                db.save_first_analysis(sym, r, scan_id=correlation_id)
                            else:
                                # Rescan — save as new version
                                db.save_rescan_analysis(sym, r, scan_id=correlation_id,
                                                        change_reason="rescan")
                                if is_active:
                                    # Overlay first analysis values onto current result
                                    # so frontend always shows the original recommendation
                                    for lock_key in ("entry_low", "entry_high", "stop_loss",
                                                     "target_price", "target1", "target2", "target3",
                                                     "risk_reward", "score", "grade",
                                                     "confidence_score", "risk_score"):
                                        if first.get(lock_key) is not None:
                                            r[lock_key] = first[lock_key]
                                    r["first_analysis_date"] = str(first.get("analysis_timestamp", ""))
                                    r["rescan_count"] = (first.get("version", 1))
                        except Exception as fa_exc:
                            log.debug("[%s] First analysis lock failed for %s: %s",
                                      correlation_id[:12], sym, fa_exc)

                        batch_results.append(r)
                        batch_publish_buffer.append(r)
            except Exception as exc:
                log.debug("[%s][%s] Symbol %s failed: %s",
                          correlation_id[:12], worker_id, sym, exc)

            # Progressive publish every publish_interval stocks (Rule 8 intent)
            if len(batch_publish_buffer) >= publish_interval:
                try:
                    db.save_results(batch_publish_buffer, scan_id=scan_id)
                    log.info("[%s][%s] Progressive publish: %d results (batch %d, %d/%d)",
                             correlation_id[:12], worker_id, len(batch_publish_buffer),
                             batch_idx, sym_idx + 1, len(symbols))
                except Exception as exc:
                    log.warning("[%s][%s] Progressive publish failed: %s",
                                correlation_id[:12], worker_id, exc)
                batch_publish_buffer = []

        # Publish remaining results in buffer
        if batch_publish_buffer:
            try:
                db.save_results(batch_publish_buffer, scan_id=scan_id)
            except Exception as exc:
                log.warning("[%s][%s] Final batch publish failed: %s",
                            correlation_id[:12], worker_id, exc)

        log.info("[%s][%s] Batch %d complete: %d results from %d symbols",
                 correlation_id[:12], worker_id, batch_idx, len(batch_results), len(symbols))

        # Send results back to main thread
        results_queue.put((batch_idx, batch_results))

    log.info("[%s][%s] Worker exiting", correlation_id[:12], worker_id)

