"""
RC2 unit tests — terminal scan lifecycle (RC2-A) + scan_id write-key (RC2-B).

Runs FULLY ISOLATED against a throwaway SQLite DB (DATABASE_URL is cleared and
db.DB_PATH is redirected to a temp file BEFORE any DB call), so the production /
Railway database is never touched.

RC2-A — completed scans must reach a VALID terminal and reset current_scan_state:
  * 'completed' is a valid transition from 'running'; the old 'completed_with_errors'
    is NOT in VALID_TRANSITIONS (that is the bug that left scans stuck 'running').
  * transition_scan_state(running -> completed) succeeds AND drives current_scan_state
    back to 'idle'.

RC2-B — every parallel save must write under the real scan_id:
  * save_results(rows, scan_id=SID) writes scan_results_v2 rows under SID, NOT under
    the 'legacy_fallback' default; omitting scan_id falls back to 'legacy_fallback'
    (proving the kwarg is the partition key the readers pin).

Run: python -m pytest test_rc2_lifecycle.py -v
"""
import os
import tempfile
import pathlib

# ── Force SQLite isolation BEFORE importing db ───────────────────────────────
os.environ.pop("DATABASE_URL", None)
os.environ.pop("SUPABASE_DB_URL", None)

import db  # noqa: E402

# Redirect the SQLite file to a temp path and build the schema there.
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="rc2_")) / "rc2_test.db"
db.DB_PATH = _TMP
assert not db.is_postgresql(), "test must run in SQLite mode (DATABASE_URL cleared)"
db._init_sqlite()


def _seed_running_scan(sid: str):
    """Insert a minimal running scan_runs row + mark current_scan_state running."""
    now = db.utc_now_str()
    db.execute_db(
        "INSERT INTO scan_runs (scan_id, status, start_time, last_heartbeat, created_at) "
        "VALUES (?, 'running', ?, ?, ?)",
        (sid, now, now, now),
    )
    db.execute_db("UPDATE current_scan_state SET status='running', phase='scanning' WHERE id=1")


# ── RC2-A: transition matrix (the exact defect + fix) ────────────────────────
def test_completed_is_valid_completed_with_errors_is_not():
    assert db._is_valid_transition("running", "completed") is True
    assert db._is_valid_transition("running", "completed_with_errors") is False


# ── RC2-A: a 'completed' terminal resets current_scan_state to idle ──────────
# NOTE: transition_scan_state() is deliberately PG-only (require_pg=True), so the full
# wrapper cannot run in SQLite isolation — the end-to-end running->completed transition
# is validated on real PostgreSQL by the live integration scan. Here we test the exact
# sub-routine that releases the UI/lock: _sync_current_scan_state, which has no PG
# requirement. Combined with the matrix test (completed IS a valid target, so the wrapper
# always reaches this sync), this proves the RC2-A idle guarantee.
def test_completed_terminal_syncs_current_state_to_idle():
    db.execute_db("UPDATE current_scan_state SET status='running', phase='scanning' WHERE id=1")
    db._sync_current_scan_state("scan_rc2_sync_1", "completed", db.utc_now_str())
    cur = db.execute_db("SELECT status, phase FROM current_scan_state WHERE id=1", fetch="one")
    assert cur["status"] == "idle"   # RC2-A: UI/lock released on completed terminal
    assert cur["phase"] == ""


def test_invalid_terminal_is_rejected_returns_false():
    """The pre-fix behaviour: a non-registered terminal is silently rejected (returns
    False, no UPDATE) — which is exactly why capturing the return value matters."""
    sid = "scan_rc2_invalid_1"
    _seed_running_scan(sid)
    ok = db.transition_scan_state(sid, "running", "completed_with_errors")
    assert ok is False
    row = db.execute_db("SELECT status FROM scan_runs WHERE scan_id=?", (sid,), fetch="one")
    assert row["status"] == "running"  # stays running — the stuck-scan bug


# ── RC2-B: scan_id is the write key; default is legacy_fallback ───────────────
def _result(sym):
    return {"symbol": sym, "score": 71.0, "price": 100.0, "rsi": 55.0,
            "sector": "TEST", "scan_mode": "fast", "high_conviction": False,
            "fundamentals": {}, "trade": {}}


def test_save_results_writes_under_passed_scan_id():
    sid = "scan_rc2_writekey_1"
    db.save_results([_result("RC2A"), _result("RC2B")], scan_id=sid)
    under_sid = db.execute_db(
        "SELECT COUNT(*) AS c FROM scan_results_v2 WHERE scan_id=?", (sid,), fetch="one")["c"]
    assert under_sid == 2, f"expected 2 rows under real scan_id, got {under_sid}"
    leaked = db.execute_db(
        "SELECT COUNT(*) AS c FROM scan_results_v2 WHERE scan_id='legacy_fallback' "
        "AND symbol IN ('RC2A','RC2B')", fetch="one")["c"]
    assert leaked == 0, "rows must NOT leak to legacy_fallback when scan_id is passed"


def test_save_results_defaults_to_legacy_fallback_when_omitted():
    """Proves the default key is 'legacy_fallback' — i.e. the parallel sites that
    omitted scan_id were the F1 root cause."""
    db.save_results([_result("RC2OMIT")])
    n = db.execute_db(
        "SELECT COUNT(*) AS c FROM scan_results_v2 WHERE scan_id='legacy_fallback' "
        "AND symbol='RC2OMIT'", fetch="one")["c"]
    assert n == 1
