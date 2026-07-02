"""RC3-A unit tests — Recommendation Store hardening (store.py only).

Covers the seven RC3-A scope items: append-only persistence, immutable history,
recommendation identity (logical_key), input-hash consistency, TTL cleanup, and removal of
the dead recommendation_live_state. Fully ISOLATED against a throwaway SQLite DB
(DATABASE_URL cleared, db.DB_PATH redirected) BEFORE importing db / recommendation_engine,
so production is never touched.

Run: python -m pytest test_rc3a_store.py -v
"""
import os
import json
import tempfile
import pathlib

import pytest

os.environ.pop("DATABASE_URL", None)
os.environ.pop("SUPABASE_DB_URL", None)
os.environ["RE2_RO_BUILD"] = "0"           # store stays shadow/dormant — RC3-A changes no behaviour

import db  # noqa: E402
_RC3_DB = pathlib.Path(tempfile.mkdtemp(prefix="rc3a_")) / "rc3a.db"
db.DB_PATH = _RC3_DB
assert not db.is_postgresql()
db._init_sqlite()

from recommendation_engine import store, build_recommendation_object, SCHEMA_VERSION  # noqa: E402

store.init_recommendation_store()


@pytest.fixture(autouse=True)
def _pin_db():
    db.DB_PATH = _RC3_DB
    store.init_recommendation_store()
    db.execute_db("DELETE FROM recommendations")
    yield


def _r(sym, price=100.0):
    return {"symbol": sym, "scan_mode": "fast", "price": price, "atr_pct": 2.0,
            "trade": {"entry_low": 99.0, "entry_high": 101.0, "stop_loss": 95.0,
                      "target1": 108.0, "target2": 115.0, "target3": 125.0},
            "resistances": [108.0, 115.0, 125.0], "supports": [95.0],
            "technical_score": 70, "fundamental_score": 60, "score": 65, "grade": "B"}


def _ro(sym, scan_id, price=100.0, gen="2026-06-25 10:00:00"):
    return build_recommendation_object(_r(sym, price), scan_id=scan_id, generated_at_utc=gen)


def _col(rec_id, col):
    return db.execute_db(f"SELECT {col} FROM recommendations WHERE recommendation_id=?",
                         (rec_id,), fetch="one")


# ── RC3-A append-only / immutability: a persisted row is NEVER mutated in place ──
def test_append_only_first_write_wins_no_inplace_mutation():
    v1 = _ro("AAA", "s1", price=100.0)
    v2 = _ro("AAA", "s1", price=222.0)            # same id (AAA|s1|schema), DIFFERENT content
    assert v1["meta"]["recommendation_id"] == v2["meta"]["recommendation_id"]
    assert v1["audit"]["input_hash"] != v2["audit"]["input_hash"]   # genuinely different payload

    store.save_recommendation(v1)
    store.save_recommendation(v2)                  # must be a NO-OP (DO NOTHING), not an overwrite

    rid = v1["meta"]["recommendation_id"]
    cnt = db.execute_db("SELECT COUNT(*) AS c FROM recommendations WHERE recommendation_id=?",
                        (rid,), fetch="one")["c"]
    assert cnt == 1                                # exactly one immutable row
    assert _col(rid, "input_hash")["input_hash"] == v1["audit"]["input_hash"]   # FIRST write preserved
    assert _col(rid, "input_hash")["input_hash"] != v2["audit"]["input_hash"]   # NOT overwritten


# ── RC3-A versioned history: new scan_id ⇒ new immutable row; latest is most recent ──
def test_append_only_versioned_history_across_scans():
    store.save_recommendation(_ro("BBB", "s1", price=100.0, gen="2026-06-25 10:00:00"))
    store.save_recommendation(_ro("BBB", "s2", price=140.0, gen="2026-06-25 12:00:00"))
    rows = db.execute_db("SELECT recommendation_id FROM recommendations WHERE symbol='BBB'", fetch="all")
    assert len(rows) == 2                          # two immutable versions retained
    latest = store.get_recommendation("BBB")
    assert latest["generated_at_utc"] == "2026-06-25 12:00:00"   # newest version is authoritative


