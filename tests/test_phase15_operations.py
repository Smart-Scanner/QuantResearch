"""Phase 1.5 Change Set D unit tests — db.scan_health() (the /api/operations payload).

Fully ISOLATED against a throwaway SQLite DB (DATABASE_URL cleared, db.DB_PATH redirected)
BEFORE importing db, so production is never touched. market_open is controlled by injecting a
fake `live_feed` module into sys.modules (db.scan_health lazily imports live_feed).

Route-level checks (flag OFF -> 404, Cache-Control: no-store) are trivial and verified by manual
+ production validation per the Change Set D design (importing the full Flask app is not test-safe).

Run: python -m pytest test_phase15_operations.py -v
"""
import os
import sys
import types
import time
import tempfile
import pathlib
from datetime import datetime, timedelta

import pytest

os.environ.pop("DATABASE_URL", None)
os.environ.pop("SUPABASE_DB_URL", None)

import db  # noqa: E402
_OPS_DB = pathlib.Path(tempfile.mkdtemp(prefix="phase15ops_")) / "ops.db"
db.DB_PATH = _OPS_DB
assert not db.is_postgresql()
db._init_sqlite()


def _set_market(open_val):
    """Inject a fake live_feed so scan_health's lazy import is controlled."""
    m = types.ModuleType("live_feed")
    m.is_market_open = lambda: open_val
    sys.modules["live_feed"] = m


@pytest.fixture(autouse=True)
def _pin_db():
    db.DB_PATH = _OPS_DB
    db.execute_db("DELETE FROM scan_runs")
    db.execute_db("DELETE FROM scan_meta")
    try:
        db.execute_db("UPDATE current_scan_state SET status='idle', scan_id=NULL WHERE id=1")
    except Exception:
        pass
    db._meta_cache.clear() if hasattr(db, "_meta_cache") else None
    _set_market(True)            # default: market open
    yield


