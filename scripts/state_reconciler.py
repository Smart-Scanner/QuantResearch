"""
Phase I: State Reconciliation (Forensic Tool)

Detects and logs scan state mismatches without auto-healing blindly.
Rules: Detect, Log, Audit, Alert. No automatic destructive repair.
"""

import sys
import os
import logging
import sqlite3
from datetime import datetime, timezone

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("reconciler")

def reconcile_states():
    log.info("Starting state reconciliation audit...")
    mismatches_found = 0

    # Check 1: scan_runs = failed/cancelled BUT universe_chunk_runs = RUNNING
    try:
        rows = db.execute_db("""
            SELECT ucr.id, ucr.scan_id, ucr.chunk_name, ucr.status as chunk_status, sr.status as scan_status 
            FROM universe_chunk_runs ucr
            JOIN scan_runs sr ON ucr.scan_id = sr.scan_id
            WHERE ucr.status = 'RUNNING' AND sr.status IN ('failed', 'cancelled', 'stale')
        """, fetch="all")
        
        for row in rows:
            scan_id = row['scan_id']
            details = f"Chunk {row['chunk_name']} (ID {row['id']}) is {row['chunk_status']} but scan {scan_id} is {row['scan_status']}"
            log.warning("SCAN_STATE_MISMATCH: %s", details)
            db.log_scan_event(scan_id, "SCAN_STATE_MISMATCH", details)
            mismatches_found += 1
            # Explicitly NO AUTO-HEAL here based on Phase I rules
            
    except Exception as exc:
        log.error("Failed Check 1: %s", exc)

    # Check 2: current_scan_state = idle BUT scan_runs = running
    try:
        current = db.execute_db("SELECT status FROM current_scan_state WHERE id=1", fetch="one")
        if current and current['status'] == 'idle':
            running_scans = db.execute_db("""
                SELECT scan_id FROM scan_runs WHERE status = 'running'
            """, fetch="all")
            
            for row in running_scans:
                scan_id = row['scan_id']
                details = f"current_scan_state is idle but scan_runs claims {scan_id} is running"
                log.warning("SCAN_STATE_MISMATCH: %s", details)
                db.log_scan_event(scan_id, "SCAN_STATE_MISMATCH", details)
                mismatches_found += 1
    except Exception as exc:
        log.error("Failed Check 2: %s", exc)

    log.info("State reconciliation audit completed. Found %d mismatches.", mismatches_found)

if __name__ == "__main__":
    db.init_db()
    reconcile_states()
