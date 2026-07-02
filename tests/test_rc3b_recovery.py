"""RC3-B unit tests — Recommendation Recovery & persist-path hardening.

Covers WI-B1 (batch transaction + pooled-connection robustness), WI-B2 (idempotent /
atomic recovery), WI-B3 (recovery metrics), WI-B4 (fail-open supersession reads), plus
RC3-A regression. The PostgreSQL failure paths (rollback, broken-conn discard, autocommit
restore failure, pool-None fallback) are exercised with a fake pool/connection so no real PG
is required. Fully ISOLATED against a throwaway SQLite DB.

Run: python -m pytest test_rc3b_recovery.py -v
"""
import os
import tempfile
import pathlib

import pytest

os.environ.pop("DATABASE_URL", None)
os.environ.pop("SUPABASE_DB_URL", None)
os.environ["RE2_RO_BUILD"] = "0"           # shadow/dormant — RC3-B changes no production behaviour

psycopg2_extras = pytest.importorskip("psycopg2.extras")  # needed to patch execute_values

import db  # noqa: E402
_RC3B_DB = pathlib.Path(tempfile.mkdtemp(prefix="rc3b_")) / "rc3b.db"
db.DB_PATH = _RC3B_DB
assert not db.is_postgresql()
db._init_sqlite()

from recommendation_engine import store, build_recommendation_object  # noqa: E402
from recommendation_engine.reconcile import shadow_build  # noqa: E402

store.init_recommendation_store()


@pytest.fixture(autouse=True)
def _pin_db():
    db.DB_PATH = _RC3B_DB
    store.init_recommendation_store()
    db.execute_db("DELETE FROM recommendations")
    db.execute_db("DELETE FROM scan_results_v2")
    yield


def _r(sym, mode="fast", price=100.0):
    return {"symbol": sym, "scan_mode": mode, "price": price, "atr_pct": 2.0,
            "trade": {"entry_low": 99.0, "entry_high": 101.0, "stop_loss": 95.0,
                      "target1": 108.0, "target2": 115.0, "target3": 125.0},
            "resistances": [108.0, 115.0, 125.0], "supports": [95.0],
            "technical_score": 70, "fundamental_score": 60, "score": 65, "grade": "B"}


def _ro(sym, scan_id, price=100.0, gen="2026-06-25 10:00:00"):
    return build_recommendation_object(_r(sym, price), scan_id=scan_id, generated_at_utc=gen)


# ── fake PG pool/connection for WI-B1 failure-path coverage ──────────────────
class _FakeCursor:
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _FakeConn:
    """Simulates a pooled psycopg2 connection. `fail` ⊆ {'commit','rollback','autocommit'}."""
    def __init__(self, fail=()):
        self.fail = set(fail)
        self._autocommit = True
        self.committed = False
        self.rolled_back = False

    @property
    def autocommit(self):
        return self._autocommit

    @autocommit.setter
    def autocommit(self, v):
        if v is True and "autocommit" in self.fail:   # fail only on the restore-to-default
            raise RuntimeError("autocommit restore failed")
        self._autocommit = v

    def cursor(self):
        return _FakeCursor()

    def commit(self):
        if "commit" in self.fail:
            raise RuntimeError("commit failed")
        self.committed = True

    def rollback(self):
        if "rollback" in self.fail:
            raise RuntimeError("rollback failed")
        self.rolled_back = True


class _FakePool:
    def __init__(self, conn):
        self.conn = conn
        self.put = []                       # records the `close` flag per putconn

    def getconn(self):
        return self.conn

    def putconn(self, conn, key=None, close=False):
        self.put.append(close)


def _use_pg(monkeypatch, conn, exec_raise=False):
    pool = _FakePool(conn)
    monkeypatch.setattr(db, "is_postgresql", lambda: True)
    monkeypatch.setattr(db, "_get_pg_pool", lambda: pool)

    def _ev(cur, sql, rows, page_size=100):
        if exec_raise:
            raise RuntimeError("execute_values failed mid-chunk")

    monkeypatch.setattr(psycopg2_extras, "execute_values", _ev)
    return pool


# ── WI-B1: PG happy path — commit, restore autocommit, return conn to pool ────
def test_pg_batch_happy_path(monkeypatch):
    conn = _FakeConn()
    pool = _use_pg(monkeypatch, conn)
    n = store.save_recommendations_batch([_ro("AAA", "s1"), _ro("BBB", "s1")])
    assert n == 2
    assert conn.committed is True
    assert conn.autocommit is True          # restored to pooled default
    assert pool.put == [False]              # returned healthy, not discarded


# ── WI-B1 / transaction rollback: commit fails → rollback, no partial, conn healthy ──
def test_pg_commit_fail_rolls_back(monkeypatch):
    conn = _FakeConn(fail={"commit"})
    pool = _use_pg(monkeypatch, conn)
    with pytest.raises(Exception):
        store.save_recommendations_batch([_ro("AAA", "s1")])
    assert conn.committed is False
    assert conn.rolled_back is True         # whole batch rolled back (no partial persist)
    assert pool.put == [False]              # rollback succeeded ⇒ conn healthy, reused


# ── WI-B1 / interrupted persist: execute fails mid-chunk → no commit, rollback ──
def test_pg_execute_fail_no_partial(monkeypatch):
    conn = _FakeConn()
    pool = _use_pg(monkeypatch, conn, exec_raise=True)
    with pytest.raises(Exception):
        store.save_recommendations_batch([_ro("AAA", "s1"), _ro("BBB", "s1")])
    assert conn.committed is False          # nothing committed ⇒ atomic, no partial write
    assert conn.rolled_back is True
    assert pool.put == [False]


