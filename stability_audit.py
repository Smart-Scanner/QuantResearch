#!/usr/bin/env python3
import os
import sys
import csv
import logging
from datetime import datetime, date

# Allow importing local modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("audit")

AUDIT_CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "audit_stability.csv")

def generate_daily_scorecard(target_date: date = None):
    """
    Generate the stability scorecard for a specific date and append to CSV.
    If target_date is None, uses today's date.
    """
    if target_date is None:
        target_date = date.today()
    
    date_str = target_date.isoformat()
    log.info(f"Generating P0.1C Stability Scorecard for {date_str}...")

    # Ensure logs directory exists
    os.makedirs(os.path.dirname(AUDIT_CSV_PATH), exist_ok=True)

    # 1. Base counts from scan_runs
    runs_query = """
        SELECT status, COUNT(*) as cnt
        FROM scan_runs
        WHERE DATE(start_time) = ?
        GROUP BY status
    """
    rows = db.execute_db(runs_query, (date_str,), fetch="all")
    
    # Initialize metric counters
    status_counts = {"completed": 0, "failed": 0, "cancelled": 0, "running": 0, "created": 0}
    for r in rows:
        status_counts[r["status"]] = r["cnt"]

    # Total started = sum of all runs created today
    scans_started = sum(status_counts.values())
    scans_completed = status_counts.get("completed", 0)
    scans_failed = status_counts.get("failed", 0)
    scans_cancelled = status_counts.get("cancelled", 0)

    # 2. Watchdog & Zombie Metrics from scan_state_transitions
    # actor='watchdog' (or ACTOR_WATCHDOG) logs watchdog activations
    # new_state='zombie_detected' logs zombies caught
    transitions_query = """
        SELECT new_state, actor, COUNT(*) as cnt
        FROM scan_state_transitions
        WHERE DATE(created_at) = ?
        GROUP BY new_state, actor
    """
    trans_rows = db.execute_db(transitions_query, (date_str,), fetch="all")
    
    watchdog_activations = 0
    zombie_scans = 0
    for r in trans_rows:
        if r["actor"] == "watchdog":
            watchdog_activations += r["cnt"]
        if r["new_state"] == "zombie_detected":
            zombie_scans += r["cnt"]

    # 3. Success Rate
    # Formula: completed / (completed + failed)
    # Cancelled scans are excluded.
    denominator = scans_completed + scans_failed
    if denominator > 0:
        success_rate = round((scans_completed / denominator) * 100, 2)
    else:
        success_rate = 100.0 if scans_started == 0 else 0.0

    # 4. Universe Governance Audit (Option B-Prime)
    # Check what the scanner is actually running vs the bootstrap snapshot
    active_version = db.get_meta("active_universe_version") or "UNKNOWN"
    
    # Bootstrap count (from eligible_universe for the active version)
    bootstrap_query = "SELECT COUNT(*) as cnt FROM eligible_universe WHERE universe_version = ?"
    bootstrap_rows = db.execute_db(bootstrap_query, (active_version,), fetch="one")
    bootstrap_count = bootstrap_rows["cnt"] if bootstrap_rows else 0
    
    # Scanner count (from scan_runs for today)
    # We take the maximum candidate_count seen today
    scanner_query = "SELECT MAX(candidate_count) as max_c FROM scan_runs WHERE DATE(start_time) = ?"
    scanner_rows = db.execute_db(scanner_query, (date_str,), fetch="one")
    scanner_count = scanner_rows["max_c"] if scanner_rows and scanner_rows["max_c"] is not None else 0
    
    delta = bootstrap_count - scanner_count
    if delta != 0 and scans_started > 0:
        log.error(f"[GOVERNANCE ALERT] Universe Count Mismatch! Bootstrap={bootstrap_count}, Scanner={scanner_count}, Delta={delta}")
    
    # 5. Pass/Fail Logic
    passed = (
        zombie_scans == 0 and 
        watchdog_activations == 0 and 
        success_rate >= 95.0 and
        (delta == 0 or scans_started == 0)
    )

    # 6. Write to CSV
    file_exists = os.path.exists(AUDIT_CSV_PATH)
    with open(AUDIT_CSV_PATH, mode="a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "date", "started", "completed", "failed", "cancelled", 
                "zombies", "watchdogs", "success_rate", "bootstrap_count", "scanner_count", "delta", "passed"
            ])
        writer.writerow([
            date_str, scans_started, scans_completed, scans_failed, scans_cancelled,
            zombie_scans, watchdog_activations, success_rate, bootstrap_count, scanner_count, delta, int(passed)
        ])

    log.info(f"Scorecard saved to {AUDIT_CSV_PATH}")
    log.info(f"Date: {date_str} | Started: {scans_started} | Completed: {scans_completed} | Failed: {scans_failed} | Cancelled: {scans_cancelled}")
    log.info(f"Zombies: {zombie_scans} | Watchdogs: {watchdog_activations} | Success Rate: {success_rate}%")
    log.info(f"Governance -> Bootstrap: {bootstrap_count} | Scanner: {scanner_count} | Delta: {delta}")
    log.info(f"Passed: {passed}")

if __name__ == "__main__":
    generate_daily_scorecard()
