"""
Active Watchdog — Sections 3, 8, 19, 38 of the Master Plan.

Dedicated background thread that actively hunts for and recovers
stuck/stale scans. Replaces the passive _recover_stale() pattern.

Key behaviors:
  - Checks every WATCHDOG_CHECK_INTERVAL_SEC seconds
  - Queries scan_runs for status='running' older than SCAN_TIMEOUT_MIN
  - Transitions stale scans: running → stale → failed
  - Emits a heartbeat metric every loop for dead-man's-switch alerting
  - Respects shutdown_event for clean process termination
"""

import time
import logging
import threading
from datetime import datetime

log = logging.getLogger("watchdog")

# ── Configuration ─────────────────────────────────────────────────────
WATCHDOG_CHECK_INTERVAL_SEC = 60   # How often the watchdog loop runs
SCAN_TIMEOUT_MIN = 15              # Scans running longer than this without heartbeat are zombies
HEARTBEAT_KEY = "watchdog_heartbeat_ts"

# ── Module state ──────────────────────────────────────────────────────
_watchdog_thread: threading.Thread | None = None
_shutdown_event: threading.Event | None = None


def start_watchdog(shutdown_event: threading.Event) -> threading.Thread:
    """Start the active watchdog background thread.

    Args:
        shutdown_event: Shared threading.Event for graceful shutdown.
    Returns:
        The watchdog Thread object (for join/status checks).
    """
    global _watchdog_thread, _shutdown_event
    _shutdown_event = shutdown_event

    if _watchdog_thread is not None and _watchdog_thread.is_alive():
        log.info("[WATCHDOG] Already running")
        return _watchdog_thread

    _watchdog_thread = threading.Thread(
        target=_watchdog_loop,
        name="watchdog",
        daemon=False,  # Managed lifecycle — NOT a fire-and-forget daemon
    )
    _watchdog_thread.start()
    log.info("[WATCHDOG] Started (interval=%ds, timeout=%dmin)",
             WATCHDOG_CHECK_INTERVAL_SEC, SCAN_TIMEOUT_MIN)
    return _watchdog_thread


def stop_watchdog():
    """Signal the watchdog to stop and wait for it to exit."""
    global _watchdog_thread
    if _shutdown_event:
        _shutdown_event.set()
    if _watchdog_thread and _watchdog_thread.is_alive():
        _watchdog_thread.join(timeout=10)
        log.info("[WATCHDOG] Stopped")
    _watchdog_thread = None


def is_watchdog_healthy() -> bool:
    """Check if the watchdog has emitted a heartbeat recently.
    Used by /api/debug/health for dead-man's-switch alerting.
    """
    import db
    try:
        ts_str = db.get_meta(HEARTBEAT_KEY)
        if not ts_str:
            return False
        last_beat = float(ts_str)
        age_sec = time.time() - last_beat
        # Healthy if heartbeat is less than 5 minutes old
        return age_sec < (5 * 60)
    except Exception:
        return False


def _watchdog_loop():
    """Main watchdog loop. Runs until shutdown_event is set."""
    import db
    from events import (
        WATCHDOG_TRIGGERED, WATCHDOG_HEARTBEAT,
        SCAN_STALE, SCAN_RECOVERED, ACTOR_WATCHDOG,
    )

    log.info("[WATCHDOG] Loop started")

    # Brief startup delay to let DB init complete
    if _shutdown_event and _shutdown_event.wait(timeout=10):
        return  # Shutdown requested during startup

    while not (_shutdown_event and _shutdown_event.is_set()):
        try:
            _recover_stale_scans(db)
            _emit_heartbeat(db)
        except Exception as exc:
            log.error("[WATCHDOG] Loop error (continuing): %s", exc)

        # Wait for interval OR shutdown signal
        if _shutdown_event and _shutdown_event.wait(timeout=WATCHDOG_CHECK_INTERVAL_SEC):
            break  # Shutdown requested

    log.info("[WATCHDOG] Loop exiting")


