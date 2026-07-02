import sys
import os
# Ensure the project root is on sys.path so `import db` / `import scanner`
# resolve when this script is run as `python scripts/run_scan.py` from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s %(message)s")

import db
import scanner

# Step 0: Initialize both SQLite and Postgres schemas
print("=== INITIALIZING DATABASE ===")
db.init_db()
print("init_db() completed.")

# Step 1: Force clear the stale scan lock
print("\n=== CLEARING STALE SCAN LOCK ===")
db.execute_db("UPDATE current_scan_state SET status='idle', scan_id=NULL, cancel_requested=0 WHERE id=1")
row = db.execute_db("SELECT scan_id, status FROM current_scan_state WHERE id=1", fetch="one")
print(f"Scan state after clear: {dict(row) if row else 'N/A'}")

# Step 2: Verify Postgres connectivity
print("\n=== POSTGRES CONNECTIVITY CHECK ===")
try:
    import psycopg2, os
    conn = psycopg2.connect(os.getenv('DATABASE_URL'), connect_timeout=10)
    cur = conn.cursor()
    cur.execute("SELECT 1")
    print("Postgres: CONNECTED")
    conn.close()
except Exception as e:
    print(f"Postgres: FAILED - {e}")
    print("WARNING: Scan will likely fall back to SQLite and abort!")

# Step 3: Trigger fresh scan
ctx = scanner.ScanContext.create(
    trigger_source="manual",
    user_id="forensic_test",
    session_id="forensic_session",
    mode="manual",
)
print(f"\n=== STARTING FRESH SCAN ===")
print(f"scan_id={ctx.scan_id}")

try:
    scanner.run_full_scan(ctx)
    print("\nScan completed successfully.")
except Exception as e:
    import traceback
    print("\nSCAN FAILED:")
    traceback.print_exc()

# Step 4: Post-scan audit
print("\n=== POST-SCAN AUDIT ===")

row = db.execute_db("SELECT COUNT(*) as c FROM scan_results", fetch="one")
print(f"scan_results count: {row.get('c')}")

row = db.execute_db("SELECT MAX(score) as c FROM scan_results", fetch="one")
print(f"max score: {row.get('c')}")

row = db.execute_db("SELECT COUNT(*) as c FROM research_snapshots_v2", fetch="one")
print(f"research_snapshots_v2 count: {row.get('c')}")

try:
    rows = db.execute_db("SELECT symbol, version, status, score_at_generation, cmp_at_generation, created_at FROM research_snapshots_v2 ORDER BY created_at DESC LIMIT 20", fetch="all")
    if rows:
        print("\nSnapshot details:")
        for r in rows:
            print(f"  {dict(r)}")
    else:
        print("\nNo snapshot rows found.")
except Exception as e:
    print(f"Snapshot query error: {e}")

rows = db.execute_db("SELECT symbol, score FROM scan_results WHERE score >= 65 ORDER BY score DESC", fetch="all")
print(f"\nSymbols with score >= 65:")
if rows:
    for r in rows:
        print(f"  {r['symbol']}: {r['score']}")
else:
    print("  None")
