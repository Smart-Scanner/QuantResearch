"""RE-3 Recommendation store (RE-2A §4) — the single canonical RO read/write path.

Table (additive, idempotent, cross-DB via db.execute_db):
  * recommendations — APPEND-ONLY, immutable, versioned RO core + payloads + audit.

RC3-A (store hardening — confined to this module; no consumer/behaviour change):
  * Append-only persistence: writes use ON CONFLICT DO NOTHING — a persisted row is NEVER
    mutated in place (was DO UPDATE). First write wins; a new scan_id yields a new
    recommendation_id → a new immutable row (versioned history).
  * Input-hash consistency: input_hash + formula_versions are read from the audit block
    (the single authoritative provenance record) so the persisted columns can never diverge
    from the audit JSON.
  * Recommendation identity: `logical_key` (symbol|exchange|schema_version) is the stable
    cross-run lineage key; `recommendation_id` stays the per-scan version id (RO unchanged).
  * TTL cleanup: `purge_expired()` enforces the per-row ttl_sec as RETENTION (not mutation).
  * Removed the dead recommendation_live_state table + update_live_state writer (no
    reader/writer existed anywhere in the repo).

Shadow only: the build/persist path is gated by RE2_RO_BUILD (default OFF). No consumer
reads this table yet.
"""
import json

import db

