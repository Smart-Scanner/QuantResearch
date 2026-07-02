"""
PR1 (RC1 / ADR-009) unit tests — canonical UTC clock + last_heartbeat-only watchdog.

Exercises watchdog._recover_stale_scans against a fake db (no real DB / no production
connection — the function takes `db` as a parameter and calls db.utc_now()). Proves the
frozen design:
  * watchdog depends ONLY on last_heartbeat, compared on the canonical UTC clock,
  * start_time is NEVER used for staleness (the timezone defect is removed),
  * a genuinely stale scan IS still reaped (G1 canary), a fresh one is not,
  * a legacy NULL-heartbeat row is skipped (drained), not killed.

db.utc_now()/utc_now_str() themselves are not imported here (importing db would touch
production); the canonical formula is asserted directly and exercised via the fake db.

Run: .venv/Scripts/python.exe -m pytest test_pr1_watchdog_clock.py -v
"""
import sys
import types
from datetime import datetime, timezone, timedelta

# Stub `events` BEFORE importing watchdog (lazy `from events import ACTOR_WATCHDOG`).
if "events" not in sys.modules:
    _fe = types.ModuleType("events")
    for _n in ("WATCHDOG_TRIGGERED", "WATCHDOG_HEARTBEAT", "SCAN_STALE",
               "SCAN_RECOVERED", "ACTOR_WATCHDOG"):
        setattr(_fe, _n, _n.lower())
    sys.modules["events"] = _fe

import watchdog  # noqa: E402  (top-level imports are stdlib only)


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hb(minutes_ago):
    """A last_heartbeat string `minutes_ago` minutes before canonical UTC now."""
    return (_utc_now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")


class FakeDB:
    """Stand-in for the db module; provides the canonical clock the watchdog calls."""
    def __init__(self, rows):
        self._rows = rows
        self.transitions = []
        self.events = []
        self.updates = []

    def utc_now(self):                       # mirrors db.utc_now (canonical naive UTC)
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def execute_db(self, sql, params=None, fetch=None, require_pg=False):
        s = " ".join(sql.split())
        if "FROM scan_runs" in s and "status = 'running'" in s and fetch == "all":
            return self._rows
        self.updates.append((s, params))
        return 1

    def log_scan_event(self, scan_id, event_type, details=""):
        self.events.append((scan_id, event_type, details))

    def transition_scan_state(self, scan_id, from_status, to_status,
                              reason=None, error_message=None, actor=None):
        self.transitions.append((scan_id, from_status, to_status, reason))
        return True


def _run(rows):
    db = FakeDB(rows)
    watchdog._recover_stale_scans(db)
    return db


def _zombied(db, scan_id):
    return any(t[0] == scan_id and t[2] == "zombie_detected" for t in db.transitions)


# ── canonical clock helper formula ────────────────────────────────────────
def test_canonical_clock_is_naive_utc():
    n = datetime.now(timezone.utc).replace(tzinfo=None)
    assert n.tzinfo is None                                   # naive
    assert abs((n - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds()) < 5


# ── watchdog: last_heartbeat only ─────────────────────────────────────────
def test_fresh_heartbeat_not_zombied():
    sid = "scan_auto_1_a"
    db = _run([{"scan_id": sid, "start_time": "x", "last_heartbeat": _hb(2)}])
    assert not _zombied(db, sid)


def test_stale_heartbeat_is_zombied():
    """G1 canary: a scan whose heartbeat is stale past SCAN_TIMEOUT_MIN IS reaped."""
    sid = "scan_auto_2_b"
    db = _run([{"scan_id": sid, "start_time": "x", "last_heartbeat": _hb(20)}])
    assert _zombied(db, sid)
    assert (sid, "zombie_detected", "failed", "Heartbeat timeout") in db.transitions


def test_null_heartbeat_is_skipped_not_killed():
    """Legacy row with no heartbeat is drained, never killed (no start_time fallback)."""
    sid = "scan_auto_3_c"
    db = _run([{"scan_id": sid, "start_time": _hb(600), "last_heartbeat": None}])
    assert not _zombied(db, sid)
    assert db.transitions == []


def test_start_time_is_ignored_when_heartbeat_fresh():
    """Very old start_time but FRESH heartbeat -> NOT zombied (proves start_time unused)."""
    sid = "scan_auto_4_d"
    db = _run([{"scan_id": sid, "start_time": _hb(600), "last_heartbeat": _hb(1)}])
    assert not _zombied(db, sid)


def test_heartbeat_governs_over_start_time():
    """Fresh start_time but STALE heartbeat -> zombied (heartbeat governs)."""
    sid = "scan_auto_5_e"
    db = _run([{"scan_id": sid, "start_time": _hb(0), "last_heartbeat": _hb(30)}])
    assert _zombied(db, sid)


def test_no_running_scans_is_noop():
    db = _run([])
    assert db.transitions == [] and db.events == []