def _seed_completed(scan_id="scan_x", age_s=10):
    ts = (datetime.now() - timedelta(seconds=age_s)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute_db(
        "INSERT INTO scan_runs (scan_id, status, start_time, end_time, created_at, "
        "processed_count, duration_seconds) VALUES (?,?,?,?,?,?,?)",
        (scan_id, "completed", ts, ts, ts, 100, 200.0))
    return scan_id


# ── no completed scan -> red ─────────────────────────────────────────────────
def test_no_completed_scan_red():
    h = db.scan_health()
    assert h["health_verdict"] == "red"
    assert "no_completed_scan" in h["verdict_reasons"]
    assert h["last_scan_id"] is None


# ── recent completed + market open + fresh sched -> green ─────────────────────
def test_completed_recent_green():
    sid = _seed_completed(age_s=30)
    db.set_meta("scheduler_heartbeat_ts", str(time.time()))
    h = db.scan_health()
    assert h["health_verdict"] == "green"
    assert h["last_scan_id"] == sid
    assert h["cache_generation"] == sid          # cache_generation == last_scan_id (A-5 enriches)
    assert h["last_scan_age_s"] is not None and h["last_scan_age_s"] < 120


# ── stale scan age, market OPEN -> red ───────────────────────────────────────
def test_scan_age_red_market_open():
    _seed_completed(age_s=3 * 3600)              # 3h > OPS_AGE_RED_S(150min)
    db.set_meta("scheduler_heartbeat_ts", str(time.time()))
    _set_market(True)
    h = db.scan_health()
    assert h["health_verdict"] == "red"
    assert "scan_age_red" in h["verdict_reasons"]


# ── same stale age, market CLOSED -> age NOT flagged (green) ──────────────────
def test_scan_age_ignored_market_closed():
    _seed_completed(age_s=3 * 3600)
    db.set_meta("scheduler_heartbeat_ts", str(time.time()))
    _set_market(False)
    h = db.scan_health()
    assert h["health_verdict"] == "green"
    assert "scan_age_red" not in h["verdict_reasons"]


# ── scheduler stalled -> red ─────────────────────────────────────────────────
def test_scheduler_stalled_red():
    _seed_completed(age_s=30)
    db.set_meta("scheduler_heartbeat_ts", str(time.time() - 600))   # >300s and >boot-grace
    _set_market(False)                            # isolate scheduler signal
    h = db.scan_health()
    assert h["health_verdict"] == "red"
    assert "scheduler_stalled" in h["verdict_reasons"]
    assert h["scheduler_heartbeat_age_s"] >= 600


# ── stale heartbeat BUT scan running -> scheduler NOT flagged (Change Set D fix) ──
def test_scheduler_stale_but_scanning_not_flagged():
    _seed_completed(age_s=30)
    db.set_meta("scheduler_heartbeat_ts", str(time.time() - 600))   # stale (would be red if idle)
    _set_market(False)
    try:
        db.execute_db("INSERT OR IGNORE INTO current_scan_state (id, status, scan_id) VALUES (1,'running','s')")
        db.execute_db("UPDATE current_scan_state SET status='running', scan_id='s' WHERE id=1")
    except Exception:
        pytest.skip("current_scan_state schema not insertable in isolation")
    h = db.scan_health()
    assert h["scan_status"] == "running"
    assert "scheduler_stalled" not in h["verdict_reasons"]   # busy scheduler != stalled
    assert "scheduler_lagging" not in h["verdict_reasons"]
    assert h["health_verdict"] != "red"


# ── scheduler within boot grace -> not flagged (green) ───────────────────────
def test_scheduler_boot_grace_green():
    _seed_completed(age_s=30)
    db.set_meta("scheduler_heartbeat_ts", str(time.time() - 100))   # <120s boot grace
    _set_market(False)
    h = db.scan_health()
    assert h["health_verdict"] == "green"
    assert "scheduler_stalled" not in h["verdict_reasons"]


# ── auto-scan disabled during market hours -> yellow ─────────────────────────
def test_auto_disabled_yellow():
    _seed_completed(age_s=30)
    db.set_meta("scheduler_heartbeat_ts", str(time.time()))
    db.set_meta("auto_scan_enabled", "0")
    _set_market(True)
    h = db.scan_health()
    assert h["health_verdict"] == "yellow"
    assert "auto_scan_disabled" in h["verdict_reasons"]
    assert h["auto_scan_enabled"] is False


# ── running scan -> scan_status running ──────────────────────────────────────
def test_running_status():
    _seed_completed(age_s=30)
    try:
        db.execute_db("INSERT OR IGNORE INTO current_scan_state (id, status, scan_id) VALUES (1,'running','scan_run')")
        db.execute_db("UPDATE current_scan_state SET status='running', scan_id='scan_run' WHERE id=1")
    except Exception:
        pytest.skip("current_scan_state schema not insertable in isolation")
    h = db.scan_health()
    assert h["scan_status"] == "running"
    assert h["running_scan_id"] == "scan_run"


# ── DB error -> degraded red, never raises, no secrets ───────────────────────
def test_db_error_degraded(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("server closed the connection unexpectedly @ host=secret")
    monkeypatch.setattr(db, "execute_db", _boom)
    h = db.scan_health()                          # must NOT raise
    assert h["health_verdict"] == "red"
    assert "health_check_failed" in h["verdict_reasons"]
    # no raw exception / secrets leaked into the public payload
    blob = " ".join(str(v) for v in h.values())
    assert "host=" not in blob and "secret" not in blob and "connection" not in blob


# ── contract: required keys present, no secret-like values ───────────────────
def test_contract_keys_and_no_secrets():
    _seed_completed(age_s=30)
    db.set_meta("scheduler_heartbeat_ts", str(time.time()))
    h = db.scan_health()
    for k in ("last_scan_id", "last_scan_age_s", "scan_status", "cache_generation",
              "scheduler_heartbeat_age_s", "next_scheduled_scan", "market_open",
              "auto_scan_enabled", "health_verdict", "verdict_reasons", "generated_at"):
        assert k in h, f"missing key {k}"
    blob = " ".join(str(v) for v in h.values()).lower()
    assert "postgres://" not in blob and "password" not in blob and "database_url" not in blob