_DDL_REC = """
CREATE TABLE IF NOT EXISTS recommendations (
    recommendation_id TEXT PRIMARY KEY,
    symbol            TEXT NOT NULL,
    exchange          TEXT DEFAULT 'NSE',
    scan_id           TEXT NOT NULL,
    schema_version    TEXT NOT NULL,
    model_version     TEXT NOT NULL,
    formula_versions  TEXT,
    input_hash        TEXT,
    generated_at_utc  TEXT NOT NULL,
    ttl_sec           INTEGER DEFAULT 86400,
    status            TEXT NOT NULL,
    eligible          INTEGER DEFAULT 0,
    supersedes_id     TEXT,
    scan_mode         TEXT,
    logical_key       TEXT,
    core              TEXT NOT NULL,
    payloads          TEXT,
    audit             TEXT,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

# Canonical column order — shared by single + batch insert so they stay identical.
_COLS = ["recommendation_id", "symbol", "exchange", "scan_id", "schema_version",
         "model_version", "formula_versions", "input_hash", "generated_at_utc",
         "ttl_sec", "status", "eligible", "supersedes_id", "scan_mode",
         "logical_key", "core", "payloads", "audit"]


def init_recommendation_store():
    """Idempotent table creation + additive columns. Safe to call repeatedly.

    Append-only RO store. The dead recommendation_live_state table is no longer created (it
    had no writer or reader). An orphaned empty table on a pre-RC3 install is inert; an
    optional `DROP TABLE IF EXISTS recommendation_live_state` may be run as separate cleanup.
    """
    db.execute_db(_DDL_REC)
    for _alter in ("ALTER TABLE recommendations ADD COLUMN scan_mode TEXT",
                   "ALTER TABLE recommendations ADD COLUMN logical_key TEXT"):
        try:                              # additive for pre-existing tables (cross-DB, no IF NOT EXISTS)
            db.execute_db(_alter)
        except Exception:
            pass                          # column already exists
    db.execute_db("CREATE INDEX IF NOT EXISTS idx_rec_symbol_status ON recommendations(symbol, status)")
    db.execute_db("CREATE INDEX IF NOT EXISTS idx_rec_scan ON recommendations(scan_id)")
    db.execute_db("CREATE INDEX IF NOT EXISTS idx_rec_logical ON recommendations(logical_key)")


def _to_row(ro: dict) -> tuple:
    """Flatten an RO into the canonical column tuple (shared single/batch).

    input_hash + formula_versions are read from the audit block — the single authoritative
    provenance record — so the persisted columns can never diverge from the audit JSON.
    logical_key = symbol|exchange|schema_version is the stable cross-run lineage identity;
    recommendation_id stays the per-scan version id.
    """
    m = ro["meta"]
    core = {k: ro[k] for k in ("engines", "scoring", "eligibility", "trade",
                               "sizing", "allocation", "presentation", "inputs_snapshot")}
    isnap = ro.get("inputs_snapshot", {})
    audit = ro.get("audit") or {}
    logical_key = f'{m["symbol"]}|{m.get("exchange", "NSE")}|{m["schema_version"]}'
    return (m["recommendation_id"], m["symbol"], m.get("exchange", "NSE"), m["scan_id"],
            m["schema_version"], m["model_version"], json.dumps(audit.get("formula_versions")),
            audit.get("input_hash") or isnap.get("input_hash"), m["generated_at_utc"],
            m.get("ttl_sec", 86400), m["status"], 1 if ro["eligibility"]["eligible"] else 0,
            m.get("supersedes_id"), isnap.get("scan_mode"), logical_key,
            json.dumps(core), json.dumps(ro["payloads"]), json.dumps(ro["audit"]))


def save_recommendation(ro: dict):
    """Append-only insert of one RO (first write wins; immutable thereafter).

    A persisted row is NEVER updated in place. A re-run with the same recommendation_id is a
    no-op (ON CONFLICT DO NOTHING); a new scan yields a new recommendation_id → a new
    immutable row (versioned history). Cross-DB single-row path.
    """
    ph = ",".join("?" * len(_COLS))
    db.execute_db(
        f"INSERT INTO recommendations ({','.join(_COLS)}) VALUES ({ph}) "
        f"ON CONFLICT(recommendation_id) DO NOTHING",
        _to_row(ro))


def save_recommendations_batch(ros: list) -> int:
    """Batched, transaction-safe APPEND-ONLY insert (O2). Deterministic order by
    recommendation_id.

    PostgreSQL: one execute_values per chunk inside an explicit transaction (commit on
    success, rollback on error → rollback-safe). SQLite: single-row fallback (identical
    append-only semantics via the same ON CONFLICT DO NOTHING). Returns rows attempted.
    """
    if not ros:
        return 0
    ros = sorted(ros, key=lambda r: r["meta"]["recommendation_id"])   # deterministic ordering
    if not db.is_postgresql():
        for ro in ros:
            save_recommendation(ro)
        return len(ros)

    from psycopg2.extras import execute_values
    pool = db._get_pg_pool()
    if pool is None:                       # PG configured but pool unavailable (cooldown) →
        for ro in ros:                     # safe per-row fallback; never crash the (shadow) caller
            save_recommendation(ro)
        return len(ros)

    conn = pool.getconn()
    broken = False                         # track whether the pooled conn is safe to reuse (RC3-B)
    try:
        conn.autocommit = False
        rows = [_to_row(ro) for ro in ros]
        sql = (f"INSERT INTO recommendations ({','.join(_COLS)}) VALUES %s "
               f"ON CONFLICT(recommendation_id) DO NOTHING")
        with conn.cursor() as cur:         # atomic: every chunk commits together, or none at all
            for i in range(0, len(rows), 200):
                execute_values(cur, sql, rows[i:i + 200], page_size=200)
        conn.commit()
        return len(rows)
    except Exception:
        try:
            conn.rollback()                # no partial persist — roll the whole batch back
        except Exception:
            broken = True                  # rollback failed ⇒ conn unusable; discard, don't poison pool
        raise
    finally:
        if not broken:
            try:
                conn.autocommit = True     # restore pooled-conn default before returning it
            except Exception:
                broken = True
        try:
            pool.putconn(conn, close=broken)   # close=True discards a broken conn instead of reusing it
        except Exception:
            pass


def get_deep_symbols_today(today: str) -> set:
    """Symbols with a same-day DEEP analysis — the supersession authority (O1).

    In P1 the deep-scan path (the auto-scan deep enrichment) writes only to
    scan_results_v2 and does NOT build ROs, so the authoritative deep-set is read from
    scan_results_v2 — exactly mirroring the save_results staleness guard (db.py:3706/3711).
    The RO pipeline OWNS the supersession DECISION; the deep-set SOURCE remains
    scan_results_v2 until the deep path is RO-ified (P2+), after which this can read the RO
    store (the `scan_mode` column) directly. `today` = the generated_at_utc date (YYYY-MM-DD).
    """
    try:
        if db.is_postgresql():
            rows = db.execute_db(
                "SELECT DISTINCT symbol FROM scan_results_v2 "
                "WHERE scan_date=? AND (data->>'scan_mode')='deep'", (today,), fetch="all")
        else:
            rows = db.execute_db(
                "SELECT DISTINCT symbol FROM scan_results_v2 "
                "WHERE scan_date=? AND json_extract(data,'$.scan_mode')='deep'", (today,), fetch="all")
        return {r["symbol"] for r in (rows or [])}
    except Exception:
        return set()   # fail-open: no cross-scan supersession this run (in-batch still applies)


def get_recommendation(symbol: str):
    """Latest RO for a symbol (the future SSOT read path).

    Append-only store ⇒ a symbol may have many immutable rows over time; the latest version
    is the most recent generated_at_utc.
    """
    row = db.execute_db(
        "SELECT core, payloads, audit, status, scan_id, generated_at_utc "
        "FROM recommendations WHERE symbol=? ORDER BY generated_at_utc DESC LIMIT 1",
        (symbol.upper(),), fetch="one")
    return _row_to_ro(row)


def get_recommendations(scan_id: str):
    rows = db.execute_db(
        "SELECT core, payloads, audit, status, scan_id, generated_at_utc "
        "FROM recommendations WHERE scan_id=?", (scan_id,), fetch="all")
    return [_row_to_ro(r) for r in (rows or [])]


def _row_to_ro(row):
    if not row:
        return None
    core = json.loads(row["core"]) if row.get("core") else {}
    return {**core, "payloads": json.loads(row["payloads"]) if row.get("payloads") else None,
            "audit": json.loads(row["audit"]) if row.get("audit") else None,
            "status": row.get("status"), "scan_id": row.get("scan_id"),
            "generated_at_utc": row.get("generated_at_utc")}


def purge_expired(now_utc: str) -> int:
    """Retention cleanup — delete recommendations whose TTL has elapsed (enforces ttl_sec).

    RETENTION, not mutation: immutable rows are never modified in place; fully-expired rows
    (generated_at_utc + ttl_sec < now) are removed wholesale. On-demand maintenance ONLY —
    never on the scan/live path. `now_utc` = 'YYYY-MM-DD HH:MM:SS'. Returns the number of
    rows purged; fail-safe → 0 on any error.
    """
    try:
        if db.is_postgresql():
            where = "(generated_at_utc::timestamp + (ttl_sec || ' seconds')::interval) < ?::timestamp"
        else:
            where = "datetime(generated_at_utc, '+' || ttl_sec || ' seconds') < datetime(?)"
        row = db.execute_db(f"SELECT COUNT(*) AS n FROM recommendations WHERE {where}",
                            (now_utc,), fetch="one")
        n = (row["n"] if row else 0) or 0
        if n:
            db.execute_db(f"DELETE FROM recommendations WHERE {where}", (now_utc,))
        return int(n)
    except Exception:
        return 0