def _recover_stale_scans(db):
    """Find and recover scans stuck in 'running' past the timeout."""
    from events import ACTOR_WATCHDOG

    try:
        # Query for stale running scans using last_heartbeat
        stale_scans = db.execute_db("""
            SELECT scan_id, start_time, last_heartbeat
            FROM scan_runs
            WHERE status = 'running'
        """, fetch="all")

        if not stale_scans:
            return

        now = db.utc_now()
        for row in stale_scans:
            scan_id = row.get("scan_id", "")
            # Frozen design (RC1/ADR-009): depend ONLY on last_heartbeat, compared on the
            # SAME canonical UTC clock it is written with (db.utc_now). last_heartbeat is
            # initialized at scan creation, so it is always present for current scans. Never
            # fall back to start_time — comparing the watchdog clock against a start_time
            # written by a different clock was the timezone defect this fix removes.
            last_hb = row.get("last_heartbeat")
            if not last_hb:
                # Legacy row created before heartbeat-init — cannot age safely; leave to drain.
                log.debug("[WATCHDOG] No last_heartbeat (legacy row); skipping %s", scan_id)
                continue

            try:
                last_update = last_hb if isinstance(last_hb, datetime) else datetime.fromisoformat(str(last_hb))
                if last_update.tzinfo is not None:
                    last_update = last_update.replace(tzinfo=None)
                age_min = (now - last_update).total_seconds() / 60.0
            except (ValueError, TypeError) as exc:
                log.error("[WATCHDOG] Heartbeat parse error for %s: %s", scan_id, exc)
                continue

            if age_min > SCAN_TIMEOUT_MIN:
                log.warning(
                    "[ZOMBIE_DETECTED] Zombie scan detected: scan_id=%s, heartbeat_age=%.1f min. Recovering...",
                    scan_id, age_min
                )
                db.log_scan_event(scan_id, "ZOMBIE_DETECTED", f"No heartbeat received for {age_min:.1f} minutes")
                db.log_scan_event(scan_id, "WATCHDOG_RECOVERY_STARTED", "Initiating watchdog recovery via zombie state")

                # Transition 1: running → zombie_detected
                # P0.1D: transition_scan_state uses require_pg=True. If PG is down,
                # the watchdog is a recovery actor and MUST still clean up.
                try:
                    zombie_marked = db.transition_scan_state(
                        scan_id=scan_id,
                        from_status="running",
                        to_status="zombie_detected",
                        reason="Heartbeat timeout",
                        error_message="Heartbeat timeout",
                        actor=ACTOR_WATCHDOG,
                    )
                except RuntimeError as pg_err:
                    # PG unavailable — fall back to direct UPDATE for recovery
                    log.warning("[WATCHDOG] PG unavailable during recovery, using direct UPDATE: %s", pg_err)
                    rc = db.execute_db(
                        "UPDATE scan_runs SET status='zombie_detected', error_message='Heartbeat timeout' WHERE scan_id=? AND status='running'",
                        (scan_id,), fetch="rowcount"
                    )
                    zombie_marked = (rc or 0) > 0

                if zombie_marked:
                    # Transition 2: zombie_detected → failed
                    try:
                        recovered = db.transition_scan_state(
                            scan_id=scan_id,
                            from_status="zombie_detected",
                            to_status="failed",
                            reason="Heartbeat timeout",
                            error_message="Heartbeat timeout",
                            actor=ACTOR_WATCHDOG,
                        )
                    except RuntimeError as pg_err:
                        log.warning("[WATCHDOG] PG unavailable during recovery (phase 2), using direct UPDATE: %s", pg_err)
                        rc = db.execute_db(
                            "UPDATE scan_runs SET status='failed', error_message='Heartbeat timeout' WHERE scan_id=? AND status='zombie_detected'",
                            (scan_id,), fetch="rowcount"
                        )
                        recovered = (rc or 0) > 0
                        # Also sync current_scan_state manually since we bypassed transition_scan_state
                        db.execute_db(
                            "UPDATE current_scan_state SET status='idle', phase='', cancel_requested=0 WHERE id=1",
                        )
                    
                    # Guardrail: Release the scan lock explicitly
                    db.execute_db("UPDATE scan_lock SET scan_id = NULL, owner_id = NULL, heartbeat = NULL, expires_at = NULL WHERE id = 1 AND scan_id = ?", (scan_id,))

                    if recovered:
                        log.warning(
                            "[WATCHDOG_RECOVERY_COMPLETED] Recovered zombie scan: %s (heartbeat dead for %.1f min)",
                            scan_id, age_min
                        )
                        db.log_scan_event(scan_id, "WATCHDOG_RECOVERY_COMPLETED", f"Transitioned to failed after {age_min:.1f}m without heartbeat")
                else:
                    log.info(
                        "[WATCHDOG] Scan %s already transitioned (race OK)", scan_id
                    )
                    db.log_scan_event(scan_id, "WATCHDOG_RECOVERY_FAILED", "Scan already transitioned")
            else:
                log.debug("[WATCHDOG_HEARTBEAT_OK] scan_id=%s, heartbeat_age=%.1f min", scan_id, age_min)

    except Exception as exc:
        log.error("[WATCHDOG_RECOVERY_FAILED] Stale scan recovery failed: %s", exc)


def _emit_heartbeat(db):
    """Write heartbeat timestamp to scan_meta for health monitoring."""
    try:
        db.set_meta(HEARTBEAT_KEY, str(time.time()))
    except Exception as exc:
        log.warning("[WATCHDOG] Heartbeat write failed: %s", exc)
