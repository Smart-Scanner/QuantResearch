"""Phase 1.5 Final Freshness — canonical last_scan + canonical single-symbol read regression.

Locks in the freshness graduation:
  Issue 1 — db.get_last_scan_display() (scan_runs.end_time of the latest completed scan) is the
            DEFAULT 'last scan' source everywhere, replacing the drift-prone scan_meta 'last_scan'
            (which is bumped mid-scan and never reset on failure). Legacy meta is preserved only as
            an internal fallback when the completed scan row has no end_time.
  Issue 2 — db.get_stock_from_results() binds to scan_results_v2 pinned to the latest completed
            scan (was the deprecated scan_results table, no scan binding, raw %s placeholder).

Isolated SQLite. Run: python -m pytest test_phase15_freshness.py -v
"""
import os
import tempfile
import pathlib

import pytest

os.environ.pop("DATABASE_URL", None)
os.environ.pop("SUPABASE_DB_URL", None)

import db  # noqa: E402

_DB = pathlib.Path(tempfile.mkdtemp(prefix="phase15fresh_")) / "f.db"
db.DB_PATH = _DB
assert not db.is_postgresql()
db._init_sqlite()


def _seed():
    db.execute_db("DELETE FROM scan_runs")
    db.execute_db("DELETE FROM scan_results_v2")
    # two COMPLETED scans; scanB is newer (later end_time)
    db.execute_db("INSERT INTO scan_runs (scan_id,status,start_time,end_time,created_at) VALUES (?,?,?,?,?)",
                  ("scanA", "completed", "2026-06-27 09:00:00", "2026-06-27 09:05:00", "2026-06-27 09:00:00"))
    db.execute_db("INSERT INTO scan_runs (scan_id,status,start_time,end_time,created_at) VALUES (?,?,?,?,?)",
                  ("scanB", "completed", "2026-06-27 10:00:00", "2026-06-27 10:07:30", "2026-06-27 10:00:00"))
    # same symbol X in both scans with different price
    db.execute_db("INSERT INTO scan_results_v2 (scan_id,scan_date,symbol,data,score,updated_at) VALUES (?,?,?,?,?,?)",
                  ("scanA", "2026-06-27", "X", '{"symbol":"X","price":100}', 40, "2026-06-27 09:05:00"))
    db.execute_db("INSERT INTO scan_results_v2 (scan_id,scan_date,symbol,data,score,updated_at) VALUES (?,?,?,?,?,?)",
                  ("scanB", "2026-06-27", "X", '{"symbol":"X","price":222}', 55, "2026-06-27 10:07:30"))
    # DRIFT: stale legacy meta (simulates a mid-scan/failed-scan bump pointing at an OLD time)
    db.set_meta("last_scan", "2026-06-27 08:00:00")
    if hasattr(db, "_meta_cache"):
        db._meta_cache.clear()


@pytest.fixture(autouse=True)
def _fixture():
    db.DB_PATH = _DB
    _seed()
    yield


def test_latest_completed_is_canonical_anchor():
    assert db.get_latest_completed_scan_id() == "scanB"


def test_last_scan_display_ignores_stale_meta():
    # Issue 1: canonical scan_runs.end_time of the latest completed scan — NOT the 08:00 drift meta.
    assert db.get_last_scan_display().startswith("2026-06-27 10:07:30")


def test_last_scan_display_falls_back_when_no_end_time():
    # The legacy meta is preserved ONLY for the edge case where the completed scan row lacks end_time.
    db.execute_db("UPDATE scan_runs SET end_time=NULL")
    if hasattr(db, "_meta_cache"):
        db._meta_cache.clear()
    assert db.get_last_scan_display() == "2026-06-27 08:00:00"


def test_get_stock_from_results_binds_latest_completed():
    # Issue 2: scan_results_v2 pinned to latest completed (scanB=222), not scanA(100), not deprecated table.
    s = db.get_stock_from_results("X")
    assert s and s.get("price") == 222
