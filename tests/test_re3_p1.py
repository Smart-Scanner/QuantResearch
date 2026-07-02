"""RE-3 P1 unit tests — supersession (O1), batch upsert (O2), instrumentation (O3).

Fully ISOLATED against a throwaway SQLite DB (DATABASE_URL cleared, db.DB_PATH redirected)
BEFORE importing db / recommendation_engine, so production is never touched.

Run: python -m pytest test_re3_p1.py -v
"""
import os
import tempfile
import pathlib

import pytest

os.environ.pop("DATABASE_URL", None)
os.environ.pop("SUPABASE_DB_URL", None)

import db  # noqa: E402
_P1_DB = pathlib.Path(tempfile.mkdtemp(prefix="re3p1_")) / "re3p1.db"
db.DB_PATH = _P1_DB
assert not db.is_postgresql()
db._init_sqlite()

from recommendation_engine import store, build_recommendation_object  # noqa: E402
from recommendation_engine.reconcile import shadow_build  # noqa: E402

store.init_recommendation_store()


@pytest.fixture(autouse=True)
def _pin_db():
    db.DB_PATH = _P1_DB
    store.init_recommendation_store()
    # clean slate each test (isolated tables)
    db.execute_db("DELETE FROM recommendations")
    db.execute_db("DELETE FROM scan_results_v2")
    yield


def _r(sym, mode="fast", price=100.0):
    return {"symbol": sym, "scan_mode": mode, "price": price, "atr_pct": 2.0,
            "trade": {"entry_low": 99.0, "entry_high": 101.0, "stop_loss": 95.0,
                      "target1": 108.0, "target2": 115.0, "target3": 125.0},
            "resistances": [108.0, 115.0, 125.0], "supports": [95.0],
            "technical_score": 70, "fundamental_score": 60, "score": 65, "grade": "B"}


# ── O1: in-batch DEEP supersedes FAST (no duplicate ROs) ─────────────────────
def test_supersession_deep_beats_fast_in_batch():
    res = [_r("TCS", "fast"), _r("TCS", "deep")]   # same symbol, both modes
    m = shadow_build(res, scan_id="s1", persist=True, generated_at_utc="2026-06-25 10:00:00")
    assert m["total"] == 1 and m["duplicate"] == 1   # collapsed to one logical recommendation
    rows = db.execute_db("SELECT scan_mode FROM recommendations WHERE symbol='TCS'", fetch="all")
    assert len(rows) == 1 and rows[0]["scan_mode"] == "deep"   # DEEP won


# ── O1: cross-scan FAST superseded by same-day DEEP analysis (scan_results_v2 source) ──
def test_supersession_crossscan_fast_skipped():
    import json as _j
    today = "2026-06-25"
    # the deep-scan path writes a DEEP analysis to scan_results_v2 (the authoritative source)
    db.execute_db(
        "INSERT INTO scan_results_v2 (scan_id, symbol, data, score, scan_date, updated_at) VALUES (?,?,?,?,?,?)",
        ("deep_scan", "INFY", _j.dumps({"scan_mode": "deep"}), 70, today, today + " 10:00:00"))
    # later FAST scan, same day -> FAST must be superseded (skipped, no RO created)
    m = shadow_build([_r("INFY", "fast")], scan_id="fast_scan", persist=True,
                     generated_at_utc=f"{today} 11:00:00")
    assert m["superseded"] == 1 and m["built"] == 0 and m["persisted"] == 0
    cnt = db.execute_db("SELECT COUNT(*) AS c FROM recommendations WHERE symbol='INFY'", fetch="one")["c"]
    assert cnt == 0   # no fast RO created → the deep analysis remains authoritative


def test_crossscan_deep_supersedes_prior_deep():
    shadow_build([_r("WIPRO", "deep")], scan_id="d1", persist=True, generated_at_utc="2026-06-25 10:00:00")
    m = shadow_build([_r("WIPRO", "deep")], scan_id="d2", persist=True, generated_at_utc="2026-06-25 12:00:00")
    assert m["built"] == 1 and m["superseded"] == 0   # newer DEEP allowed


# ── O2: batch upsert semantics (SQLite fallback path) + idempotency ──────────
def test_batch_upsert_persists_and_is_idempotent():
    ros = [build_recommendation_object(_r(s), scan_id="b1", generated_at_utc="t")
           for s in ("AAA", "BBB", "CCC")]
    n1 = store.save_recommendations_batch(ros)
    n2 = store.save_recommendations_batch(ros)     # re-run = upsert, no duplicates
    assert n1 == 3 and n2 == 3
    cnt = db.execute_db("SELECT COUNT(*) AS c FROM recommendations WHERE scan_id='b1'", fetch="one")["c"]
    assert cnt == 3


# ── O3: instrumentation metrics ──────────────────────────────────────────────
def test_instrumentation_metrics_complete():
    res = [_r("E1"), _r("E2"), _r("E1"),            # E1 duplicate
           {"scan_mode": "fast", "price": 100},     # missing symbol
           _r("PENNY", price=5.0)]                  # rejected (liquidity)
    m = shadow_build(res, scan_id="s3", persist=True, generated_at_utc="2026-06-25 10:00:00")
    for k in ("built", "rejected", "duplicate", "missing_symbol", "superseded", "persisted"):
        assert k in m, f"missing metric {k}"
    assert m["missing_symbol"] == 1
    assert m["duplicate"] == 1
    assert m["rejected"] == 1            # PENNY below liquidity floor
    assert m["built"] == m["eligible"] + m["rejected"]
    assert m["persisted"] == m["built"]


# ── determinism preserved under P1 ───────────────────────────────────────────
def test_determinism_preserved():
    a = build_recommendation_object(_r("DET"), scan_id="s1", generated_at_utc="t")
    b = build_recommendation_object(_r("DET"), scan_id="s1", generated_at_utc="t")
    assert a["meta"]["recommendation_id"] == b["meta"]["recommendation_id"]
    assert a["inputs_snapshot"]["input_hash"] == b["inputs_snapshot"]["input_hash"]


def test_scan_mode_persisted_column():
    shadow_build([_r("MODE", "deep")], scan_id="s1", persist=True, generated_at_utc="2026-06-25 10:00:00")
    row = db.execute_db("SELECT scan_mode FROM recommendations WHERE symbol='MODE'", fetch="one")
    assert row["scan_mode"] == "deep"