# ── WI-B1 / broken pooled connection: commit + rollback fail → DISCARD conn ────
def test_pg_broken_conn_discarded(monkeypatch):
    conn = _FakeConn(fail={"commit", "rollback"})
    pool = _use_pg(monkeypatch, conn)
    with pytest.raises(Exception):
        store.save_recommendations_batch([_ro("AAA", "s1")])
    assert pool.put == [True]               # broken conn discarded (close=True), pool not poisoned


# ── WI-B1 / autocommit restore failure on success → discard rather than reuse ──
def test_pg_autocommit_restore_fail_discards(monkeypatch):
    conn = _FakeConn(fail={"autocommit"})
    pool = _use_pg(monkeypatch, conn)
    n = store.save_recommendations_batch([_ro("AAA", "s1")])
    assert n == 1 and conn.committed is True
    assert pool.put == [True]               # restore failed ⇒ conn discarded, not returned dirty


# ── WI-B1 / pool unavailable (cooldown) → safe per-row fallback, never crash ───
def test_pg_pool_none_fallback(monkeypatch):
    monkeypatch.setattr(db, "is_postgresql", lambda: True)
    monkeypatch.setattr(db, "_get_pg_pool", lambda: None)
    calls = []
    monkeypatch.setattr(store, "save_recommendation", lambda ro: calls.append(ro["meta"]["recommendation_id"]))
    n = store.save_recommendations_batch([_ro("AAA", "s1"), _ro("BBB", "s1")])
    assert n == 2 and len(calls) == 2       # fell back to per-row, no pooled-conn use


# ── WI-B2: idempotent replay (SQLite real path) — no duplicates on re-run ─────
def test_idempotent_replay(monkeypatch):
    ros = [_ro(s, "s1") for s in ("AAA", "BBB", "CCC")]
    n1 = store.save_recommendations_batch(ros)
    n2 = store.save_recommendations_batch(ros)   # replay
    assert n1 == 3 and n2 == 3
    cnt = db.execute_db("SELECT COUNT(*) c FROM recommendations WHERE scan_id='s1'", fetch="one")["c"]
    assert cnt == 3                          # idempotent — no duplicates


# ── WI-B2: duplicate prevention on same recommendation_id ─────────────────────
def test_duplicate_prevention():
    store.save_recommendation(_ro("AAA", "s1"))
    store.save_recommendation(_ro("AAA", "s1"))
    cnt = db.execute_db("SELECT COUNT(*) c FROM recommendations WHERE symbol='AAA'", fetch="one")["c"]
    assert cnt == 1


# ── WI-B2 + WI-B3: interrupted persist isolated, recorded, and self-heals ─────
def _raise(*a, **k):
    raise RuntimeError("disk full")


def test_interrupted_persist_then_recovery(monkeypatch):
    res = [_r("AAA"), _r("BBB"), _r("CCC")]
    monkeypatch.setattr(store, "save_recommendations_batch", _raise)
    m1 = shadow_build(res, scan_id="s1", persist=True, generated_at_utc="2026-06-25 10:00:00")
    assert m1["persist_attempted"] is True
    assert m1["persist_ok"] is False and "disk full" in (m1["persist_error"] or "")
    assert m1["built"] == 3                  # scan did NOT crash — failure isolated
    cnt0 = db.execute_db("SELECT COUNT(*) c FROM recommendations WHERE scan_id='s1'", fetch="one")["c"]
    assert cnt0 == 0                         # nothing persisted on failure

    monkeypatch.undo()                       # restore real persist → recovery on next scan
    m2 = shadow_build(res, scan_id="s1", persist=True, generated_at_utc="2026-06-25 10:00:00")
    assert m2["persist_ok"] is True
    cnt1 = db.execute_db("SELECT COUNT(*) c FROM recommendations WHERE scan_id='s1'", fetch="one")["c"]
    assert cnt1 == 3                         # fully recovered, no duplicates


# ── WI-B3: recovery metrics present + correct on the success path ─────────────
def test_recovery_metrics_present():
    m = shadow_build([_r("AAA")], scan_id="s1", persist=True, generated_at_utc="2026-06-25 10:00:00")
    for k in ("persist_attempted", "persist_ok", "persist_error"):
        assert k in m
    assert m["persist_attempted"] is True
    assert m["persist_ok"] is True
    assert m["persist_error"] is None


# ── WI-B4: fail-open supersession read — DB error degrades to empty set ────────
def test_supersession_fail_open(monkeypatch):
    monkeypatch.setattr(db, "execute_db", _raise)
    assert store.get_deep_symbols_today("2026-06-25") == set()   # in-batch supersession still applies


# ── RC3-A regression: append-only immutability still holds ────────────────────
def test_rc3a_append_only_regression():
    v1 = _ro("AAA", "s1", price=100.0)
    v2 = _ro("AAA", "s1", price=222.0)       # same id, different payload
    store.save_recommendation(v1)
    store.save_recommendation(v2)            # must be a no-op (immutable)
    rid = v1["meta"]["recommendation_id"]
    row = db.execute_db("SELECT input_hash FROM recommendations WHERE recommendation_id=?",
                        (rid,), fetch="one")
    assert row["input_hash"] == v1["audit"]["input_hash"]   # first write preserved
    cnt = db.execute_db("SELECT COUNT(*) c FROM recommendations WHERE recommendation_id=?",
                        (rid,), fetch="one")["c"]
    assert cnt == 1