# ── RC3-A input-hash consistency: column == audit blob == inputs_snapshot (single source) ──
def test_input_hash_single_source_consistency():
    ro = _ro("CCC", "s1")
    store.save_recommendation(ro)
    rid = ro["meta"]["recommendation_id"]
    row = db.execute_db("SELECT input_hash, audit, core FROM recommendations WHERE recommendation_id=?",
                        (rid,), fetch="one")
    audit_hash = json.loads(row["audit"])["input_hash"]
    isnap_hash = json.loads(row["core"])["inputs_snapshot"]["input_hash"]
    assert row["input_hash"] == audit_hash         # column sourced from the audit block
    assert row["input_hash"] == isnap_hash         # and consistent with inputs_snapshot
    assert row["input_hash"] == ro["audit"]["input_hash"]


# ── RC3-A recommendation identity: stable logical_key; recommendation_id is per-scan ──
def test_logical_key_stable_identity():
    a = _ro("DDD", "s1")
    b = _ro("DDD", "s2")                            # same symbol, different scan
    store.save_recommendation(a)
    store.save_recommendation(b)
    ka = _col(a["meta"]["recommendation_id"], "logical_key")["logical_key"]
    kb = _col(b["meta"]["recommendation_id"], "logical_key")["logical_key"]
    assert ka == kb == f"DDD|NSE|{SCHEMA_VERSION}"  # stable lineage across runs
    assert a["meta"]["recommendation_id"] != b["meta"]["recommendation_id"]   # per-scan version id


# ── RC3-A TTL cleanup: expired rows purged (retention); fresh rows retained ──
def test_purge_expired_enforces_ttl():
    old = _ro("OLD", "s_old", gen="2020-01-01 00:00:00")
    old["meta"]["ttl_sec"] = 60                     # expired long ago
    fresh = _ro("NEW", "s_new", gen="2026-06-26 09:00:00")   # ttl default 86400 → still valid
    store.save_recommendation(old)
    store.save_recommendation(fresh)

    purged = store.purge_expired("2026-06-26 12:00:00")
    assert purged == 1                              # only OLD elapsed (2020 + 60s < now)
    remaining = {r["symbol"] for r in
                 db.execute_db("SELECT symbol FROM recommendations", fetch="all")}
    assert remaining == {"NEW"}                     # fresh row retained, immutable, untouched


# ── RC3-A dead-code removal: no recommendation_live_state writer or table ──
def test_dead_live_state_removed():
    assert not hasattr(store, "update_live_state")  # writer stub removed
    t = db.execute_db("SELECT name FROM sqlite_master WHERE type='table' AND name='recommendation_live_state'",
                      fetch="all")
    assert not t                                    # table no longer created by init


# ── RC3-A regression: batch path is append-only + idempotent (RE-3 P1 semantics preserved) ──
def test_batch_append_only_idempotent():
    ros = [_ro(s, "b1") for s in ("AAA", "BBB", "CCC")]
    n1 = store.save_recommendations_batch(ros)
    n2 = store.save_recommendations_batch(ros)      # re-run = no-op (DO NOTHING)
    assert n1 == 3 and n2 == 3
    cnt = db.execute_db("SELECT COUNT(*) AS c FROM recommendations WHERE scan_id='b1'", fetch="one")["c"]
    assert cnt == 3                                 # no duplicates, no mutation


# ── RC3-A schema: logical_key column present and indexed-safe ──
def test_schema_has_logical_key_column():
    cols = {r["name"] for r in db.execute_db("PRAGMA table_info(recommendations)", fetch="all")}
    assert "logical_key" in cols
    assert {"recommendation_id", "input_hash", "scan_mode", "ttl_sec"} <= cols   # nothing dropped
