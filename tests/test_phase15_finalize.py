"""Phase 1.5 Change Sets B + C unit tests — atomic finalization ordering + cache gap closure.

Verifies the publish-barrier conditional UPDATE is preserved byte-identical (P0-2), the version
switch is PUSHed on completion (B-1), invalidation failure is observable (B-2), and the detail
cleanup fires on completion (C-1) — all flag-gated (OFF = production-identical). Isolated SQLite.

Run: python -m pytest test_phase15_finalize.py -v
"""
import os
import tempfile
import pathlib

import pytest

os.environ.pop("DATABASE_URL", None)
os.environ.pop("SUPABASE_DB_URL", None)
os.environ["PHASE15_ATOMIC_FINALIZE"] = "0"
os.environ["PHASE15_CACHE_GAPS"] = "0"

import db  # noqa: E402
_BC_DB = pathlib.Path(tempfile.mkdtemp(prefix="phase15bc_")) / "bc.db"
db.DB_PATH = _BC_DB
assert not db.is_postgresql()
db._init_sqlite()

import cache_layer  # noqa: E402
from metrics import counters  # noqa: E402


def _start_scan(sid):
    db.execute_db("INSERT INTO scan_runs (scan_id,status,start_time,created_at) VALUES (?,?,?,?)",
                  (sid, "running", "2026-06-27 10:00:00", "2026-06-27 10:00:00"))
    try:
        db.execute_db("INSERT OR IGNORE INTO current_scan_state (id,status,scan_id) VALUES (1,'running',?)", (sid,))
        db.execute_db("UPDATE current_scan_state SET status='running', scan_id=? WHERE id=1", (sid,))
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _pin(monkeypatch):
    db.DB_PATH = _BC_DB
    db.execute_db("DELETE FROM scan_runs")
    # The state-machine UPDATE uses require_pg=True (CP in prod); force the SQLite path in tests
    # so the conditional-UPDATE LOGIC (WHERE status=from_status, rowcount) is exercised offline.
    _orig = db.execute_db
    monkeypatch.setattr(db, "execute_db",
                        lambda q, params=None, fetch=None, require_pg=False: _orig(q, params, fetch, require_pg=False))
    cache_layer._cache_gen = None
    cache_layer._cache_gen_at = 0.0
    os.environ["PHASE15_ATOMIC_FINALIZE"] = "0"
    os.environ["PHASE15_CACHE_GAPS"] = "0"
    yield


# ── P0-2: the conditional-UPDATE publish barrier is preserved (state machine intact) ──
def test_conditional_update_preserved():
    _start_scan("s1")
    assert db.transition_scan_state("s1", "idle", "completed") is False        # wrong from_status -> rejected
    assert db.transition_scan_state("s1", "running", "completed") is True      # valid transition -> committed


# ── B-1: version switch PUSHed on completion when atomic-finalize is ON ───────
def test_b1_version_push_flag_on():
    _start_scan("s2")
    os.environ["PHASE15_ATOMIC_FINALIZE"] = "1"
    assert db.transition_scan_state("s2", "running", "completed") is True
    assert cache_layer._cache_gen == "s2"                                       # generation pushed


# ── B-1: flag OFF -> no push (prod-identical) ────────────────────────────────
def test_b1_no_push_flag_off():
    _start_scan("s3")
    db.transition_scan_state("s3", "running", "completed")
    assert cache_layer._cache_gen is None                                       # untouched


# ── B-2: invalidation failure is observable (counter), never raises ──────────
def test_b2_observable_invalidation(monkeypatch):
    _start_scan("s4")
    os.environ["PHASE15_ATOMIC_FINALIZE"] = "1"
    before = counters.get("cache_invalidate_failed") or 0
    monkeypatch.setattr(cache_layer, "invalidate_all",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert db.transition_scan_state("s4", "running", "completed") is True       # scan still completes
    assert (counters.get("cache_invalidate_failed") or 0) == before + 1


# ── B-2: flag OFF -> legacy swallow (no counter) ─────────────────────────────
def test_b2_legacy_swallow_flag_off(monkeypatch):
    _start_scan("s4b")
    before = counters.get("cache_invalidate_failed") or 0
    monkeypatch.setattr(cache_layer, "invalidate_all",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert db.transition_scan_state("s4b", "running", "completed") is True
    assert (counters.get("cache_invalidate_failed") or 0) == before            # not incremented


# ── C-1: detail-cache cleanup fires on completion when cache-gaps is ON ───────
def test_c1_detail_cleanup_flag_on(monkeypatch):
    _start_scan("s5")
    os.environ["PHASE15_CACHE_GAPS"] = "1"
    called = []
    monkeypatch.setattr(cache_layer, "cleanup_detail_cache", lambda *a, **k: called.append(1))
    db.transition_scan_state("s5", "running", "completed")
    assert called == [1]


def test_c1_no_detail_cleanup_flag_off(monkeypatch):
    _start_scan("s6")
    called = []
    monkeypatch.setattr(cache_layer, "cleanup_detail_cache", lambda *a, **k: called.append(1))
    db.transition_scan_state("s6", "running", "completed")
    assert called == []                                                        # flag OFF -> not called


# ── non-completion transitions never push/cleanup ────────────────────────────
def test_running_transition_no_push():
    db.execute_db("INSERT INTO scan_runs (scan_id,status,start_time,created_at) VALUES (?,?,?,?)",
                  ("s7", "queued", "2026-06-27 10:00:00", "2026-06-27 10:00:00"))
    os.environ["PHASE15_ATOMIC_FINALIZE"] = "1"
    db.transition_scan_state("s7", "queued", "running")
    assert cache_layer._cache_gen is None                                      # only 'completed' pushes
