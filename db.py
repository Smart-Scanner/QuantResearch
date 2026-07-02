"""
SQLite and PostgreSQL database wrapper for Smart Screener.
Stores scan results, historical scores, metadata, and normalized analytics tables.

Phase 1 Changes:
- Replaced thread-local PG connections with ThreadedConnectionPool (psycopg2.pool)
- maxconn dynamically reads MAX_DB_CONNECTIONS env variable (default 10)
- execute_db() now uses pool.getconn()/putconn() with proper finally clause
- SQLite path uses fresh connect() per call (no thread-local, WAL mode)
- All direct cursor usage (remove_custom_stock, create_portfolio, add_position,
  update_position) migrated to execute_db()
- Added pool_status(), pg_cooldown_active() helpers
- Added _collect_result() and _execute_sqlite() helpers
- Removed _local threading.local() global, _get_conn(), _get_sqlite_conn()
"""

import os
import json
import logging
import math
import atexit
import threading
import sqlite3
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from metrics.timer import timed

log = logging.getLogger("db")

# ── P0: JSON Sanitization & Observability Metrics ───────────────────────
# Thread-safe memory counters for JSON sanitization and operational metrics.
# Flushed to scan_meta periodically (every 5 min) and on application exit.
# Success-only reset: counters are only decremented after confirmed DB write.
_batch_metrics = {
    "json_processed_count": 0,
    "json_sanitized_count": 0,
    "json_rejected_count": 0,
    "angel_reauth_count": 0,
    "sqlite_fallback_count": 0,
}
_metrics_lock = threading.Lock()


def increment_mem_counter(key: str, amount: int = 1):
    """Increment an in-memory metric counter (thread-safe)."""
    with _metrics_lock:
        _batch_metrics[key] = _batch_metrics.get(key, 0) + amount


def flush_metrics_to_db():
    """Flush in-memory metric counters to scan_meta.

    Success-only reset: counters are decremented only after confirmed
    DB write so that failed flushes retain the data for the next attempt.
    """
    with _metrics_lock:
        snapshot = {k: v for k, v in _batch_metrics.items() if v != 0}
    if not snapshot:
        return
    for key, amount in snapshot.items():
        try:
            current = get_meta(key) or 0
            try:
                current = int(current)
            except (ValueError, TypeError):
                current = 0
            set_meta(key, str(current + amount))
            # Success — deduct flushed amount
            with _metrics_lock:
                _batch_metrics[key] = _batch_metrics.get(key, 0) - amount
        except Exception as exc:
            log.warning("[METRICS_FLUSH] Failed to flush %s=%d: %s — retaining for next flush", key, amount, exc)


def _metrics_flush_loop():
    """Background daemon thread: flush metrics every 5 minutes."""
    while True:
        try:
            time.sleep(300)
            flush_metrics_to_db()
        except Exception as exc:
            log.warning("[METRICS_FLUSH] Background flush error (non-fatal): %s", exc)


try:
    _metrics_flush_thread = threading.Thread(target=_metrics_flush_loop, daemon=True)
    _metrics_flush_thread.start()
except Exception:
    pass  # Thread failure must never block startup

atexit.register(flush_metrics_to_db)


def sanitize_for_json(obj, symbol=None, scan_id=None, component=None, _path="", _visited=None):
    """Recursively sanitize a Python object for safe JSON serialization.

    - Replaces float NaN, inf, -inf with None.
    - Tracks visited containers (dict/list/tuple/set) to detect circular references.
    - Primitives (str, int, float, bool, None) are NOT tracked to avoid false
      positives from Python's object interning.
    - Logs each replacement for root-cause analysis.

    Returns the sanitized object (new copy for containers, in-place for primitives).
    """
    if _visited is None:
        _visited = set()

    # Primitive float check — the main NaN/inf catch
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            label = "NaN" if math.isnan(obj) else ("inf" if obj > 0 else "-inf")
            log.warning(
                "[JSON_SANITIZER] symbol=%s field=%s component=%s original=%s scan_id=%s",
                symbol or "?", _path or "root", component or "unknown", label, scan_id or "?"
            )
            increment_mem_counter("json_sanitized_count")
            return None
        return obj

    # numpy scalar check (has .item())
    if hasattr(obj, "item"):
        try:
            native = obj.item()
            if isinstance(native, float) and (math.isnan(native) or math.isinf(native)):
                label = "NaN" if math.isnan(native) else ("inf" if native > 0 else "-inf")
                log.warning(
                    "[JSON_SANITIZER] symbol=%s field=%s component=%s original=%s(numpy) scan_id=%s",
                    symbol or "?", _path or "root", component or "unknown", label, scan_id or "?"
                )
                increment_mem_counter("json_sanitized_count")
                return None
            return native
        except Exception:
            pass

    # Container types — track by id to detect circular references
    if isinstance(obj, dict):
        obj_id = id(obj)
        if obj_id in _visited:
            log.warning("[JSON_SANITIZER_CIRCULAR] Circular reference detected at symbol=%s path=%s", symbol, _path)
            return None
        _visited.add(obj_id)
        result = {}
        for k, v in obj.items():
            child_path = f"{_path}.{k}" if _path else k
            child_component = component
            # Infer component from top-level keys
            if not _path:
                if k == "fundamentals":
                    child_component = "fundamentals"
                elif k in ("news_sentiment", "gdelt"):
                    child_component = "sentiment"
                elif k in ("_score_components", "earnings_signals"):
                    child_component = "scoring"
                elif k in ("trade", "support_resistance"):
                    child_component = "trade_levels"
                elif child_component is None:
                    child_component = "technical_scoring"
            result[k] = sanitize_for_json(v, symbol=symbol, scan_id=scan_id, component=child_component, _path=child_path, _visited=_visited)
        _visited.discard(obj_id)
        return result

    if isinstance(obj, (list, tuple)):
        obj_id = id(obj)
        if obj_id in _visited:
            log.warning("[JSON_SANITIZER_CIRCULAR] Circular reference detected at symbol=%s path=%s", symbol, _path)
            return None
        _visited.add(obj_id)
        items = [
            sanitize_for_json(v, symbol=symbol, scan_id=scan_id, component=component,
                              _path=f"{_path}[{i}]", _visited=_visited)
            for i, v in enumerate(obj)
        ]
        _visited.discard(obj_id)
        return items if isinstance(obj, list) else tuple(items)

    if isinstance(obj, set):
        obj_id = id(obj)
        if obj_id in _visited:
            log.warning("[JSON_SANITIZER_CIRCULAR] Circular reference detected at symbol=%s path=%s", symbol, _path)
            return None
        _visited.add(obj_id)
        items = [
            sanitize_for_json(v, symbol=symbol, scan_id=scan_id, component=component,
                              _path=f"{_path}{{set}}", _visited=_visited)
            for v in obj
        ]
        _visited.discard(obj_id)
        return items

    # All other primitives (str, int, bool, None) — pass through
    return obj


def verify_json_nan_prevention():
    """Startup verification: ensure sanitizer and allow_nan=False work correctly.

    Non-fatal: logs CRITICAL on failure but does NOT crash the application.
    """
    try:
        # Test 1: sanitize_for_json replaces NaN
        test_obj = {"price": float("nan"), "rsi": float("inf"), "atr": float("-inf"), "name": "TEST"}
        sanitized = sanitize_for_json(test_obj, symbol="STARTUP_TEST", scan_id="startup", component="verify")
        assert sanitized["price"] is None, "NaN was not replaced"
        assert sanitized["rsi"] is None, "inf was not replaced"
        assert sanitized["atr"] is None, "-inf was not replaced"
        assert sanitized["name"] == "TEST", "Normal value was corrupted"

        # Test 2: json.dumps with allow_nan=False passes on sanitized data
        json.dumps(sanitized, allow_nan=False)

        # Test 3: json.dumps with allow_nan=False rejects raw NaN
        try:
            json.dumps({"bad": float("nan")}, allow_nan=False)
            assert False, "json.dumps should have raised ValueError"
        except ValueError:
            pass  # Expected

        # Test 4: circular reference guard
        circular = {}
        circular["self"] = circular
        result = sanitize_for_json(circular, symbol="CIRCULAR_TEST")
        assert result is not None, "Top-level should not be None"

        log.info("[JSON_STARTUP_VERIFY] Startup JSON sanitizer check passed successfully.")
    except Exception as exc:
        log.critical("[JSON_STARTUP_VERIFY] Startup validation failed, but continuing: %s", exc)

DB_PATH = Path(__file__).parent / "cache" / "screener.db"

DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")

# ── Phase 1: In-memory scan_meta cache ──────────────────────────────────
# Eliminates ~272ms DB round-trip per get_meta() call.
# Write-through on set_meta() keeps cache consistent.
# clear_meta_cache() called on scan start/end for instant UI refresh.
_META_CACHE_ENABLED = os.getenv("ENABLE_META_CACHE", "true").lower() == "true"
_meta_cache: dict = {}   # key -> (parsed_value, timestamp)
_META_TTL = 30           # seconds — stale reads acceptable for display-only data

# ── Phase 1C: slim_data — pre-stripped JSON for list/card views ─────────
# These heavy fields account for ~92% of the /api/results payload.
# slim_data stores everything EXCEPT these fields, reducing 3.6MB → ~300KB.
# ╔══════════════════════════════════════════════════════════════╗
# ║ IMPORTANT: Whenever a new large field is added to scan      ║
# ║ result payloads, _HEAVY_FIELDS must be reviewed and updated.║
# ║ Failure to do so may silently increase dashboard payload.   ║
# ║ Slim payload must remain significantly smaller than full.   ║
# ╚══════════════════════════════════════════════════════════════╝
_HEAVY_FIELDS = frozenset({
    "chart_data", "signals", "fundamentals", "trade", "order_book",
    "seasonal", "news_sentiment", "support_resistance", "sector_rotation",
    "gdelt", "macro_event", "earnings_signals",
    # Phase 0 audit fields (internal — never sent to frontend)
    "_score_components", "_data_source", "_source_reason",
    "_provider_latency_ms", "_data_staleness_hours",
})
_DB_USE_SLIM = os.getenv("DB_USE_SLIM", "true").lower() == "true"

# ── Phase 0.5: Data Integrity — required fields for valid scan results ──────
_REQUIRED_FIELDS = frozenset({"score", "sector", "price"})
_RECOMMENDED_FIELDS = frozenset({"risk_reward", "target_price", "stop_loss", "weekly_trend", "trade"})


def validate_scan_result(stock: dict) -> tuple[bool, list[str]]:
    """Validate a single scan result has all critical fields.
    Returns (is_valid, list_of_issues).
    """
    issues = []
    if not isinstance(stock, dict):
        return False, ["not a dict"]
    for f in _REQUIRED_FIELDS:
        if f not in stock or stock[f] is None:
            issues.append(f"missing required: {f}")
    for f in _RECOMMENDED_FIELDS:
        if f not in stock or stock[f] is None:
            issues.append(f"missing recommended: {f}")
    # trade object should have entry_low at minimum
    trade = stock.get("trade")
    if trade and isinstance(trade, dict):
        if "entry_low" not in trade:
            issues.append("trade missing entry_low")
    elif "trade" in _RECOMMENDED_FIELDS:
        pass  # already flagged above
    is_valid = not any("missing required" in i for i in issues)
    return is_valid, issues


def run_data_integrity_audit():
    """Startup audit: validate all scan results in DB. Logs summary."""
    try:
        rows = execute_db("SELECT data FROM scan_results LIMIT 2000", fetch="all")
        if not rows:
            log.info("[DATA INTEGRITY] No scan results to audit")
            return
        valid = 0
        invalid = 0
        issues_summary = {}  # field -> count
        for row in rows:
            r = _parse_data_column(row.get("data"))
            if not r:
                invalid += 1
                continue
            is_valid, issues = validate_scan_result(r)
            if is_valid:
                valid += 1
            else:
                invalid += 1
            for issue in issues:
                issues_summary[issue] = issues_summary.get(issue, 0) + 1
        log.info("[DATA INTEGRITY] valid=%d invalid=%d total=%d", valid, invalid, valid + invalid)
        if issues_summary:
            top_issues = sorted(issues_summary.items(), key=lambda x: -x[1])[:10]
            for issue, count in top_issues:
                log.warning("[DATA INTEGRITY]   %s: %d stocks", issue, count)
    except Exception as exc:
        log.warning("[DATA INTEGRITY] Audit failed (non-fatal): %s", exc)


def _build_slim(r: dict) -> str:
    """Build slim JSON string from a result dict, stripping heavy fields.

    Extracts a compact trade_summary per the Trade Contract Matrix:
    entry_low, entry_high, stop_loss, target1, target2, target3,
    risk_reward, booking_plan, target_1, target_2.
    """
    slim = {k: v for k, v in r.items() if k not in _HEAVY_FIELDS}
    # Release 1: Preserve news sentiment score in slim payload
    # (the full news_sentiment object is stripped by _HEAVY_FIELDS for bandwidth)
    if r.get("news_sentiment"):
        slim["news_sentiment_score"] = r["news_sentiment"].get("score", 0.0)
    trade = r.get("trade")
    if isinstance(trade, dict):
        trade_summary = {
            "entry_low":    trade.get("entry_low"),
            "entry_high":   trade.get("entry_high"),
            "stop_loss":    trade.get("stop_loss"),
            "target1":      trade.get("target1"),
            "target2":      trade.get("target2"),
            "target3":      trade.get("target3"),
            "target_1":     trade.get("target_1") or trade.get("target1"),
            "target_2":     trade.get("target_2") or trade.get("target2"),
            "risk_reward":  trade.get("risk_reward"),
            "booking_plan": trade.get("booking_plan"),
            "cmp":          trade.get("cmp"),
        }
        # Keep compact: drop None values
        slim["trade_summary"] = {k: v for k, v in trade_summary.items() if v is not None}
    # P0: Sanitize slim payload and serialize with allow_nan=False
    sym = r.get("symbol", "?")
    slim = sanitize_for_json(slim, symbol=sym, component="slim_data")
    return json.dumps(slim, default=str, allow_nan=False)


def is_postgresql() -> bool:
    """Check if PostgreSQL is configured."""
    return bool(
        DATABASE_URL and (
            DATABASE_URL.startswith("postgres://")
            or DATABASE_URL.startswith("postgresql://")
        )
    )


# Canonical engine tag for the legacy-vs-scoring_v1 comparison. The scoring_v1
# pipeline tags its output 'scoring_v1'; EVERYTHING else (analyzer's 'R2.1', '',
# NULL, legacy junk) canonicalises to 'legacy'. Applied at every persistence sink
# (scan_results_v2, paper_trades, recommendation_snapshots, paper_portfolio_daily)
# and in the model-aware dedup, so "WHERE model_version='legacy'" always resolves.
# Distinct engines that KEEP their own model_version tag across all sinks
# (scan_results_v2, paper_trades, recommendation_snapshots, paper_portfolio_daily) so
# their per-engine dedup/stats never blend. Everything else canonicalizes to 'legacy'.
# Add a new engine's model_version here so its paper-trades stay separate (e.g. a same-
# symbol trade can coexist across legacy + scoring_v1 + legacy_cleaned as independent rows).
_KNOWN_ENGINES = {"scoring_v1", "legacy_cleaned"}


def _canon_model_version(mv) -> str:
    """A known distinct engine keeps its tag; anything else -> 'legacy' (single canonical
    legacy tag). scoring_v1 + legacy behaviour is unchanged; legacy_cleaned now stays distinct."""
    m = (mv or "")
    return m if m in _KNOWN_ENGINES else "legacy"


_MIG_MV_FLAG = "mig_model_version_v1_done"


def _migrate_model_version_v1(cur, is_pg: bool):
    """One-time, idempotent migration for the legacy-vs-scoring_v1 comparison.

    Validated on a staging DB (staging_migration_test) incl. the CHANGE-2 exact
    restore-from-backup proof (NULL-safe). Runs inside the init transaction.

      CHANGE-1 (additive): scan_results_v2 + paper_orders get a real indexed
        model_version column; explicit CASE backfill from JSON is the source of
        truth (DEFAULT 'legacy' is only a safety net). data JSON is never mutated,
        so CHANGE-1 rollback = just leave the column (no DROP).
      CHANGE-2 (destructive retag): paper_trades / recommendation_snapshots /
        paper_portfolio_daily — back up model_version into model_version_backup
        (NULL-safe one-shot) BEFORE canonicalising every non-scoring_v1 tag to
        'legacy'. Rollback = restore from model_version_backup.

    The destructive block runs EXACTLY ONCE under a scan_meta flag; additive
    ALTER/INDEX statements are always safe to re-run.
    """
    add_col = "ADD COLUMN IF NOT EXISTS" if is_pg else "ADD COLUMN"
    ph = "%s" if is_pg else "?"
    sinks = ("paper_trades", "recommendation_snapshots", "paper_portfolio_daily")

    def _try(sql):
        # SQLite has no ADD COLUMN IF NOT EXISTS -> ignore duplicate-column errors.
        try:
            cur.execute(sql)
        except Exception:
            pass

    # --- additive structure (always idempotent) ---
    _try(f"ALTER TABLE scan_results_v2 {add_col} model_version TEXT DEFAULT 'legacy'")
    _try(f"ALTER TABLE paper_orders {add_col} model_version TEXT DEFAULT 'legacy'")
    for t in sinks:
        _try(f"ALTER TABLE {t} {add_col} model_version_backup TEXT")
    # scoring_v1 analytic fields on the recommendation_snapshots ledger (NULL for legacy)
    for coldef in ("composite_z REAL", "drivers TEXT", "weaknesses TEXT",
                   "data_integrity TEXT", "signal_agreement TEXT"):
        _try(f"ALTER TABLE recommendation_snapshots {add_col} {coldef}")
    _try("CREATE INDEX IF NOT EXISTS idx_srv2_model ON scan_results_v2(model_version)")
    _try("CREATE INDEX IF NOT EXISTS idx_srv2_model_updated ON scan_results_v2(model_version, updated_at DESC)")

    # --- one-time destructive block (flag-guarded; atomic in the init txn) ---
    try:
        cur.execute(f"SELECT value FROM scan_meta WHERE key = {ph}", (_MIG_MV_FLAG,))
        if cur.fetchone():
            return  # already applied -> skip (NULL backups preserved)
    except Exception:
        return  # scan_meta not ready -> defer to a later init pass

    # CHANGE-1: explicit CASE backfill (source of truth, not the DEFAULT)
    json_mv = "data->>'model_version'" if is_pg else "json_extract(data,'$.model_version')"
    cur.execute(f"""UPDATE scan_results_v2 SET model_version = CASE
        WHEN COALESCE({json_mv},'') = 'scoring_v1' THEN 'scoring_v1' ELSE 'legacy' END""")
    # CHANGE-2: NULL-safe one-shot backup BEFORE destructive retag (x3 sinks)
    for t in sinks:
        cur.execute(f"UPDATE {t} SET model_version_backup = model_version")
        cur.execute(f"UPDATE {t} SET model_version = 'legacy' WHERE COALESCE(model_version,'') <> 'scoring_v1'")
    # set the flag LAST (same transaction) so a mid-failure cleanly retries
    if is_pg:
        cur.execute("INSERT INTO scan_meta (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                    (_MIG_MV_FLAG, "1"))
    else:
        cur.execute("INSERT OR IGNORE INTO scan_meta (key, value) VALUES (?, ?)", (_MIG_MV_FLAG, "1"))
    log.info("[MIGRATION] model_version v1 applied (scan_results_v2+paper_orders col; legacy retag x3 sinks)")

# ─── ThreadedConnectionPool (Phase 1) ───

_pg_pool = None
_pg_pool_lock = threading.Lock()
_pg_cooldown_until = 0.0

def _normalize_pg_url(url: str) -> str:
    """Ensure URL uses postgresql:// scheme and has sslmode set."""
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if "sslmode" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url

def _get_pg_pool():
    """Lazy-initialise a ThreadedConnectionPool. Returns None if PG unavailable."""
    global _pg_pool, _pg_cooldown_until
    if _pg_pool is not None:
        return _pg_pool
    with _pg_pool_lock:
        if _pg_pool is not None:
            return _pg_pool
        now = time.time()
        if now < _pg_cooldown_until:
            return None  # still in cooldown after a failure
        try:
            import psycopg2.pool
            url = _normalize_pg_url(DATABASE_URL)
            max_conn = int(os.getenv("MAX_DB_CONNECTIONS", "15"))
            _pg_pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=max_conn,
                dsn=url,
                connect_timeout=3,
            )
            log.info("PG pool created (minconn=1, maxconn=%d)", max_conn)
        except Exception as exc:
            log.error("PG pool failed: %s — SQLite fallback 60s", exc)
            _pg_cooldown_until = time.time() + 60
            return None
    return _pg_pool

def pg_cooldown_active() -> bool:
    """True if PG is currently under cooldown."""
    return time.time() < _pg_cooldown_until

def pool_status() -> dict:
    """Return the current pool health for /api/health."""
    return {
        "pg_pool_available": _pg_pool is not None,
        "pg_cooldown_active": pg_cooldown_active(),
        "pg_cooldown_remaining_s": max(0, round(_pg_cooldown_until - time.time())),
    }

def log_pool_health():
    """Log connection pool metrics for observability."""
    pool = _pg_pool
    if pool is None:
        return
    try:
        # psycopg2 ThreadedConnectionPool tracks _used and _pool internally
        used = len(getattr(pool, '_used', {}))
        idle = len(getattr(pool, '_pool', []))
        maxconn = getattr(pool, 'maxconn', 0)
        waiting = max(0, used - maxconn) if used > maxconn else 0
        log.info("[DB POOL] active=%s idle=%s waiting=%s maxconn=%s", used, idle, waiting, maxconn)
    except Exception:
        pass

# ─── Configurable batch size ───
DB_BATCH_SIZE = int(os.getenv("DB_BATCH_SIZE", "250"))


# ─── Type helpers ───

def _to_native(val):
    if hasattr(val, "item"):
        try:
            return val.item()
        except Exception:
            pass
    if isinstance(val, list):
        return [_to_native(v) for v in val]
    if isinstance(val, tuple):
        return tuple(_to_native(v) for v in val)
    if isinstance(val, dict):
        return {k: _to_native(v) for k, v in val.items()}
    return val

# ─── Result collectors ───

def _collect_result(cur, fetch: str):
    """Extract the requested result shape from a cursor (PG path)."""
    if fetch == "one":
        return cur.fetchone()
    if fetch == "all":
        return cur.fetchall()
    if fetch == "count":
        row = cur.fetchone()
        return list(row.values())[0] if row else 0
    if fetch == "rowcount":
        return cur.rowcount
    if fetch == "lastrowid":
        return cur.lastrowid
    return None

def _execute_sqlite(query: str, params, fetch: str):
    """Fresh-connection SQLite executor — no thread-local, WAL mode enabled."""
    DB_PATH.parent.mkdir(exist_ok=True)
    with sqlite3.connect(str(DB_PATH), timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        cur = conn.cursor()
        cur.execute(query, params or ())
        if fetch == "one":
            row = cur.fetchone()
            return dict(row) if row else None
        if fetch == "all":
            return [dict(r) for r in cur.fetchall()]
        if fetch == "count":
            row = cur.fetchone()
            return row[0] if row else 0
        if fetch == "rowcount":
            return cur.rowcount
        if fetch == "lastrowid":
            return cur.lastrowid
        conn.commit()
        return None

# ─── Unified executor ───

# P0.1D: Retry ladder configuration for CP-critical paths
_REQUIRE_PG_RETRIES = [0.5, 1.0, 2.0]  # Exponential backoff: 0.5s, 1s, 2s

def execute_db(query: str, params=None, fetch: str = None, require_pg: bool = False):
    """
    Unified query executor for PostgreSQL and SQLite.
    - PG path: uses ThreadedConnectionPool, always returns connection to pool.
    - SQLite path: fresh connect() per call (WAL mode, thread-safe reads).
    - Automatically translates '?' placeholders to '%s' for PG.
    - Falls through to SQLite on any PG failure (with 60s cooldown).
    - Pool exhaustion: if getconn() would block, falls through to SQLite
      immediately instead of hanging the Flask request thread.

    P0.1D: require_pg=True
    - Used by state machine operations (transitions, locks, resume state).
    - Instead of falling through to SQLite, retries with exponential backoff.
    - If all retries exhaust, raises RuntimeError (caller decides fatal action).
    - This guarantees CP (Consistency) for governance-critical state paths.
    """
    global _pg_cooldown_until, _pg_pool
    from metrics import counters
    counters.inc("db_queries")

    if params is not None:
        params = tuple(_to_native(v) for v in params)

    _last_pg_exc = None  # Track for require_pg error reporting

    if is_postgresql() and not pg_cooldown_active():
        pool = _get_pg_pool()
        if pool:
            conn = None
            try:
                from psycopg2.extras import RealDictCursor
                conn = pool.getconn()
                conn.autocommit = True
                query_pg = query.replace("?", "%s")
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("SET statement_timeout = '15s'")
                    _t0 = time.perf_counter()
                    cur.execute(query_pg, params or ())
                    result = _collect_result(cur, fetch)
                    _dur_ms = (time.perf_counter() - _t0) * 1000
                    # Phase F: Tiered slow-query logging + pool telemetry
                    if _dur_ms > 1000:
                        log.critical("[SLOW QUERY] query=%.150s | duration=%dms", query_pg, round(_dur_ms))
                        log_pool_health()
                    elif _dur_ms > 200:
                        log.warning("[DB SLOW QUERY] %dms | %s", round(_dur_ms), query_pg[:150])
                        log_pool_health()
                    return result
            except Exception as exc:
                _last_pg_exc = exc
                exc_str = str(exc).lower()
                is_connection_error = (
                    "PoolError" in type(exc).__name__ or 
                    "connection pool exhausted" in exc_str or
                    "connection" in exc_str or 
                    "terminating" in exc_str or 
                    "closed" in exc_str or
                    "operationalerror" in type(exc).__name__.lower()
                )
                
                if "PoolError" in type(exc).__name__ or "connection pool exhausted" in exc_str:
                    # Pool exhausted — retry once after 50ms (connection likely freed)
                    conn = None  # no connection to return
                    time.sleep(0.05)
                    try:
                        conn = pool.getconn()
                        conn.autocommit = True
                        query_pg = query.replace("?", "%s")
                        with conn.cursor(cursor_factory=RealDictCursor) as cur:
                            cur.execute("SET statement_timeout = '15s'")
                            cur.execute(query_pg, params or ())
                            return _collect_result(cur, fetch)
                    except Exception as retry_exc:
                        _last_pg_exc = retry_exc
                        log.warning("PG pool exhausted after retry, falling back to SQLite | Query: %.100s", query)
                        counters.inc("db_pool_exhausted")
                        if conn:
                            try:
                                pool.putconn(conn)
                            except Exception:
                                pass
                            conn = None
                        # fall through to SQLite (no cooldown — pool is fine, just busy)
                elif is_connection_error:
                    log.error("PG connection error: %s | Query: %.200s", exc, query)
                    counters.inc("db_failures")
                    _pg_cooldown_until = time.time() + 60
                    # Destroy the failed connection so the pool doesn't reuse it
                    if conn:
                        try:
                            pool.putconn(conn, close=True)
                        except Exception:
                            pass
                        conn = None
                    # fall through to SQLite
                else:
                    # Query syntax error, missing column, or data error
                    # Do NOT trigger global cooldown, just log and fallback for this specific query
                    log.error("PG query error (syntax/data): %s | Query: %.200s", exc, query)
                    counters.inc("db_failures")
                    if conn:
                        try:
                            pool.putconn(conn)
                        except Exception:
                            pass
                        conn = None
                    # fall through to SQLite without triggering cooldown
            finally:
                if conn:
                    try:
                        pool.putconn(conn)
                    except Exception:
                        pass

    # ── P0.1D: CP-critical path — retry ladder instead of SQLite fallback ──
    # Local-first: when no PostgreSQL is configured (single-user SQLite mode), the
    # CP/PG requirement does not apply — there is no multi-worker contention, so the
    # state machine runs on SQLite. The retry-and-raise behavior is preserved only
    # when PostgreSQL is actually the backend (production).
    if require_pg and is_postgresql():
        for attempt, delay in enumerate(_REQUIRE_PG_RETRIES, 1):
            log.warning(
                "[STATE MACHINE CP] PG unavailable, retry %d/%d in %.1fs | query=%.100s",
                attempt, len(_REQUIRE_PG_RETRIES), delay, query
            )
            time.sleep(delay)
            try:
                pool = _get_pg_pool()
                if pool:
                    conn = None
                    try:
                        from psycopg2.extras import RealDictCursor
                        conn = pool.getconn()
                        conn.autocommit = True
                        query_pg = query.replace("?", "%s")
                        with conn.cursor(cursor_factory=RealDictCursor) as cur:
                            cur.execute(query_pg, params or ())
                            result = _collect_result(cur, fetch)
                            log.info(
                                "[STATE MACHINE CP] PG recovered on retry %d/%d | query=%.100s",
                                attempt, len(_REQUIRE_PG_RETRIES), query
                            )
                            return result
                    finally:
                        if conn:
                            try:
                                pool.putconn(conn)
                            except Exception:
                                pass
            except Exception as retry_exc:
                _last_pg_exc = retry_exc
                log.warning(
                    "[STATE MACHINE CP] Retry %d/%d failed: %s",
                    attempt, len(_REQUIRE_PG_RETRIES), retry_exc
                )

        # All retries exhausted — raise to caller (scanner.py decides fatal action)
        counters.inc("state_machine_pg_failures")
        raise RuntimeError(
            f"State Machine Persistence Failure: PostgreSQL required but unavailable after "
            f"{len(_REQUIRE_PG_RETRIES)} retries. Last error: {_last_pg_exc}. "
            f"Query: {query[:150]}"
        )

    # Phase G: SQLite fallback telemetry
    log.warning("[SQLITE FALLBACK USED] query=%.100s", query)
    counters.inc("sqlite_fallback_used")
    return _execute_sqlite(query, params, fetch)

def execute_many(query: str, params_list: list):
    """
    Execute a query multiple times using execute_batch for PostgreSQL,
    or executemany for SQLite. Highly optimized for batch updates.
    """
    if not params_list:
        return
    
    if is_postgresql() and not pg_cooldown_active():
        pool = _get_pg_pool()
        if pool:
            conn = None
            try:
                from psycopg2.extras import execute_batch
                conn = pool.getconn()
                conn.autocommit = False # Use false so we can commit the batch atomically
                query_pg = query.replace("?", "%s")
                with conn.cursor() as cur:
                    _t0 = time.perf_counter()
                    execute_batch(cur, query_pg, params_list, page_size=500)
                    conn.commit()
                    _dur_ms = (time.perf_counter() - _t0) * 1000
                    if _dur_ms > 1000:
                        log.warning("[DB SLOW BATCH] %dms | %s (rows: %d)", round(_dur_ms), query_pg[:150], len(params_list))
                return
            except Exception as exc:
                log.error("PG execute_many error: %s | Query: %.200s", exc, query)
                if conn:
                    try:
                        conn.rollback()
                        pool.putconn(conn, close=True)
                    except Exception:
                        pass
                    conn = None
            finally:
                if conn:
                    try:
                        conn.autocommit = True
                        pool.putconn(conn)
                    except Exception:
                        pass
    
    # SQLite fallback
    try:
        DB_PATH.parent.mkdir(exist_ok=True)
        with sqlite3.connect(str(DB_PATH), timeout=10) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            cur = conn.cursor()
            cur.executemany(query, params_list)
            conn.commit()
    except Exception as exc:
        log.error("[SQLite execute_many] Failed: %s", exc)


# ─── Database Initialisation ───

_db_initialized = False
_db_init_lock = threading.Lock()

def init_db():
    global _db_initialized
    with _db_init_lock:
        if _db_initialized:
            return
        try:
            _run_init_db_logic()
            _db_initialized = True
            # P0: Non-fatal startup verification of JSON sanitizer
            verify_json_nan_prevention()
        except Exception:
            _db_initialized = False
            raise

def _run_init_db_logic():
    """Create tables if they don't exist.
    
    Uses an explicit temporary connection rather than the pool so that
    DDL runs atomically even when called before the pool is initialised.
    """
    if is_postgresql():
        try:
            import psycopg2
            from psycopg2.extras import RealDictCursor
            url = _normalize_pg_url(DATABASE_URL)
            conn = psycopg2.connect(url, cursor_factory=RealDictCursor, connect_timeout=5)
            conn.autocommit = True
            try:
                cur = conn.cursor()
                # ── Phase 0: Core tables (each in its own execute for safety) ──
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS scan_results (
                        symbol TEXT PRIMARY KEY,
                        data JSONB NOT NULL,
                        score INTEGER DEFAULT 0,
                        high_conviction INTEGER DEFAULT 0,
                        sector TEXT DEFAULT '',
                        scan_date TEXT NOT NULL,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        slim_data JSONB
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS scan_results_v2 (
                        scan_id TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        data JSONB NOT NULL,
                        score INTEGER DEFAULT 0,
                        high_conviction INTEGER DEFAULT 0,
                        sector TEXT DEFAULT '',
                        scan_date TEXT NOT NULL,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        slim_data JSONB,
                        PRIMARY KEY (scan_id, symbol)
                    );
                """)
                # Migration: Clone legacy data safely if v2 is empty (non-fatal on fresh DB)
                try:
                    cur.execute("""
                        INSERT INTO scan_results_v2 (scan_id, symbol, data, score, high_conviction, sector, scan_date, updated_at, slim_data)
                        SELECT 'scan_legacy_migration', symbol, data, score, high_conviction, sector, scan_date, updated_at, slim_data
                        FROM scan_results
                        ON CONFLICT DO NOTHING;
                    """)
                except Exception as mig_exc:
                    log.warning("Legacy data migration scan_results->v2 skipped (non-fatal): %s", mig_exc)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS scan_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE TABLE IF NOT EXISTS score_history (
                        symbol TEXT NOT NULL,
                        score INTEGER NOT NULL,
                        price REAL NOT NULL,
                        rsi REAL,
                        scan_date TEXT NOT NULL,
                        PRIMARY KEY (symbol, scan_date)
                    );

                    CREATE TABLE IF NOT EXISTS custom_stocks (
                        symbol TEXT PRIMARY KEY,
                        exchange TEXT DEFAULT 'NSE',
                        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        note TEXT DEFAULT ''
                    );

                    CREATE TABLE IF NOT EXISTS portfolios (
                        id SERIAL PRIMARY KEY,
                        name TEXT NOT NULL UNIQUE,
                        description TEXT DEFAULT '',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE TABLE IF NOT EXISTS positions (
                        id SERIAL PRIMARY KEY,
                        portfolio_id INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
                        symbol TEXT NOT NULL,
                        trade_type TEXT DEFAULT 'BUY',
                        quantity INTEGER NOT NULL DEFAULT 1,
                        buy_price REAL NOT NULL,
                        buy_date TEXT NOT NULL,
                        sell_price REAL,
                        sell_date TEXT,
                        stop_loss REAL,
                        target REAL,
                        status TEXT DEFAULT 'OPEN',
                        notes TEXT DEFAULT '',
                        scan_analysis TEXT DEFAULT 'Hold (Position Active)',
                        last_scan_at TIMESTAMP,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    -- Normalized scanner tables
                    CREATE TABLE IF NOT EXISTS stocks (
                        symbol TEXT PRIMARY KEY,
                        name TEXT,
                        sector TEXT,
                        industry TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE TABLE IF NOT EXISTS news_articles (
                        id SERIAL PRIMARY KEY,
                        symbol TEXT NOT NULL,
                        title TEXT NOT NULL,
                        url TEXT,
                        source TEXT,
                        age_hours REAL,
                        raw_score REAL,
                        scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE TABLE IF NOT EXISTS sentiment_scores (
                        symbol TEXT NOT NULL,
                        scan_date TEXT NOT NULL,
                        gdelt_sentiment REAL,
                        gdelt_spike REAL,
                        gdelt_freshness REAL,
                        final_sentiment_score REAL,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (symbol, scan_date)
                    );

                    CREATE TABLE IF NOT EXISTS technical_indicators (
                        symbol TEXT NOT NULL,
                        scan_date TEXT NOT NULL,
                        rsi REAL,
                        adx REAL,
                        macd_signal TEXT,
                        volume_ratio REAL,
                        atr_pct REAL,
                        stoch_k REAL,
                        stoch_d REAL,
                        pct_1w REAL,
                        pct_2w REAL,
                        pct_1m REAL,
                        bb_position REAL,
                        dist_from_high REAL,
                        rs_vs_nifty REAL,
                        vwap_position REAL,
                        is_breakout BOOLEAN,
                        vp_divergence BOOLEAN,
                        weekly_trend TEXT,
                        below_ema200 BOOLEAN,
                        high_52w REAL,
                        low_52w REAL,
                        pullback_pct REAL,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (symbol, scan_date)
                    );

                    CREATE TABLE IF NOT EXISTS fundamentals (
                        symbol TEXT PRIMARY KEY,
                        pe REAL,
                        pb REAL,
                        fwd_pe REAL,
                        roe REAL,
                        roa REAL,
                        revenue_growth REAL,
                        earnings_growth REAL,
                        debt_to_equity REAL,
                        promoter_pct REAL,
                        market_cap REAL,
                        free_cash_flow REAL,
                        total_revenue REAL,
                        capex REAL,
                        eps_fwd REAL,
                        eps_trail REAL,
                        fund_score INTEGER,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        detailed_json JSONB
                    );

                    CREATE TABLE IF NOT EXISTS macro_events (
                        id SERIAL PRIMARY KEY,
                        title TEXT NOT NULL,
                        country TEXT,
                        impact TEXT,
                        actual TEXT,
                        forecast TEXT,
                        surprise_dir TEXT,
                        score REAL,
                        event_date TEXT,
                        event_time TEXT,
                        scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE TABLE IF NOT EXISTS final_scores (
                        symbol TEXT NOT NULL,
                        scan_date TEXT NOT NULL,
                        news_sentiment_score REAL,
                        news_spike_score REAL,
                        technical_score REAL,
                        fundamental_score REAL,
                        macro_score REAL,
                        marketaux_score REAL,
                        final_score REAL,
                        grade TEXT,
                        high_conviction BOOLEAN,
                        bear_play BOOLEAN,
                        is_golden BOOLEAN DEFAULT FALSE,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (symbol, scan_date)
                    );
                """)
                try:
                    cur.execute("ALTER TABLE fundamentals ADD COLUMN IF NOT EXISTS detailed_json JSONB;")
                except Exception as e:
                    log.warning("ALTER TABLE fundamentals detailed_json failed: %s", e)

                # Phase 1: Essential performance indexes
                try:
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_scan_results_score ON scan_results(score DESC);")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_paper_trades_status ON paper_trades(status);")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_paper_trades_return ON paper_trades(return_pct);")
                    # Phase A: Composite index for paper trade status+date queries
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_paper_trades_status_date ON paper_trades(status, entry_date);")
                    log.info("Performance indexes verified")
                except Exception as e:
                    log.warning("Index creation failed (non-fatal): %s", e)

                # PG Symbol Aliases System
                try:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS symbol_aliases (
                            old_symbol TEXT PRIMARY KEY,
                            new_symbol TEXT NOT NULL,
                            reason TEXT,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        );
                    """)
                    cur.execute("""
                        INSERT INTO symbol_aliases (old_symbol, new_symbol, reason)
                        VALUES ('HDFC', 'HDFCBANK', 'HDFC-HDFCBANK merger (July 2023)'),
                               ('HDFC.NS', 'HDFCBANK.NS', 'HDFC-HDFCBANK merger (July 2023)')
                        ON CONFLICT (old_symbol) DO NOTHING;
                    """)
                except Exception as e:
                    log.warning("PG symbol_aliases migration failed: %s", e)

                # Phase 6: scan state tables
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS scan_runs (
                        scan_id TEXT PRIMARY KEY,
                        mode TEXT NOT NULL DEFAULT 'manual',
                        status TEXT NOT NULL DEFAULT 'running',
                        phase TEXT DEFAULT '',
                        start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        end_time TIMESTAMP,
                        processed_count INTEGER DEFAULT 0,
                        failed_count INTEGER DEFAULT 0,
                        deferred_count INTEGER DEFAULT 0,
                        candidate_count INTEGER DEFAULT 0,
                        duration_seconds REAL,
                        error_message TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        -- Governance columns
                        correlation_id TEXT,
                        request_id TEXT,
                        trigger_source TEXT DEFAULT 'manual',
                        user_id TEXT DEFAULT 'system',
                        scanner_version TEXT,
                        scoring_version TEXT,
                        recommendation_version TEXT,
                        config_snapshot JSONB,
                        parent_scan_id TEXT,
                        degraded_data BOOLEAN DEFAULT FALSE,
                        last_heartbeat TIMESTAMP,
                        -- Phase 5.8
                        universe_version TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_scan_runs_status ON scan_runs(status);

                    CREATE TABLE IF NOT EXISTS current_scan_state (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        scan_id TEXT,
                        mode TEXT DEFAULT '',
                        status TEXT DEFAULT 'idle',
                        phase TEXT DEFAULT '',
                        start_time TIMESTAMP,
                        processed_count INTEGER DEFAULT 0,
                        failed_count INTEGER DEFAULT 0,
                        candidate_count INTEGER DEFAULT 0,
                        cancel_requested INTEGER DEFAULT 0,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    INSERT INTO current_scan_state (id, status, cancel_requested, updated_at)
                    VALUES (1, 'idle', 0, CURRENT_TIMESTAMP)
                    ON CONFLICT (id) DO NOTHING;
                """)

                # Phase 7: symbol freshness tracking
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS symbol_state (
                        symbol TEXT PRIMARY KEY,
                        last_price_update TIMESTAMP,
                        last_technical_update TIMESTAMP,
                        last_news_update TIMESTAMP,
                        last_sentiment_update TIMESTAMP,
                        last_financial_update TIMESTAMP,
                        last_deep_scan TIMESTAMP,
                        price_change_pct REAL DEFAULT 0.0,
                        prev_score INTEGER DEFAULT 0,
                        needs_deep_scan INTEGER DEFAULT 0,
                        deep_scan_reason TEXT DEFAULT '',
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # ── Release 3: Outcome Intelligence Layer ──────────────
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS paper_trades (
                        id SERIAL PRIMARY KEY,
                        symbol TEXT NOT NULL,
                        sector TEXT DEFAULT '',
                        entry_date TEXT NOT NULL,
                        entry_price REAL NOT NULL,
                        target_price REAL,
                        stop_loss REAL,
                        virtual_capital REAL DEFAULT 25000,
                        quantity INTEGER DEFAULT 0,
                        source TEXT DEFAULT 'QUANT',
                        -- Component scores at entry
                        score_at_entry INTEGER DEFAULT 0,
                        grade_at_entry TEXT DEFAULT '',
                        technical_score REAL DEFAULT 0,
                        fundamental_score REAL DEFAULT 0,
                        earnings_momentum_score REAL DEFAULT 0,
                        earnings_grade TEXT DEFAULT '',
                        smart_money_score REAL DEFAULT 0,
                        sector_rotation_score REAL DEFAULT 0,
                        catalyst_score REAL DEFAULT 0,
                        news_sentiment_score REAL DEFAULT 0,
                        risk_score REAL DEFAULT 0,
                        risk_reward REAL DEFAULT 0,
                        -- Regime snapshot
                        model_version TEXT DEFAULT '',
                        market_regime TEXT DEFAULT '',
                        nifty_entry REAL,
                        high_conviction INTEGER DEFAULT 0,
                        is_golden INTEGER DEFAULT 0,
                        signals_json TEXT DEFAULT '[]',
                        earnings_signals_json TEXT DEFAULT '[]',
                        -- Exit
                        exit_date TEXT,
                        exit_price REAL,
                        exit_reason TEXT,
                        nifty_exit REAL,
                        days_held INTEGER DEFAULT 0,
                        return_pct REAL,
                        alpha_pct REAL,
                        max_drawdown_pct REAL DEFAULT 0,
                        max_runup_pct REAL DEFAULT 0,
                        -- Status
                        status TEXT DEFAULT 'OPEN',
                        position_size_pct REAL DEFAULT 20.0,
                        weight_version TEXT DEFAULT '',
                        confidence_score REAL DEFAULT 0,
                        entry_rank INTEGER DEFAULT 0,
                        -- Market breadth at entry
                        breadth_advances INTEGER DEFAULT 0,
                        breadth_declines INTEGER DEFAULT 0,
                        breadth_ratio REAL DEFAULT 0,
                        -- R4 prep (NULL until calibrated)
                        probability_bucket TEXT,
                        expected_return_bucket TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        -- Release 4: Execution Engine columns
                        entry_time TIMESTAMP,
                        exit_time TIMESTAMP,
                        order_id INTEGER,
                        fill_price REAL,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        execution_latency_ms INTEGER
                    );
                    CREATE INDEX IF NOT EXISTS idx_paper_trades_status ON paper_trades(status);
                    CREATE INDEX IF NOT EXISTS idx_paper_trades_symbol ON paper_trades(symbol);
                    CREATE INDEX IF NOT EXISTS idx_paper_trades_entry ON paper_trades(entry_date);

                    CREATE TABLE IF NOT EXISTS recommendation_snapshots (
                        id SERIAL PRIMARY KEY,
                        snapshot_date TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        rank INTEGER NOT NULL,
                        score INTEGER DEFAULT 0,
                        grade TEXT DEFAULT '',
                        technical_score REAL DEFAULT 0,
                        fundamental_score REAL DEFAULT 0,
                        earnings_momentum_score REAL DEFAULT 0,
                        earnings_grade TEXT DEFAULT '',
                        smart_money_score REAL DEFAULT 0,
                        risk_score REAL DEFAULT 0,
                        price REAL DEFAULT 0,
                        model_version TEXT DEFAULT '',
                        market_regime TEXT DEFAULT '',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE (snapshot_date, symbol)
                    );
                    CREATE INDEX IF NOT EXISTS idx_rec_snap_date ON recommendation_snapshots(snapshot_date);

                    CREATE TABLE IF NOT EXISTS paper_portfolio_daily (
                        date TEXT PRIMARY KEY,
                        portfolio_value REAL DEFAULT 0,
                        invested_value REAL DEFAULT 0,
                        open_positions INTEGER DEFAULT 0,
                        closed_today INTEGER DEFAULT 0,
                        total_closed INTEGER DEFAULT 0,
                        win_count INTEGER DEFAULT 0,
                        loss_count INTEGER DEFAULT 0,
                        total_return_pct REAL DEFAULT 0,
                        nifty_level REAL DEFAULT 0,
                        model_version TEXT DEFAULT '',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # P5: Performance indexes for dashboard queries
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_scan_results_score ON scan_results(score DESC);
                    CREATE INDEX IF NOT EXISTS idx_scan_results_hc ON scan_results(high_conviction) WHERE high_conviction = 1;
                    CREATE INDEX IF NOT EXISTS idx_paper_trades_model ON paper_trades(model_version);
                """)

                # Sprint 1 Phase 1: Functional indexes for golden/breakout JSONB queries
                try:
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_scan_results_golden ON scan_results(((data->>'is_golden')::text)) WHERE (data->>'is_golden')::text IN ('true','1');")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_scan_results_breakout ON scan_results(((data->>'is_breakout')::text)) WHERE (data->>'is_breakout')::text IN ('true','1');")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_score_history_date ON score_history(scan_date DESC);")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_news_articles_date ON news_articles(scanned_at DESC);")
                    log.info("Sprint 1: Functional indexes for golden/breakout created")
                except Exception as e:
                    log.warning("Sprint 1: Functional index creation failed (non-fatal): %s", e)

                # Phase 1C: slim_data column (safe ALTER — no-op if exists)
                try:
                    cur.execute("ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS slim_data JSONB;")
                except Exception:
                    pass  # column already exists

                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_news_articles_symbol ON news_articles(symbol);
                """)

                # Phase 0: Trust & Observability — score_audit + scan_audit
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS score_audit (
                        id BIGSERIAL PRIMARY KEY,
                        symbol TEXT NOT NULL,
                        scan_id TEXT NOT NULL,
                        scan_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        technical_score NUMERIC,
                        earnings_momentum_score NUMERIC,
                        fundamental_score NUMERIC,
                        smart_money_score NUMERIC,
                        sector_rotation_score NUMERIC,
                        news_sentiment_score NUMERIC,
                        news_spike_score NUMERIC,
                        macro_score NUMERIC,
                        catalyst_score NUMERIC,
                        final_score NUMERIC NOT NULL,
                        data_source TEXT,
                        source_reason TEXT,
                        provider_latency_ms INTEGER,
                        data_staleness_hours REAL,
                        scan_version TEXT,
                        score_breakdown JSONB,
                        UNIQUE (symbol, scan_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_score_audit_symbol ON score_audit(symbol);
                    CREATE INDEX IF NOT EXISTS idx_score_audit_time ON score_audit(scan_time DESC);
                """)

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS scan_audit (
                        id BIGSERIAL PRIMARY KEY,
                        scan_id TEXT,
                        start_time TIMESTAMP,
                        end_time TIMESTAMP,
                        duration_ms BIGINT,
                        stocks_scanned INTEGER,
                        stocks_succeeded INTEGER,
                        stocks_failed INTEGER,
                        data_source TEXT,
                        scan_version TEXT,
                        scan_mode TEXT DEFAULT 'manual'
                    );
                    CREATE INDEX IF NOT EXISTS idx_scan_audit_time ON scan_audit(start_time DESC);
                """)

                # ── Phase 0A+1: Governance schema additions (backward compatible) ──
                # Section 5: Context columns on scan_runs
                for col_def in [
                    "ALTER TABLE scan_runs ADD COLUMN IF NOT EXISTS correlation_id TEXT;",
                    "ALTER TABLE scan_runs ADD COLUMN IF NOT EXISTS request_id TEXT;",
                    "ALTER TABLE scan_runs ADD COLUMN IF NOT EXISTS trigger_source TEXT DEFAULT 'manual';",
                    "ALTER TABLE scan_runs ADD COLUMN IF NOT EXISTS user_id TEXT DEFAULT 'system';",
                    "ALTER TABLE scan_runs ADD COLUMN IF NOT EXISTS scanner_version TEXT;",
                    "ALTER TABLE scan_runs ADD COLUMN IF NOT EXISTS scoring_version TEXT;",
                    "ALTER TABLE scan_runs ADD COLUMN IF NOT EXISTS recommendation_version TEXT;",
                    "ALTER TABLE scan_runs ADD COLUMN IF NOT EXISTS config_snapshot JSONB;",
                    "ALTER TABLE scan_runs ADD COLUMN IF NOT EXISTS parent_scan_id TEXT;",
                    "ALTER TABLE scan_runs ADD COLUMN IF NOT EXISTS degraded_data BOOLEAN DEFAULT FALSE;",
                    "ALTER TABLE scan_runs ADD COLUMN IF NOT EXISTS last_heartbeat TIMESTAMP;",
                ]:
                    try:
                        cur.execute(col_def)
                    except Exception as exc:
                        log.error("[MIGRATION FAILED] PostgreSQL schema error: %s", exc, exc_info=True)
                        raise

                # Section 36: Score breakdown for explainability
                try:
                    cur.execute("ALTER TABLE score_audit ADD COLUMN IF NOT EXISTS score_breakdown JSONB;")
                except Exception as exc:
                    log.error("[MIGRATION FAILED] PostgreSQL score_audit error: %s", exc, exc_info=True)
                    raise

                # Section 4, 30: State transition audit log (append-only)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS scan_state_transitions (
                        id BIGSERIAL PRIMARY KEY,
                        scan_id TEXT NOT NULL,
                        old_state TEXT NOT NULL,
                        new_state TEXT NOT NULL,
                        reason TEXT,
                        actor TEXT DEFAULT 'system',
                        correlation_id TEXT,
                        hash_chain TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE INDEX IF NOT EXISTS idx_sst_scan_id ON scan_state_transitions(scan_id);
                    CREATE INDEX IF NOT EXISTS idx_sst_created ON scan_state_transitions(created_at DESC);

                    CREATE TABLE IF NOT EXISTS scan_event_audit (
                        id BIGSERIAL PRIMARY KEY,
                        scan_id TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        details TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE INDEX IF NOT EXISTS idx_sea_scan_id ON scan_event_audit(scan_id);
                    CREATE INDEX IF NOT EXISTS idx_sea_created ON scan_event_audit(created_at DESC);
                """)
                
                # Production Schema additions
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS universe_catalog (
                        symbol TEXT PRIMARY KEY,
                        company_name TEXT,
                        market_cap REAL,
                        market_cap_bucket TEXT,
                        sector TEXT,
                        industry TEXT,
                        is_active BOOLEAN DEFAULT TRUE,
                        last_scanned_at TIMESTAMP,
                        -- Phase 5.5: Universe Engine columns
                        avg_volume_20d REAL DEFAULT 0,
                        avg_turnover_20d REAL DEFAULT 0,
                        instrument_type TEXT DEFAULT 'EQ',
                        exchange TEXT DEFAULT 'NSE',
                        price REAL DEFAULT 0,
                        last_synced_at TIMESTAMP,
                        first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        sync_fail_count INTEGER DEFAULT 0,
                        -- Phase 5.6B/C: Liquidity Enrichment
                        liquidity_synced_at TIMESTAMP,
                        liquidity_sync_fail_count INTEGER DEFAULT 0,
                        liquidity_excluded BOOLEAN DEFAULT FALSE,
                        liquidity_excluded_reason TEXT,
                        liquidity_excluded_at TIMESTAMP,
                        -- Dhan Fundamental Data
                        isin TEXT,
                        pe REAL,
                        pb REAL,
                        roe REAL,
                        roce REAL,
                        eps REAL,
                        div_yield REAL,
                        industry_pe REAL,
                        revenue REAL,
                        free_cash_flow REAL,
                        net_profit_margin REAL,
                        high_52w REAL,
                        low_52w REAL,
                        pct_change_1m REAL,
                        pct_change_1y REAL,
                        rsi_14 REAL,
                        sma_50 REAL,
                        sma_200 REAL,
                        dhan_sid TEXT,
                        fundamentals_updated_at TIMESTAMP
                    );

                    CREATE TABLE IF NOT EXISTS universe_chunk_runs (
                        id BIGSERIAL PRIMARY KEY,
                        scan_id TEXT NOT NULL,
                        chunk_name TEXT NOT NULL,
                        status TEXT NOT NULL,
                        symbol_count INTEGER,
                        symbols_processed INTEGER DEFAULT 0,
                        error_message TEXT,
                        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        completed_at TIMESTAMP,
                        chunk_last_activity TIMESTAMP,
                        last_symbol TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_ucr_scan_id ON universe_chunk_runs(scan_id);

                    CREATE TABLE IF NOT EXISTS research_snapshots_v2 (
                        id BIGSERIAL PRIMARY KEY,
                        version INTEGER NOT NULL DEFAULT 1,
                        symbol TEXT NOT NULL,
                        status TEXT DEFAULT 'ACTIVE',
                        outcome_status TEXT DEFAULT 'PENDING',
                        recommendation TEXT,
                        entry_low REAL,
                        entry_high REAL,
                        stop_loss REAL,
                        target_1 REAL,
                        target_2 REAL,
                        target_3 REAL,
                        risk_reward REAL,
                        confidence REAL,
                        confidence_breakdown JSONB,
                        research_thesis TEXT,
                        cmp_at_generation REAL,
                        score_at_generation REAL,
                        raw_score_at_generation REAL,
                        scan_id TEXT,
                        correlation_id TEXT,
                        scanner_version TEXT,
                        scoring_version TEXT,
                        recommendation_version TEXT,
                        config_snapshot JSONB,
                        snapshot_hash TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(symbol, version)
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_research_snapshot_scan_symbol ON research_snapshots_v2(scan_id, symbol);

                    CREATE TABLE IF NOT EXISTS research_advisories (
                        id BIGSERIAL PRIMARY KEY,
                        symbol TEXT NOT NULL,
                        advisory_type TEXT NOT NULL,
                        advisory_text TEXT NOT NULL,
                        priority TEXT DEFAULT 'MEDIUM',
                        issued_by TEXT DEFAULT 'system',
                        is_active BOOLEAN DEFAULT TRUE,
                        valid_until TIMESTAMP,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE INDEX IF NOT EXISTS idx_adv_symbol ON research_advisories(symbol);
                """)
                cur.execute("ALTER TABLE universe_chunk_runs ADD COLUMN IF NOT EXISTS symbol_count INTEGER;")
                cur.execute("ALTER TABLE universe_chunk_runs ADD COLUMN IF NOT EXISTS symbols_processed INTEGER DEFAULT 0;")
                cur.execute("ALTER TABLE universe_chunk_runs ADD COLUMN IF NOT EXISTS error_message TEXT;")
                cur.execute("ALTER TABLE universe_chunk_runs ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP;")
                cur.execute("ALTER TABLE universe_chunk_runs ADD COLUMN IF NOT EXISTS chunk_last_activity TIMESTAMP;")
                cur.execute("ALTER TABLE universe_chunk_runs ADD COLUMN IF NOT EXISTS last_symbol TEXT;")
                log.info("Governance schema additions verified (scan_state_transitions, context columns, chunks, snapshots, advisories)")

                # ── Phase 5.5: Universe Engine Schema ──────────────────────
                try:
                    # Extend universe_catalog (Stock Master Registry)
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS avg_volume_20d REAL DEFAULT 0;")
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS avg_turnover_20d REAL DEFAULT 0;")
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS instrument_type TEXT DEFAULT 'EQ';")
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS exchange TEXT DEFAULT 'NSE';")
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS price REAL DEFAULT 0;")
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS last_synced_at TIMESTAMP;")
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;")
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS sync_fail_count INTEGER DEFAULT 0;")

                    # Eligible Universe (versioned, filtered subset)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS eligible_universe (
                            symbol TEXT PRIMARY KEY,
                            market_cap_cr REAL,
                            avg_volume_20d REAL,
                            avg_turnover_20d REAL,
                            price REAL,
                            eligibility_reason TEXT DEFAULT 'FILTER_PASS',
                            universe_version TEXT NOT NULL,
                            generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        );
                        CREATE INDEX IF NOT EXISTS idx_eu_version ON eligible_universe(universe_version);
                    """)

                    # Scan Resume State (lightweight — batch_index only)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS scan_resume_state (
                            scan_id TEXT PRIMARY KEY,
                            universe_version TEXT NOT NULL,
                            total_batches INTEGER NOT NULL,
                            current_batch_index INTEGER DEFAULT 0,
                            status TEXT DEFAULT 'running',
                            last_heartbeat TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        );
                    """)

                    # Scan Lock (heartbeat-based ownership)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS scan_lock (
                            id INTEGER PRIMARY KEY DEFAULT 1,
                            scan_id TEXT,
                            owner_id TEXT,
                            heartbeat TIMESTAMP,
                            expires_at TIMESTAMP,
                            acquired_at TIMESTAMP
                        );
                        INSERT INTO scan_lock (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
                    """)

                    # Scan Batches (queue-based batch tracking)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS scan_batches (
                            id BIGSERIAL PRIMARY KEY,
                            scan_id TEXT NOT NULL,
                            batch_index INTEGER NOT NULL,
                            status TEXT DEFAULT 'PENDING',
                            worker_id TEXT,
                            symbol_count INTEGER DEFAULT 0,
                            symbols_processed INTEGER DEFAULT 0,
                            retry_count INTEGER DEFAULT 0,
                            started_at TIMESTAMP,
                            completed_at TIMESTAMP,
                            UNIQUE(scan_id, batch_index)
                        );
                        CREATE INDEX IF NOT EXISTS idx_sb_scan ON scan_batches(scan_id, status);
                    """)

                    # Universe Rebuild History (audit trail for universe drift)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS universe_rebuild_history (
                            id BIGSERIAL PRIMARY KEY,
                            universe_version TEXT NOT NULL,
                            generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            input_count INTEGER DEFAULT 0,
                            eligible_count INTEGER DEFAULT 0,
                            rejected_mcap INTEGER DEFAULT 0,
                            rejected_turnover INTEGER DEFAULT 0,
                            rejected_volume INTEGER DEFAULT 0,
                            rejected_price INTEGER DEFAULT 0,
                            rejected_etf INTEGER DEFAULT 0,
                            rejected_sme INTEGER DEFAULT 0,
                            rejected_suspended INTEGER DEFAULT 0,
                            rejected_ipo_age INTEGER DEFAULT 0,
                            force_included INTEGER DEFAULT 0,
                            fallback_used BOOLEAN DEFAULT FALSE
                        );
                    """)

                    log.info("Phase 5.5: Universe Engine schema verified")
                except Exception as exc:
                    log.warning("Phase 5.5: Schema migration failed (non-fatal): %s", exc)

                # ── Phase 5.6B/C: Liquidity Enrichment & Universe Governance ──
                try:
                    # Extend universe_catalog with liquidity tracking columns
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS liquidity_synced_at TIMESTAMP;")
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS liquidity_sync_fail_count INTEGER DEFAULT 0;")
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS liquidity_excluded BOOLEAN DEFAULT FALSE;")
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS liquidity_excluded_reason TEXT;")
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS liquidity_excluded_at TIMESTAMP;")

                    # ── Dhan Fundamental Data (replaces yfinance) ──
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS isin TEXT;")
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS pe REAL;")
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS pb REAL;")
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS roe REAL;")
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS roce REAL;")
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS eps REAL;")
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS div_yield REAL;")
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS industry_pe REAL;")
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS revenue REAL;")
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS free_cash_flow REAL;")
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS net_profit_margin REAL;")
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS high_52w REAL;")
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS low_52w REAL;")
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS pct_change_1m REAL;")
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS pct_change_1y REAL;")
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS rsi_14 REAL;")
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS sma_50 REAL;")
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS sma_200 REAL;")
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS dhan_sid TEXT;")
                    cur.execute("ALTER TABLE universe_catalog ADD COLUMN IF NOT EXISTS fundamentals_updated_at TIMESTAMP;")

                    # Universe Snapshot (append-only audit trail per build)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS universe_snapshot (
                            id BIGSERIAL PRIMARY KEY,
                            universe_version TEXT NOT NULL,
                            symbol TEXT NOT NULL,
                            market_cap_cr REAL,
                            avg_volume_20d REAL,
                            avg_turnover_20d REAL,
                            price REAL,
                            eligibility_reason TEXT DEFAULT 'FILTER_PASS',
                            generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        );
                        CREATE INDEX IF NOT EXISTS idx_us_version ON universe_snapshot(universe_version);
                    """)

                    # Candidate Universe (version-locked snapshot for enrichment)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS candidate_universe (
                            id BIGSERIAL PRIMARY KEY,
                            universe_version TEXT NOT NULL,
                            symbol TEXT NOT NULL,
                            market_cap_cr REAL,
                            frozen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            UNIQUE(universe_version, symbol)
                        );
                        CREATE INDEX IF NOT EXISTS idx_cu_version ON candidate_universe(universe_version);
                    """)

                    # Universe Build Validation Snapshot (forensic evidence per activation)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS universe_build_validation_snapshot (
                            id BIGSERIAL PRIMARY KEY,
                            universe_version TEXT NOT NULL,
                            candidate_count INTEGER DEFAULT 0,
                            eligible_count INTEGER DEFAULT 0,
                            marketcap_coverage_pct REAL DEFAULT 0,
                            liquidity_coverage_pct REAL DEFAULT 0,
                            build_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        );
                    """)

                    log.info("Phase 5.6B/C: Liquidity & Governance schema verified (PG)")
                except Exception as exc:
                    log.warning("Phase 5.6B/C: Schema migration failed (non-fatal): %s", exc)

                # ── Release 4: Execution Engine Schema ──────────────────────
                try:
                    cur.execute("""

                        CREATE TABLE IF NOT EXISTS historical_cache (
                            symbol_token TEXT NOT NULL,
                            exchange TEXT NOT NULL,
                            timeframe TEXT NOT NULL,
                            last_refresh TIMESTAMP NOT NULL,
                            expires_at TIMESTAMP NOT NULL,
                            payload_json JSONB NOT NULL,
                            PRIMARY KEY(symbol_token, exchange, timeframe)
                        );

                        CREATE TABLE IF NOT EXISTS provider_stats (
                            provider_name TEXT PRIMARY KEY,
                            historical_calls INTEGER DEFAULT 0
                        );

            CREATE TABLE IF NOT EXISTS paper_orders (
                            id SERIAL PRIMARY KEY,
                            symbol TEXT NOT NULL,
                            order_type TEXT DEFAULT 'LIMIT',
                            side TEXT DEFAULT 'BUY',
                            status TEXT DEFAULT 'PENDING',
                            entry_low REAL,
                            entry_high REAL,
                            target_price REAL,
                            stop_loss REAL,
                            virtual_capital REAL DEFAULT 25000,
                            score_at_signal INTEGER DEFAULT 0,
                            grade_at_signal TEXT DEFAULT '',
                            scan_id TEXT,
                            signal_source TEXT DEFAULT 'scanner',
                            signal_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            order_created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            triggered_at TIMESTAMP,
                            filled_at TIMESTAMP,
                            cancelled_at TIMESTAMP,
                            expires_at TIMESTAMP,
                            research_snapshot_id INTEGER,
                            correlation_id TEXT,
                            recommendation_id TEXT
                        );
                        CREATE INDEX IF NOT EXISTS idx_paper_orders_status ON paper_orders(status);
                        CREATE INDEX IF NOT EXISTS idx_paper_orders_symbol ON paper_orders(symbol);
                    """)

                    # Extend paper_trades with full timestamp precision
                    for col_def in [
                        "ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS entry_time TIMESTAMP;",
                        "ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS exit_time TIMESTAMP;",
                        "ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS order_id INTEGER;",
                        "ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS fill_price REAL;",
                        "ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;",
                        "ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS execution_latency_ms INTEGER;",
                        "ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS scan_id TEXT;",
                        "ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS recommendation_id TEXT;",
                        "ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'QUANT';",
                        "ALTER TABLE paper_orders ADD COLUMN IF NOT EXISTS recommendation_id TEXT;",
                    ]:
                        try:
                            cur.execute(col_def)
                        except Exception:
                            pass

                    log.info("Release 4: Execution Engine schema verified (paper_orders + paper_trades timestamps)")
                except Exception as exc:
                    log.warning("Release 4: Execution Engine schema migration failed (non-fatal): %s", exc)

                # ── scoring_v1 comparison: model_version tagging migration (PG) ──
                try:
                    _migrate_model_version_v1(cur, is_pg=True)
                except Exception as exc:
                    log.error("[MIGRATION FAILED] model_version v1 (PG): %s", exc, exc_info=True)
                    raise

                # ── Phase 5.7: Immutable First Analysis Lock ─────────────
                try:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS recommendation_history (
                            id SERIAL PRIMARY KEY,
                            symbol TEXT NOT NULL,
                            scan_id TEXT,
                            version INTEGER DEFAULT 1,
                            entry_low REAL,
                            entry_high REAL,
                            stop_loss REAL,
                            target_price REAL,
                            target1 REAL,
                            target2 REAL,
                            target3 REAL,
                            risk_reward REAL,
                            score INTEGER DEFAULT 0,
                            grade TEXT DEFAULT '',
                            confidence_score REAL DEFAULT 0,
                            risk_score REAL DEFAULT 0,
                            technical_score REAL DEFAULT 0,
                            fundamental_score REAL DEFAULT 0,
                            price_at_analysis REAL,
                            analysis_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            is_first_analysis BOOLEAN DEFAULT FALSE,
                            change_reason TEXT,
                            data_snapshot JSONB,
                            UNIQUE(symbol, version)
                        );
                        CREATE INDEX IF NOT EXISTS idx_rh_symbol ON recommendation_history(symbol);
                        CREATE INDEX IF NOT EXISTS idx_rh_first ON recommendation_history(symbol) WHERE is_first_analysis = TRUE;
                    """)
                    log.info("Phase 5.7: recommendation_history table verified")
                except Exception as exc:
                    log.warning("Phase 5.7: recommendation_history migration failed (non-fatal): %s", exc)

                # ── Phase 5.7b: Recommendation Locks (thesis state tracking) ──
                try:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS recommendation_locks (
                            symbol TEXT PRIMARY KEY,
                            locked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            recommended_price REAL,
                            entry_low REAL,
                            entry_high REAL,
                            stop_loss REAL,
                            target1 REAL,
                            target2 REAL,
                            target3 REAL,
                            risk_reward REAL,
                            thesis_status TEXT DEFAULT 'ACTIVE',
                            score_at_lock INTEGER DEFAULT 0,
                            updates JSONB DEFAULT '[]'::jsonb
                        );
                        CREATE INDEX IF NOT EXISTS idx_rl_status ON recommendation_locks(thesis_status);
                    """)
                    log.info("Phase 5.7b: recommendation_locks table verified")
                except Exception as exc:
                    log.warning("Phase 5.7b: recommendation_locks migration failed (non-fatal): %s", exc)

                # ── Phase 5.8: Add universe_version to scan_runs ─────────
                try:
                    cur.execute("ALTER TABLE scan_runs ADD COLUMN IF NOT EXISTS universe_version TEXT;")
                    log.info("Phase 5.8: scan_runs.universe_version column verified")
                except Exception as exc:
                    log.warning("Phase 5.8: scan_runs.universe_version migration failed (non-fatal): %s", exc)

                log.info("PostgreSQL tables checked/created.")
            finally:
                conn.close()
        except Exception as exc:
            log.error("init_db PG failed: %s — falling back to SQLite init", exc)

    # Always init SQLite tables as safety net for pool-exhaustion fallback
    _init_sqlite()

    auto_clear_daily_cache()

    # ── Phase B: One-time slim_data backfill on startup ──────────────────
    MAX_BACKFILL_ROWS_PER_STARTUP = 1000
    try:
        row = execute_db("SELECT COUNT(*) as count FROM scan_results WHERE slim_data IS NULL", fetch="one")
        rows_needing_backfill = row["count"] if row else 0
        if rows_needing_backfill > MAX_BACKFILL_ROWS_PER_STARTUP:
            log.warning("[SLIM BACKFILL] Skipping startup backfill: %d rows exceeds safety limit of %d. Requires manual maintenance run.",
                        rows_needing_backfill, MAX_BACKFILL_ROWS_PER_STARTUP)
        elif rows_needing_backfill > 0:
            backfilled = backfill_missing_slim_data()
            log.info("[SLIM BACKFILL] Startup maintenance: backfilled %d rows", backfilled)
        else:
            log.info("[SLIM BACKFILL] No rows need backfill")
    except Exception as exc:
        log.warning("[SLIM BACKFILL] Startup backfill failed (non-fatal): %s", exc)

    log_slim_coverage()

    # ── Phase 0.5: Data Integrity Audit on startup ───────────────────────
    try:
        run_data_integrity_audit()
    except Exception as exc:
        log.warning("[DATA INTEGRITY] Startup audit failed (non-fatal): %s", exc)

    # ── Phase A: Index Verification ──────────────────────────────────────
    verify_indexes_startup()

    # ── HDFC Legacy Cleanup (transient cache and catalog inactive status only)
    migrate_legacy_hdfc_ticker()


def migrate_legacy_hdfc_ticker():
    """Clean up legacy HDFC scan results and catalog entries (transient cache only)."""
    try:
        # Safe to delete from scan_results since it is only a temporary scan cache
        execute_db("DELETE FROM scan_results WHERE symbol IN ('HDFC', 'HDFC.NS')")
        
        # Mark inactive in universe catalog
        execute_db("UPDATE universe_catalog SET is_active = FALSE WHERE symbol IN ('HDFC', 'HDFC.NS')")
        log.info("[HDFC_MIGRATION] Cleaned up scan_results and universe_catalog for legacy symbol HDFC")
    except Exception as exc:
        log.warning("[HDFC_MIGRATION] Migration warning: %s", exc)


def verify_indexes_startup():
    """Verify that performance-critical indexes exist. Logs WARNING if missing.

    Phase A: Performance issue != availability issue.
    Missing index degrades performance but NEVER crashes startup.
    """
    required = "idx_paper_trades_status_date"
    try:
        if DATABASE_URL:
            row = execute_db(
                "SELECT 1 FROM pg_indexes WHERE indexname = %s",
                (required,), fetch="one"
            )
        else:
            row = execute_db(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
                (required,), fetch="one"
            )
        if row:
            log.info("[INDEX VERIFIED] %s exists", required)
        else:
            log.warning("[INDEX MISSING] %s not found — paper trade queries may be slow", required)
    except Exception as exc:
        log.warning("[INDEX CHECK] Could not verify %s (non-fatal): %s", required, exc)


def _init_sqlite():
    """Create SQLite tables using a single fresh connection."""
    DB_PATH.parent.mkdir(exist_ok=True)
    with sqlite3.connect(str(DB_PATH), timeout=10) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS scan_results (
                symbol TEXT PRIMARY KEY,
                data JSON NOT NULL,
                score INTEGER DEFAULT 0,
                high_conviction INTEGER DEFAULT 0,
                sector TEXT DEFAULT '',
                scan_date TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scan_results_v2 (
                scan_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                data JSON NOT NULL,
                score INTEGER DEFAULT 0,
                high_conviction INTEGER DEFAULT 0,
                sector TEXT DEFAULT '',
                scan_date TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                slim_data JSON,
                PRIMARY KEY (scan_id, symbol)
            );

            -- Migration: Clone legacy data safely for SQLite
            INSERT OR IGNORE INTO scan_results_v2 (scan_id, symbol, data, score, high_conviction, sector, scan_date, updated_at)
            SELECT 'scan_legacy_migration', symbol, data, score, high_conviction, sector, scan_date, updated_at
            FROM scan_results;

            CREATE TABLE IF NOT EXISTS scan_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS score_history (
                symbol TEXT NOT NULL,
                score INTEGER NOT NULL,
                price REAL NOT NULL,
                rsi REAL,
                scan_date TEXT NOT NULL,
                PRIMARY KEY (symbol, scan_date)
            );

            CREATE TABLE IF NOT EXISTS custom_stocks (
                symbol TEXT PRIMARY KEY,
                exchange TEXT DEFAULT 'NSE',
                added_at TEXT NOT NULL,
                note TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS portfolios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                portfolio_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                trade_type TEXT DEFAULT 'BUY',
                quantity INTEGER NOT NULL DEFAULT 1,
                buy_price REAL NOT NULL,
                buy_date TEXT NOT NULL,
                sell_price REAL,
                sell_date TEXT,
                stop_loss REAL,
                target REAL,
                status TEXT DEFAULT 'OPEN',
                notes TEXT DEFAULT '',
                scan_analysis TEXT DEFAULT 'Hold (Position Active)',
                last_scan_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (portfolio_id) REFERENCES portfolios(id) ON DELETE CASCADE
            );

            -- Normalized scanner tables
            CREATE TABLE IF NOT EXISTS stocks (
                symbol TEXT PRIMARY KEY,
                name TEXT,
                sector TEXT,
                industry TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS news_articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT,
                source TEXT,
                age_hours REAL,
                raw_score REAL,
                scanned_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sentiment_scores (
                symbol TEXT NOT NULL,
                scan_date TEXT NOT NULL,
                gdelt_sentiment REAL,
                gdelt_spike REAL,
                gdelt_freshness REAL,
                final_sentiment_score REAL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (symbol, scan_date)
            );

            CREATE TABLE IF NOT EXISTS technical_indicators (
                symbol TEXT NOT NULL,
                scan_date TEXT NOT NULL,
                rsi REAL,
                adx REAL,
                macd_signal TEXT,
                volume_ratio REAL,
                atr_pct REAL,
                stoch_k REAL,
                stoch_d REAL,
                pct_1w REAL,
                pct_2w REAL,
                pct_1m REAL,
                bb_position REAL,
                dist_from_high REAL,
                rs_vs_nifty REAL,
                vwap_position REAL,
                is_breakout INTEGER,
                vp_divergence INTEGER,
                weekly_trend TEXT,
                below_ema200 INTEGER,
                high_52w REAL,
                low_52w REAL,
                pullback_pct REAL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (symbol, scan_date)
            );

            CREATE TABLE IF NOT EXISTS fundamentals (
                symbol TEXT PRIMARY KEY,
                pe REAL,
                pb REAL,
                fwd_pe REAL,
                roe REAL,
                roa REAL,
                revenue_growth REAL,
                earnings_growth REAL,
                debt_to_equity REAL,
                promoter_pct REAL,
                market_cap REAL,
                free_cash_flow REAL,
                total_revenue REAL,
                capex REAL,
                eps_fwd REAL,
                eps_trail REAL,
                fund_score INTEGER,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS macro_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                country TEXT,
                impact TEXT,
                actual TEXT,
                forecast TEXT,
                surprise_dir TEXT,
                score REAL,
                event_date TEXT,
                event_time TEXT,
                scanned_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS final_scores (
                symbol TEXT NOT NULL,
                scan_date TEXT NOT NULL,
                news_sentiment_score REAL,
                news_spike_score REAL,
                technical_score REAL,
                fundamental_score REAL,
                macro_score REAL,
                marketaux_score REAL,
                final_score REAL,
                grade TEXT,
                high_conviction INTEGER,
                bear_play INTEGER,
                is_golden INTEGER DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (symbol, scan_date)
            );

            DROP TABLE IF EXISTS users;
        """)
        # Add detailed_json column if missing (idempotent)
        try:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(fundamentals)")
            cols = [col[1] for col in cur.fetchall()]
            if "detailed_json" not in cols:
                conn.execute("ALTER TABLE fundamentals ADD COLUMN detailed_json TEXT;")
                conn.commit()
        except Exception as e:
            log.warning("SQLite ALTER TABLE fundamentals detailed_json failed: %s", e)

        # Phase 6: scan state tables
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS scan_runs (
                scan_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL DEFAULT 'manual',
                status TEXT NOT NULL DEFAULT 'running',
                phase TEXT DEFAULT '',
                start_time TEXT DEFAULT (datetime('now')),
                end_time TEXT,
                processed_count INTEGER DEFAULT 0,
                failed_count INTEGER DEFAULT 0,
                deferred_count INTEGER DEFAULT 0,
                candidate_count INTEGER DEFAULT 0,
                duration_seconds REAL,
                error_message TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_scan_runs_status ON scan_runs(status);

            CREATE TABLE IF NOT EXISTS current_scan_state (
                id INTEGER PRIMARY KEY DEFAULT 1,
                scan_id TEXT,
                mode TEXT DEFAULT '',
                status TEXT DEFAULT 'idle',
                phase TEXT DEFAULT '',
                start_time TEXT,
                processed_count INTEGER DEFAULT 0,
                failed_count INTEGER DEFAULT 0,
                candidate_count INTEGER DEFAULT 0,
                cancel_requested INTEGER DEFAULT 0,
                updated_at TEXT DEFAULT (datetime('now'))
            );
            INSERT OR IGNORE INTO current_scan_state (id, status, cancel_requested, updated_at)
            VALUES (1, 'idle', 0, datetime('now'));

            CREATE TABLE IF NOT EXISTS symbol_state (
                symbol TEXT PRIMARY KEY,
                last_price_update TEXT,
                last_technical_update TEXT,
                last_news_update TEXT,
                last_sentiment_update TEXT,
                last_financial_update TEXT,
                last_deep_scan TEXT,
                price_change_pct REAL DEFAULT 0.0,
                prev_score INTEGER DEFAULT 0,
                needs_deep_scan INTEGER DEFAULT 0,
                deep_scan_reason TEXT DEFAULT '',
                updated_at TEXT DEFAULT (datetime('now'))
            );
        """)

        # ── Release 3: Outcome Intelligence Layer ──────────────
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                sector TEXT DEFAULT '',
                entry_date TEXT NOT NULL,
                entry_price REAL NOT NULL,
                target_price REAL,
                stop_loss REAL,
                virtual_capital REAL DEFAULT 25000,
                quantity INTEGER DEFAULT 0,
                source TEXT DEFAULT 'QUANT',
                score_at_entry INTEGER DEFAULT 0,
                grade_at_entry TEXT DEFAULT '',
                technical_score REAL DEFAULT 0,
                fundamental_score REAL DEFAULT 0,
                earnings_momentum_score REAL DEFAULT 0,
                earnings_grade TEXT DEFAULT '',
                smart_money_score REAL DEFAULT 0,
                sector_rotation_score REAL DEFAULT 0,
                catalyst_score REAL DEFAULT 0,
                news_sentiment_score REAL DEFAULT 0,
                risk_score REAL DEFAULT 0,
                risk_reward REAL DEFAULT 0,
                model_version TEXT DEFAULT '',
                market_regime TEXT DEFAULT '',
                nifty_entry REAL,
                high_conviction INTEGER DEFAULT 0,
                is_golden INTEGER DEFAULT 0,
                signals_json TEXT DEFAULT '[]',
                earnings_signals_json TEXT DEFAULT '[]',
                exit_date TEXT,
                exit_price REAL,
                exit_reason TEXT,
                nifty_exit REAL,
                days_held INTEGER DEFAULT 0,
                return_pct REAL,
                alpha_pct REAL,
                max_drawdown_pct REAL DEFAULT 0,
                max_runup_pct REAL DEFAULT 0,
                status TEXT DEFAULT 'OPEN',
                position_size_pct REAL DEFAULT 20.0,
                weight_version TEXT DEFAULT '',
                confidence_score REAL DEFAULT 0,
                entry_rank INTEGER DEFAULT 0,
                breadth_advances INTEGER DEFAULT 0,
                breadth_declines INTEGER DEFAULT 0,
                breadth_ratio REAL DEFAULT 0,
                probability_bucket TEXT,
                expected_return_bucket TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_paper_trades_status ON paper_trades(status);
            CREATE INDEX IF NOT EXISTS idx_paper_trades_symbol ON paper_trades(symbol);
            CREATE INDEX IF NOT EXISTS idx_paper_trades_entry ON paper_trades(entry_date);
            CREATE INDEX IF NOT EXISTS idx_paper_trades_status_date ON paper_trades(status, entry_date);

            CREATE TABLE IF NOT EXISTS recommendation_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                rank INTEGER NOT NULL,
                score INTEGER DEFAULT 0,
                grade TEXT DEFAULT '',
                technical_score REAL DEFAULT 0,
                fundamental_score REAL DEFAULT 0,
                earnings_momentum_score REAL DEFAULT 0,
                earnings_grade TEXT DEFAULT '',
                smart_money_score REAL DEFAULT 0,
                risk_score REAL DEFAULT 0,
                price REAL DEFAULT 0,
                model_version TEXT DEFAULT '',
                market_regime TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE (snapshot_date, symbol)
            );
            CREATE INDEX IF NOT EXISTS idx_rec_snap_date ON recommendation_snapshots(snapshot_date);

            CREATE TABLE IF NOT EXISTS paper_portfolio_daily (
                date TEXT PRIMARY KEY,
                portfolio_value REAL DEFAULT 0,
                invested_value REAL DEFAULT 0,
                open_positions INTEGER DEFAULT 0,
                closed_today INTEGER DEFAULT 0,
                total_closed INTEGER DEFAULT 0,
                win_count INTEGER DEFAULT 0,
                loss_count INTEGER DEFAULT 0,
                total_return_pct REAL DEFAULT 0,
                nifty_level REAL DEFAULT 0,
                model_version TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS recommendation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                scan_id TEXT,
                version INTEGER DEFAULT 1,
                entry_low REAL,
                entry_high REAL,
                stop_loss REAL,
                target_price REAL,
                target1 REAL,
                target2 REAL,
                target3 REAL,
                risk_reward REAL,
                score INTEGER DEFAULT 0,
                grade TEXT DEFAULT '',
                confidence_score REAL DEFAULT 0,
                risk_score REAL DEFAULT 0,
                technical_score REAL DEFAULT 0,
                fundamental_score REAL DEFAULT 0,
                price_at_analysis REAL,
                analysis_timestamp TEXT DEFAULT (datetime('now')),
                is_first_analysis INTEGER DEFAULT 0,
                change_reason TEXT,
                data_snapshot TEXT,
                UNIQUE(symbol, version)
            );

            CREATE TABLE IF NOT EXISTS recommendation_locks (
                symbol TEXT PRIMARY KEY,
                locked_at TEXT DEFAULT (datetime('now')),
                recommended_price REAL,
                entry_low REAL,
                entry_high REAL,
                stop_loss REAL,
                target1 REAL,
                target2 REAL,
                target3 REAL,
                risk_reward REAL,
                thesis_status TEXT DEFAULT 'ACTIVE',
                score_at_lock INTEGER DEFAULT 0,
                updates TEXT DEFAULT '[]'
            );
        """)

        # P5: Performance indexes for dashboard queries (parity with PostgreSQL path)
        conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_scan_results_score ON scan_results(score DESC);
            CREATE INDEX IF NOT EXISTS idx_scan_results_hc ON scan_results(high_conviction);
            CREATE INDEX IF NOT EXISTS idx_news_articles_symbol ON news_articles(symbol);
        """)

        # Release 4: Execution Engine Schema (SQLite parity)
        conn.executescript("""

            CREATE TABLE IF NOT EXISTS historical_cache (
                symbol_token TEXT NOT NULL,
                exchange TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                last_refresh TIMESTAMP NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                payload_json JSON NOT NULL,
                PRIMARY KEY(symbol_token, exchange, timeframe)
            );

            CREATE TABLE IF NOT EXISTS provider_stats (
                provider_name TEXT PRIMARY KEY,
                historical_calls INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS paper_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                order_type TEXT DEFAULT 'LIMIT',
                side TEXT DEFAULT 'BUY',
                status TEXT DEFAULT 'PENDING',
                entry_low REAL,
                entry_high REAL,
                target_price REAL,
                stop_loss REAL,
                virtual_capital REAL DEFAULT 25000,
                score_at_signal INTEGER DEFAULT 0,
                grade_at_signal TEXT DEFAULT '',
                scan_id TEXT,
                signal_source TEXT DEFAULT 'scanner',
                signal_time TEXT NOT NULL DEFAULT (datetime('now')),
                order_created_at TEXT DEFAULT (datetime('now')),
                triggered_at TEXT,
                filled_at TEXT,
                cancelled_at TEXT,
                expires_at TEXT,
                research_snapshot_id INTEGER,
                correlation_id TEXT,
                recommendation_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_paper_orders_status ON paper_orders(status);
            CREATE INDEX IF NOT EXISTS idx_paper_orders_symbol ON paper_orders(symbol);
        """)
        # Extend paper_trades with timestamp columns (SQLite)
        for col_sql in [
            "ALTER TABLE paper_trades ADD COLUMN entry_time TEXT;",
            "ALTER TABLE paper_trades ADD COLUMN exit_time TEXT;",
            "ALTER TABLE paper_trades ADD COLUMN order_id INTEGER;",
            "ALTER TABLE paper_trades ADD COLUMN fill_price REAL;",
            "ALTER TABLE paper_trades ADD COLUMN updated_at TEXT DEFAULT (datetime('now'));",
            "ALTER TABLE paper_trades ADD COLUMN execution_latency_ms INTEGER;",
            "ALTER TABLE paper_trades ADD COLUMN scan_id TEXT;",
            "ALTER TABLE paper_trades ADD COLUMN recommendation_id TEXT;",
            "ALTER TABLE paper_trades ADD COLUMN source TEXT DEFAULT 'QUANT';",
            "ALTER TABLE paper_orders ADD COLUMN recommendation_id TEXT;",
        ]:
            try:
                conn.execute(col_sql)
            except Exception:
                pass

        # Phase 1C: slim_data column for SQLite
        try:
            conn.execute("ALTER TABLE scan_results ADD COLUMN slim_data TEXT;")
        except Exception:
            pass  # column already exists

        # Phase 0: Trust & Observability — score_audit + scan_audit (SQLite)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS score_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                scan_id TEXT NOT NULL,
                scan_time TEXT NOT NULL DEFAULT (datetime('now')),
                technical_score REAL,
                earnings_momentum_score REAL,
                fundamental_score REAL,
                smart_money_score REAL,
                sector_rotation_score REAL,
                news_sentiment_score REAL,
                news_spike_score REAL,
                macro_score REAL,
                catalyst_score REAL,
                final_score REAL NOT NULL,
                data_source TEXT,
                source_reason TEXT,
                provider_latency_ms INTEGER,
                data_staleness_hours REAL,
                scan_version TEXT,
                UNIQUE (symbol, scan_id)
            );
            CREATE INDEX IF NOT EXISTS idx_score_audit_symbol ON score_audit(symbol);
            CREATE INDEX IF NOT EXISTS idx_score_audit_time ON score_audit(scan_time DESC);

            CREATE TABLE IF NOT EXISTS scan_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id TEXT,
                start_time TEXT,
                end_time TEXT,
                duration_ms INTEGER,
                stocks_scanned INTEGER,
                stocks_succeeded INTEGER,
                stocks_failed INTEGER,
                data_source TEXT,
                scan_version TEXT,
                scan_mode TEXT DEFAULT 'manual'
            );
            CREATE INDEX IF NOT EXISTS idx_scan_audit_time ON scan_audit(start_time DESC);
        """)

        # ── Phase 0A+1: Governance schema additions (SQLite) ──
        # Section 4, 30: State transition audit log
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS scan_state_transitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id TEXT NOT NULL,
                old_state TEXT NOT NULL,
                new_state TEXT NOT NULL,
                reason TEXT,
                actor TEXT DEFAULT 'system',
                correlation_id TEXT,
                hash_chain TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_sst_scan_id ON scan_state_transitions(scan_id);
            CREATE INDEX IF NOT EXISTS idx_sst_created ON scan_state_transitions(created_at DESC);

            CREATE TABLE IF NOT EXISTS scan_event_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                details TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_sea_scan_id ON scan_event_audit(scan_id);
            CREATE INDEX IF NOT EXISTS idx_sea_created ON scan_event_audit(created_at DESC);
        """)

        # Production Schema additions
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS universe_catalog (
                symbol TEXT PRIMARY KEY,
                company_name TEXT,
                market_cap REAL,
                market_cap_bucket TEXT,
                sector TEXT,
                industry TEXT,
                is_active INTEGER DEFAULT 1,
                last_scanned_at TEXT
            );

            CREATE TABLE IF NOT EXISTS universe_chunk_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id TEXT NOT NULL,
                chunk_name TEXT NOT NULL,
                status TEXT NOT NULL,
                symbol_count INTEGER,
                symbols_processed INTEGER DEFAULT 0,
                error_message TEXT,
                started_at TEXT DEFAULT (datetime('now')),
                completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ucr_scan_id ON universe_chunk_runs(scan_id);

            CREATE TABLE IF NOT EXISTS research_snapshots_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version INTEGER NOT NULL DEFAULT 1,
                symbol TEXT NOT NULL,
                status TEXT DEFAULT 'ACTIVE',
                outcome_status TEXT DEFAULT 'PENDING',
                recommendation TEXT,
                entry_low REAL,
                entry_high REAL,
                stop_loss REAL,
                target_1 REAL,
                target_2 REAL,
                target_3 REAL,
                risk_reward REAL,
                confidence REAL,
                confidence_breakdown TEXT,
                research_thesis TEXT,
                cmp_at_generation REAL,
                score_at_generation REAL,
                raw_score_at_generation REAL,
                scan_id TEXT,
                correlation_id TEXT,
                scanner_version TEXT,
                scoring_version TEXT,
                recommendation_version TEXT,
                config_snapshot TEXT,
                snapshot_hash TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                UNIQUE(symbol, version)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_research_snapshot_scan_symbol ON research_snapshots_v2(scan_id, symbol);

            CREATE TABLE IF NOT EXISTS research_advisories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                advisory_type TEXT NOT NULL,
                advisory_text TEXT NOT NULL,
                priority TEXT DEFAULT 'MEDIUM',
                issued_by TEXT DEFAULT 'system',
                is_active INTEGER DEFAULT 1,
                valid_until TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_adv_symbol ON research_advisories(symbol);
        """)
        # Phase 6, Section 40: hash_chain column (idempotent for existing DBs)
        try:
            conn.execute("ALTER TABLE scan_state_transitions ADD COLUMN hash_chain TEXT;")
        except Exception:
            pass

        # Section 5: Context columns on scan_runs (SQLite ALTER is limited)
        for col_sql in [
            "ALTER TABLE scan_runs ADD COLUMN correlation_id TEXT;",
            "ALTER TABLE scan_runs ADD COLUMN request_id TEXT;",
            "ALTER TABLE scan_runs ADD COLUMN trigger_source TEXT DEFAULT 'manual';",
            "ALTER TABLE scan_runs ADD COLUMN user_id TEXT DEFAULT 'system';",
            "ALTER TABLE scan_runs ADD COLUMN scanner_version TEXT;",
            "ALTER TABLE scan_runs ADD COLUMN scoring_version TEXT;",
            "ALTER TABLE scan_runs ADD COLUMN recommendation_version TEXT;",
            "ALTER TABLE scan_runs ADD COLUMN universe_version TEXT;",
            "ALTER TABLE scan_runs ADD COLUMN config_snapshot TEXT;",
            "ALTER TABLE scan_runs ADD COLUMN parent_scan_id TEXT;",
            # Phase 4, Section 37: Data quality degradation flag
            "ALTER TABLE scan_runs ADD COLUMN degraded_data BOOLEAN DEFAULT FALSE;",
            "ALTER TABLE scan_runs ADD COLUMN last_heartbeat TEXT;",
            "ALTER TABLE universe_chunk_runs ADD COLUMN chunk_last_activity TEXT;",
            "ALTER TABLE universe_chunk_runs ADD COLUMN last_symbol TEXT;",
        ]:
            try:
                conn.execute(col_sql)
            except Exception as exc:
                if "duplicate column name" in str(exc).lower():
                    pass
                else:
                    log.error("[MIGRATION FAILED] SQLite schema error: %s", exc, exc_info=True)
                    raise

        # Section 36: Score breakdown
        try:
            conn.execute("ALTER TABLE score_audit ADD COLUMN score_breakdown TEXT;")
        except Exception as exc:
            if "duplicate column name" in str(exc).lower():
                pass
            else:
                log.error("[MIGRATION FAILED] SQLite score_audit error: %s", exc, exc_info=True)
                raise

        # ── scoring_v1 comparison: model_version tagging migration (SQLite fallback) ──
        try:
            _migrate_model_version_v1(conn.cursor(), is_pg=False)
        except Exception as exc:
            log.error("[MIGRATION FAILED] model_version v1 (SQLite, non-fatal): %s", exc, exc_info=True)
                
        # Phase 1: Snapshot Schema Migration
        for col_sql in [
            "ALTER TABLE research_snapshots_v2 ADD COLUMN cmp_at_generation REAL;",
            "ALTER TABLE research_snapshots_v2 ADD COLUMN score_at_generation REAL;",
            "ALTER TABLE research_snapshots_v2 ADD COLUMN raw_score_at_generation REAL;",
        ]:
            try:
                conn.execute(col_sql)
            except Exception as exc:
                if "duplicate column name" in str(exc).lower():
                    pass
                else:
                    log.error("[MIGRATION FAILED] SQLite snapshot schema error: %s", exc, exc_info=True)

        # Phase 5.5: Universe Engine tables (SQLite)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS eligible_universe (
                symbol TEXT PRIMARY KEY,
                market_cap_cr REAL,
                avg_volume_20d REAL,
                avg_turnover_20d REAL,
                price REAL,
                eligibility_reason TEXT DEFAULT 'FILTER_PASS',
                universe_version TEXT NOT NULL DEFAULT '',
                generated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS scan_resume_state (
                scan_id TEXT PRIMARY KEY,
                universe_version TEXT NOT NULL,
                total_batches INTEGER NOT NULL,
                current_batch_index INTEGER DEFAULT 0,
                status TEXT DEFAULT 'running',
                last_heartbeat TEXT DEFAULT (datetime('now')),
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS scan_lock (
                id INTEGER PRIMARY KEY DEFAULT 1,
                scan_id TEXT,
                owner_id TEXT,
                heartbeat TEXT,
                expires_at TEXT,
                acquired_at TEXT
            );
            INSERT OR IGNORE INTO scan_lock (id) VALUES (1);

            CREATE TABLE IF NOT EXISTS scan_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id TEXT NOT NULL,
                batch_index INTEGER NOT NULL,
                status TEXT DEFAULT 'PENDING',
                worker_id TEXT,
                symbol_count INTEGER DEFAULT 0,
                symbols_processed INTEGER DEFAULT 0,
                retry_count INTEGER DEFAULT 0,
                started_at TEXT,
                completed_at TEXT,
                UNIQUE(scan_id, batch_index)
            );
        """)

        # SQLite ALTERs for universe_catalog Phase 5.5 columns
        for col_sql in [
            "ALTER TABLE universe_catalog ADD COLUMN avg_volume_20d REAL DEFAULT 0;",
            "ALTER TABLE universe_catalog ADD COLUMN avg_turnover_20d REAL DEFAULT 0;",
            "ALTER TABLE universe_catalog ADD COLUMN instrument_type TEXT DEFAULT 'EQ';",
            "ALTER TABLE universe_catalog ADD COLUMN exchange TEXT DEFAULT 'NSE';",
            "ALTER TABLE universe_catalog ADD COLUMN price REAL DEFAULT 0;",
            "ALTER TABLE universe_catalog ADD COLUMN last_synced_at TEXT;",
            "ALTER TABLE universe_catalog ADD COLUMN first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP;",
            "ALTER TABLE universe_catalog ADD COLUMN sync_fail_count INTEGER DEFAULT 0;",
        ]:
            try:
                conn.execute(col_sql)
            except Exception as exc:
                if "duplicate column name" in str(exc).lower():
                    pass

        # Universe Rebuild History (SQLite)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS universe_rebuild_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                universe_version TEXT NOT NULL,
                generated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                input_count INTEGER DEFAULT 0,
                eligible_count INTEGER DEFAULT 0,
                rejected_mcap INTEGER DEFAULT 0,
                rejected_turnover INTEGER DEFAULT 0,
                rejected_volume INTEGER DEFAULT 0,
                rejected_price INTEGER DEFAULT 0,
                rejected_etf INTEGER DEFAULT 0,
                rejected_sme INTEGER DEFAULT 0,
                rejected_suspended INTEGER DEFAULT 0,
                rejected_ipo_age INTEGER DEFAULT 0,
                force_included INTEGER DEFAULT 0,
                fallback_used INTEGER DEFAULT 0
            );
        """)

        # Retry count for scan_batches (for existing DBs that lack the column)
        try:
            conn.execute("ALTER TABLE scan_batches ADD COLUMN retry_count INTEGER DEFAULT 0;")
        except Exception:
            pass  # already exists

        # ── Phase 5.6B/C: Liquidity Enrichment & Universe Governance (SQLite) ──
        for col_sql in [
            "ALTER TABLE universe_catalog ADD COLUMN liquidity_synced_at TEXT;",
            "ALTER TABLE universe_catalog ADD COLUMN liquidity_sync_fail_count INTEGER DEFAULT 0;",
            "ALTER TABLE universe_catalog ADD COLUMN liquidity_excluded INTEGER DEFAULT 0;",
            "ALTER TABLE universe_catalog ADD COLUMN liquidity_excluded_reason TEXT;",
            "ALTER TABLE universe_catalog ADD COLUMN liquidity_excluded_at TEXT;",
        ]:
            try:
                conn.execute(col_sql)
            except Exception as exc:
                if "duplicate column name" in str(exc).lower():
                    pass
        # ── Dhan Fundamental Data columns (SQLite, replaces yfinance) ──
        for col_sql in [
            "ALTER TABLE universe_catalog ADD COLUMN isin TEXT;",
            "ALTER TABLE universe_catalog ADD COLUMN pe REAL;",
            "ALTER TABLE universe_catalog ADD COLUMN pb REAL;",
            "ALTER TABLE universe_catalog ADD COLUMN roe REAL;",
            "ALTER TABLE universe_catalog ADD COLUMN roce REAL;",
            "ALTER TABLE universe_catalog ADD COLUMN eps REAL;",
            "ALTER TABLE universe_catalog ADD COLUMN div_yield REAL;",
            "ALTER TABLE universe_catalog ADD COLUMN industry_pe REAL;",
            "ALTER TABLE universe_catalog ADD COLUMN revenue REAL;",
            "ALTER TABLE universe_catalog ADD COLUMN free_cash_flow REAL;",
            "ALTER TABLE universe_catalog ADD COLUMN net_profit_margin REAL;",
            "ALTER TABLE universe_catalog ADD COLUMN high_52w REAL;",
            "ALTER TABLE universe_catalog ADD COLUMN low_52w REAL;",
            "ALTER TABLE universe_catalog ADD COLUMN pct_change_1m REAL;",
            "ALTER TABLE universe_catalog ADD COLUMN pct_change_1y REAL;",
            "ALTER TABLE universe_catalog ADD COLUMN rsi_14 REAL;",
            "ALTER TABLE universe_catalog ADD COLUMN sma_50 REAL;",
            "ALTER TABLE universe_catalog ADD COLUMN sma_200 REAL;",
            "ALTER TABLE universe_catalog ADD COLUMN dhan_sid TEXT;",
            "ALTER TABLE universe_catalog ADD COLUMN fundamentals_updated_at TEXT;",
        ]:
            try:
                conn.execute(col_sql)
            except Exception as exc:
                if "duplicate column name" in str(exc).lower():
                    pass

        # Universe Snapshot (append-only audit trail, SQLite)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS universe_snapshot (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                universe_version TEXT NOT NULL,
                symbol TEXT NOT NULL,
                market_cap_cr REAL,
                avg_volume_20d REAL,
                avg_turnover_20d REAL,
                price REAL,
                eligibility_reason TEXT DEFAULT 'FILTER_PASS',
                generated_at TEXT DEFAULT (datetime('now'))
            );
        """)

        # Candidate Universe (version-locked snapshot, SQLite)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS candidate_universe (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                universe_version TEXT NOT NULL,
                symbol TEXT NOT NULL,
                market_cap_cr REAL,
                frozen_at TEXT DEFAULT (datetime('now')),
                UNIQUE(universe_version, symbol)
            );
        """)

        # Universe Build Validation Snapshot (forensic evidence, SQLite)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS universe_build_validation_snapshot (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                universe_version TEXT NOT NULL,
                candidate_count INTEGER DEFAULT 0,
                eligible_count INTEGER DEFAULT 0,
                marketcap_coverage_pct REAL DEFAULT 0,
                liquidity_coverage_pct REAL DEFAULT 0,
                build_timestamp TEXT DEFAULT (datetime('now'))
            );
        """)

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS symbol_aliases (
                old_symbol TEXT PRIMARY KEY,
                new_symbol TEXT NOT NULL,
                reason TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            INSERT INTO symbol_aliases (old_symbol, new_symbol, reason) 
            VALUES ('HDFC', 'HDFCBANK', 'HDFC-HDFCBANK merger (July 2023)'),
                   ('HDFC.NS', 'HDFCBANK.NS', 'HDFC-HDFCBANK merger (July 2023)')
            ON CONFLICT (old_symbol) DO NOTHING;
        """)

        # Phase 5.7: recommendation_history table for SQLite parity
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS recommendation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                scan_id TEXT,
                version INTEGER DEFAULT 1,
                entry_low REAL,
                entry_high REAL,
                stop_loss REAL,
                target_price REAL,
                target1 REAL,
                target2 REAL,
                target3 REAL,
                risk_reward REAL,
                score INTEGER DEFAULT 0,
                grade TEXT DEFAULT '',
                confidence_score REAL DEFAULT 0,
                risk_score REAL DEFAULT 0,
                technical_score REAL DEFAULT 0,
                fundamental_score REAL DEFAULT 0,
                price_at_analysis REAL,
                analysis_timestamp TEXT DEFAULT (datetime('now')),
                is_first_analysis BOOLEAN DEFAULT 0,
                change_reason TEXT,
                data_snapshot TEXT,
                UNIQUE(symbol, version)
            );
            CREATE INDEX IF NOT EXISTS idx_rh_symbol ON recommendation_history(symbol);
            CREATE INDEX IF NOT EXISTS idx_rh_first ON recommendation_history(symbol) WHERE is_first_analysis = 1;
        """)

        log.info("SQLite Database initialized: %s", DB_PATH)


def auto_clear_daily_cache():
    """Clear local SQLite database and/or table scan_results when calendar date changes."""
    try:
        from datetime import date
        from pathlib import Path
        cache_dir = Path(__file__).parent / "cache"
        cache_dir.mkdir(exist_ok=True)
        clear_tracker = cache_dir / "last_clear_date.txt"
        today_str = date.today().isoformat()

        # If the file doesn't exist, we write it and DO NOT clear results (fresh setup).
        if not clear_tracker.exists():
            clear_tracker.write_text(today_str)
            log.info("Daily cache clear tracker initialized for %s", today_str)
            return

        last_date = clear_tracker.read_text().strip()
        if last_date != today_str:
            log.info("Daily cache auto-clear triggered (Last run: %s, Today: %s).", last_date, today_str)
            # Removed DELETE FROM scan_results to prevent midnight data wipe
            clear_tracker.write_text(today_str)
            log.info("Daily cache successfully cleared for %s", today_str)
    except Exception as exc:
        log.warning("Daily cache auto-clear failed: %s", exc)

# ─── Unified Scan State (Phase 6 + Phase 0A Hardening) ───

import uuid as _uuid
from events import ACTOR_SYSTEM, ACTOR_WATCHDOG, ACTOR_USER

_SCAN_LOCK_TIMEOUT_MIN = 30  # stale scan recovery threshold


# ═══════════════════════════════════════════════════════════════
# Section 30: GOVERNANCE STATE MATRIX
# Defines all valid state transitions. Any transition not listed
# here is ILLEGAL and will be rejected with a CRITICAL log.
# ═══════════════════════════════════════════════════════════════
VALID_TRANSITIONS = {
    "created":    {"running", "cancelled", "rejected"},
    "running":    {"completed", "failed", "cancelled", "stale", "zombie_detected"},
    "completed":  set(),           # Terminal — no transitions allowed
    "failed":     {"recovering"},   # Can retry via new linked scan
    "cancelled":  set(),           # Terminal
    "stale":      {"failed"},      # Watchdog marks stale → failed
    "zombie_detected": {"failed"}, # Watchdog marks zombie_detected → failed
    "recovering": {"running"},     # Recovery creates new scan
    "rejected":   set(),           # Terminal — client error
    "idle":       {"running"},     # current_scan_state special state
}


def _is_valid_transition(from_status: str, to_status: str) -> bool:
    """Check if a state transition is allowed per the governance matrix."""
    allowed = VALID_TRANSITIONS.get(from_status, set())
    return to_status in allowed


# ═══════════════════════════════════════════════════════════════
# Section 4, 30: STATE TRANSITION FUNCTIONS
# Atomic, audited state transitions. Every transition creates
# an append-only row in scan_state_transitions.
# ═══════════════════════════════════════════════════════════════

def utc_now() -> datetime:
    """Canonical scan-lifecycle clock (ADR-009): timezone-naive UTC, independent of the
    container's local timezone. Single source of truth for the scan_runs lifecycle
    timestamps (start_time / created_at / last_heartbeat / end_time), scan_state_transitions,
    the watchdog staleness comparison, and duration — so they are never compared cross-zone."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_now_str() -> str:
    """Canonical UTC timestamp in the scan_runs storage string format."""
    return utc_now().strftime("%Y-%m-%d %H:%M:%S")


def save_state_transition(scan_id: str, old_state: str, new_state: str,
                          reason: str = "", actor: str = "system",
                          correlation_id: str = ""):
    """Insert an append-only state transition audit row.
    Section 4: Every state machine transition creates exactly one row.
    These rows are NEVER updated or deleted.

    Phase 6, Section 40: Tamper-evident hash-chaining.
    Each row stores SHA256(prev_hash + fields), enabling
    detection of deleted, modified, or out-of-order rows.
    """
    import hashlib
    now = utc_now_str()
    try:
        # Fetch the hash_chain of the most recent transition
        prev_row = execute_db(
            "SELECT hash_chain FROM scan_state_transitions ORDER BY id DESC LIMIT 1",
            fetch="one"
        )
        prev_hash = prev_row.get("hash_chain", "") if prev_row else ""
        prev_hash = prev_hash or ""

        # Compute SHA256 of (prev_hash + current transition fields)
        chain_input = "|".join([
            prev_hash, scan_id, old_state, new_state,
            reason or "", actor, correlation_id or "", now
        ])
        current_hash = hashlib.sha256(chain_input.encode("utf-8")).hexdigest()

        execute_db("""
            INSERT INTO scan_state_transitions
            (scan_id, old_state, new_state, reason, actor, correlation_id, hash_chain, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (scan_id, old_state, new_state, reason or "", actor,
              correlation_id or "", current_hash, now))
    except Exception as exc:
        log.warning("State transition audit insert failed: %s", exc)


def transition_scan_state(scan_id: str, from_status: str, to_status: str,
                          reason: str = "", actor: str = "system",
                          correlation_id: str = "",
                          error_message: str = "") -> bool:
    """Atomically transition a scan's state using conditional UPDATE.

    Section 3, 30, 32:
    - Validates against VALID_TRANSITIONS matrix
    - Uses conditional UPDATE WHERE status=from_status (prevents TOCTOU)
    - If rowcount == 0, the transition was raced or illegal
    - Always logs the transition to scan_state_transitions (append-only)
    - Syncs current_scan_state for O(1) UI polling

    Returns True if transition succeeded, False if it was rejected/raced.
    """
    # Validate against governance matrix
    if not _is_valid_transition(from_status, to_status):
        log.critical(
            "[STATE MACHINE] ILLEGAL transition rejected: %s -> %s for scan %s (reason=%s, actor=%s)",
            from_status, to_status, scan_id, reason, actor
        )
        return False

    now = utc_now_str()

    # Atomic conditional UPDATE on scan_runs
    # P0.1D: require_pg=True — state transitions must never degrade to SQLite
    rowcount = execute_db("""
        UPDATE scan_runs SET status=?, error_message=COALESCE(?, error_message)
        WHERE scan_id=? AND status=?
    """, (to_status, error_message or None, scan_id, from_status), fetch="rowcount", require_pg=True)

    if rowcount == 0:
        log.warning(
            "[STATE MACHINE] Transition raced or invalid: %s -> %s for scan %s (rowcount=0)",
            from_status, to_status, scan_id
        )
        return False

    # Calculate duration if reaching a terminal state
    if to_status in ("completed", "failed", "cancelled"):
        try:
            row = execute_db("SELECT start_time FROM scan_runs WHERE scan_id=?", (scan_id,), fetch="one")
            duration = 0.0
            if row and row.get("start_time"):
                st = datetime.fromisoformat(str(row["start_time"]))
                # Canonical UTC clock on both ends: duration = end_time - start_time
                duration = max(0.0, (utc_now() - st).total_seconds())
            execute_db("""
                UPDATE scan_runs SET end_time=?, duration_seconds=? WHERE scan_id=?
            """, (now, round(duration, 1), scan_id))
        except Exception:
            pass

    # Sync current_scan_state (Section 6, 29: always update both tables together)
    _sync_current_scan_state(scan_id, to_status, now)

    # Append-only audit trail (Section 4)
    save_state_transition(scan_id, from_status, to_status, reason, actor, correlation_id)

    log.info(
        "[STATE MACHINE] %s -> %s | scan=%s | reason=%s | actor=%s",
        from_status, to_status, scan_id[:20], reason, actor
    )

    # Phase A: Event-based cache invalidation
    # Guard: only invalidate when transitioning to a DIFFERENT state to prevent stampede
    if from_status != to_status and to_status in ("running", "completed", "failed", "cancelled"):
        try:
            import cache_layer
            # Phase 1.5 completion ordering (Change Sets B + C), all flag-gated (OFF = identical):
            #   publish barrier (status committed above) -> version switch -> targeted detail
            #   cleanup -> invalidate_all. The version switch (B) is the correctness anchor, so
            #   invalidate_all becomes eager cleanup whose failure is observable, not silent.
            if to_status == "completed" and os.environ.get("PHASE15_ATOMIC_FINALIZE") == "1":
                try:
                    cache_layer.set_cache_generation(scan_id)        # B-1: PUSH the new generation
                except Exception:
                    pass
            if to_status == "completed" and os.environ.get("PHASE15_CACHE_GAPS") == "1":
                try:
                    cache_layer.cleanup_detail_cache()               # C-1: detail-cache parity (parallel path)
                except Exception:
                    pass
            cache_layer.invalidate_all()
            log.info("[CACHE_INVALIDATED] Scan state %s -> %s | scan=%s",
                     from_status, to_status, scan_id[:20])
            if to_status == "completed":
                log.info("[SCAN_COMPLETED] scan_id=%s", scan_id[:20])
        except Exception as exc:
            # B-2: observable invalidation (no longer silently swallowed) when atomic-finalize is on.
            if os.environ.get("PHASE15_ATOMIC_FINALIZE") == "1":
                try:
                    from metrics import counters as _c
                    _c.inc("cache_invalidate_failed")
                except Exception:
                    pass
                log.error("[CACHE_INVALIDATED] invalidation FAILED (Phase 1.5; correctness preserved "
                          "by versioning): %s", exc)
            else:
                log.warning("[CACHE_INVALIDATED] Cache invalidation failed (non-fatal): %s", exc)

    return True


def _sync_current_scan_state(scan_id: str, status: str, now: str):
    """Sync the current_scan_state singleton row with scan_runs.
    Section 6, 29: Both tables are always updated together.
    """
    if status in ("completed", "failed", "cancelled", "stale"):
        # Terminal or near-terminal: reset to idle for UI
        ui_status = "idle" if status in ("completed", "failed", "cancelled") else status
        execute_db("""
            UPDATE current_scan_state SET
                status=?, phase='', cancel_requested=0, updated_at=?
            WHERE id=1
        """, (ui_status, now))
        ScanState._is_scanning_cache = (False, time.time())
    else:
        execute_db("""
            UPDATE current_scan_state SET
                status=?, updated_at=?
            WHERE id=1
        """, (status, now))
        if status == "running":
            ScanState._is_scanning_cache = (True, time.time())


# ═══════════════════════════════════════════════════════════════
# Section 32: ATOMIC LOCK ACQUISITION
# Eliminates TOCTOU race condition on scan start.
# ═══════════════════════════════════════════════════════════════

def acquire_scan_lock(scan_context) -> bool:
    """Atomically acquire the scan lock using conditional UPDATE.

    Section 3, 32:
    - Uses UPDATE current_scan_state SET status='running' WHERE status != 'running'
    - If rowcount == 1, lock acquired successfully
    - If rowcount == 0, another scan is already running — reject
    - Creates the scan_runs row and logs the transition

    Args:
        scan_context: ScanContext object with all metadata.
    Returns:
        True if lock acquired, False if rejected (scan already running).
    """
    now = utc_now_str()
    scan_id = scan_context.scan_id

    # Atomic conditional UPDATE — this IS the lock
    rowcount = execute_db("""
        UPDATE current_scan_state SET
            scan_id=?, mode=?, status='running', phase='init',
            start_time=?, processed_count=0, failed_count=0,
            candidate_count=0, cancel_requested=0, updated_at=?
        WHERE id=1 AND status != 'running'
    """, (scan_id, scan_context.trigger_source, now, now), fetch="rowcount")

    if rowcount == 0:
        # Lock NOT acquired — another scan is running
        log.warning(
            "[SCAN LOCK] Rejected: scan already running. Attempted scan_id=%s",
            scan_id[:30]
        )
        save_state_transition(scan_id, "created", "rejected",
                              reason="scan_already_active",
                              actor=scan_context.trigger_source,
                              correlation_id=scan_context.correlation_id)
        return False

    # Lock acquired — create scan_runs row with full context
    import json as _json
    execute_db("""
        INSERT INTO scan_runs
        (scan_id, mode, status, phase, start_time, last_heartbeat, candidate_count, created_at,
         correlation_id, request_id, trigger_source, user_id,
         scanner_version, scoring_version, recommendation_version,
         config_snapshot, parent_scan_id)
        VALUES (?, ?, 'running', 'init', ?, ?, 0, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?)
    """, (
        scan_id, scan_context.trigger_source, now, now, now,
        scan_context.correlation_id, scan_context.request_id,
        scan_context.trigger_source, scan_context.user_id,
        scan_context.scanner_version, scan_context.scoring_version,
        scan_context.recommendation_version,
        _json.dumps(scan_context.config_snapshot),
        scan_context.parent_scan_id,
    ))

    # Audit trail
    save_state_transition(scan_id, "idle", "running",
                          reason="scan_started",
                          actor=scan_context.trigger_source,
                          correlation_id=scan_context.correlation_id)

    ScanState._is_scanning_cache = (True, time.time())
    log.info(
        "[SCAN LOCK] Acquired: scan_id=%s | trigger=%s | correlation=%s",
        scan_id[:30], scan_context.trigger_source, scan_context.correlation_id[:12]
    )
    return True


def is_scan_active() -> tuple[bool, str]:
    """Check if a scan is currently running.
    Returns (is_active, active_scan_id).
    Section 4: Used by API for HTTP 409 duplicate rejection.
    """
    row = execute_db("SELECT scan_id, status FROM current_scan_state WHERE id=1", fetch="one")
    if row and row.get("status") == "running":
        return True, row.get("scan_id", "")
    return False, ""


def update_scan_progress(scan_id: str, **kwargs):
    """Update scan progress fields. Accepts: phase, processed_count, failed_count, candidate_count.
    Section 6: Always updates both scan_runs and current_scan_state together.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    allowed = {"phase", "processed_count", "failed_count", "candidate_count"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return
    set_clause = ", ".join(f"{k}=?" for k in updates)
    vals = list(updates.values())
    # Update current_scan_state
    execute_db(
        f"UPDATE current_scan_state SET {set_clause}, updated_at=? WHERE id=1",
        vals + [now]
    )
    # Update scan_runs
    if scan_id:
        execute_db(
            f"UPDATE scan_runs SET {set_clause} WHERE scan_id=?",
            vals + [scan_id]
        )


def update_scan_heartbeat(scan_id: str):
    """Update last_heartbeat for scanner liveness tracking."""
    now = utc_now_str()
    execute_db("UPDATE scan_runs SET last_heartbeat=? WHERE scan_id=?", (now, scan_id))
    execute_db("UPDATE current_scan_state SET updated_at=? WHERE id=1", (now,))


def check_scan_status(scan_id: str) -> str:
    """Check the true status of a specific scan_id from scan_runs."""
    row = execute_db("SELECT status FROM scan_runs WHERE scan_id=?", (scan_id,), fetch="one")
    return row["status"] if row else "unknown"


def log_scan_event(scan_id: str, event_type: str, details: str = ""):
    """Log an event to the append-only scan_event_audit table."""
    try:
        execute_db(
            "INSERT INTO scan_event_audit (scan_id, event_type, details) VALUES (?, ?, ?)",
            (scan_id, event_type, details)
        )
    except Exception as exc:
        log.warning("Failed to log scan event %s: %s", event_type, exc)


def cleanup_audit_events(days_to_keep: int = 7):
    """Phase J Audit Growth: Cleanup audit events older than specified days."""
    try:
        if is_postgresql() and not pg_cooldown_active():
            execute_db("DELETE FROM scan_event_audit WHERE created_at < NOW() - INTERVAL '%s days'", (days_to_keep,))
        else:
            execute_db("DELETE FROM scan_event_audit WHERE created_at < datetime('now', '-{} days')".format(days_to_keep))
        log.info("Audit cleanup: Removed scan_event_audit records older than %d days", days_to_keep)
    except Exception as exc:
        log.warning("Failed to cleanup audit events: %s", exc)


def get_scan_cancel_requested() -> bool:
    """Check if cancellation has been requested for the active scan."""
    row = execute_db("SELECT cancel_requested FROM current_scan_state WHERE id=1", fetch="one")
    return bool(row["cancel_requested"]) if row else False


def set_scan_cancel_requested(value: bool):
    """Set cancellation request flag."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    execute_db(
        "UPDATE current_scan_state SET cancel_requested=?, updated_at=? WHERE id=1",
        (1 if value else 0, now)
    )


def get_scan_status() -> dict:
    """Return current scan state with explicit priority mapping for P0.1A.
    Note: Stale scan recovery is now handled by the Watchdog (Phase 2),
    not inline during status reads.
    """
    row = execute_db("SELECT * FROM current_scan_state WHERE id=1", fetch="one")
    if not row:
        return {"scanning": False, "status": "IDLE", "progress": 0, "total": 0, "status_source": "default", "is_terminal": True}
        
    scan_id = row.get("scan_id")
    
    # Deterministic Status State Machine (Absolute RUNNING Priority)
    status_source = "current_scan_state"
    final_status = "IDLE"
    failed_reason = ""
    is_terminal = True
    
    if row.get("status") == "running":
        final_status = "RUNNING"
        is_terminal = False
    else:
        # ORDER BY created_at DESC LIMIT 1
        latest_run = execute_db(
            "SELECT status, error_message FROM scan_runs ORDER BY created_at DESC LIMIT 1", 
            fetch="one"
        )
        if latest_run:
            if latest_run["status"] == "failed":
                final_status = "FAILED"
                failed_reason = latest_run.get("error_message") or row.get("phase") or "Unknown failure"
                status_source = "scan_runs_latest"
            elif latest_run["status"] == "completed":
                final_status = "COMPLETED"
                status_source = "scan_runs_latest"
            else:
                final_status = "IDLE"
                status_source = "fallback"

    # Resume Metadata Protection
    resume_version = None
    try:
        resume = get_pending_resume()
        if resume:
            resume_version = resume.get("universe_version") or None
    except Exception:
        pass

    # Timestamps & Progress Age
    # Freshness: bind to scan_runs.end_time of the latest completed scan (canonical),
    # not the drift-prone scan_meta 'last_scan' (which is bumped mid-scan and never reset
    # on failure). get_last_scan_display() self-resolves the latest completed scan and
    # falls back to the legacy meta only when that row has no end_time.
    last_successful_scan = get_last_scan_display() or ""
    last_attempt = row.get("updated_at", "")
    if not last_attempt:
        last_attempt = row.get("start_time", "")
        
    progress_updated_at = row.get("updated_at", "")

    return {
        "scanning": final_status == "RUNNING",
        "status": final_status,
        "status_source": status_source,
        "failed_reason": failed_reason,
        "scan_id": scan_id or "",
        "resume_version": resume_version,
        "last_attempt": last_attempt,
        "last_successful_scan": last_successful_scan,
        "progress_updated_at": progress_updated_at,
        "is_terminal": is_terminal,
        "mode": row.get("mode", ""),
        "phase": row.get("phase", ""),
        "progress": row.get("processed_count", 0),
        "total": row.get("candidate_count", 0),
        "failed": row.get("failed_count", 0),
        "cancel_requested": bool(row.get("cancel_requested", 0)),
    }


class ScanState:
    """DB-backed scan state — backward-compatible wrapper.

    Phase 0A: This class now delegates to the module-level functions
    (acquire_scan_lock, transition_scan_state, etc.) for all critical
    operations. The singleton pattern is preserved ONLY for backward
    compatibility with callers that use scan_state.is_scanning,
    scan_state.status(), etc.

    New code should call the module-level functions directly.
    """
    _is_scanning_cache = None  # (bool, timestamp)
    _IS_SCANNING_TTL = 2.0     # seconds

    def start(self, total: int, mode: str = "manual", context=None) -> str:
        """Start a new scan. Returns scan_id.
        If context (ScanContext) is provided, uses atomic lock.
        Otherwise, falls back to legacy behavior for backward compatibility.
        """
        if context is not None:
            # Phase 0A: Atomic lock path
            acquired = acquire_scan_lock(context)
            if not acquired:
                return None  # Lock not acquired
            self._scan_id = context.scan_id
            self._total = total
            # Update candidate count now that we know it
            update_scan_progress(context.scan_id, candidate_count=total)
            return context.scan_id

        # Legacy path (backward compat for auto-scan and other callers)
        scan_id = f"scan_{mode}_{int(time.time())}_{_uuid.uuid4().hex[:6]}"
        now = utc_now_str()
        execute_db("""
            INSERT INTO scan_runs (scan_id, mode, status, phase, start_time, last_heartbeat, candidate_count, created_at)
            VALUES (?, ?, 'running', 'init', ?, ?, ?, ?)
        """, (scan_id, mode, now, now, total, now))
        execute_db("""
            UPDATE current_scan_state SET
                scan_id=?, mode=?, status='running', phase='init',
                start_time=?, processed_count=0, failed_count=0,
                candidate_count=?, cancel_requested=0, updated_at=?
            WHERE id=1
        """, (scan_id, mode, now, total, now))
        self._scan_id = scan_id
        self._total = total
        ScanState._is_scanning_cache = (True, time.time())
        save_state_transition(scan_id, "idle", "running", reason="scan_started_legacy", actor="system")
        return scan_id

    def update(self, **kwargs):
        """Update scan progress."""
        scan_id = getattr(self, "_scan_id", None)
        update_scan_progress(scan_id or "", **kwargs)

    def set_progress(self, value: int):
        """Convenience: update processed_count.
        Batched writes — only writes to DB every 25 stocks or on first/last.
        """
        total = getattr(self, '_total', 0)
        if value == 1 or value == total or value % 25 == 0:
            self.update(processed_count=value)
            ScanState._is_scanning_cache = (True, time.time())

    def complete(self, success: bool = True, error_message: str = ""):
        """Mark scan as complete. Uses atomic transition."""
        scan_id = getattr(self, "_scan_id", None)
        to_status = "completed" if success else "failed"
        if scan_id:
            transition_scan_state(
                scan_id=scan_id,
                from_status="running",
                to_status=to_status,
                reason=error_message or ("scan_completed" if success else "scan_failed"),
                actor=ACTOR_SYSTEM,
            )
        else:
            # No scan_id — just reset the UI state
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            execute_db("""
                UPDATE current_scan_state SET
                    status='idle', phase='', cancel_requested=0, updated_at=?
                WHERE id=1
            """, (now,))
        self._scan_id = None
        ScanState._is_scanning_cache = (False, time.time())

    def finish(self):
        """Alias for complete(success=True)."""
        self.complete(success=True)

    @property
    def is_scanning(self) -> bool:
        cached = ScanState._is_scanning_cache
        if cached is not None:
            val, ts = cached
            if (time.time() - ts) < ScanState._IS_SCANNING_TTL:
                return val
        # Phase 2: No more inline _recover_stale() — Watchdog handles it
        row = execute_db("SELECT status FROM current_scan_state WHERE id=1", fetch="one")
        result = row["status"] == "running" if row else False
        ScanState._is_scanning_cache = (result, time.time())
        return result

    @property
    def cancel_requested(self) -> bool:
        return get_scan_cancel_requested()

    @cancel_requested.setter
    def cancel_requested(self, value: bool):
        set_scan_cancel_requested(value)

    def status(self) -> dict:
        """Return current scan state as dict. O(1) read."""
        return get_scan_status()


def get_recent_scan_runs(limit: int = 10) -> list:
    """Get recent scan runs for admin dashboard."""
    rows = execute_db(
        "SELECT * FROM scan_runs ORDER BY created_at DESC LIMIT ?",
        (limit,), fetch="all"
    )
    return [dict(r) for r in rows] if rows else []

scan_state = ScanState()


# ─── Symbol Freshness Tracking (Phase 7) ───

def get_symbol_state(symbol: str) -> dict | None:
    """Get freshness state for a symbol."""
    row = execute_db("SELECT * FROM symbol_state WHERE symbol=?", (symbol,), fetch="one")
    return dict(row) if row else None


def set_symbol_state(symbol: str, **kwargs):
    """Partial update of symbol state. Only provided fields are changed."""
    allowed = {
        "last_price_update", "last_technical_update", "last_news_update",
        "last_sentiment_update", "last_financial_update", "last_deep_scan",
        "price_change_pct", "prev_score", "needs_deep_scan", "deep_scan_reason",
    }
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Upsert
    existing = execute_db("SELECT symbol FROM symbol_state WHERE symbol=?", (symbol,), fetch="one")
    if existing:
        set_clause = ", ".join(f"{k}=?" for k in updates)
        vals = list(updates.values())
        execute_db(
            f"UPDATE symbol_state SET {set_clause}, updated_at=? WHERE symbol=?",
            vals + [now, symbol]
        )
    else:
        updates["symbol"] = symbol
        updates["updated_at"] = now
        cols = ", ".join(updates.keys())
        placeholders = ", ".join("?" for _ in updates)
        execute_db(
            f"INSERT INTO symbol_state ({cols}) VALUES ({placeholders})",
            list(updates.values())
        )


def get_symbols_needing_deep_scan(limit: int = 100) -> list[str]:
    """Get symbols flagged for deep scan, prioritized by need + staleness."""
    rows = execute_db("""
        SELECT symbol FROM symbol_state
        WHERE needs_deep_scan = 1
        ORDER BY last_deep_scan ASC NULLS FIRST
        LIMIT ?
    """, (limit,), fetch="all")
    return [r["symbol"] for r in rows] if rows else []


def mark_deep_scan_needed(symbol: str, reason: str = ""):
    """Flag a symbol as needing deep scan."""
    set_symbol_state(symbol, needs_deep_scan=1, deep_scan_reason=reason)


def bulk_update_symbol_state(updates: list[dict]):
    """Batch update symbol states. Each dict must have 'symbol' key + fields to update."""
    for u in updates:
        sym = u.pop("symbol", None)
        if sym:
            set_symbol_state(sym, **u)



import hashlib
from dataclasses import dataclass, field
from pathlib import Path as _Path

_DLQ_FILE = _Path(__file__).parent / "cache" / "dead_letter_queue.jsonl"
_deferred_writes: list = []
_deferred_lock = threading.Lock()
_DLQ_MAX_RETRIES = 3


@dataclass
class DeferredBatch:
    batch_id: str = ""
    created_at: str = ""
    retry_count: int = 0
    symbols: list = field(default_factory=list)
    results: list = field(default_factory=list)
    checksum: str = ""


def _compute_checksum(results: list) -> str:
    raw = json.dumps([r.get("symbol", "") for r in results], sort_keys=True)
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def queue_deferred_write(results: list):
    """Queue failed writes for retry. Checksum-based dedup."""
    if not results:
        return
    cs = _compute_checksum(results)
    with _deferred_lock:
        # Dedup by checksum
        for existing in _deferred_writes:
            if existing.checksum == cs:
                return
        batch = DeferredBatch(
            batch_id=f"dlq_{int(time.time())}_{cs}",
            created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            retry_count=0,
            symbols=[r.get("symbol", "") for r in results],
            results=results,
            checksum=cs,
        )
        _deferred_writes.append(batch)
    log.warning("DLQ: Queued %d results (batch=%s)", len(results), batch.batch_id)


def flush_deferred_writes() -> int:
    """Retry all deferred writes. Returns count of successfully flushed."""
    flushed = 0
    with _deferred_lock:
        remaining = []
        for batch in _deferred_writes:
            try:
                # Try to save without DLQ fallback (avoid infinite recursion)
                _save_results_raw(batch.results)
                flushed += len(batch.results)
                log.info("DLQ: Flushed batch %s (%d results)", batch.batch_id, len(batch.results))
            except Exception as exc:
                batch.retry_count += 1
                if batch.retry_count >= _DLQ_MAX_RETRIES:
                    _move_to_dlq(batch)
                else:
                    remaining.append(batch)
                    log.warning("DLQ: Retry %d/%d failed for batch %s: %s",
                                batch.retry_count, _DLQ_MAX_RETRIES, batch.batch_id, exc)
        _deferred_writes.clear()
        _deferred_writes.extend(remaining)
    return flushed


def _save_results_raw(results: list):
    """Raw save without DLQ fallback (used by flush to avoid recursion)."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    scan_date = datetime.now().strftime("%Y-%m-%d")
    for r in results:
        sym = r["symbol"]
        # P0: Sanitize before serialization
        r_sanitized = sanitize_for_json(r, symbol=sym, component="_save_results_raw")
        slim = _build_slim(r_sanitized) if _DB_USE_SLIM else None
        execute_db("""
            INSERT INTO scan_results (symbol, data, score, high_conviction, sector, scan_date, updated_at, slim_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                data=excluded.data, score=excluded.score,
                high_conviction=excluded.high_conviction, sector=excluded.sector,
                scan_date=excluded.scan_date, updated_at=excluded.updated_at,
                slim_data=excluded.slim_data
        """, (sym, json.dumps(r_sanitized, default=str, allow_nan=False), r_sanitized.get("score", 0), 1 if r_sanitized.get("high_conviction") else 0,
              r_sanitized.get("sector", ""), scan_date, now, slim))


def _move_to_dlq(batch: DeferredBatch):
    """Persist failed batch to JSONL file. NEVER silently drop data."""
    try:
        entry = {
            "batch_id": batch.batch_id,
            "created_at": batch.created_at,
            "retry_count": batch.retry_count,
            "symbols": batch.symbols,
            "results": batch.results,
            "moved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(_DLQ_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        log.error("DLQ: Batch %s moved to dead-letter queue after %d retries (%d results). FILE: %s",
                   batch.batch_id, batch.retry_count, len(batch.results), _DLQ_FILE)
    except Exception as exc:
        log.critical("DLQ: FAILED to write to DLQ file: %s -- DATA MAY BE LOST for batch %s", exc, batch.batch_id)


def replay_dlq() -> int:
    """Re-attempt all DLQ entries. Returns count replayed successfully."""
    if not _DLQ_FILE.exists():
        return 0
    replayed = 0
    remaining_lines = []
    try:
        with open(_DLQ_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    _save_results_raw(entry.get("results", []))
                    replayed += len(entry.get("results", []))
                    log.info("DLQ replay: batch %s replayed OK", entry.get("batch_id", "?"))
                except Exception:
                    remaining_lines.append(line)
        # Rewrite file with only failed entries
        with open(_DLQ_FILE, "w", encoding="utf-8") as f:
            for line in remaining_lines:
                f.write(line + "\n")
    except Exception as exc:
        log.warning("DLQ replay failed: %s", exc)
    return replayed


def dlq_entry_count() -> int:
    """Count entries in DLQ file."""
    if not _DLQ_FILE.exists():
        return 0
    try:
        with open(_DLQ_FILE, "r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except Exception:
        return 0


# ═══════════════════════════════════════════════════════════════
# P0.1E: GOVERNANCE DLQ — Mandatory persistence for governance artifacts
# ═══════════════════════════════════════════════════════════════
#
# Unlike operational scan_results (which have _deferred_writes),
# governance artifacts (research_snapshots_v2, paper_orders, paper_trades,
# scan_audit, score_audit, research_advisories, recommendation_snapshots,
# paper_portfolio_daily) had ZERO sync mechanism during PG outages.
#
# This Governance DLQ ensures:
# 1. When PG drops, governance writes are appended to a local JSONL file.
# 2. When PG restores, flush_governance_writes() replays them.
# 3. Governance data is NEVER silently lost.

_GOVERNANCE_DLQ_FILE = _Path(__file__).parent / "cache" / "governance_dlq.jsonl"
_governance_dlq_lock = threading.Lock()


def queue_governance_write(query: str, params: tuple, artifact_type: str = "unknown"):
    """Queue a failed governance write to the disk-backed JSONL DLQ.

    P0.1E: This is MANDATORY for all governance artifacts.
    Called when execute_db falls back to SQLite for governance-critical inserts,
    ensuring the write will eventually reach PostgreSQL.
    """
    try:
        entry = {
            "query": query,
            "params": [_to_native(v) if v is not None else None for v in params],
            "artifact_type": artifact_type,
            "queued_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "retry_count": 0,
        }
        with _governance_dlq_lock:
            _GOVERNANCE_DLQ_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(_GOVERNANCE_DLQ_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        log.warning(
            "[GOVERNANCE DLQ] Queued %s artifact for replay | query=%.100s",
            artifact_type, query
        )
    except Exception as exc:
        log.critical(
            "[GOVERNANCE DLQ] FAILED to queue %s artifact — DATA MAY BE LOST: %s | query=%.100s",
            artifact_type, exc, query
        )


def flush_governance_writes() -> int:
    """Replay all governance DLQ entries to PostgreSQL.

    P0.1E: Called periodically (e.g., by watchdog or startup).
    Returns count of successfully replayed entries.
    """
    if not _GOVERNANCE_DLQ_FILE.exists():
        return 0

    if not is_postgresql() or pg_cooldown_active():
        log.info("[GOVERNANCE DLQ] PG still unavailable, skipping flush")
        return 0

    replayed = 0
    remaining_lines = []

    try:
        with _governance_dlq_lock:
            with open(_GOVERNANCE_DLQ_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()

            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    query = entry["query"]
                    params = tuple(entry.get("params", []))
                    # Execute directly on PG (no fallback — we ARE the fallback)
                    pool = _get_pg_pool()
                    if pool:
                        conn = None
                        try:
                            from psycopg2.extras import RealDictCursor
                            conn = pool.getconn()
                            conn.autocommit = True
                            query_pg = query.replace("?", "%s")
                            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                                cur.execute(query_pg, params)
                            replayed += 1
                            log.info(
                                "[GOVERNANCE DLQ] Replayed %s artifact OK | query=%.100s",
                                entry.get("artifact_type", "?"), query
                            )
                        finally:
                            if conn:
                                try:
                                    pool.putconn(conn)
                                except Exception:
                                    pass
                    else:
                        remaining_lines.append(line)
                except Exception as exc:
                    entry_parsed = {}
                    try:
                        entry_parsed = json.loads(line)
                    except Exception:
                        pass
                    retry_count = entry_parsed.get("retry_count", 0) + 1
                    if retry_count >= _DLQ_MAX_RETRIES:
                        log.error(
                            "[GOVERNANCE DLQ] Entry permanently failed after %d retries: %s | query=%.100s",
                            retry_count, exc, entry_parsed.get("query", "?")[:100]
                        )
                    else:
                        entry_parsed["retry_count"] = retry_count
                        remaining_lines.append(json.dumps(entry_parsed, default=str))

            # Rewrite file with only failed entries
            with open(_GOVERNANCE_DLQ_FILE, "w", encoding="utf-8") as f:
                for rl in remaining_lines:
                    f.write(rl + "\n")

    except Exception as exc:
        log.warning("[GOVERNANCE DLQ] Flush failed: %s", exc)

    if replayed:
        log.info("[GOVERNANCE DLQ] Flushed %d governance artifacts to PG", replayed)
    return replayed


def governance_dlq_count() -> int:
    """Count pending entries in governance DLQ file."""
    if not _GOVERNANCE_DLQ_FILE.exists():
        return 0
    try:
        with open(_GOVERNANCE_DLQ_FILE, "r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except Exception:
        return 0


# ─── Scan Results ───

def _chunks(lst, n):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]

def _bulk_upsert_pg(table_name, sql_template, rows, cursor):
    """Execute chunked bulk UPSERT via execute_values with per-table timing.

    Sanitizes all values to prevent numpy scalars (np.float64, np.int64 etc.)
    from reaching psycopg2, which would render them as 'np.float64(...)' in SQL
    and cause 'schema "np" does not exist' errors.
    """
    from psycopg2.extras import execute_values

    def _sanitize_value(v):
        """Convert numpy scalars to Python native types."""
        if v is None:
            return None
        # Fast path for common Python types
        if isinstance(v, (int, float, str, bool)):
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                return None
            return v
        # numpy scalar → Python native
        if hasattr(v, 'item'):
            try:
                native = v.item()
                if isinstance(native, float) and (math.isnan(native) or math.isinf(native)):
                    return None
                return native
            except Exception:
                return float(v) if hasattr(v, '__float__') else str(v)
        return v

    def _sanitize_row(row):
        return tuple(_sanitize_value(v) for v in row)

    total_rows = 0
    for chunk in _chunks(rows, DB_BATCH_SIZE):
        sanitized_chunk = [_sanitize_row(r) for r in chunk]
        t0 = time.perf_counter()
        execute_values(cursor, sql_template, sanitized_chunk, page_size=DB_BATCH_SIZE)
        dur = (time.perf_counter() - t0) * 1000
        total_rows += len(chunk)
        log.info("[UPSERT] table=%s duration=%sms rows=%s", table_name, round(dur), len(chunk))
    return total_rows

@timed("db_write_batch")
def save_results(results: list[dict], scan_id: str = 'legacy_fallback', meta: dict = None):
    """Save scan results to DB and populate normalized tables.

    Deploy A.1: Bulk UPSERT rewrite.
    - PostgreSQL: uses execute_values() with chunked batches (DB_BATCH_SIZE).
    - SQLite fallback: preserved as emergency parachute.
    - Warm staleness guard: single query pre-loads deep scan symbols.
    - Per-table timing: [UPSERT] logs for each table.
    - Transaction timing: [SAVE_RESULTS] total KPI.
    - Scan metrics: [SCAN] processed/saved/skipped.
    - Pool health: [DB POOL] active/idle/waiting.
    """
    save_start = time.perf_counter()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    scan_date = datetime.now().strftime("%Y-%m-%d")

    # ── Warm Staleness Guard (1 query) ──
    deep_scanned_symbols = set()
    try:
        if is_postgresql() and not pg_cooldown_active():
            rows = execute_db(
                "SELECT symbol FROM scan_results_v2 WHERE scan_date = ? AND (data->>'scan_mode') = 'deep'",
                (scan_date,), fetch="all"
            )
        else:
            rows = execute_db(
                "SELECT symbol FROM scan_results_v2 WHERE scan_date = ? AND json_extract(data, '$.scan_mode') = 'deep'",
                (scan_date,), fetch="all"
            )
        deep_scanned_symbols = {row["symbol"] for row in (rows or [])}
    except Exception:
        pass  # proceed without guard if check fails

    # ── Filter results: skip fast overwrites of deep scans ──
    to_save = []
    skipped = 0
    for r in results:
        sym = r.get("symbol")
        if not sym:
            log.warning("[SAVE_RESULTS] Skipping result with missing 'symbol' key: %s", str(r)[:100])
            skipped += 1
            continue
        new_mode = r.get("scan_mode", "fast")
        if new_mode == "fast" and sym in deep_scanned_symbols:
            log.debug("Staleness guard: skipping fast overwrite of deep scan for %s", sym)
            skipped += 1
            continue
        to_save.append(r)

    if not to_save:
        log.info("[SCAN] processed=%s saved=0 skipped=%s duration=%.1fs",
                 len(results), skipped,
                 (time.perf_counter() - save_start))
        if meta:
            for k, v in meta.items():
                set_meta(k, v)
        return

    # ── RE-3 P2 (approach A): project Recommendation Object trade levels into the persisted
    # results so consumers show/act on RO values. Gated (RE2_RO_PROJECT, default OFF),
    # exception-isolated (falls back to legacy values), trade-levels-only (scoring untouched).
    # Single point — covers every save_results caller (active/deep/marketaux/custom/sequential).
    try:
        import recommendation_engine
        if recommendation_engine.RO_PROJECT_ENABLED:
            # scoring_v1 rows carry their OWN trade levels (levels.py: 2*ATR stop, R-multiple
            # targets). The legacy RO projection imposes legacy 1.5R targets and would
            # overwrite them — so skip projection for scoring_v1; legacy rows still project.
            # scoring_v1 AND legacy_cleaned carry their OWN trade levels (their own levels module:
            # v1 = 2*ATR/R-multiple; legacy_cleaned = structure swing-low/resistance + varied R:R).
            # The legacy RO projection imposes legacy 1.5R targets and would overwrite them, so skip
            # projection for BOTH engines; only legacy rows still project (unchanged behavior).
            _keep_own_levels = {"scoring_v1", "legacy_cleaned"}
            to_save = [
                r if (r.get("symbol") is None
                      or _canon_model_version(r.get("model_version")) in _keep_own_levels)
                else recommendation_engine.projection.project_result_copy(r, scan_id, now)
                for r in to_save
            ]
    except Exception as _proj_exc:
        log.warning("[RE3-P2] RO projection skipped (non-fatal, writing legacy): %s", _proj_exc)

    # ── Thesis Locking Integration ──
    try:
        init_recommendation_locks()
        for r in to_save:
            sym = r.get("symbol", "")
            if not sym:
                continue
            price = r.get("price", 0)
            # Try to lock thesis (first analysis)
            was_locked = lock_thesis(sym, r)
            if was_locked:
                log.info("[THESIS] Locked new thesis for %s at ₹%.2f", sym, price)
            else:
                # Already has active thesis — append rescan update
                rsi_val = r.get("rsi", 0)
                score_val = r.get("score", 0)
                append_thesis_update(sym, f"Rescan: CMP ₹{price:.2f}, Score {score_val:.0f}, RSI {rsi_val:.1f}")
                # Check if SL or Target hit
                check_thesis_completion(sym, price)
    except Exception as exc:
        log.warning("[THESIS] Thesis lock integration error: %s", exc)

    # ── Try PostgreSQL bulk path ──
    if is_postgresql() and not pg_cooldown_active():
        pool = _get_pg_pool()
        if pool:
            conn = None
            try:
                conn = pool.getconn()
                conn.autocommit = False  # explicit transaction for atomicity
                cursor = conn.cursor()

                # ── Prepare batch data ──
                scan_results_rows = []
                score_history_rows = []
                stocks_rows = []
                sentiment_rows = []
                technical_rows = []
                fundamentals_rows = []
                final_scores_rows = []
                all_news_symbols = []
                news_rows = []

                def _sf(v):
                    if v is None: return None
                    try:
                        f = float(v)
                        import math
                        if math.isnan(f) or math.isinf(f): return None
                        return f
                    except (ValueError, TypeError):
                        return None

                rejected_symbols = []
                for r in to_save:
                    sym = r["symbol"]

                    # P0: Sanitize the full result dict before serialization
                    try:
                        r_sanitized = sanitize_for_json(r, symbol=sym, scan_id=get_meta("scan_id"), component="save_results")
                        data_json = json.dumps(r_sanitized, default=str, allow_nan=False)
                        increment_mem_counter("json_processed_count")
                    except (ValueError, OverflowError) as json_err:
                        log.error("[JSON_REJECTED] symbol=%s error=%s — skipping from batch", sym, json_err)
                        increment_mem_counter("json_rejected_count")
                        rejected_symbols.append(sym)
                        continue

                    f = r_sanitized.get("fundamentals", {})

                    # 1. scan_results_v2 (+ slim_data for Phase 1C)
                    slim = _build_slim(r_sanitized) if _DB_USE_SLIM else None
                    scan_results_rows.append((
                        scan_id, sym, data_json, _sf(r_sanitized.get("score")),
                        1 if r_sanitized.get("high_conviction") else 0,
                        r_sanitized.get("sector", ""), scan_date, now, slim
                    ))

                    # 2. score_history
                    score_history_rows.append((
                        sym, _sf(r_sanitized.get("score")), _sf(r_sanitized.get("price")),
                        _sf(r_sanitized.get("rsi")), scan_date
                    ))

                    # 3. stocks
                    stocks_rows.append((
                        sym, r_sanitized.get("name", sym), r_sanitized.get("sector", "Other"),
                        f.get("industry", ""), now
                    ))

                    # 4. news — collect symbols and articles
                    all_news_symbols.append(sym)
                    gdelt_data = r_sanitized.get("gdelt", {})
                    articles = list(gdelt_data.get("articles", []))
                    news_s = r_sanitized.get("news_sentiment", {})
                    for item in news_s.get("items", []):
                        if item.get("source") == "marketaux":
                            articles.append({
                                "title": item.get("title", ""),
                                "score": item.get("score", 0.0),
                                "source": "marketaux",
                                "age_h": 1.0
                            })
                    for art in articles[:10]:
                        news_rows.append((
                            sym, art.get("title", ""),
                            art.get("url", ""), art.get("source", "GDELT"),
                            _sf(art.get("age_h", 12.0)), _sf(art.get("score", 0.0)), now
                        ))

                    # 5. sentiment_scores
                    sentiment_rows.append((
                        sym, scan_date, _sf(gdelt_data.get("sentiment", 0.0)),
                        _sf(gdelt_data.get("spike", 1.0)), _sf(gdelt_data.get("freshness", 0.0)),
                        _sf(r_sanitized.get("news_sentiment_score", 0.0)), now
                    ))

                    # 6. technical_indicators
                    technical_rows.append((
                        sym, scan_date, _sf(r_sanitized.get("rsi")), _sf(r_sanitized.get("adx")),
                        r_sanitized.get("macd_signal"), _sf(r_sanitized.get("volume_ratio")),
                        _sf(r_sanitized.get("atr_pct")), _sf(r_sanitized.get("stoch_k")), _sf(r_sanitized.get("stoch_d")),
                        _sf(r_sanitized.get("pct_1w")), _sf(r_sanitized.get("pct_2w")), _sf(r_sanitized.get("pct_1m")),
                        _sf(r_sanitized.get("bb_position")), _sf(r_sanitized.get("dist_from_high")),
                        _sf(r_sanitized.get("rs_vs_nifty")), _sf(r_sanitized.get("vwap_position")),
                        True if r_sanitized.get("is_breakout") else False,
                        True if r_sanitized.get("vp_divergence") else False,
                        r_sanitized.get("weekly_trend", "flat"),
                        True if r_sanitized.get("below_ema200") else False,
                        _sf(r_sanitized.get("high_52w")), _sf(r_sanitized.get("low_52w")),
                        _sf(r_sanitized.get("pullback_pct")), now
                    ))

                    # 7. fundamentals
                    fundamentals_rows.append((
                        sym, _sf(f.get("pe")), _sf(f.get("pb")), _sf(f.get("fwd_pe")),
                        _sf(f.get("roe")), _sf(f.get("roa")), _sf(f.get("revenue_growth")),
                        _sf(f.get("earnings_growth")), _sf(f.get("debt_to_equity")),
                        _sf(f.get("promoter_pct")), _sf(f.get("market_cap")),
                        _sf(f.get("free_cash_flow")), _sf(f.get("total_revenue")),
                        _sf(f.get("capex")), _sf(f.get("eps_fwd")), _sf(f.get("eps_trail")),
                        _sf(f.get("fund_score", 0)), now
                    ))

                    # 8. final_scores
                    final_scores_rows.append((
                        sym, scan_date, _sf(r_sanitized.get("news_sentiment_score", 0.0)),
                        _sf(r_sanitized.get("news_spike_score", 0.0)), _sf(r_sanitized.get("technical_score", 0.0)),
                        _sf(r_sanitized.get("fundamental_score", 0.0)), _sf(r_sanitized.get("macro_score", 0.0)),
                        _sf(r_sanitized.get("marketaux_catalyst_score", 0.0)),
                        _sf(r_sanitized.get("score", 0)), r_sanitized.get("grade", ""),
                        True if r_sanitized.get("high_conviction") else False,
                        True if r_sanitized.get("bear_play") else False,
                        True if r_sanitized.get("is_golden") else False, now
                    ))

                # P0: Dual rejection threshold — abort if >=25 AND >25% rejected
                if rejected_symbols:
                    total = len(to_save)
                    rejected_count = len(rejected_symbols)
                    log.warning("[JSON_REJECTION_SUMMARY] rejected=%d/%d symbols=%s",
                                rejected_count, total, rejected_symbols[:10])
                    if rejected_count >= 25 and rejected_count / total > 0.25:
                        log.critical("[JSON_REJECTION_ABORT] Rejecting entire batch: %d/%d (%.0f%%) symbols had invalid JSON",
                                     rejected_count, total, (rejected_count / total) * 100)
                        raise ValueError(f"JSON rejection threshold exceeded: {rejected_count}/{total}")

                # ── Execute bulk UPSERTs ──

                # 1. scan_results_v2 (+ slim_data)
                _bulk_upsert_pg("scan_results_v2", """
                    INSERT INTO scan_results_v2 (scan_id, symbol, data, score, high_conviction, sector, scan_date, updated_at, slim_data)
                    VALUES %s
                    ON CONFLICT(scan_id, symbol) DO UPDATE SET
                        data=EXCLUDED.data, score=EXCLUDED.score,
                        high_conviction=EXCLUDED.high_conviction, sector=EXCLUDED.sector,
                        scan_date=EXCLUDED.scan_date, updated_at=EXCLUDED.updated_at,
                        slim_data=EXCLUDED.slim_data
                """, scan_results_rows, cursor)

                # 2. score_history
                _bulk_upsert_pg("score_history", """
                    INSERT INTO score_history (symbol, score, price, rsi, scan_date)
                    VALUES %s
                    ON CONFLICT(symbol, scan_date) DO UPDATE SET
                        score=EXCLUDED.score, price=EXCLUDED.price, rsi=EXCLUDED.rsi
                """, score_history_rows, cursor)

                # 3. stocks
                _bulk_upsert_pg("stocks", """
                    INSERT INTO stocks (symbol, name, sector, industry, updated_at)
                    VALUES %s
                    ON CONFLICT(symbol) DO UPDATE SET
                        name=EXCLUDED.name, sector=EXCLUDED.sector,
                        industry=EXCLUDED.industry, updated_at=EXCLUDED.updated_at
                """, stocks_rows, cursor)

                # 4. news_articles — batch DELETE + bulk INSERT
                if all_news_symbols:
                    t0 = time.perf_counter()
                    placeholders = ",".join(["%s"] * len(all_news_symbols))
                    cursor.execute(
                        f"DELETE FROM news_articles WHERE symbol IN ({placeholders})",
                        all_news_symbols
                    )
                    dur = (time.perf_counter() - t0) * 1000
                    log.info("[UPSERT] table=news_articles_delete duration=%sms rows=%s",
                             round(dur), len(all_news_symbols))

                if news_rows:
                    _bulk_upsert_pg("news_articles", """
                        INSERT INTO news_articles (symbol, title, url, source, age_hours, raw_score, scanned_at)
                        VALUES %s
                    """, news_rows, cursor)

                # 5. sentiment_scores
                _bulk_upsert_pg("sentiment_scores", """
                    INSERT INTO sentiment_scores (symbol, scan_date, gdelt_sentiment, gdelt_spike, gdelt_freshness, final_sentiment_score, updated_at)
                    VALUES %s
                    ON CONFLICT(symbol, scan_date) DO UPDATE SET
                        gdelt_sentiment=EXCLUDED.gdelt_sentiment, gdelt_spike=EXCLUDED.gdelt_spike,
                        gdelt_freshness=EXCLUDED.gdelt_freshness, final_sentiment_score=EXCLUDED.final_sentiment_score,
                        updated_at=EXCLUDED.updated_at
                """, sentiment_rows, cursor)

                # 6. technical_indicators
                _bulk_upsert_pg("technical_indicators", """
                    INSERT INTO technical_indicators (
                        symbol, scan_date, rsi, adx, macd_signal, volume_ratio, atr_pct, stoch_k, stoch_d,
                        pct_1w, pct_2w, pct_1m, bb_position, dist_from_high, rs_vs_nifty, vwap_position,
                        is_breakout, vp_divergence, weekly_trend, below_ema200, high_52w, low_52w, pullback_pct, updated_at
                    ) VALUES %s
                    ON CONFLICT(symbol, scan_date) DO UPDATE SET
                        rsi=EXCLUDED.rsi, adx=EXCLUDED.adx, macd_signal=EXCLUDED.macd_signal,
                        volume_ratio=EXCLUDED.volume_ratio, atr_pct=EXCLUDED.atr_pct, stoch_k=EXCLUDED.stoch_k,
                        stoch_d=EXCLUDED.stoch_d, pct_1w=EXCLUDED.pct_1w, pct_2w=EXCLUDED.pct_2w, pct_1m=EXCLUDED.pct_1m,
                        bb_position=EXCLUDED.bb_position, dist_from_high=EXCLUDED.dist_from_high, rs_vs_nifty=EXCLUDED.rs_vs_nifty,
                        vwap_position=EXCLUDED.vwap_position, is_breakout=EXCLUDED.is_breakout, vp_divergence=EXCLUDED.vp_divergence,
                        weekly_trend=EXCLUDED.weekly_trend, below_ema200=EXCLUDED.below_ema200, high_52w=EXCLUDED.high_52w,
                        low_52w=EXCLUDED.low_52w, pullback_pct=EXCLUDED.pullback_pct, updated_at=EXCLUDED.updated_at
                """, technical_rows, cursor)

                # 7. fundamentals
                _bulk_upsert_pg("fundamentals", """
                    INSERT INTO fundamentals (
                        symbol, pe, pb, fwd_pe, roe, roa, revenue_growth, earnings_growth,
                        debt_to_equity, promoter_pct, market_cap, free_cash_flow, total_revenue,
                        capex, eps_fwd, eps_trail, fund_score, updated_at
                    ) VALUES %s
                    ON CONFLICT(symbol) DO UPDATE SET
                        pe=EXCLUDED.pe, pb=EXCLUDED.pb, fwd_pe=EXCLUDED.fwd_pe, roe=EXCLUDED.roe, roa=EXCLUDED.roa,
                        revenue_growth=EXCLUDED.revenue_growth, earnings_growth=EXCLUDED.earnings_growth,
                        debt_to_equity=EXCLUDED.debt_to_equity, promoter_pct=EXCLUDED.promoter_pct,
                        market_cap=EXCLUDED.market_cap, free_cash_flow=EXCLUDED.free_cash_flow,
                        total_revenue=EXCLUDED.total_revenue, capex=EXCLUDED.capex, eps_fwd=EXCLUDED.eps_fwd,
                        eps_trail=EXCLUDED.eps_trail, fund_score=EXCLUDED.fund_score, updated_at=EXCLUDED.updated_at
                """, fundamentals_rows, cursor)

                # 8. final_scores
                _bulk_upsert_pg("final_scores", """
                    INSERT INTO final_scores (
                        symbol, scan_date, news_sentiment_score, news_spike_score, technical_score,
                        fundamental_score, macro_score, marketaux_score, final_score, grade,
                        high_conviction, bear_play, is_golden, updated_at
                    ) VALUES %s
                    ON CONFLICT(symbol, scan_date) DO UPDATE SET
                        news_sentiment_score=EXCLUDED.news_sentiment_score, news_spike_score=EXCLUDED.news_spike_score,
                        technical_score=EXCLUDED.technical_score, fundamental_score=EXCLUDED.fundamental_score,
                        macro_score=EXCLUDED.macro_score, marketaux_score=EXCLUDED.marketaux_score,
                        final_score=EXCLUDED.final_score, grade=EXCLUDED.grade,
                        high_conviction=EXCLUDED.high_conviction, bear_play=EXCLUDED.bear_play,
                        is_golden=EXCLUDED.is_golden, updated_at=EXCLUDED.updated_at
                """, final_scores_rows, cursor)

                # ── Commit transaction ──
                conn.commit()
                saved_count = len(to_save)

                # ── Transaction timing ──
                save_duration = (time.perf_counter() - save_start) * 1000
                log.info("[SAVE_RESULTS] rows=%s duration=%sms", saved_count, round(save_duration))

                # ── Scan metrics ──
                log.info("[SCAN] processed=%s saved=%s skipped=%s duration=%.1fs",
                         len(results), saved_count, skipped,
                         save_duration / 1000)

                # ── Pool health ──
                log_pool_health()

                # P0: Flush metrics after successful PG commit
                flush_metrics_to_db()

                return  # PG bulk path succeeded

            except ValueError as json_exc:
                # P0: JSON rejection threshold or serialization error — do NOT fall back to SQLite
                # The data itself is bad; SQLite won't fix it.
                log.error("[PG_BULK_JSON_ERROR] %s — NOT falling back to SQLite (data issue, not connection)", json_exc)
                if conn:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                # Do NOT fall through — return after logging
                save_duration = (time.perf_counter() - save_start) * 1000
                log.info("[SAVE_RESULTS] rows=0 duration=%sms (json_rejection)", round(save_duration))
                if meta:
                    for k, v in meta.items():
                        set_meta(k, v)
                return

            except Exception as exc:
                log.error("[SQLITE_FALLBACK_TRIGGERED] reason=pg_bulk_upsert_failed error=%s", exc)
                increment_mem_counter("sqlite_fallback_count")
                if conn:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                # Fall through to SQLite per-row path
            finally:
                if conn:
                    try:
                        conn.autocommit = True  # restore default
                        pool.putconn(conn)
                    except Exception:
                        pass

    # ── SQLite fallback path (per-row, kept as emergency parachute) ──
    # P0: Use single transaction for atomicity and rollback protection
    saved_count = 0
    sqlite_conn = None
    try:
        sqlite_conn = sqlite3.connect(str(DB_PATH))
        sqlite_conn.execute("PRAGMA journal_mode=WAL")
        sqlite_conn.execute("BEGIN")

        for r in to_save:
            sym = r["symbol"]
            try:
                # P0: Sanitize before SQLite serialization too
                r_sanitized = sanitize_for_json(r, symbol=sym, component="sqlite_fallback")
                data_json = json.dumps(r_sanitized, default=str, allow_nan=False)

                # 1. scan_results_v2 (+ slim_data)
                slim = _build_slim(r_sanitized) if _DB_USE_SLIM else None
                sqlite_conn.execute("""
                    INSERT INTO scan_results_v2 (scan_id, symbol, data, score, high_conviction, sector, scan_date, updated_at, slim_data)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(scan_id, symbol) DO UPDATE SET
                        data=excluded.data, score=excluded.score,
                        high_conviction=excluded.high_conviction, sector=excluded.sector,
                        scan_date=excluded.scan_date, updated_at=excluded.updated_at,
                        slim_data=excluded.slim_data
                """, (scan_id, sym, data_json, r_sanitized.get("score", 0), 1 if r_sanitized.get("high_conviction") else 0, r_sanitized.get("sector", ""), scan_date, now, slim))

                # 2. score_history
                sqlite_conn.execute("""
                    INSERT INTO score_history (symbol, score, price, rsi, scan_date)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, scan_date) DO UPDATE SET
                        score=excluded.score, price=excluded.price, rsi=excluded.rsi
                """, (sym, r_sanitized.get("score", 0), r_sanitized.get("price", 0.0), r_sanitized.get("rsi"), scan_date))

                # 3. stocks
                f = r_sanitized.get("fundamentals", {})
                sqlite_conn.execute("""
                    INSERT INTO stocks (symbol, name, sector, industry, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        name=excluded.name, sector=excluded.sector, industry=excluded.industry, updated_at=excluded.updated_at
                """, (sym, r_sanitized.get("name", sym), r_sanitized.get("sector", "Other"), f.get("industry", ""), now))

                # 4. news_articles
                sqlite_conn.execute("DELETE FROM news_articles WHERE symbol=?", (sym,))
                gdelt_data = r_sanitized.get("gdelt", {})
                articles = list(gdelt_data.get("articles", []))
                news_s = r_sanitized.get("news_sentiment", {})
                for item in news_s.get("items", []):
                    if item.get("source") == "marketaux":
                        articles.append({
                            "title": item.get("title", ""),
                            "score": item.get("score", 0.0),
                            "source": "marketaux",
                            "age_h": 1.0
                        })
                for art in articles[:10]:
                    sqlite_conn.execute("""
                        INSERT INTO news_articles (symbol, title, url, source, age_hours, raw_score, scanned_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (sym, art.get("title", ""), art.get("url", ""), art.get("source", "GDELT"), art.get("age_h", 12.0), art.get("score", 0.0), now))

                # 5. sentiment_scores
                sqlite_conn.execute("""
                    INSERT INTO sentiment_scores (symbol, scan_date, gdelt_sentiment, gdelt_spike, gdelt_freshness, final_sentiment_score, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, scan_date) DO UPDATE SET
                        gdelt_sentiment=excluded.gdelt_sentiment, gdelt_spike=excluded.gdelt_spike,
                        gdelt_freshness=excluded.gdelt_freshness, final_sentiment_score=excluded.final_sentiment_score,
                        updated_at=excluded.updated_at
                """, (sym, scan_date, gdelt_data.get("sentiment", 0.0), gdelt_data.get("spike", 1.0), gdelt_data.get("freshness", 0.0), r_sanitized.get("news_sentiment_score", 0.0), now))

                # 6. technical_indicators
                sqlite_conn.execute("""
                    INSERT INTO technical_indicators (
                        symbol, scan_date, rsi, adx, macd_signal, volume_ratio, atr_pct, stoch_k, stoch_d,
                        pct_1w, pct_2w, pct_1m, bb_position, dist_from_high, rs_vs_nifty, vwap_position,
                        is_breakout, vp_divergence, weekly_trend, below_ema200, high_52w, low_52w, pullback_pct, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, scan_date) DO UPDATE SET
                        rsi=excluded.rsi, adx=excluded.adx, macd_signal=excluded.macd_signal,
                        volume_ratio=excluded.volume_ratio, atr_pct=excluded.atr_pct, stoch_k=excluded.stoch_k,
                        stoch_d=excluded.stoch_d, pct_1w=excluded.pct_1w, pct_2w=excluded.pct_2w, pct_1m=excluded.pct_1m,
                        bb_position=excluded.bb_position, dist_from_high=excluded.dist_from_high, rs_vs_nifty=excluded.rs_vs_nifty,
                        vwap_position=excluded.vwap_position, is_breakout=excluded.is_breakout, vp_divergence=excluded.vp_divergence,
                        weekly_trend=excluded.weekly_trend, below_ema200=excluded.below_ema200, high_52w=excluded.high_52w,
                        low_52w=excluded.low_52w, pullback_pct=excluded.pullback_pct, updated_at=excluded.updated_at
                """, (
                    sym, scan_date, r_sanitized.get("rsi"), r_sanitized.get("adx"), r_sanitized.get("macd_signal"), r_sanitized.get("volume_ratio"), r_sanitized.get("atr_pct"),
                    r_sanitized.get("stoch_k"), r_sanitized.get("stoch_d"), r_sanitized.get("pct_1w"), r_sanitized.get("pct_2w"), r_sanitized.get("pct_1m"), r_sanitized.get("bb_position"),
                    r_sanitized.get("dist_from_high"), r_sanitized.get("rs_vs_nifty"), r_sanitized.get("vwap_position"),
                    True if r_sanitized.get("is_breakout") else False, True if r_sanitized.get("vp_divergence") else False, r_sanitized.get("weekly_trend", "flat"),
                    True if r_sanitized.get("below_ema200") else False, r_sanitized.get("high_52w"), r_sanitized.get("low_52w"), r_sanitized.get("pullback_pct"), now
                ))

                # 7. fundamentals
                f = r_sanitized.get("fundamentals", {})
                sqlite_conn.execute("""
                    INSERT INTO fundamentals (
                        symbol, pe, pb, fwd_pe, roe, roa, revenue_growth, earnings_growth,
                        debt_to_equity, promoter_pct, market_cap, free_cash_flow, total_revenue, capex, eps_fwd, eps_trail, fund_score, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        pe=excluded.pe, pb=excluded.pb, fwd_pe=excluded.fwd_pe, roe=excluded.roe, roa=excluded.roa,
                        revenue_growth=excluded.revenue_growth, earnings_growth=excluded.earnings_growth,
                        debt_to_equity=excluded.debt_to_equity, promoter_pct=excluded.promoter_pct,
                        market_cap=excluded.market_cap, free_cash_flow=excluded.free_cash_flow,
                        total_revenue=excluded.total_revenue, capex=excluded.capex, eps_fwd=excluded.eps_fwd,
                        eps_trail=excluded.eps_trail, fund_score=excluded.fund_score, updated_at=excluded.updated_at
                """, (
                    sym, f.get("pe"), f.get("pb"), f.get("fwd_pe"), f.get("roe"), f.get("roa"), f.get("revenue_growth"),
                    f.get("earnings_growth"), f.get("debt_to_equity"), f.get("promoter_pct"), f.get("market_cap"),
                    f.get("free_cash_flow"), f.get("total_revenue"), f.get("capex"), f.get("eps_fwd"), f.get("eps_trail"),
                    f.get("fund_score", 0), now
                ))

                # 8. final_scores
                sqlite_conn.execute("""
                    INSERT INTO final_scores (
                        symbol, scan_date, news_sentiment_score, news_spike_score, technical_score,
                        fundamental_score, macro_score, marketaux_score, final_score, grade, high_conviction, bear_play, is_golden, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, scan_date) DO UPDATE SET
                        news_sentiment_score=excluded.news_sentiment_score, news_spike_score=excluded.news_spike_score,
                        technical_score=excluded.technical_score, fundamental_score=excluded.fundamental_score,
                        macro_score=excluded.macro_score, marketaux_score=excluded.marketaux_score,
                        final_score=excluded.final_score, grade=excluded.grade,
                        high_conviction=excluded.high_conviction, bear_play=excluded.bear_play,
                        is_golden=excluded.is_golden, updated_at=excluded.updated_at
                """, (
                    sym, scan_date, r_sanitized.get("news_sentiment_score", 0.0), r_sanitized.get("news_spike_score", 0.0), r_sanitized.get("technical_score", 0.0),
                    r_sanitized.get("fundamental_score", 0.0), r_sanitized.get("macro_score", 0.0), r_sanitized.get("marketaux_catalyst_score", 0.0),
                    r_sanitized.get("score", 0), r_sanitized.get("grade", ""), True if r_sanitized.get("high_conviction") else False, True if r_sanitized.get("bear_play") else False,
                    True if r_sanitized.get("is_golden") else False, now
                ))

                saved_count += 1
            except Exception as exc:
                log.warning("SQLite write failed for %s: %s -- queueing to DLQ", sym, exc)
                queue_deferred_write([r])

        # P0: Commit entire batch atomically
        sqlite_conn.execute("COMMIT")
    except Exception as txn_exc:
        log.error("[SQLITE_FALLBACK_TXN_FAILED] Rolling back entire batch: %s", txn_exc)
        if sqlite_conn:
            try:
                sqlite_conn.execute("ROLLBACK")
            except Exception:
                pass
        saved_count = 0
    finally:
        if sqlite_conn:
            try:
                sqlite_conn.close()
            except Exception:
                pass

    # ── Transaction timing (SQLite path) ──
    save_duration = (time.perf_counter() - save_start) * 1000
    log.info("[SAVE_RESULTS] rows=%s duration=%sms (sqlite_fallback)", saved_count, round(save_duration))
    log.info("[SCAN] processed=%s saved=%s skipped=%s duration=%.1fs",
             len(results), saved_count, skipped, save_duration / 1000)

    if meta:
        for k, v in meta.items():
            set_meta(k, v)

    log_slim_coverage()


def save_macro_events(events: list):
    """Save Forex Factory macro events into the macro_events table."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Clean old events
    execute_db("DELETE FROM macro_events")
    for ev in events:
        execute_db("""
            INSERT INTO macro_events (title, country, impact, actual, forecast, surprise_dir, score, event_date, event_time, scanned_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (ev.get("title"), ev.get("country"), ev.get("impact"), str(ev.get("actual", "")), str(ev.get("forecast", "")), ev.get("surprise_dir"), ev.get("score", 0.0), ev.get("date"), ev.get("time"), now))
    log.info("Saved %d macro events to database", len(events))

def _ensure_trade_populated(r):
    if not r:
        return r
    if "trade" in r and r["trade"] and "entry_low" in r["trade"]:
        return r
    try:
        current_price = r.get("price")
        if not current_price:
            current_price = r.get("close") or r.get("ltp") or r.get("last_price")
        if not current_price:
            return r
        current_price = float(current_price)
        if current_price <= 0:
            return r

        atr_pct = r.get("atr_pct", 2.0)
        if atr_pct is None or atr_pct <= 0:
            atr_pct = 2.0
        atr_pct = float(atr_pct)

        atr_val = (atr_pct * current_price) / 100
        sr = r.get("support_resistance", {})
        s1 = sr.get("s1")
        if s1 is not None: s1 = float(s1)
        s2 = sr.get("s2")
        if s2 is not None: s2 = float(s2)
        pivot = sr.get("pivot")
        if pivot is not None: pivot = float(pivot)
        fib_s = r.get("fib_support")
        if fib_s is not None: fib_s = float(fib_s)

        atr_stop = current_price - (2.0 * atr_val)
        sl_candidates = [atr_stop]
        if s1 and s1 < current_price and (current_price - s1) / current_price <= 0.07:
            sl_candidates.append(s1 * 0.99)
        if fib_s and fib_s < current_price and (current_price - fib_s) / current_price <= 0.07:
            sl_candidates.append(fib_s * 0.99)
        valid_supports = [s for s in sl_candidates if s < current_price]
        if valid_supports:
            structural_sl = max(valid_supports)
            if current_price - structural_sl < current_price * 0.015:
                structural_sl = min(valid_supports)
        else:
            structural_sl = atr_stop

        strict_sl = round(structural_sl, 2)
        if strict_sl >= current_price or strict_sl <= 0:
            strict_sl = round(current_price * 0.97, 2)

        weekly_trend = r.get("weekly_trend", "flat")
        adx = r.get("adx")
        if adx is None:
            adx = 20.0
        else:
            adx = float(adx)

        macd_signal = r.get("macd_signal", "Bearish")
        base_mult = 2.0
        if weekly_trend == "up" and macd_signal == "Bullish":
            base_mult = 3.0
        elif weekly_trend == "down":
            base_mult = 1.8
        if adx > 25:
            base_mult += 0.5

        risk_distance = current_price - strict_sl
        if risk_distance <= 0:
            risk_distance = current_price * 0.03
            strict_sl = round(current_price - risk_distance, 2)

        default_target = current_price + (base_mult * risk_distance)
        target_candidates = [default_target]
        r1 = sr.get("r1")
        if r1 is not None: r1 = float(r1)
        r2 = sr.get("r2")
        if r2 is not None: r2 = float(r2)
        fib_r = r.get("fib_resistance")
        if fib_r is not None: fib_r = float(fib_r)

        if fib_r and fib_r > current_price * 1.02:
            target_candidates.append(fib_r)
        if r1 and r1 > current_price * 1.02:
            target_candidates.append(r1)
        realistic = [t for t in target_candidates if t <= default_target * 1.5]
        target_price = max(realistic) if realistic else default_target
        if target_price <= current_price:
            target_price = default_target

        risk_dist = current_price - strict_sl
        risk_reward = round((target_price - current_price) / risk_dist, 1) if risk_dist > 0 else 0.0
        target1 = round(r1 if (r1 and r1 > current_price) else target_price, 2)
        target2 = round(r2 if (r2 and r2 > target1) else target1 * 1.08, 2)
        target3 = round(target2 * 1.10, 2)
        rr1_val = round((target1 - current_price) / risk_dist, 1) if risk_dist > 0 else 1.5
        rr2_val = round((target2 - current_price) / risk_dist, 1) if risk_dist > 0 else 2.5
        rr3_val = round((target3 - current_price) / risk_dist, 1) if risk_dist > 0 else 3.5

        is_breakout = r.get("is_breakout", False)
        if is_breakout:
            breakout_level = r1 or fib_r or current_price
            if breakout_level > current_price * 0.95 and breakout_level < current_price * 1.05:
                entry_low = round(breakout_level * 0.995, 2)
                entry_high = round(breakout_level * 1.015, 2)
            else:
                entry_low = round(current_price * 0.995, 2)
                entry_high = round(current_price * 1.01, 2)
        else:
            pullback_supports = []
            if s1 and s1 < current_price and (current_price - s1) / current_price <= 0.03:
                pullback_supports.append(s1)
            if pivot and pivot < current_price and (current_price - pivot) / current_price <= 0.03:
                pullback_supports.append(pivot)
            if pullback_supports:
                entry_low = round(max(pullback_supports), 2)
                if entry_low >= current_price:
                    entry_low = round(current_price * 0.99, 2)
                entry_high = round(current_price * 1.005, 2)
            else:
                entry_low = round(current_price * 0.99, 2)
                entry_high = round(current_price * 1.005, 2)

        if entry_low > entry_high:
            entry_low, entry_high = entry_high, entry_low

        regime = r.get("market_regime") or get_meta("market_regime", "unknown")
        if regime == "bearish":
            booking_plan = "Book 100% at Target 1 (Bear Market defensive play)"
        elif weekly_trend == "up":
            booking_plan = "Book 50% at Target 1, trail 50% to Target 2 with SL at Cost"
        else:
            booking_plan = "Book 70% at Target 1, trail 30% with tight trailing SL"

        r["trade"] = {
            "entry_low": entry_low,
            "entry_high": entry_high,
            "stop_loss": strict_sl,
            "target1": target1,
            "rr1": rr1_val,
            "target2": target2,
            "rr2": rr2_val,
            "target3": target3,
            "rr3": rr3_val,
            "booking_plan": booking_plan,
            "risk_reward": risk_reward,
            "target_1": target1,
            "target_2": target2,
        }
    except Exception as e:
        log.warning("Fallback trade generation failed: %s", e)
    return r


# ── Phase B: slim_data backfill & coverage helpers ───────────────────────
_SLIM_BACKFILL_BATCH_SIZE = int(os.getenv("SLIM_BACKFILL_BATCH_SIZE", "50"))


def log_slim_coverage():
    """Log current slim_data coverage ratio."""
    try:
        row = execute_db("SELECT COUNT(*) as total, COUNT(slim_data) as slim FROM scan_results_v2 WHERE scan_id = ?", (get_latest_completed_scan_id(),), fetch="one")
        if row:
            total = row.get("total", 0) if isinstance(row, dict) else 0
            slim = row.get("slim", 0) if isinstance(row, dict) else 0
            pct = round((slim / total * 100)) if total > 0 else 100
            log.info("[SLIM COVERAGE] %d/%d (%d%%)", slim, total, pct)
    except Exception:
        pass


def backfill_missing_slim_data() -> int:
    """One-time backfill: populate slim_data for rows where it's NULL.
    Uses _build_slim(). Batch processing. Idempotent. Safe re-run."""
    rows = execute_db("SELECT symbol, data FROM scan_results WHERE slim_data IS NULL", fetch="all")
    if not rows:
        return 0
    backfilled = 0
    for row in rows:
        try:
            raw_data = row["data"] if isinstance(row, dict) else row[1]
            parsed = _parse_data_column(raw_data)
            slim_val = _build_slim(parsed) if parsed else None
            if slim_val:
                sym = row["symbol"] if isinstance(row, dict) else row[0]
                execute_db("UPDATE scan_results SET slim_data=? WHERE symbol=?", (slim_val, sym))
                backfilled += 1
        except Exception as exc:
            log.warning("[SLIM BACKFILL] Failed for row: %s", exc)
    log.info("[SLIM BACKFILL] backfilled %d/%d rows", backfilled, len(rows))
    return backfilled


def _parse_data_column(val):
    if not val:
        return {}
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return {}
    return {}

def load_results(limit: int = 750, slim: bool = False, scan_id: str = None) -> list[dict]:
    """Load scan results from DB, ordered by score.

    Sprint 1 Phase 1: When slim=True, query ONLY slim_data column (not both).
    This avoids transferring the full data JSONB (~6KB/row) from PG when
    slim_data (~600B/row) is sufficient. Falls back to data column only
    for rows where slim_data IS NULL.

    Change Set A: `scan_id` pins the generation ONCE (also resolved once internally now,
    not per-query) so a caller can thread a single generation across load_results +
    get_result_count + get_last_scan_display, preventing intra-request generation mixing.
    """
    global _DB_USE_SLIM
    _sid = scan_id or get_latest_completed_scan_id()
    use_slim = slim and is_slim_results_enabled() and _DB_USE_SLIM
    t0 = time.perf_counter()
    rows = []
    fallback_rows = []
    
    if use_slim:
        # Sprint 1: Two-pass approach — first try slim-only, then fallback
        try:
            rows = execute_db(
                "SELECT slim_data FROM scan_results_v2 WHERE scan_id = ? AND slim_data IS NOT NULL ORDER BY score DESC LIMIT ?",
                (_sid, limit,), fetch="all"
            )
            slim_count = len(rows) if rows else 0
            # If we got fewer than limit, fill remaining from data column
            if slim_count < limit:
                remaining = limit - slim_count
                fallback_rows = execute_db(
                    "SELECT data FROM scan_results_v2 WHERE scan_id = ? AND slim_data IS NULL ORDER BY score DESC LIMIT ?",
                    (_sid, remaining,), fetch="all"
                )
        except Exception as e:
            # If slim_data column does not exist (e.g. init_db failed or hasn't run)
            # execute_db will log the syntax error but we need to disable slim queries to survive
            log.warning("[DB PERF] slim_data query failed, disabling _DB_USE_SLIM permanently: %s", str(e)[:100])
            _DB_USE_SLIM = False
            rows = []
            fallback_rows = execute_db(
                "SELECT data FROM scan_results_v2 WHERE scan_id = ? ORDER BY score DESC LIMIT ?",
                (_sid, limit,), fetch="all"
            )
    else:
        fallback_rows = execute_db(
            "SELECT data FROM scan_results_v2 WHERE scan_id = ? ORDER BY score DESC LIMIT ?",
            (_sid, limit,), fetch="all"
        )
    t_query = round((time.perf_counter() - t0) * 1000, 2)

    t0 = time.perf_counter()
    results = []
    slim_hits = 0
    # Parse slim_data rows
    for row in (rows or []):
        try:
            raw = row.get("slim_data")
            if raw:
                r = _parse_data_column(raw)
                if r:
                    # Single Mapping Location: reconstruct trade from trade_summary
                    ts = r.pop("trade_summary", None)
                    if ts:
                        r["trade"] = ts
                    slim_hits += 1
                    results.append(r)
        except Exception:
            pass
    # Parse fallback data rows
    for row in (fallback_rows or []):
        try:
            raw = row.get("data")
            r = _parse_data_column(raw)
            if r:
                if not slim:
                    _ensure_trade_populated(r)
                results.append(r)
        except Exception:
            pass
    t_parse = round((time.perf_counter() - t0) * 1000, 2)

    total_rows = slim_hits + len(fallback_rows or [])
    mode = f"slim({slim_hits}/{total_rows})" if slim and _DB_USE_SLIM else "full"
    log.info("[DB PERF] load_results limit=%d mode=%s | query=%s ms | parse_json=%s ms | total_rows=%d", limit, mode, t_query, t_parse, len(results))
    print(f"[DB PERF] load_results limit={limit} mode={mode} | query={t_query} ms | parse_json={t_parse} ms | total_rows={len(results)}")
    return results


def get_stock_from_results(symbol: str) -> dict | None:
    """Fetch a single stock's data from the LATEST COMPLETED scan results by symbol.

    Freshness fix: bind to scan_results_v2 pinned to get_latest_completed_scan_id()
    (mirrors get_stock). The prior query hit the deprecated scan_results table with no
    scan binding and a raw %s placeholder that breaks on the SQLite fallback path.
    """
    try:
        scan_id = get_latest_completed_scan_id()
        row = execute_db(
            "SELECT data FROM scan_results_v2 WHERE symbol = ? AND scan_id = ? LIMIT 1",
            (symbol.upper(), scan_id),
            fetch="one",
        )
        if row:
            val = row[0] if isinstance(row, (tuple, list)) else row.get("data")
            if isinstance(val, str):
                return json.loads(val)
            if isinstance(val, dict):
                return val
    except Exception:
        pass
    return None


# ── Thesis Locking (Recommendation Freeze) ─────────────────────────────
def init_recommendation_locks():
    """Create recommendation_locks table if not exists."""
    try:
        execute_db("""
            CREATE TABLE IF NOT EXISTS recommendation_locks (
                symbol TEXT PRIMARY KEY,
                locked_at TEXT,
                recommended_price REAL,
                entry_low REAL,
                entry_high REAL,
                stop_loss REAL,
                target1 REAL,
                target2 REAL,
                target3 REAL,
                risk_reward REAL,
                thesis_status TEXT DEFAULT 'ACTIVE',
                score_at_lock REAL,
                updates TEXT DEFAULT '[]'
            )
        """)
    except Exception as exc:
        log.warning("[THESIS] Failed to create recommendation_locks: %s", exc)


def lock_thesis(symbol: str, data: dict) -> bool:
    """Lock a thesis for a symbol. Returns True if newly locked, False if already locked."""
    try:
        existing = execute_db(
            "SELECT thesis_status FROM recommendation_locks WHERE symbol = %s",
            (symbol.upper(),), fetch="one"
        )
        if existing and existing.get('thesis_status') == 'ACTIVE':
            return False  # Already locked, don't overwrite

        trade = data.get("trade", {})
        now = datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S") if '_IST' in dir() else __import__('datetime').datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        execute_db("""
            INSERT INTO recommendation_locks (symbol, locked_at, recommended_price, entry_low, entry_high,
                stop_loss, target1, target2, target3, risk_reward, thesis_status, score_at_lock, updates)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'ACTIVE', %s, '[]')
            ON CONFLICT (symbol) DO UPDATE SET
                locked_at = EXCLUDED.locked_at, recommended_price = EXCLUDED.recommended_price,
                entry_low = EXCLUDED.entry_low, entry_high = EXCLUDED.entry_high,
                stop_loss = EXCLUDED.stop_loss, target1 = EXCLUDED.target1,
                target2 = EXCLUDED.target2, target3 = EXCLUDED.target3,
                risk_reward = EXCLUDED.risk_reward, thesis_status = 'ACTIVE',
                score_at_lock = EXCLUDED.score_at_lock, updates = '[]'
        """, (
            symbol.upper(), now, data.get("price", 0),
            trade.get("entry_low", 0), trade.get("entry_high", 0),
            data.get("stop_loss", trade.get("stop_loss", 0)),
            trade.get("target1", data.get("target_price", 0)),
            trade.get("target2", 0), trade.get("target3", 0),
            data.get("risk_reward", trade.get("risk_reward", 0)),
            data.get("score", 0),
        ))
        return True
    except Exception as exc:
        log.warning("[THESIS] Failed to lock thesis for %s: %s", symbol, exc)
        return False


def get_locked_thesis(symbol: str) -> dict | None:
    """Get locked thesis for a symbol."""
    try:
        row = execute_db(
            "SELECT symbol, locked_at, recommended_price, entry_low, entry_high, stop_loss, target1, target2, target3, risk_reward, thesis_status, score_at_lock, updates FROM recommendation_locks WHERE symbol = %s",
            (symbol.upper(),), fetch="one"
        )
        if row:
            cols = ["symbol", "locked_at", "recommended_price", "entry_low", "entry_high", "stop_loss", "target1", "target2", "target3", "risk_reward", "thesis_status", "score_at_lock", "updates"]
            result = dict(zip(cols, row)) if isinstance(row, (tuple, list)) else row
            if isinstance(result.get("updates"), str):
                result["updates"] = json.loads(result["updates"])
            return result
    except Exception:
        pass
    return None


def append_thesis_update(symbol: str, update_text: str):
    """Append a timestamped update to an active thesis."""
    try:
        thesis = get_locked_thesis(symbol)
        if not thesis or thesis.get("thesis_status") != "ACTIVE":
            return
        updates = thesis.get("updates", [])
        if not isinstance(updates, list):
            updates = []
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d %H:%M:%S")
        updates.append({"date": now, "text": update_text})
        # Keep last 50 updates max
        updates = updates[-50:]
        execute_db(
            "UPDATE recommendation_locks SET updates = %s WHERE symbol = %s",
            (json.dumps(updates), symbol.upper())
        )
    except Exception as exc:
        log.warning("[THESIS] Failed to append update for %s: %s", symbol, exc)


def check_thesis_completion(symbol: str, current_price: float):
    """Check if current price has hit SL or Target, auto-close thesis."""
    try:
        thesis = get_locked_thesis(symbol)
        if not thesis or thesis.get("thesis_status") != "ACTIVE":
            return
        sl = thesis.get("stop_loss", 0)
        tg = thesis.get("target1", 0)
        if sl and current_price <= sl:
            execute_db(
                "UPDATE recommendation_locks SET thesis_status = 'SL_HIT' WHERE symbol = %s",
                (symbol.upper(),)
            )
            execute_db(
                "UPDATE recommendation_history SET is_first_analysis = FALSE WHERE symbol = %s",
                (symbol.upper(),)
            )
            append_thesis_update(symbol, f"SL Hit at ₹{current_price:.2f}")
        elif tg and current_price >= tg:
            execute_db(
                "UPDATE recommendation_locks SET thesis_status = 'TG_HIT' WHERE symbol = %s",
                (symbol.upper(),)
            )
            execute_db(
                "UPDATE recommendation_history SET is_first_analysis = FALSE WHERE symbol = %s",
                (symbol.upper(),)
            )
            append_thesis_update(symbol, f"Target 1 Hit at ₹{current_price:.2f}")
    except Exception as exc:
        log.warning("[THESIS] Completion check failed for %s: %s", symbol, exc)


def load_golden_results(limit: int = 100, scan_id: str = None) -> list[dict]:
    """Load Golden stocks from DB, ordered by score.

    Uses PG JSONB syntax when PostgreSQL is active, with automatic
    fallback to SQLite json_extract if PG connection fails mid-request.

    scan_id: pins the generation ONCE (engine-aware when the caller passes
    get_ui_scan_id()); None -> latest-completed legacy scan (unchanged default).
    """
    global _DB_USE_SLIM
    use_slim = is_slim_results_enabled() and _DB_USE_SLIM
    _sid = scan_id or get_latest_completed_scan_id()

    if use_slim:
        pg_query = "SELECT slim_data FROM scan_results_v2 WHERE scan_id = ? AND ((slim_data->>'is_golden')::text = 'true' OR (slim_data->>'is_golden')::text = '1') AND slim_data IS NOT NULL ORDER BY score DESC LIMIT ?"
        sqlite_query = "SELECT slim_data FROM scan_results_v2 WHERE scan_id = ? AND (json_extract(slim_data, '$.is_golden') = 1 OR json_extract(slim_data, '$.is_golden') = 'true') AND slim_data IS NOT NULL ORDER BY score DESC LIMIT ?"
    else:
        pg_query = "SELECT data FROM scan_results_v2 WHERE scan_id = ? AND ((data->>'is_golden')::text = 'true' OR (data->>'is_golden')::text = '1') ORDER BY score DESC LIMIT ?"
        sqlite_query = "SELECT data FROM scan_results_v2 WHERE scan_id = ? AND (json_extract(data, '$.is_golden') = 1 OR json_extract(data, '$.is_golden') = 'true') ORDER BY score DESC LIMIT ?"

    try:
        query = pg_query if is_postgresql() and not pg_cooldown_active() else sqlite_query
        rows = execute_db(query, (_sid, limit), fetch="all")
    except Exception:
        rows = _execute_sqlite(sqlite_query, (_sid, limit), "all")
    results = []
    for row in (rows or []):
        try:
            raw = row.get("slim_data") if use_slim else row.get("data")
            if not raw:
                continue
            r = _parse_data_column(raw)
            if r:
                _ensure_trade_populated(r)
                results.append(r)
        except Exception:
            pass

    if use_slim and len(results) < limit:
        remaining = limit - len(results)
        fallback_pg = "SELECT data FROM scan_results_v2 WHERE scan_id = ? AND ((data->>'is_golden')::text = 'true' OR (data->>'is_golden')::text = '1') AND slim_data IS NULL ORDER BY score DESC LIMIT ?"
        fallback_sqlite = "SELECT data FROM scan_results_v2 WHERE scan_id = ? AND (json_extract(data, '$.is_golden') = 1 OR json_extract(data, '$.is_golden') = 'true') AND slim_data IS NULL ORDER BY score DESC LIMIT ?"
        try:
            f_query = fallback_pg if is_postgresql() and not pg_cooldown_active() else fallback_sqlite
            f_rows = execute_db(f_query, (_sid, remaining), fetch="all")
        except Exception:
            f_rows = _execute_sqlite(fallback_sqlite, (_sid, remaining), "all")
        for row in (f_rows or []):
            try:
                r = _parse_data_column(row.get("data"))
                if r:
                    _ensure_trade_populated(r)
                    results.append(r)
            except Exception:
                pass
    return results


def load_breakout_results(limit: int = 100) -> list[dict]:
    """Load Breakout stocks from DB, ordered by score.
    
    Uses PG JSONB syntax when PostgreSQL is active, with automatic
    fallback to SQLite json_extract if PG connection fails mid-request.
    """
    global _DB_USE_SLIM
    use_slim = is_slim_results_enabled() and _DB_USE_SLIM

    if use_slim:
        pg_query = "SELECT slim_data FROM scan_results_v2 WHERE scan_id = ? AND ((slim_data->>'is_breakout')::text = 'true' OR (slim_data->>'is_breakout')::text = '1') AND slim_data IS NOT NULL ORDER BY score DESC LIMIT ?"
        sqlite_query = "SELECT slim_data FROM scan_results_v2 WHERE scan_id = ? AND (json_extract(slim_data, '$.is_breakout') = 1 OR json_extract(slim_data, '$.is_breakout') = 'true') AND slim_data IS NOT NULL ORDER BY score DESC LIMIT ?"
    else:
        pg_query = "SELECT data FROM scan_results_v2 WHERE scan_id = ? AND ((data->>'is_breakout')::text = 'true' OR (data->>'is_breakout')::text = '1') ORDER BY score DESC LIMIT ?"
        sqlite_query = "SELECT data FROM scan_results_v2 WHERE scan_id = ? AND (json_extract(data, '$.is_breakout') = 1 OR json_extract(data, '$.is_breakout') = 'true') ORDER BY score DESC LIMIT ?"

    try:
        query = pg_query if is_postgresql() and not pg_cooldown_active() else sqlite_query
        scan_id = get_latest_completed_scan_id()
        rows = execute_db(query, (scan_id, limit), fetch="all")
    except Exception:
        rows = _execute_sqlite(sqlite_query, (get_latest_completed_scan_id(), limit), "all")
    results = []
    for row in (rows or []):
        try:
            raw = row.get("slim_data") if use_slim else row.get("data")
            if not raw:
                continue
            r = _parse_data_column(raw)
            if r:
                _ensure_trade_populated(r)
                results.append(r)
        except Exception:
            pass

    if use_slim and len(results) < limit:
        remaining = limit - len(results)
        fallback_pg = "SELECT data FROM scan_results_v2 WHERE scan_id = ? AND ((data->>'is_breakout')::text = 'true' OR (data->>'is_breakout')::text = '1') AND slim_data IS NULL ORDER BY score DESC LIMIT ?"
        fallback_sqlite = "SELECT data FROM scan_results_v2 WHERE scan_id = ? AND (json_extract(data, '$.is_breakout') = 1 OR json_extract(data, '$.is_breakout') = 'true') AND slim_data IS NULL ORDER BY score DESC LIMIT ?"
        try:
            f_query = fallback_pg if is_postgresql() and not pg_cooldown_active() else fallback_sqlite
            scan_id = get_latest_completed_scan_id()
            f_rows = execute_db(f_query, (scan_id, remaining), fetch="all")
        except Exception:
            f_rows = _execute_sqlite(fallback_sqlite, (get_latest_completed_scan_id(), remaining), "all")
        for row in (f_rows or []):
            try:
                r = _parse_data_column(row.get("data"))
                if r:
                    _ensure_trade_populated(r)
                    results.append(r)
            except Exception:
                pass
    return results


def load_high_conviction_results(limit: int = 100, scan_id: str = None) -> list[dict]:
    """Load High Conviction stocks from DB, ordered by score.

    scan_id: pins the generation ONCE (engine-aware when the caller passes
    get_ui_scan_id()); None -> latest-completed legacy scan (unchanged default).
    """
    global _DB_USE_SLIM
    use_slim = is_slim_results_enabled() and _DB_USE_SLIM
    _sid = scan_id or get_latest_completed_scan_id()

    if use_slim:
        query = "SELECT slim_data FROM scan_results_v2 WHERE scan_id = ? AND high_conviction = 1 AND slim_data IS NOT NULL ORDER BY score DESC LIMIT ?"
    else:
        query = "SELECT data FROM scan_results_v2 WHERE scan_id = ? AND high_conviction = 1 ORDER BY score DESC LIMIT ?"

    rows = execute_db(query, (_sid, limit), fetch="all")
    results = []
    for row in (rows or []):
        try:
            raw = row.get("slim_data") if use_slim else row.get("data")
            if not raw:
                continue
            r = _parse_data_column(raw)
            if r:
                _ensure_trade_populated(r)
                results.append(r)
        except Exception:
            pass

    if use_slim and len(results) < limit:
        remaining = limit - len(results)
        fallback_query = "SELECT data FROM scan_results_v2 WHERE scan_id = ? AND high_conviction = 1 AND slim_data IS NULL ORDER BY score DESC LIMIT ?"
        f_rows = execute_db(fallback_query, (_sid, remaining), fetch="all")
        for row in (f_rows or []):
            try:
                r = _parse_data_column(row.get("data"))
                if r:
                    _ensure_trade_populated(r)
                    results.append(r)
            except Exception:
                pass
    return results

def get_result_count(scan_id: str = None) -> int:
    sid = scan_id or get_latest_completed_scan_id()   # Change Set A: accept a pinned generation
    return execute_db("SELECT COUNT(*) as cnt FROM scan_results_v2 WHERE scan_id = ?", (sid,), fetch="count")


def get_last_scan_display(scan_id: str = None):
    """Change Set A (canonical freshness): the authoritative 'last scan' display value, derived
    from scan_runs.end_time of the (optionally pinned) completed scan — the single source of
    truth, replacing the drift-prone scan_meta 'last_scan'. Falls back to the legacy meta if the
    scan row has no end_time. Format matches the existing 'YYYY-MM-DD HH:MM:SS' contract.
    """
    try:
        sid = scan_id or get_latest_completed_scan_id()
        row = execute_db("SELECT end_time FROM scan_runs WHERE scan_id = ?", (sid,), fetch="one")
        if row and row.get("end_time"):
            et = row["end_time"]
            return et.strftime("%Y-%m-%d %H:%M:%S") if not isinstance(et, str) else str(et)[:19]
    except Exception:
        pass
    return get_meta("last_scan")

def get_meta(key: str, default=None):
    """Get a metadata value. Served from memory cache when fresh."""
    if _META_CACHE_ENABLED:
        now = time.time()
        cached = _meta_cache.get(key)
        if cached is not None:
            val, ts = cached
            if now - ts < _META_TTL:
                return val  # Cache hit — zero DB cost

    # Cache miss or disabled — hit DB
    row = execute_db("SELECT value FROM scan_meta WHERE key=?", (key,), fetch="one")
    if row:
        val = row["value"]
        try:
            parsed = json.loads(val)
        except (json.JSONDecodeError, TypeError, ValueError):
            parsed = val
        if _META_CACHE_ENABLED:
            _meta_cache[key] = (parsed, time.time())
        return parsed
    if _META_CACHE_ENABLED:
        _meta_cache[key] = (default, time.time())
    return default


def is_slim_results_enabled() -> bool:
    """Check if slim results feature flag is enabled."""
    env_val = os.getenv("USE_SLIM_RESULTS")
    if env_val is not None:
        return env_val.lower() == "true"
    try:
        meta_val = get_meta("USE_SLIM_RESULTS")
        if meta_val is not None:
            return str(meta_val).lower() == "true"
    except Exception:
        pass
    return True


_symbol_aliases_cache = {}
_aliases_loaded = False
_aliases_lock = threading.Lock()

def resolve_symbol(symbol: str) -> str:
    """Resolve a legacy or merged symbol to its active alias (e.g. HDFC -> HDFCBANK)."""
    global _aliases_loaded
    if not symbol:
        return symbol
    upper_symbol = symbol.upper().strip()
    
    if not _aliases_loaded:
        with _aliases_lock:
            if not _aliases_loaded:
                try:
                    rows = execute_db("SELECT old_symbol, new_symbol FROM symbol_aliases", fetch="all")
                    for row in (rows or []):
                        _symbol_aliases_cache[row["old_symbol"].upper()] = row["new_symbol"]
                    _aliases_loaded = True
                except Exception:
                    pass
                    
    if upper_symbol in _symbol_aliases_cache:
        return _symbol_aliases_cache[upper_symbol]
        
    has_ns = upper_symbol.endswith(".NS")
    clean = upper_symbol.replace(".NS", "")
    if clean in _symbol_aliases_cache:
        resolved = _symbol_aliases_cache[clean]
        return f"{resolved}.NS" if has_ns and not resolved.endswith(".NS") else resolved
        
    return symbol



def set_meta(key: str, value):
    """Set a metadata value. Write-through to memory cache."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # P0: Sanitize non-string values before serialization
    if not isinstance(value, str):
        value = sanitize_for_json(value, component="set_meta")
        v = json.dumps(value, default=str, allow_nan=False)
    else:
        v = value
    execute_db("""
        INSERT INTO scan_meta (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
    """, (key, v, now))
    # Write-through: update cache immediately
    if _META_CACHE_ENABLED:
        _meta_cache[key] = (value, time.time())

def clear_meta_cache(key=None):
    """Invalidate meta cache. Call on scan start/complete, regime update, login refresh."""
    if key:
        _meta_cache.pop(key, None)
    else:
        _meta_cache.clear()
    log.debug("Meta cache cleared: %s", key or "ALL")


# ─── Phase 0: Trust & Observability — Audit Functions ───

def save_score_audit(results: list, scan_id: str, scan_version: str):
    """Batch-insert score audit rows for one scan cycle.
    
    Uses individual execute_db calls (not executemany) because:
    1. execute_db handles PG/SQLite dual-path automatically
    2. ON CONFLICT DO NOTHING makes partial failures safe
    3. Runs once per scan (~500 rows), not per request
    
    Gated by ENABLE_SCORE_AUDIT env var (default: true).
    """
    if not os.getenv("ENABLE_SCORE_AUDIT", "true").lower() == "true":
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    inserted = 0
    for r in results:
        try:
            comp = r.get("_score_components", {})
            # Phase 5, Section 36: Extract and serialize score_breakdown
            _breakdown = comp.get("score_breakdown")
            _breakdown_json = None
            if _breakdown:
                try:
                    # P0: Sanitize score breakdown before serialization
                    _breakdown = sanitize_for_json(_breakdown, symbol=r.get("symbol"), component="score_audit")
                    _breakdown_json = json.dumps(_breakdown, default=str, allow_nan=False)
                except Exception:
                    pass
            _score_query = """
                INSERT INTO score_audit
                (symbol, scan_id, scan_time, technical_score, earnings_momentum_score,
                 fundamental_score, smart_money_score, sector_rotation_score,
                 news_sentiment_score, news_spike_score, macro_score, catalyst_score,
                 final_score, data_source, source_reason, provider_latency_ms,
                 data_staleness_hours, scan_version, score_breakdown)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            _score_params = (
                r.get("symbol", ""),
                scan_id,
                now,
                comp.get("technical"),
                comp.get("earnings_momentum"),
                comp.get("fundamental"),
                comp.get("smart_money"),
                comp.get("sector_rotation"),
                comp.get("news_sentiment"),
                comp.get("news_spike"),
                comp.get("macro"),
                comp.get("catalyst"),
                r.get("score", 0),
                r.get("_data_source", "UNKNOWN"),
                r.get("_source_reason", "UNKNOWN"),
                r.get("_provider_latency_ms"),
                r.get("_data_staleness_hours"),
                scan_version,
                _breakdown_json,
            )
            execute_db(_score_query, _score_params)
            
            # P0.1E: Queue to governance DLQ for PG replay if currently on SQLite fallback
            if pg_cooldown_active() or not is_postgresql():
                queue_governance_write(_score_query, _score_params, artifact_type="score_audit")
                
            inserted += 1
        except Exception as exc:
            # ON CONFLICT or other error — skip this row, continue batch
            log.debug("score_audit insert skipped for %s: %s", r.get("symbol"), exc)
    if inserted:
        log.info("Phase 0: score_audit saved %d/%d rows (scan_id=%s)", inserted, len(results), scan_id[:20])


def save_scan_audit(scan_id: str, start_time: str, end_time: str,
                    duration_ms: int, stocks_scanned: int,
                    stocks_succeeded: int, stocks_failed: int,
                    data_source: str, scan_version: str,
                    scan_mode: str = "manual"):
    """Insert one scan_audit row summarising the scan run."""
    if not os.getenv("ENABLE_SCORE_AUDIT", "true").lower() == "true":
        return
    _audit_query = """
        INSERT INTO scan_audit
        (scan_id, start_time, end_time, duration_ms, stocks_scanned,
         stocks_succeeded, stocks_failed, data_source, scan_version, scan_mode)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    _audit_params = (
        scan_id, start_time, end_time, duration_ms,
        stocks_scanned, stocks_succeeded, stocks_failed,
        data_source, scan_version, scan_mode,
    )
    try:
        execute_db(_audit_query, _audit_params)
        log.info("Phase 0: scan_audit saved (scan_id=%s, %d stocks, %dms)",
                 scan_id[:20], stocks_scanned, duration_ms)
        # P0.1E: Queue to governance DLQ for PG replay if currently on SQLite fallback
        if pg_cooldown_active() or not is_postgresql():
            queue_governance_write(_audit_query, _audit_params, artifact_type="scan_audit")
    except Exception as exc:
        log.error("Failed to insert scan audit log: %s", exc)


def start_chunk_run(scan_id: str, chunk_name: str, symbol_count: int) -> int:
    """Log the start of a chunk execution."""
    return execute_db("""
        INSERT INTO universe_chunk_runs (scan_id, chunk_name, status, symbol_count, started_at, chunk_last_activity)
        VALUES (?, ?, 'RUNNING', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        RETURNING id
    """, (scan_id, chunk_name, symbol_count), fetch="one").get("id")

def end_chunk_run(chunk_run_id: int, status: str, symbols_processed: int, error_message: str = None):
    """Log the completion or failure of a chunk execution."""
    execute_db("""
        UPDATE universe_chunk_runs
        SET status = ?, symbols_processed = ?, error_message = ?, completed_at = CURRENT_TIMESTAMP, chunk_last_activity = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (status, symbols_processed, error_message, chunk_run_id))

def update_chunk_progress(chunk_run_id: int, symbol: str, symbols_processed: int):
    """Update chunk progress and last_symbol per symbol to prevent zombie tracking."""
    execute_db("""
        UPDATE universe_chunk_runs
        SET symbols_processed = ?, last_symbol = ?, chunk_last_activity = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (symbols_processed, symbol, chunk_run_id))

def get_stock(symbol: str, scan_id: str = None) -> dict | None:
    """Get a single stock's scan data.

    `scan_id` is optional; when omitted, defaults to get_latest_completed_scan_id()
    (byte-identical to prior behavior). Callers may pass get_display_scan_id() to surface
    a symbol's in-progress result while a scan is running (GOAL #1, live partial results).
    """
    resolved = resolve_symbol(symbol)
    scan_id = scan_id or get_latest_completed_scan_id()
    row = execute_db("SELECT data FROM scan_results_v2 WHERE symbol=? AND scan_id=?", (resolved.upper(), scan_id), fetch="one")
    if row:
        try:
            r = _parse_data_column(row["data"])
            if r:
                _ensure_trade_populated(r)
                return r
        except Exception:
            pass
    return None

def save_detailed_fundamentals(symbol: str, data: dict):
    """Save processed detailed financials JSON to fundamentals table."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # P0: Sanitize detailed fundamentals before serialization
    data = sanitize_for_json(data, symbol=symbol, component="detailed_fundamentals")
    v = json.dumps(data, default=str, allow_nan=False)
    execute_db("""
        INSERT INTO fundamentals (symbol, detailed_json, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET detailed_json=excluded.detailed_json, updated_at=excluded.updated_at
    """, (symbol.upper(), v, now))

def get_detailed_fundamentals(symbol: str) -> dict | None:
    """Get stored detailed financials JSON from fundamentals table."""
    resolved = resolve_symbol(symbol)
    row = execute_db("SELECT detailed_json FROM fundamentals WHERE symbol=?", (resolved.upper(),), fetch="one")
    if row and row.get("detailed_json"):
        try:
            return json.loads(row["detailed_json"])
        except Exception:
            pass
    return None

def get_stocks_map(symbols: list[str]) -> dict[str, dict]:
    """Get multiple stocks by symbol in one query."""
    if not symbols:
        return {}
    # Map each original symbol to its resolved counterpart
    resolved_to_orig = {}
    for s in symbols:
        res_sym = resolve_symbol(s)
        resolved_to_orig.setdefault(res_sym.upper(), []).append(s)
        
    resolved_symbols = list(resolved_to_orig.keys())
    placeholders = ",".join("?" * len(resolved_symbols))
    scan_id = get_latest_completed_scan_id()
    query_args = resolved_symbols + [scan_id]
    rows = execute_db(
        f"SELECT symbol, data FROM scan_results_v2 WHERE symbol IN ({placeholders}) AND scan_id = ?",
        query_args,
        fetch="all"
    )
    res = {}
    for row in rows:
        try:
            r = _parse_data_column(row["data"])
            if r:
                _ensure_trade_populated(r)
                # Map back to original queried symbols
                for orig_sym in resolved_to_orig.get(row["symbol"].upper(), []):
                    res[orig_sym] = r
        except Exception:
            pass
    return res

def get_latest_completed_scan_id() -> str:
    """Get the scan_id of the most recent completed scan."""
    row = execute_db("SELECT scan_id FROM scan_runs WHERE LOWER(status) = 'completed' ORDER BY end_time DESC LIMIT 1", fetch="one")
    if row and row.get("scan_id"):
        return row["scan_id"]
    return "scan_legacy_migration"


def get_display_scan_id() -> str:
    """Generation to surface in the UI (live-partial-results aware).

    GOAL #1: while a scan is IN PROGRESS, show its partial results live instead of
    waiting for full completion. The scanner batch-saves partial rows to
    scan_results_v2 under the running scan_id during the scan, so:

      - If a scan is ACTIVE *and* that active scan_id already has >=1 row in
        scan_results_v2, return the active scan_id (board fills live as batches land).
      - Otherwise (no scan running, or the active scan hasn't saved its first batch
        yet) fall back to get_latest_completed_scan_id() — identical to today.

    Cheap: one COUNT query, only when a scan is active. Does NOT mutate or change
    get_latest_completed_scan_id().
    """
    try:
        active, active_id = is_scan_active()
        if active and active_id:
            row = execute_db(
                "SELECT COUNT(*) AS n FROM scan_results_v2 WHERE scan_id = ?",
                (active_id,), fetch="one"
            )
            n = (row.get("n") if isinstance(row, dict) else (row[0] if row else 0)) or 0
            if n and int(n) >= 1:
                return active_id
    except Exception:
        # Fail-safe: never let the live-view helper break the read path.
        pass
    return get_latest_completed_scan_id()


def get_ui_scan_id(model_version: str = None):
    """Scan generation the UI/analytics should read, BY ENGINE (model_version).

    - Any engine tagged in scan_results_v2 (scoring_v1, legacy_cleaned, and any
      FUTURE engine) -> that engine's OWN latest generation (by updated_at). Returns
      None if it has no scan yet (caller shows an empty state) — an engine can NEVER
      fall back to / masquerade as another (MUST-FIX 2 preserved). Generalized from
      the original scoring_v1-only hardcode so N engines are truly data-driven.
    - 'legacy' -> the existing live/latest LEGACY generation via get_display_scan_id()
      (legacy lives in scan_runs / current_scan_state, not addressable by scan_id here).
    - None -> resolve the UI toggle meta 'ui_reco_source' (default 'scoring_v1').

    NOTE: scoring_v1 + legacy behaviour is byte-unchanged (same query / same path);
    only the previously-dead 'anything else -> legacy' branch is now per-engine.
    """
    if model_version is None:
        model_version = get_meta("ui_reco_source") or "scoring_v1"
    if model_version and model_version != "legacy":
        try:
            row = execute_db(
                "SELECT scan_id FROM scan_results_v2 WHERE model_version = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (model_version,), fetch="one"
            )
            return (row.get("scan_id") if row else None)
        except Exception:
            return None
    return get_display_scan_id()


def engine_coverage() -> list[dict]:
    """Per-engine data-quality coverage for the Comparison page (counts only; cheap).

    Returns a list aligned with list_engines(): each {id, label, scored, universe,
    earnings_rows, earnings_pct, sector_pct}. universe / sector_pct / earnings are
    catalog-global (repeated per engine for easy per-column render); scored is per-engine.
    Engine separation preserved: scored counts the engine's own latest scan only.
    """
    def _c(sql, params=None):
        try:
            r = execute_db(sql, params, fetch="one") if params else execute_db(sql, fetch="one")
            return int((r.get("c") if isinstance(r, dict) else (r[0] if r else 0)) or 0)
        except Exception:
            return 0
    universe = _c("SELECT COUNT(*) AS c FROM universe_catalog")
    earnings_rows = _c("SELECT COUNT(*) AS c FROM earnings_store")
    with_sector = _c("SELECT COUNT(*) AS c FROM universe_catalog WHERE sector IS NOT NULL AND sector <> ''")
    # Fundamentals coverage = names with a usable quality signal (ROCE present AND non-zero);
    # 0 is treated as ABSENT, mirroring legacy_cleaned's 0-as-missing fundamental rule.
    with_fund = _c("SELECT COUNT(*) AS c FROM universe_catalog WHERE roce IS NOT NULL AND roce <> 0")
    sector_pct = round(with_sector / universe * 100, 1) if universe else 0.0
    earnings_pct = round(min(100.0, earnings_rows / universe * 100), 1) if universe else 0.0
    fundamentals_pct = round(with_fund / universe * 100, 1) if universe else 0.0
    out = []
    for e in list_engines():
        mv = e.get("id")
        sid = get_ui_scan_id(mv)
        scored = _c("SELECT COUNT(*) AS c FROM scan_results_v2 WHERE scan_id = ?", (sid,)) if sid else 0
        out.append({
            "id": mv, "label": e.get("label", mv), "scored": scored,
            "universe": universe, "earnings_rows": earnings_rows,
            "earnings_pct": earnings_pct, "sector_pct": sector_pct,
            "fundamentals_pct": fundamentals_pct,
        })
    return out


def list_engines() -> list[dict]:
    """Data-driven engine list for the dynamic engine switcher (foundation for /api/engines).

    Additive + idempotent: ensures an `engine_registry` table exists and seeds the two known
    engines IF the table is empty. Returns the UNION of:
      - DISTINCT model_version present in scan_results_v2 (so a brand-new engine auto-appears
        even without a registry row — default label = title-cased id, sort_order 100), and
      - every registry row (curated label / sort_order / is_default).
    Each entry: {id, label, is_default, sort_order}. Ordered by sort_order, then id.

    Engine separation is preserved: this only ENUMERATES engines; it never blends their data.
    """
    # 1) Idempotent additive table + seed (no-op once seeded; ON CONFLICT keeps it safe).
    try:
        execute_db(
            "CREATE TABLE IF NOT EXISTS engine_registry ("
            "model_version TEXT PRIMARY KEY, label TEXT, "
            "sort_order INTEGER DEFAULT 100, is_default INTEGER DEFAULT 0)"
        )
        # Ensure the known engines exist (idempotent; ON CONFLICT keeps curated labels).
        # Always-run (not only when empty) so a newly-added engine like legacy_cleaned
        # registers its clean label with zero manual migration.
        for mv, label, so, isd in (
            ("scoring_v1", "Scoring V1", 10, 1),
            ("legacy", "Legacy", 20, 0),
            ("legacy_cleaned", "Legacy Cleaned", 30, 0),
        ):
            execute_db(
                "INSERT INTO engine_registry (model_version, label, sort_order, is_default) "
                "VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (mv, label, so, isd),
            )
    except Exception:
        log.exception("[list_engines] registry ensure/seed failed")

    # 2) Curated registry rows (id -> meta).
    reg: dict[str, dict] = {}
    try:
        rows = execute_db(
            "SELECT model_version, label, sort_order, is_default FROM engine_registry",
            fetch="all",
        ) or []
        for r in rows:
            mv = (r.get("model_version") if isinstance(r, dict) else r[0])
            if not mv:
                continue
            reg[mv] = {
                "id": mv,
                "label": (r.get("label") if isinstance(r, dict) else r[1]) or mv.replace("_", " ").title(),
                "sort_order": int((r.get("sort_order") if isinstance(r, dict) else r[2]) or 100),
                "is_default": int((r.get("is_default") if isinstance(r, dict) else r[3]) or 0),
            }
    except Exception:
        log.exception("[list_engines] registry read failed")

    # 3) Engines that actually have scan rows (auto-discovery).
    try:
        drows = execute_db(
            "SELECT DISTINCT model_version FROM scan_results_v2 WHERE model_version IS NOT NULL",
            fetch="all",
        ) or []
        for r in drows:
            mv = (r.get("model_version") if isinstance(r, dict) else r[0])
            if mv and mv not in reg:
                reg[mv] = {
                    "id": mv,
                    "label": mv.replace("_", " ").title(),
                    "sort_order": 100,
                    "is_default": 0,
                }
    except Exception:
        log.exception("[list_engines] distinct model_version read failed")

    return sorted(reg.values(), key=lambda e: (e["sort_order"], e["id"]))


def scan_health() -> dict:
    """Phase 1.5 (Change Set D): read-only operations/health aggregator for /api/operations.

    No secrets. Timestamps are server-local (UTC on Railway), consistent with how scan_runs
    stores end_time. Fail-safe: any error yields a degraded 'red' verdict and never raises.
    `cache_generation` == the canonical latest-completed scan_id (A-5 enriches it with counters).
    """
    import os as _os
    from datetime import datetime as _dt

    def _envi(name, default):
        try:
            return int(_os.environ.get(name, default))
        except Exception:
            return default

    AGE_YELLOW   = _envi("OPS_AGE_YELLOW_S", 5400)   # 90 min (market hours)
    AGE_RED      = _envi("OPS_AGE_RED_S", 9000)      # 150 min (market hours)
    SCHED_YELLOW = _envi("OPS_SCHED_YELLOW_S", 120)
    SCHED_RED    = _envi("OPS_SCHED_RED_S", 300)
    BOOT_GRACE   = _envi("OPS_BOOT_GRACE_S", 120)

    try:
        now = _dt.now()
        now_epoch = time.time()

        def _age_str(ts_str):
            if not ts_str:
                return None
            try:
                return int((now - _dt.strptime(str(ts_str)[:19], "%Y-%m-%d %H:%M:%S")).total_seconds())
            except Exception:
                return None

        # Canonical generation = latest COMPLETED scan
        row = execute_db(
            "SELECT scan_id, end_time FROM scan_runs WHERE LOWER(status)='completed' "
            "ORDER BY end_time DESC LIMIT 1", fetch="one")
        last_scan_id = row["scan_id"] if (row and row.get("scan_id")) else None
        last_end = row["end_time"] if row else None
        last_age = _age_str(last_end)

        # Running scan?
        srow = execute_db("SELECT status, scan_id FROM current_scan_state WHERE id=1", fetch="one")
        scanning = bool(srow and str(srow.get("status")).lower() == "running")
        running_scan_id = srow.get("scan_id") if (srow and scanning) else None

        # Scheduler heartbeat (epoch; written by _auto_scan_loop only when PHASE15_OPS_ENDPOINT=1)
        hb = get_meta("scheduler_heartbeat_ts")
        sched_age = None
        if hb:
            try:
                sched_age = int(now_epoch - float(hb))
            except Exception:
                sched_age = None

        _e = get_meta("auto_scan_enabled")
        auto_enabled = True if _e is None else (str(_e) == "1")

        market_open = None
        try:
            import live_feed as _lf
            market_open = bool(_lf.is_market_open())
        except Exception:
            market_open = None

        # next_scheduled_scan: best-effort, only meaningful when enabled AND market open
        next_scheduled = None
        try:
            if auto_enabled and market_open:
                from config import AUTO_SCAN_INTERVAL as _ASI
                lf = get_meta("last_fast_scan_ts")
                if lf:
                    next_scheduled = _dt.fromtimestamp(float(lf) + _ASI * 60).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            next_scheduled = None

        # ── verdict (market-hours-aware) ──
        reasons, verdict = [], "green"
        if last_scan_id is None:
            verdict = "red"; reasons.append("no_completed_scan")
        # Change Set D fix: the heartbeat goes stale WHILE a scan runs (the single-threaded
        # _auto_scan_loop blocks inside run_full_scan), so only flag the scheduler when it is
        # NOT scanning — a busy scheduler is not a stalled one.
        if sched_age is not None and sched_age > BOOT_GRACE and not scanning:
            if sched_age >= SCHED_RED:
                verdict = "red"; reasons.append("scheduler_stalled")
            elif sched_age >= SCHED_YELLOW and verdict != "red":
                verdict = "yellow"; reasons.append("scheduler_lagging")
        if market_open and last_age is not None:
            if last_age >= AGE_RED:
                verdict = "red"; reasons.append("scan_age_red")
            elif last_age >= AGE_YELLOW and verdict != "red":
                verdict = "yellow"; reasons.append("scan_age_yellow")
        if market_open and not auto_enabled:
            if verdict == "green":
                verdict = "yellow"
            reasons.append("auto_scan_disabled")
        if not reasons:
            reasons.append("ok")

        return {
            "last_scan_id": last_scan_id,
            "last_scan_end": last_end,
            "last_scan_age_s": last_age,
            "scan_status": "running" if scanning else "idle",
            "running_scan_id": running_scan_id,
            "cache_generation": last_scan_id,
            "scheduler_heartbeat_age_s": sched_age,
            "next_scheduled_scan": next_scheduled,
            "market_open": market_open,
            "auto_scan_enabled": auto_enabled,
            "health_verdict": verdict,
            "verdict_reasons": reasons,
            "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception as exc:
        # Observability must never 500/blind monitoring; degrade to red. No secrets in the payload.
        log.warning("[ops] scan_health failed: %s", exc)
        return {
            "last_scan_id": None, "last_scan_end": None, "last_scan_age_s": None,
            "scan_status": "unknown", "running_scan_id": None, "cache_generation": None,
            "scheduler_heartbeat_age_s": None, "next_scheduled_scan": None,
            "market_open": None, "auto_scan_enabled": None,
            "health_verdict": "red", "verdict_reasons": ["health_check_failed"],
            "generated_at": None,
        }


def get_all_symbols() -> list[str]:
    """Get all symbols in scan_results_v2 for the latest scan."""
    scan_id = get_latest_completed_scan_id()
    rows = execute_db("SELECT symbol FROM scan_results_v2 WHERE scan_id = ? ORDER BY score DESC", (scan_id,), fetch="all")
    return [row["symbol"] for row in rows] if rows else []


def get_all_results() -> list[dict]:
    """Get all scan results as dicts. Used for deep scan shortlisting."""
    scan_id = get_latest_completed_scan_id()
    rows = execute_db("SELECT symbol, data FROM scan_results_v2 WHERE scan_id = ? ORDER BY score DESC", (scan_id,), fetch="all")
    results = []
    if rows:
        for row in rows:
            try:
                r = _parse_data_column(row["data"])
                if r:
                    results.append(r)
            except Exception:
                pass
    return results

def get_score_history(symbol: str, days: int = 30) -> list[dict]:
    """Get score history for a stock."""
    resolved = resolve_symbol(symbol)
    rows = execute_db("""
        SELECT symbol, score, price, rsi, scan_date
        FROM score_history WHERE symbol=?
        ORDER BY scan_date DESC LIMIT ?
    """, (resolved.upper(), days), fetch="all")
    return rows

def get_sector_stats() -> list[dict]:
    """Get sector-wise stats."""
    scan_id = get_latest_completed_scan_id()
    rows = execute_db("""
        SELECT sector, COUNT(*) as count,
               AVG(score) as avg_score,
               SUM(high_conviction) as hc_count
        FROM scan_results_v2
        WHERE scan_id = ?
        GROUP BY sector
        ORDER BY avg_score DESC
    """, (scan_id,), fetch="all")
    return rows if rows else []

def clear_old_results(days: int = 7):
    """Remove results older than N days."""
    if is_postgresql():
        execute_db(f"DELETE FROM scan_results WHERE updated_at < NOW() - INTERVAL '{days} days'")
    else:
        execute_db("""
            DELETE FROM scan_results
            WHERE julianday('now') - julianday(updated_at) > ?
        """, (days,))

# ─── Custom Stocks ───

def add_custom_stock(symbol: str, exchange: str = "NSE", note: str = "") -> bool:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        execute_db("""
            INSERT INTO custom_stocks (symbol, exchange, added_at, note)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET note=excluded.note
        """, (symbol.upper(), exchange.upper(), now, note))
        return True
    except Exception:
        return False

def remove_custom_stock(symbol: str) -> bool:
    """Delete a custom stock using execute_db (fully migrated away from direct cursor)."""
    rowcount = execute_db(
        "DELETE FROM custom_stocks WHERE symbol=?",
        (symbol.upper(),),
        fetch="rowcount"
    )
    return bool(rowcount and rowcount > 0)

def get_custom_stocks() -> list[dict]:
    rows = execute_db("SELECT symbol, exchange, added_at, note FROM custom_stocks ORDER BY added_at DESC", fetch="all")
    res = []
    for r in rows:
        item = dict(r)
        if isinstance(item.get("added_at"), datetime):
            item["added_at"] = item["added_at"].strftime("%Y-%m-%d %H:%M:%S")
        res.append(item)
    return res

def is_custom_stock(symbol: str) -> bool:
    row = execute_db("SELECT 1 FROM custom_stocks WHERE symbol=?", (symbol.upper(),), fetch="one")
    return row is not None

# ─── Portfolios ───

def create_portfolio(name: str, description: str = "") -> int:
    """Create a new portfolio and return its ID (fully migrated to execute_db)."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if is_postgresql():
        # PostgreSQL: use RETURNING id clause via a raw pool query
        pool = _get_pg_pool()
        if pool:
            conn = None
            try:
                from psycopg2.extras import RealDictCursor
                conn = pool.getconn()
                conn.autocommit = True
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        "INSERT INTO portfolios (name, description, created_at, updated_at) VALUES (%s, %s, %s, %s) RETURNING id",
                        (name, description, now, now)
                    )
                    row = cur.fetchone()
                    return row["id"] if row else 0
            except Exception as exc:
                log.error("create_portfolio PG failed: %s", exc)
                return 0
            finally:
                if conn:
                    try:
                        pool.putconn(conn)
                    except Exception:
                        pass
        # Fallback: SQLite
    return execute_db(
        "INSERT INTO portfolios (name, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (name, description, now, now),
        fetch="lastrowid"
    ) or 0

def get_portfolios() -> list[dict]:
    rows = execute_db("SELECT * FROM portfolios ORDER BY created_at DESC", fetch="all")
    return [dict(r) for r in rows]

def get_portfolio(pid: int) -> dict | None:
    row = execute_db("SELECT * FROM portfolios WHERE id=?", (pid,), fetch="one")
    return dict(row) if row else None

def update_portfolio(pid: int, name: str = None, description: str = None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if name:
        execute_db("UPDATE portfolios SET name=?, updated_at=? WHERE id=?", (name, now, pid))
    if description is not None:
        execute_db("UPDATE portfolios SET description=?, updated_at=? WHERE id=?", (description, now, pid))

def delete_portfolio(pid: int):
    execute_db("DELETE FROM positions WHERE portfolio_id=?", (pid,))
    execute_db("DELETE FROM portfolios WHERE id=?", (pid,))

# ─── Positions (Trades) ───

def add_position(portfolio_id: int, symbol: str, quantity: int, buy_price: float,
                 buy_date: str, stop_loss: float = None, target: float = None,
                 notes: str = "") -> int:
    """Insert a new position and return its ID (fully migrated to execute_db)."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if is_postgresql():
        pool = _get_pg_pool()
        if pool:
            conn = None
            try:
                from psycopg2.extras import RealDictCursor
                conn = pool.getconn()
                conn.autocommit = True
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("""
                        INSERT INTO positions (portfolio_id, symbol, quantity, buy_price, buy_date,
                                               stop_loss, target, notes, status, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'OPEN', %s, %s) RETURNING id
                    """, (portfolio_id, symbol.upper(), quantity, buy_price, buy_date, stop_loss, target, notes, now, now))
                    row = cur.fetchone()
                    return row["id"] if row else 0
            except Exception as exc:
                log.error("add_position PG failed: %s", exc)
                return 0
            finally:
                if conn:
                    try:
                        pool.putconn(conn)
                    except Exception:
                        pass
        # Fallback: SQLite
    return execute_db("""
        INSERT INTO positions (portfolio_id, symbol, quantity, buy_price, buy_date,
                               stop_loss, target, notes, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
    """, (portfolio_id, symbol.upper(), quantity, buy_price, buy_date, stop_loss, target, notes, now, now),
    fetch="lastrowid") or 0

def close_position(position_id: int, sell_price: float, sell_date: str = None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not sell_date:
        sell_date = datetime.now().strftime("%Y-%m-%d")
    execute_db("""
        UPDATE positions SET sell_price=?, sell_date=?, status='CLOSED', updated_at=?
        WHERE id=?
    """, (sell_price, sell_date, now, position_id))

def update_position(position_id: int, **kwargs):
    """Update allowed position fields using execute_db (fully migrated)."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    allowed = {"quantity", "buy_price", "buy_date", "sell_price", "sell_date",
               "stop_loss", "target", "notes", "status"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return
    updates["updated_at"] = now
    set_clause = ", ".join(f"{k}=?" for k in updates)
    execute_db(f"UPDATE positions SET {set_clause} WHERE id=?", list(updates.values()) + [position_id])

def delete_position(position_id: int):
    execute_db("DELETE FROM positions WHERE id=?", (position_id,))

def get_positions(portfolio_id: int, status: str = None) -> list[dict]:
    if status:
        rows = execute_db(
            "SELECT * FROM positions WHERE portfolio_id=? AND status=? ORDER BY buy_date DESC",
            (portfolio_id, status.upper()), fetch="all")
    else:
        rows = execute_db(
            "SELECT * FROM positions WHERE portfolio_id=? ORDER BY status ASC, buy_date DESC",
            (portfolio_id,), fetch="all")
    return [dict(r) for r in rows]

def get_position(position_id: int) -> dict | None:
    row = execute_db("SELECT * FROM positions WHERE id=?", (position_id,), fetch="one")
    return dict(row) if row else None

def get_portfolio_summary(portfolio_id: int) -> dict:
    open_pos = execute_db(
        "SELECT COUNT(*) as cnt, SUM(quantity * buy_price) as invested FROM positions WHERE portfolio_id=? AND status='OPEN'",
        (portfolio_id,), fetch="one")
    closed_pos = execute_db("""
        SELECT COUNT(*) as cnt,
               SUM((sell_price - buy_price) * quantity) as realized_pnl,
               SUM(quantity * buy_price) as total_cost
        FROM positions WHERE portfolio_id=? AND status='CLOSED'
    """, (portfolio_id,), fetch="one")

    invested = open_pos.get("invested") or 0 if open_pos else 0
    open_cnt = open_pos.get("cnt") or 0 if open_pos else 0
    realized_pnl = closed_pos.get("realized_pnl") or 0 if closed_pos else 0
    closed_cnt = closed_pos.get("cnt") or 0 if closed_pos else 0
    total_traded = closed_pos.get("total_cost") or 0 if closed_pos else 0

    return {
        "open_count": open_cnt,
        "invested": round(float(invested), 2),
        "closed_count": closed_cnt,
        "realized_pnl": round(float(realized_pnl), 2),
        "total_traded": round(float(total_traded), 2),
    }

def db_stats() -> dict:
    """Get DB statistics including pool health."""
    results_cnt = execute_db("SELECT COUNT(*) as cnt FROM scan_results", fetch="count")
    history_cnt = execute_db("SELECT COUNT(*) as cnt FROM score_history", fetch="count")
    meta_cnt = execute_db("SELECT COUNT(*) as cnt FROM scan_meta", fetch="count")

    size_kb = 0.0
    if not is_postgresql() or not _pg_pool:
        size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
        size_kb = round(size / 1024, 1)

    backend = "PostgreSQL/Supabase" if (is_postgresql() and _pg_pool) else ("SQLite (Fallback)" if is_postgresql() else "SQLite")

    return {
        "results": results_cnt,
        "history_records": history_cnt,
        "meta_entries": meta_cnt,
        "db_size_kb": size_kb,
        "backend": backend,
        **pool_status(),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# RELEASE 3 — OUTCOME INTELLIGENCE LAYER
# ═══════════════════════════════════════════════════════════════════════════════

_PAPER_VIRTUAL_CAPITAL = 25000  # ₹25,000 per pick
_PAPER_MAX_HOLD_DAYS = 20       # 20 trading days
_PAPER_COOLDOWN_DAYS = 5        # Don't re-pick within 5 days
_PAPER_TOP_N_SNAPSHOT = 20      # Snapshot top 20 daily
_PAPER_TOP_N_TRADES = 5         # Paper trade top 5


def create_paper_trade(stock_data: dict, nifty_price: float = None,
                       market_regime: str = "unknown",
                       source: str = "QUANT") -> int | None:
    """Create a paper trade entry from scan result data.
    Returns the trade ID, or None if duplicate cooldown applies.

    `source` tags the trade origin: 'QUANT' (system/scanner) or 'MANUAL'
    (user-entered via the Add paper trade form).
    """
    sym = stock_data.get("symbol", "")
    if not sym:
        return None

    # Model-aware dedup: one 'legacy' AND one 'scoring_v1' position may coexist on
    # the same symbol (each engine only blocks its OWN duplicate).
    mv = _canon_model_version(stock_data.get("model_version", ""))
    entry_date = datetime.now().strftime("%Y-%m-%d")

    # Duplicate prevention: 5-day cooldown (scoped to this engine)
    existing = execute_db(
        "SELECT id, entry_date FROM paper_trades WHERE symbol = ? AND status = 'OPEN' AND model_version = ?",
        (sym, mv), fetch="one"
    )
    if existing:
        return None  # this engine already has an open position

    recent = execute_db(
        "SELECT entry_date FROM paper_trades WHERE symbol = ? AND model_version = ? ORDER BY entry_date DESC LIMIT 1",
        (sym, mv), fetch="one"
    )
    if recent:
        from datetime import date as _date
        try:
            last_dt = _date.fromisoformat(recent["entry_date"])
            today_dt = _date.fromisoformat(entry_date)
            if (today_dt - last_dt).days < _PAPER_COOLDOWN_DAYS:
                # Allow if score improved by 10+
                prev_score = execute_db(
                    "SELECT score_at_entry FROM paper_trades WHERE symbol = ? AND model_version = ? ORDER BY entry_date DESC LIMIT 1",
                    (sym, mv), fetch="one"
                )
                curr_score = stock_data.get("score", 0)
                if prev_score and curr_score - prev_score.get("score_at_entry", 0) < 10:
                    log.debug("Paper trade cooldown: %s picked %s days ago", sym, (today_dt - last_dt).days)
                    return None
        except Exception:
            pass

    entry_price = stock_data.get("price", 0)
    if entry_price <= 0:
        return None

    # Honour an explicit quantity (manual entry); else size from virtual capital
    _explicit_qty = stock_data.get("quantity")
    if _explicit_qty:
        quantity = max(1, int(_explicit_qty))
    else:
        quantity = max(1, int(_PAPER_VIRTUAL_CAPITAL / entry_price))

    execute_db("""
        INSERT INTO paper_trades (
            symbol, sector, entry_date, entry_price, target_price, stop_loss,
            virtual_capital, quantity,
            score_at_entry, grade_at_entry,
            technical_score, fundamental_score, earnings_momentum_score, earnings_grade,
            smart_money_score, sector_rotation_score, catalyst_score, news_sentiment_score,
            risk_score, risk_reward,
            model_version, market_regime, nifty_entry,
            high_conviction, is_golden, signals_json, earnings_signals_json,
            weight_version, confidence_score, entry_rank,
            breadth_advances, breadth_declines, breadth_ratio,
            status, source
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        sym,
        stock_data.get("sector", ""),
        entry_date,
        entry_price,
        stock_data.get("target_price"),
        stock_data.get("stop_loss"),
        _PAPER_VIRTUAL_CAPITAL,
        quantity,
        stock_data.get("score", 0),
        stock_data.get("grade", ""),
        stock_data.get("technical_score", 0),
        stock_data.get("fundamental_score", 0),
        stock_data.get("earnings_momentum_score", 0),
        stock_data.get("earnings_grade", ""),
        stock_data.get("smart_money_score", 0),
        stock_data.get("sector_rotation_score", 0),
        stock_data.get("marketaux_catalyst_score", 0),
        stock_data.get("news_sentiment_score", 0),
        stock_data.get("risk_score", 0),
        stock_data.get("risk_reward", 0),
        mv,
        market_regime,
        nifty_price,
        1 if stock_data.get("high_conviction") else 0,
        1 if stock_data.get("is_golden") else 0,
        json.dumps(stock_data.get("signals", [])[:10]),
        json.dumps(stock_data.get("earnings_signals", [])[:10]),
        stock_data.get("weight_version", "R2.1"),
        stock_data.get("confidence_score", 0),
        stock_data.get("_entry_rank", 0),
        stock_data.get("_breadth_advances", 0),
        stock_data.get("_breadth_declines", 0),
        stock_data.get("_breadth_ratio", 0),
        "OPEN",
        source,
    ))

    trade_id = execute_db("SELECT MAX(id) as id FROM paper_trades WHERE symbol = ?", (sym,), fetch="one")
    log.info("[PaperTrade] ENTRY: %s @ ₹%.2f, score=%d, grade=%s",
             sym, entry_price, stock_data.get("score", 0), stock_data.get("grade", ""))
    # Phase A: Invalidate dashboard/stats cache on new trade
    try:
        import cache_layer
        cache_layer.invalidate_stats()
    except Exception:
        pass
    return trade_id["id"] if trade_id else None


def close_paper_trade(trade_id: int, exit_price: float, exit_reason: str,
                      nifty_price: float = None) -> bool:
    """Close a paper trade with outcome data."""
    trade = execute_db("SELECT * FROM paper_trades WHERE id = ?", (trade_id,), fetch="one")
    if not trade or trade["status"] != "OPEN":
        return False

    entry_price = trade["entry_price"]
    return_pct = ((exit_price - entry_price) / entry_price) * 100

    # Alpha vs Nifty
    alpha_pct = None
    nifty_entry = trade.get("nifty_entry")
    if nifty_entry and nifty_price and nifty_entry > 0:
        nifty_return = ((nifty_price - nifty_entry) / nifty_entry) * 100
        alpha_pct = return_pct - nifty_return

    # Days held
    from datetime import date as _date
    try:
        entry_dt = _date.fromisoformat(trade["entry_date"])
        exit_dt = _date.today()
        days_held = (exit_dt - entry_dt).days
    except Exception:
        days_held = 0

    exit_date = datetime.now().strftime("%Y-%m-%d")

    execute_db("""
        UPDATE paper_trades SET
            exit_date=?, exit_price=?, exit_reason=?, nifty_exit=?,
            days_held=?, return_pct=?, alpha_pct=?,
            status='CLOSED'
        WHERE id=?
    """, (exit_date, exit_price, exit_reason, nifty_price,
          days_held, round(return_pct, 2), round(alpha_pct, 2) if alpha_pct else None,
          trade_id))

    log.info("[PaperTrade] EXIT: %s @ ₹%.2f (%s), return=%.2f%%, alpha=%.2f%%, held=%d days",
             trade["symbol"], exit_price, exit_reason, return_pct,
             alpha_pct or 0, days_held)
    # Phase A: Invalidate dashboard/stats cache on trade close
    try:
        import cache_layer
        cache_layer.invalidate_stats()
    except Exception:
        pass

    # ── R1 Evidence Collection: trade_outcomes.csv (Append-Only) ──
    try:
        _R1_DEPLOY_DATE = "2026-06-08"
        from datetime import date as _date
        _obs_day = (_date.today() - _date.fromisoformat(_R1_DEPLOY_DATE)).days + 1
        _scan_id = get_meta("current_scan_id") or "manual"
        _outcomes_path = Path(__file__).parent / "release_audits" / "trade_outcomes.csv"
        _outcomes_path.parent.mkdir(parents=True, exist_ok=True)
        _write_header = not _outcomes_path.exists()
        with open(_outcomes_path, "a", newline="", encoding="utf-8") as f:
            import csv as _csv
            w = _csv.writer(f)
            if _write_header:
                w.writerow([
                    "Release Version", "Observation Day", "Scan ID",
                    "Date Opened", "Date Closed", "Symbol",
                    "Entry Price", "Exit Price", "Exit Reason",
                    "HC Flag (Entry)", "Golden Flag (Entry)",
                    "Score (Entry)", "Risk Score (Entry)", "RR (Entry)",
                    "Sector", "Return %", "Win/Loss",
                ])
            w.writerow([
                "R1.0", _obs_day, _scan_id,
                trade.get("entry_date", ""), exit_date, trade["symbol"],
                entry_price, exit_price, exit_reason,
                trade.get("high_conviction", 0), trade.get("is_golden", 0),
                trade.get("score_at_entry", 0), trade.get("risk_score", 0),
                trade.get("risk_reward", 0),
                trade.get("sector", ""), round(return_pct, 2),
                "WIN" if return_pct > 0 else "LOSS",
            ])
        log.info("[R1 Evidence] trade_outcomes.csv appended: %s return=%.2f%%", trade["symbol"], return_pct)
    except Exception as _ev_exc:
        log.warning("[R1 Evidence] trade_outcomes.csv write failed (non-fatal): %s", _ev_exc)

    return True


def update_paper_trade_extremes(trade_id: int, current_price: float):
    """Update max drawdown and max runup for an open trade."""
    trade = execute_db("SELECT entry_price, max_drawdown_pct, max_runup_pct FROM paper_trades WHERE id = ?",
                       (trade_id,), fetch="one")
    if not trade:
        return

    entry = trade["entry_price"]
    current_pct = ((current_price - entry) / entry) * 100
    new_dd = min(trade.get("max_drawdown_pct") or 0, current_pct)
    new_ru = max(trade.get("max_runup_pct") or 0, current_pct)

    execute_db(
        "UPDATE paper_trades SET max_drawdown_pct=?, max_runup_pct=? WHERE id=?",
        (round(new_dd, 2), round(new_ru, 2), trade_id)
    )


def get_open_paper_trades() -> list[dict]:
    """Get all open paper trades."""
    return execute_db(
        "SELECT * FROM paper_trades WHERE status = 'OPEN' ORDER BY entry_date",
        fetch="all"
    ) or []


def get_all_paper_trades(limit: int = 200, model_version: str = None) -> list[dict]:
    """Get paper trades (open + closed), latest first.

    model_version: None -> resolve the UI toggle meta 'ui_reco_source' (default
    'scoring_v1') so the paper-trade UI shows the active engine ONLY; legacy trades
    stay in the DB but hidden. Pass an explicit value to override (e.g. 'legacy').
    """
    if model_version is None:
        model_version = get_meta("ui_reco_source") or "scoring_v1"
    return execute_db(
        "SELECT * FROM paper_trades WHERE model_version = ? ORDER BY entry_date DESC LIMIT ?",
        (model_version, limit), fetch="all"
    ) or []


def get_model_comparison() -> list[dict]:
    """Per-engine PER-TRADE QUALITY by model_version over CLOSED paper_trades.

    Per D2 this is per-trade quality, NOT portfolio return (the engines submit
    different numbers of trades, so totals are not comparable). Metrics: #trades,
    win-rate, avg return_pct, avg alpha_pct, max drawdown, avg days held.
    """
    rows = execute_db("""
        SELECT model_version,
               COUNT(*) AS trades,
               ROUND(100.0 * SUM(CASE WHEN return_pct > 0 THEN 1 ELSE 0 END) / NULLIF(COUNT(*),0), 1) AS win_rate,
               ROUND(AVG(return_pct), 2)  AS avg_return_pct,
               ROUND(AVG(alpha_pct), 2)   AS avg_alpha_pct,
               ROUND(MAX(max_drawdown_pct), 2) AS max_drawdown_pct,
               ROUND(AVG(days_held), 1)   AS avg_days_held
        FROM paper_trades WHERE status='CLOSED'
        GROUP BY model_version ORDER BY model_version
    """, fetch="all") or []
    return [dict(r) for r in rows]


def save_recommendation_snapshot(snapshot_date: str, ranked_stocks: list[dict],
                                  market_regime: str = "unknown"):
    """Save daily top-N recommendation snapshot for calibration."""
    for i, stock in enumerate(ranked_stocks[:_PAPER_TOP_N_SNAPSHOT]):
        try:
            execute_db("""
                INSERT INTO recommendation_snapshots (
                    snapshot_date, symbol, rank, score, grade,
                    technical_score, fundamental_score,
                    earnings_momentum_score, earnings_grade,
                    smart_money_score, risk_score, price,
                    model_version, market_regime
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(snapshot_date, symbol) DO UPDATE SET
                    rank=excluded.rank, score=excluded.score
            """, (
                snapshot_date,
                stock.get("symbol", ""),
                i + 1,
                stock.get("score", 0),
                stock.get("grade", ""),
                stock.get("technical_score", 0),
                stock.get("fundamental_score", 0),
                stock.get("earnings_momentum_score", 0),
                stock.get("earnings_grade", ""),
                stock.get("smart_money_score", 0),
                stock.get("risk_score", 0),
                stock.get("price", 0),
                _canon_model_version(stock.get("model_version", "")),
                market_regime,
            ))
        except Exception as exc:
            log.debug("snapshot save failed for %s: %s", stock.get("symbol"), exc)

    log.info("[PaperTrade] Saved snapshot: %d stocks for %s", min(len(ranked_stocks), _PAPER_TOP_N_SNAPSHOT), snapshot_date)

def save_research_snapshot_v2(symbol: str, rec_data: dict, scan_context: 'ScanContext' = None):
    """
    Save or version an immutable research snapshot (Phase 5 & 6).
    """
    import hashlib
    import json
    
    # Extract trade details
    trade = rec_data.get("trade", {})
    if not trade:
        return
        
    entry_low = trade.get("entry_low", 0)
    entry_high = trade.get("entry_high", 0)
    stop_loss = trade.get("stop_loss", 0)
    target_1 = trade.get("target_1") or trade.get("target1", 0)
    target_2 = trade.get("target_2") or trade.get("target2", 0)
    target_3 = trade.get("target_3") or trade.get("target3", 0)
    risk_reward = trade.get("risk_reward", 0)
    cmp_at_generation = trade.get("cmp", 0)
    
    recommendation = rec_data.get("scan_analysis", "")
    if not recommendation:
        recommendation = "Hold" if "Hold" in str(rec_data) else "Buy"
        
    confidence = rec_data.get("confidence", 0)
    confidence_breakdown = rec_data.get("hc_reasons", [])
    research_thesis = rec_data.get("ai_summary", "")
    
    score_at_generation = rec_data.get("score", 0)
    raw_score_at_generation = rec_data.get("raw_score", 0) # Assumes raw_score exists, fallback to 0

    scan_id = scan_context.scan_id if scan_context else "manual"
    correlation_id = scan_context.correlation_id if scan_context else ""
    scanner_version = scan_context.scanner_version if scan_context else ""
    scoring_version = scan_context.scoring_version if scan_context else ""
    recommendation_version = scan_context.recommendation_version if scan_context else ""
    config_snapshot = json.dumps(scan_context.config_snapshot) if scan_context and scan_context.config_snapshot else "{}"
    
    # Hash of critical fields to detect material changes. 
    # Must NOT include scan_id, otherwise every scan forces a version bump!
    hash_input = f"{symbol}|{entry_low}|{stop_loss}|{target_1}|{recommendation}"
    snapshot_hash = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()
    
    # Fetch current active version
    current_active = execute_db(
        "SELECT version, recommendation, entry_low, stop_loss, target_1, snapshot_hash FROM research_snapshots_v2 WHERE symbol = ? AND status = 'ACTIVE' ORDER BY version DESC LIMIT 1",
        (symbol,), fetch="one"
    )
    
    next_version = 1
    if current_active:
        # Phase 6: Material Change Policy
        # For now, we version if the recommendation or target/SL has changed.
        old_rec = current_active.get("recommendation")
        old_sl = current_active.get("stop_loss")
        old_hash = current_active.get("snapshot_hash")
        
        # Simple policy: if hash is identical, skip. If material change, supersede and bump.
        if old_hash == snapshot_hash:
            return # No material change
            
        next_version = current_active.get("version", 0) + 1
        
        # Supersede old
        execute_db(
            "UPDATE research_snapshots_v2 SET status = 'SUPERSEDED' WHERE symbol = ? AND status = 'ACTIVE'",
            (symbol,)
        )
        
    _snapshot_query = """
        INSERT INTO research_snapshots_v2 (
            symbol, version, recommendation, entry_low, entry_high,
            stop_loss, target_1, target_2, target_3, risk_reward,
            confidence, confidence_breakdown, research_thesis, cmp_at_generation,
            score_at_generation, raw_score_at_generation,
            scan_id, correlation_id, scanner_version, scoring_version,
            recommendation_version, config_snapshot, snapshot_hash,
            status, outcome_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', 'PENDING')
    """
    _snapshot_params = (
        symbol, next_version, recommendation, entry_low, entry_high,
        stop_loss, target_1, target_2, target_3, risk_reward,
        confidence, json.dumps(confidence_breakdown), research_thesis, cmp_at_generation,
        score_at_generation, raw_score_at_generation,
        scan_id, correlation_id, scanner_version, scoring_version,
        recommendation_version, config_snapshot, snapshot_hash
    )
    execute_db(_snapshot_query, _snapshot_params)

    # P0.1E: Queue to governance DLQ for PG replay if currently on SQLite fallback
    if pg_cooldown_active() or not is_postgresql():
        queue_governance_write(_snapshot_query, _snapshot_params, artifact_type="research_snapshots_v2")

def get_research_history(symbol: str) -> list[dict]:
    """
    Retrieve the full timeline of research snapshots for a symbol.
    """
    resolved = resolve_symbol(symbol)
    rows = execute_db("""
        SELECT version, status, outcome_status, recommendation,
               entry_low, entry_high, stop_loss, target_1, target_2, target_3,
               risk_reward, confidence, confidence_breakdown, research_thesis,
               cmp_at_generation, created_at, snapshot_hash
        FROM research_snapshots_v2
        WHERE symbol = ?
        ORDER BY version DESC
    """, (resolved,), fetch="all")
    
    import json
    history = []
    for r in rows:
        r_dict = dict(r)
        if r_dict.get("confidence_breakdown"):
            try:
                r_dict["confidence_breakdown"] = json.loads(r_dict["confidence_breakdown"])
            except:
                pass
        history.append(r_dict)
    return history

def create_research_advisory(symbol: str, advisory_type: str, advisory_text: str,
                             priority: str, issued_by: str = "system",
                             valid_until: str = None) -> int:
    """Issue a new research advisory."""
    return execute_db("""
        INSERT INTO research_advisories (
            symbol, advisory_type, advisory_text, priority, issued_by, valid_until
        ) VALUES (?, ?, ?, ?, ?, ?) RETURNING id
    """, (symbol, advisory_type, advisory_text, priority, issued_by, valid_until), fetch="one").get("id")

def get_research_advisories(symbol: str = None, active_only: bool = True) -> list[dict]:
    """Retrieve research advisories, optionally filtered by symbol and active status."""
    query = "SELECT * FROM research_advisories WHERE 1=1"
    params = []
    
    if symbol:
        resolved = resolve_symbol(symbol)
        query += " AND symbol = ?"
        params.append(resolved)
        
    if active_only:
        query += " AND is_active = TRUE AND (valid_until IS NULL OR valid_until >= CURRENT_TIMESTAMP)"
        
    query += " ORDER BY created_at DESC"
    
    rows = execute_db(query, tuple(params), fetch="all")
    return [dict(r) for r in rows]

def update_research_lifecycle_outcomes(prices: dict):
    """
    Research Lifecycle Engine
    Updates outcome_status in research_snapshots_v2 dynamically.
    Transitions:
    PENDING -> ACTIVE
    ACTIVE -> TARGET1_HIT -> TARGET2_HIT -> TARGET3_HIT
    * -> STOP_LOSS_HIT
    * -> CLOSED (after 20 days)
    """
    try:
        active_snaps = execute_db(
            "SELECT id, symbol, entry_low, stop_loss, target_1, target_2, target_3, outcome_status, created_at FROM research_snapshots_v2 WHERE status = 'ACTIVE' AND outcome_status != 'CLOSED'",
            fetch="all"
        )
        if not active_snaps:
            return
            
        from datetime import datetime
        now = datetime.now()
        
        for snap in active_snaps:
            sym = snap["symbol"]
            ltp = prices.get(sym)
            if not ltp:
                continue
                
            status = snap["outcome_status"]
            new_status = status
            
            created_at = snap["created_at"]
            if created_at:
                try:
                    dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
                    days_held = (now - dt.replace(tzinfo=None)).days
                    if days_held >= 20:
                        new_status = "CLOSED"
                except Exception:
                    pass
            
            if new_status != "CLOSED":
                if status == "PENDING" and ltp >= snap.get("entry_low", 0):
                    new_status = "ACTIVE"
                    
                if snap.get("stop_loss") and ltp <= snap["stop_loss"]:
                    new_status = "STOP_LOSS_HIT"
                elif snap.get("target_3") and ltp >= snap["target_3"] and status in ("ACTIVE", "TARGET1_HIT", "TARGET2_HIT"):
                    new_status = "TARGET3_HIT"
                elif snap.get("target_2") and ltp >= snap["target_2"] and status in ("ACTIVE", "TARGET1_HIT"):
                    new_status = "TARGET2_HIT"
                elif snap.get("target_1") and ltp >= snap["target_1"] and status in ("PENDING", "ACTIVE"):
                    new_status = "TARGET1_HIT"
            
            # Illegal transition check (e.g. TARGET1_HIT back to PENDING)
            _valid_transitions = {
                "PENDING": ["ACTIVE", "TARGET1_HIT", "STOP_LOSS_HIT", "CLOSED"],
                "ACTIVE": ["TARGET1_HIT", "TARGET2_HIT", "TARGET3_HIT", "STOP_LOSS_HIT", "CLOSED"],
                "TARGET1_HIT": ["TARGET2_HIT", "TARGET3_HIT", "STOP_LOSS_HIT", "CLOSED"],
                "TARGET2_HIT": ["TARGET3_HIT", "STOP_LOSS_HIT", "CLOSED"],
                "TARGET3_HIT": ["CLOSED", "STOP_LOSS_HIT"],
                "STOP_LOSS_HIT": ["CLOSED"],
                "CLOSED": []
            }
            
            if new_status != status:
                if new_status in _valid_transitions.get(status, []):
                    execute_db("UPDATE research_snapshots_v2 SET outcome_status = ? WHERE id = ?", (new_status, snap["id"]))
                    log.info("[LifecycleEngine] %s transitioned: %s -> %s (LTP: %s)", sym, status, new_status, ltp)
                else:
                    log.error("[LifecycleEngine] ILLEGAL TRANSITION PREVENTED for %s: %s -> %s", sym, status, new_status)
    except Exception as exc:
        log.warning("[LifecycleEngine] Error updating outcomes: %s", exc)

def save_portfolio_daily(nifty_price: float = None):
    """Save daily equity curve point."""
    today = datetime.now().strftime("%Y-%m-%d")

    open_trades = execute_db("SELECT COUNT(*) as cnt FROM paper_trades WHERE status='OPEN'", fetch="one")
    closed_total = execute_db("SELECT COUNT(*) as cnt FROM paper_trades WHERE status='CLOSED'", fetch="one")
    closed_today_r = execute_db("SELECT COUNT(*) as cnt FROM paper_trades WHERE status='CLOSED' AND exit_date=?", (today,), fetch="one")
    wins = execute_db("SELECT COUNT(*) as cnt FROM paper_trades WHERE status='CLOSED' AND return_pct > 0", fetch="one")
    losses = execute_db("SELECT COUNT(*) as cnt FROM paper_trades WHERE status='CLOSED' AND return_pct <= 0", fetch="one")
    avg_return = execute_db("SELECT AVG(return_pct) as avg FROM paper_trades WHERE status='CLOSED'", fetch="one")

    # Portfolio value: sum of open positions at current virtual capital
    open_value = execute_db("SELECT SUM(virtual_capital) as total FROM paper_trades WHERE status='OPEN'", fetch="one")

    execute_db("""
        INSERT INTO paper_portfolio_daily (date, portfolio_value, invested_value,
            open_positions, closed_today, total_closed, win_count, loss_count,
            total_return_pct, nifty_level, model_version)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(date) DO UPDATE SET
            portfolio_value=excluded.portfolio_value,
            open_positions=excluded.open_positions,
            closed_today=excluded.closed_today,
            total_closed=excluded.total_closed,
            win_count=excluded.win_count,
            loss_count=excluded.loss_count,
            total_return_pct=excluded.total_return_pct,
            nifty_level=excluded.nifty_level
    """, (
        today,
        (open_value or {}).get("total") or 0,
        (open_value or {}).get("total") or 0,
        (open_trades or {}).get("cnt") or 0,
        (closed_today_r or {}).get("cnt") or 0,
        (closed_total or {}).get("cnt") or 0,
        (wins or {}).get("cnt") or 0,
        (losses or {}).get("cnt") or 0,
        round((avg_return or {}).get("avg") or 0, 2),
        nifty_price or 0,
        _canon_model_version("R2.1"),  # -> 'legacy' (per-model portfolio split out of scope per D2)
    ))


def get_paper_trade_stats(model_version: str = None) -> dict:
    """Get aggregated paper trading statistics for ONE engine.

    model_version: None -> resolve the UI toggle meta 'ui_reco_source' (default
    'scoring_v1'); pass an explicit engine to get that engine's stats. ALL
    headline aggregates are filtered by model_version so engines are NEVER
    blended. (by_model_version stays a per-engine GROUP BY breakdown, which is
    separation-by-design, not a blend — it powers the Comparison view.)

    Phase 1 V2: Consolidated from 21 queries to 5 queries.
    Uses CASE WHEN (SQLite+PG compatible) instead of PG-only FILTER.
    Gated by ENABLE_PAPER_STATS_V2 (default: true).
    """
    if os.getenv("ENABLE_PAPER_STATS_V2", "true").lower() != "true":
        return _get_paper_trade_stats_v1(model_version)

    mv = model_version if model_version else (get_meta("ui_reco_source") or "scoring_v1")
    t_start = time.perf_counter()

    # ── Query 1: Single aggregation replaces 14 scalar queries ──
    t0 = time.perf_counter()
    agg = execute_db("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END) as closed,
            SUM(CASE WHEN status='CLOSED' AND return_pct > 0 THEN 1 ELSE 0 END) as wins,
            AVG(CASE WHEN status='CLOSED' THEN return_pct ELSE NULL END) as avg_ret,
            AVG(CASE WHEN status='CLOSED' THEN days_held ELSE NULL END) as avg_days,
            AVG(CASE WHEN status='CLOSED' THEN max_drawdown_pct ELSE NULL END) as avg_dd,
            AVG(CASE WHEN status='CLOSED' AND alpha_pct IS NOT NULL THEN alpha_pct ELSE NULL END) as avg_alpha,
            MIN(CASE WHEN status='CLOSED' THEN max_drawdown_pct ELSE NULL END) as max_dd,
            SUM(CASE WHEN status='CLOSED' AND return_pct > 0 THEN return_pct ELSE 0 END) as sum_wins,
            SUM(CASE WHEN status='CLOSED' AND return_pct <= 0 THEN ABS(return_pct) ELSE 0 END) as sum_losses,
            SUM(CASE WHEN status='CLOSED' AND is_golden=1 THEN 1 ELSE 0 END) as golden_total,
            SUM(CASE WHEN status='CLOSED' AND is_golden=1 AND return_pct > 0 THEN 1 ELSE 0 END) as golden_wins,
            SUM(CASE WHEN status='CLOSED' AND high_conviction=1 THEN 1 ELSE 0 END) as hc_total,
            SUM(CASE WHEN status='CLOSED' AND high_conviction=1 AND return_pct > 0 THEN 1 ELSE 0 END) as hc_wins
        FROM paper_trades WHERE model_version = ?
    """, (mv,), fetch="one") or {}
    t_agg = round((time.perf_counter() - t0) * 1000, 2)

    total_cnt = (agg.get("total") or 0)
    closed_cnt = (agg.get("closed") or 0)
    win_cnt = (agg.get("wins") or 0)
    loss_cnt = closed_cnt - win_cnt
    total_win = (agg.get("sum_wins") or 0)
    total_loss = (agg.get("sum_losses") or 0)
    golden_cnt = (agg.get("golden_total") or 0)
    golden_win_cnt = (agg.get("golden_wins") or 0)
    hc_cnt = (agg.get("hc_total") or 0)
    hc_win_cnt = (agg.get("hc_wins") or 0)

    profit_factor = round(total_win / total_loss, 2) if total_loss > 0 else (None if total_win > 0 else 0.0)
    avg_win = total_win / win_cnt if win_cnt > 0 else 0
    avg_loss = total_loss / loss_cnt if loss_cnt > 0 else 0
    win_rate_dec = win_cnt / closed_cnt if closed_cnt > 0 else 0
    expectancy = round((win_rate_dec * avg_win) - ((1 - win_rate_dec) * avg_loss), 2) if closed_cnt > 0 else 0

    # ── Query 2: Best/worst trades ──
    t0 = time.perf_counter()
    best = execute_db("SELECT symbol, return_pct FROM paper_trades WHERE status='CLOSED' AND model_version = ? ORDER BY return_pct DESC LIMIT 1", (mv,), fetch="one")
    worst = execute_db("SELECT symbol, return_pct FROM paper_trades WHERE status='CLOSED' AND model_version = ? ORDER BY return_pct ASC LIMIT 1", (mv,), fetch="one")
    t_best_worst = round((time.perf_counter() - t0) * 1000, 2)

    # ── Query 3: Factor attribution (winners vs losers in one query) ──
    t0 = time.perf_counter()
    factor = execute_db("""
        SELECT
            AVG(CASE WHEN return_pct > 0 THEN technical_score ELSE NULL END) as win_tech,
            AVG(CASE WHEN return_pct > 0 THEN fundamental_score ELSE NULL END) as win_fund,
            AVG(CASE WHEN return_pct > 0 THEN earnings_momentum_score ELSE NULL END) as win_earn,
            AVG(CASE WHEN return_pct > 0 THEN smart_money_score ELSE NULL END) as win_smart,
            AVG(CASE WHEN return_pct > 0 THEN risk_score ELSE NULL END) as win_risk,
            AVG(CASE WHEN return_pct > 0 THEN score_at_entry ELSE NULL END) as win_score,
            AVG(CASE WHEN return_pct <= 0 THEN technical_score ELSE NULL END) as loss_tech,
            AVG(CASE WHEN return_pct <= 0 THEN fundamental_score ELSE NULL END) as loss_fund,
            AVG(CASE WHEN return_pct <= 0 THEN earnings_momentum_score ELSE NULL END) as loss_earn,
            AVG(CASE WHEN return_pct <= 0 THEN smart_money_score ELSE NULL END) as loss_smart,
            AVG(CASE WHEN return_pct <= 0 THEN risk_score ELSE NULL END) as loss_risk,
            AVG(CASE WHEN return_pct <= 0 THEN score_at_entry ELSE NULL END) as loss_score
        FROM paper_trades WHERE status='CLOSED' AND model_version = ?
    """, (mv,), fetch="one") or {}
    t_factor = round((time.perf_counter() - t0) * 1000, 2)

    # ── Query 4-5: Group breakdowns (already efficient GROUP BY) ──
    t0 = time.perf_counter()
    by_version = execute_db("""
        SELECT model_version, COUNT(*) as trades,
               SUM(CASE WHEN return_pct > 0 THEN 1 ELSE 0 END) as wins,
               AVG(return_pct) as avg_return,
               AVG(alpha_pct) as avg_alpha
        FROM paper_trades WHERE status='CLOSED'
        GROUP BY model_version ORDER BY model_version
    """, fetch="all") or []

    by_sector = execute_db("""
        SELECT sector, COUNT(*) as trades,
               AVG(return_pct) as avg_return
        FROM paper_trades WHERE status='CLOSED' AND model_version = ?
        GROUP BY sector ORDER BY avg_return DESC LIMIT 10
    """, (mv,), fetch="all") or []

    by_regime = execute_db("""
        SELECT market_regime, COUNT(*) as trades,
               AVG(return_pct) as avg_return
        FROM paper_trades WHERE status='CLOSED' AND model_version = ?
        GROUP BY market_regime
    """, (mv,), fetch="all") or []
    t_groups = round((time.perf_counter() - t0) * 1000, 2)

    total_ms = round((time.perf_counter() - t_start) * 1000, 2)
    log.info("[DB PERF] get_paper_trade_stats V2 | total_queries=5 | t_agg=%s ms | t_best_worst=%s ms | t_factor=%s ms | t_groups=%s ms | total=%s ms", t_agg, t_best_worst, t_factor, t_groups, total_ms)
    print(f"[DB PERF] get_paper_trade_stats V2 | total_queries=5 | t_agg={t_agg} ms | t_best_worst={t_best_worst} ms | t_factor={t_factor} ms | t_groups={t_groups} ms | total={total_ms} ms", flush=True)

    def _r(v): return float(round(v or 0, 2))

    return {
        "total_trades": total_cnt,
        "open_trades": total_cnt - closed_cnt,
        "closed_trades": closed_cnt,
        "win_rate": round((win_cnt / closed_cnt * 100), 1) if closed_cnt > 0 else 0,
        "avg_return_pct": _r(agg.get("avg_ret")),
        "avg_days_held": float(round((agg.get("avg_days") or 0), 1)),
        "avg_drawdown_pct": _r(agg.get("avg_dd")),
        "max_drawdown_pct": _r(agg.get("max_dd")),
        "avg_alpha_pct": _r(agg.get("avg_alpha")),
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "best_trade": {"symbol": best["symbol"], "return_pct": best["return_pct"]} if best else None,
        "worst_trade": {"symbol": worst["symbol"], "return_pct": worst["return_pct"]} if worst else None,
        # Conviction breakdowns
        "golden_stock": {
            "trades": golden_cnt,
            "win_rate": round(golden_win_cnt / golden_cnt * 100, 1) if golden_cnt > 0 else 0,
        },
        "high_conviction": {
            "trades": hc_cnt,
            "win_rate": round(hc_win_cnt / hc_cnt * 100, 1) if hc_cnt > 0 else 0,
        },
        # Factor attribution
        "factor_attribution": {
            "winners": {
                "avg_score": _r(factor.get("win_score")),
                "avg_technical": _r(factor.get("win_tech")),
                "avg_fundamental": _r(factor.get("win_fund")),
                "avg_earnings": _r(factor.get("win_earn")),
                "avg_smart_money": _r(factor.get("win_smart")),
                "avg_risk": _r(factor.get("win_risk")),
            },
            "losers": {
                "avg_score": _r(factor.get("loss_score")),
                "avg_technical": _r(factor.get("loss_tech")),
                "avg_fundamental": _r(factor.get("loss_fund")),
                "avg_earnings": _r(factor.get("loss_earn")),
                "avg_smart_money": _r(factor.get("loss_smart")),
                "avg_risk": _r(factor.get("loss_risk")),
            },
        },
        "by_model_version": [
            {"version": r["model_version"], "trades": r["trades"],
             "win_rate": round(r["wins"] / r["trades"] * 100, 1) if r["trades"] > 0 else 0,
             "avg_return": _r(r["avg_return"]),
             "avg_alpha": _r(r["avg_alpha"])}
            for r in by_version
        ],
        "by_sector": [
            {"sector": r["sector"], "trades": r["trades"], "avg_return": _r(r["avg_return"])}
            for r in by_sector
        ],
        "by_regime": [
            {"regime": r["market_regime"], "trades": r["trades"], "avg_return": _r(r["avg_return"])}
            for r in by_regime
        ],
    }


def _get_paper_trade_stats_v1(model_version: str = None) -> dict:
    """Original 21-query version. Fallback when ENABLE_PAPER_STATS_V2=false.

    Engine-scoped (model_version) identically to the V2 path so the fallback
    never blends engines either.
    """
    mv = model_version if model_version else (get_meta("ui_reco_source") or "scoring_v1")
    t_start = time.perf_counter()

    t0 = time.perf_counter()
    total = execute_db("SELECT COUNT(*) as cnt FROM paper_trades WHERE model_version = ?", (mv,), fetch="one")
    closed = execute_db("SELECT COUNT(*) as cnt FROM paper_trades WHERE status='CLOSED' AND model_version = ?", (mv,), fetch="one")
    wins = execute_db("SELECT COUNT(*) as cnt FROM paper_trades WHERE status='CLOSED' AND return_pct > 0 AND model_version = ?", (mv,), fetch="one")
    avg_ret = execute_db("SELECT AVG(return_pct) as avg FROM paper_trades WHERE status='CLOSED' AND model_version = ?", (mv,), fetch="one")
    avg_days = execute_db("SELECT AVG(days_held) as avg FROM paper_trades WHERE status='CLOSED' AND model_version = ?", (mv,), fetch="one")
    avg_dd = execute_db("SELECT AVG(max_drawdown_pct) as avg FROM paper_trades WHERE status='CLOSED' AND model_version = ?", (mv,), fetch="one")
    avg_alpha = execute_db("SELECT AVG(alpha_pct) as avg FROM paper_trades WHERE status='CLOSED' AND alpha_pct IS NOT NULL AND model_version = ?", (mv,), fetch="one")
    best = execute_db("SELECT symbol, return_pct FROM paper_trades WHERE status='CLOSED' AND model_version = ? ORDER BY return_pct DESC LIMIT 1", (mv,), fetch="one")
    worst = execute_db("SELECT symbol, return_pct FROM paper_trades WHERE status='CLOSED' AND model_version = ? ORDER BY return_pct ASC LIMIT 1", (mv,), fetch="one")
    t_basic = round((time.perf_counter() - t0) * 1000, 2)

    total_cnt = (total or {}).get("cnt") or 0
    closed_cnt = (closed or {}).get("cnt") or 0
    win_cnt = (wins or {}).get("cnt") or 0

    t0 = time.perf_counter()
    sum_wins = execute_db("SELECT SUM(return_pct) as total FROM paper_trades WHERE status='CLOSED' AND return_pct > 0 AND model_version = ?", (mv,), fetch="one")
    sum_losses = execute_db("SELECT SUM(ABS(return_pct)) as total FROM paper_trades WHERE status='CLOSED' AND return_pct <= 0 AND model_version = ?", (mv,), fetch="one")
    loss_cnt = closed_cnt - win_cnt
    total_win = (sum_wins or {}).get("total") or 0
    total_loss = (sum_losses or {}).get("total") or 0
    profit_factor = round(total_win / total_loss, 2) if total_loss > 0 else (None if total_win > 0 else 0.0)
    avg_win = total_win / win_cnt if win_cnt > 0 else 0
    avg_loss = total_loss / loss_cnt if loss_cnt > 0 else 0
    win_rate_dec = win_cnt / closed_cnt if closed_cnt > 0 else 0
    expectancy = round((win_rate_dec * avg_win) - ((1 - win_rate_dec) * avg_loss), 2) if closed_cnt > 0 else 0
    t_expectancy = round((time.perf_counter() - t0) * 1000, 2)

    t0 = time.perf_counter()
    golden_total = execute_db("SELECT COUNT(*) as cnt FROM paper_trades WHERE status='CLOSED' AND is_golden=1 AND model_version = ?", (mv,), fetch="one")
    golden_wins = execute_db("SELECT COUNT(*) as cnt FROM paper_trades WHERE status='CLOSED' AND is_golden=1 AND return_pct > 0 AND model_version = ?", (mv,), fetch="one")
    golden_cnt = (golden_total or {}).get("cnt") or 0
    golden_win_cnt = (golden_wins or {}).get("cnt") or 0
    hc_total = execute_db("SELECT COUNT(*) as cnt FROM paper_trades WHERE status='CLOSED' AND high_conviction=1 AND model_version = ?", (mv,), fetch="one")
    hc_wins = execute_db("SELECT COUNT(*) as cnt FROM paper_trades WHERE status='CLOSED' AND high_conviction=1 AND return_pct > 0 AND model_version = ?", (mv,), fetch="one")
    hc_cnt = (hc_total or {}).get("cnt") or 0
    hc_win_cnt = (hc_wins or {}).get("cnt") or 0
    t_conviction = round((time.perf_counter() - t0) * 1000, 2)

    t0 = time.perf_counter()
    factor_win = execute_db("""
        SELECT AVG(technical_score) as tech, AVG(fundamental_score) as fund,
               AVG(earnings_momentum_score) as earn, AVG(smart_money_score) as smart,
               AVG(risk_score) as risk, AVG(score_at_entry) as score
        FROM paper_trades WHERE status='CLOSED' AND return_pct > 0 AND model_version = ?
    """, (mv,), fetch="one") or {}
    factor_loss = execute_db("""
        SELECT AVG(technical_score) as tech, AVG(fundamental_score) as fund,
               AVG(earnings_momentum_score) as earn, AVG(smart_money_score) as smart,
               AVG(risk_score) as risk, AVG(score_at_entry) as score
        FROM paper_trades WHERE status='CLOSED' AND return_pct <= 0 AND model_version = ?
    """, (mv,), fetch="one") or {}
    t_factor = round((time.perf_counter() - t0) * 1000, 2)

    t0 = time.perf_counter()
    max_dd = execute_db("SELECT MIN(max_drawdown_pct) as dd FROM paper_trades WHERE status='CLOSED' AND model_version = ?", (mv,), fetch="one")
    by_version = execute_db("""
        SELECT model_version, COUNT(*) as trades,
               SUM(CASE WHEN return_pct > 0 THEN 1 ELSE 0 END) as wins,
               AVG(return_pct) as avg_return,
               AVG(alpha_pct) as avg_alpha
        FROM paper_trades WHERE status='CLOSED'
        GROUP BY model_version ORDER BY model_version
    """, fetch="all") or []
    by_sector = execute_db("""
        SELECT sector, COUNT(*) as trades,
               AVG(return_pct) as avg_return
        FROM paper_trades WHERE status='CLOSED' AND model_version = ?
        GROUP BY sector ORDER BY avg_return DESC LIMIT 10
    """, (mv,), fetch="all") or []
    by_regime = execute_db("""
        SELECT market_regime, COUNT(*) as trades,
               AVG(return_pct) as avg_return
        FROM paper_trades WHERE status='CLOSED' AND model_version = ?
        GROUP BY market_regime
    """, (mv,), fetch="all") or []
    t_groups = round((time.perf_counter() - t0) * 1000, 2)

    total_ms = round((time.perf_counter() - t_start) * 1000, 2)
    log.info("[DB PERF] get_paper_trade_stats V1 | total_queries=21 | t_basic=%s ms | t_expectancy=%s ms | t_conviction=%s ms | t_factor=%s ms | t_groups=%s ms | total=%s ms", t_basic, t_expectancy, t_conviction, t_factor, t_groups, total_ms)

    def _r(v): return float(round(v or 0, 2))

    return {
        "total_trades": total_cnt,
        "open_trades": total_cnt - closed_cnt,
        "closed_trades": closed_cnt,
        "win_rate": round((win_cnt / closed_cnt * 100), 1) if closed_cnt > 0 else 0,
        "avg_return_pct": _r((avg_ret or {}).get("avg")),
        "avg_days_held": float(round((avg_days or {}).get("avg") or 0, 1)),
        "avg_drawdown_pct": _r((avg_dd or {}).get("avg")),
        "max_drawdown_pct": _r((max_dd or {}).get("dd")),
        "avg_alpha_pct": _r((avg_alpha or {}).get("avg")),
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "best_trade": {"symbol": best["symbol"], "return_pct": best["return_pct"]} if best else None,
        "worst_trade": {"symbol": worst["symbol"], "return_pct": worst["return_pct"]} if worst else None,
        "golden_stock": {
            "trades": golden_cnt,
            "win_rate": round(golden_win_cnt / golden_cnt * 100, 1) if golden_cnt > 0 else 0,
        },
        "high_conviction": {
            "trades": hc_cnt,
            "win_rate": round(hc_win_cnt / hc_cnt * 100, 1) if hc_cnt > 0 else 0,
        },
        "factor_attribution": {
            "winners": {
                "avg_score": _r(factor_win.get("score")),
                "avg_technical": _r(factor_win.get("tech")),
                "avg_fundamental": _r(factor_win.get("fund")),
                "avg_earnings": _r(factor_win.get("earn")),
                "avg_smart_money": _r(factor_win.get("smart")),
                "avg_risk": _r(factor_win.get("risk")),
            },
            "losers": {
                "avg_score": _r(factor_loss.get("score")),
                "avg_technical": _r(factor_loss.get("tech")),
                "avg_fundamental": _r(factor_loss.get("fund")),
                "avg_earnings": _r(factor_loss.get("earn")),
                "avg_smart_money": _r(factor_loss.get("smart")),
                "avg_risk": _r(factor_loss.get("risk")),
            },
        },
        "by_model_version": [
            {"version": r["model_version"], "trades": r["trades"],
             "win_rate": round(r["wins"] / r["trades"] * 100, 1) if r["trades"] > 0 else 0,
             "avg_return": _r(r["avg_return"]),
             "avg_alpha": _r(r["avg_alpha"])}
            for r in by_version
        ],
        "by_sector": [
            {"sector": r["sector"], "trades": r["trades"], "avg_return": _r(r["avg_return"])}
            for r in by_sector
        ],
        "by_regime": [
            {"regime": r["market_regime"], "trades": r["trades"], "avg_return": _r(r["avg_return"])}
            for r in by_regime
        ],
    }






def get_equity_curve(days: int = 90) -> list[dict]:
    """Get equity curve data for charting."""
    return execute_db(
        "SELECT * FROM paper_portfolio_daily ORDER BY date DESC LIMIT ?",
        (days,), fetch="all"
    ) or []


# ═══════════════════════════════════════════════════════════════
# Phase 5.5: Universe Engine Helper Functions
# ═══════════════════════════════════════════════════════════════

# ── Eligible Universe ──────────────────────────────────────────

def save_eligible_universe(symbols_data: list, version: str):
    """Bulk upsert eligible universe for a given version.
    symbols_data: list of dicts with keys: symbol, market_cap_cr, avg_volume_20d,
                  avg_turnover_20d, price, eligibility_reason
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Clear old universe data first
    execute_db("DELETE FROM eligible_universe WHERE 1=1")
    
    params_list = []
    for s in symbols_data:
        params_list.append((
            s["symbol"], s.get("market_cap_cr", 0), s.get("avg_volume_20d", 0),
            s.get("avg_turnover_20d", 0), s.get("price", 0),
            s.get("eligibility_reason", "FILTER_PASS"), version, now
        ))
        
    if params_list:
        execute_many(
            """INSERT INTO eligible_universe
               (symbol, market_cap_cr, avg_volume_20d, avg_turnover_20d,
                price, eligibility_reason, universe_version, generated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (symbol) DO UPDATE SET
                 market_cap_cr = EXCLUDED.market_cap_cr,
                 avg_volume_20d = EXCLUDED.avg_volume_20d,
                 avg_turnover_20d = EXCLUDED.avg_turnover_20d,
                 price = EXCLUDED.price,
                 eligibility_reason = EXCLUDED.eligibility_reason,
                 universe_version = EXCLUDED.universe_version,
                 generated_at = EXCLUDED.generated_at""",
            params_list
        )
        
    set_meta("universe_version", version)
    set_meta("universe_stock_count", str(len(symbols_data)))
    set_meta("universe_generated_at", now)
    log.info("[Phase 5.5] Saved eligible universe: %d symbols, version=%s", len(symbols_data), version)


def get_eligible_universe(version: str = None) -> list:
    """Get eligible universe symbols. If version is None, gets latest."""
    if version:
        rows = execute_db(
            "SELECT * FROM eligible_universe WHERE universe_version = ? ORDER BY symbol",
            (version,), fetch="all"
        )
    else:
        rows = execute_db(
            "SELECT * FROM eligible_universe ORDER BY symbol",
            fetch="all"
        )
    return rows or []


def get_latest_universe_version() -> str:
    """Return the latest universe version string."""
    row = execute_db(
        "SELECT universe_version FROM eligible_universe ORDER BY generated_at DESC LIMIT 1",
        fetch="one"
    )
    if row:
        return row.get("universe_version", "UNIVERSE_v000")
    return "UNIVERSE_v000"


# ── Universe Rebuild History ──────────────────────────────────

def save_universe_rebuild_history(version: str, input_count: int,
                                   eligible_count: int, rejected: dict,
                                   force_included: int = 0,
                                   fallback_used: bool = False):
    """Persist a universe rebuild snapshot for Mission Control / drift debugging."""
    execute_db(
        """INSERT INTO universe_rebuild_history
           (universe_version, input_count, eligible_count,
            rejected_mcap, rejected_turnover, rejected_volume, rejected_price,
            rejected_etf, rejected_sme, rejected_suspended, rejected_ipo_age,
            force_included, fallback_used)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (version, input_count, eligible_count,
         rejected.get("mcap", 0), rejected.get("turnover", 0),
         rejected.get("volume", 0), rejected.get("price", 0),
         rejected.get("etf", 0), rejected.get("sme", 0),
         rejected.get("suspended", 0), rejected.get("ipo_age", 0),
         force_included, fallback_used)
    )
    log.info("[Phase 5.5] Saved universe rebuild history: version=%s input=%d eligible=%d",
             version, input_count, eligible_count)


def get_universe_rebuild_history(limit: int = 20) -> list:
    """Get recent universe rebuild history for Mission Control."""
    return execute_db(
        """SELECT * FROM universe_rebuild_history
           ORDER BY generated_at DESC LIMIT ?""",
        (limit,), fetch="all"
    ) or []


# ── Scan Lock (Rule 11: heartbeat-based ownership) ────────────

def acquire_scan_lock_v2(scan_id: str, owner_id: str, ttl_seconds: int = 300) -> bool:
    """Acquire the singleton scan lock. Returns True if acquired.
    If existing lock is stale (heartbeat > ttl_seconds old), steal it.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    from datetime import timedelta
    expires = (datetime.now() + timedelta(seconds=ttl_seconds)).strftime("%Y-%m-%d %H:%M:%S")

    # Check current lock
    # P0.1D: require_pg=True — lock reads must be consistent
    lock = execute_db("SELECT * FROM scan_lock WHERE id = 1", fetch="one", require_pg=True)
    if lock and lock.get("scan_id"):
        # Check if stale
        hb = lock.get("heartbeat")
        if hb:
            try:
                hb_dt = datetime.strptime(str(hb)[:19], "%Y-%m-%d %H:%M:%S")
                age = (datetime.now() - hb_dt).total_seconds()
                if 0 <= age < ttl_seconds:
                    log.warning("[ScanLock] Lock held by %s (age=%ds < ttl=%ds), cannot acquire",
                               lock.get("owner_id"), int(age), ttl_seconds)
                    return False
                log.info("[ScanLock] Stale lock detected (age=%ds), stealing", int(age))
            except Exception:
                pass  # Can't parse, allow acquisition

    # Acquire or steal
    # P0.1D: require_pg=True — lock acquisition must be consistent
    execute_db(
        """UPDATE scan_lock SET scan_id = ?, owner_id = ?, heartbeat = ?,
           expires_at = ?, acquired_at = ? WHERE id = 1""",
        (scan_id, owner_id, now, expires, now), require_pg=True
    )
    log.info("[ScanLock] Acquired: scan_id=%s, owner=%s", scan_id, owner_id)
    return True


def release_scan_lock_v2(scan_id: str):
    """Release the scan lock (only if owned by this scan_id)."""
    # P0.1D: require_pg=True — lock release must be consistent
    execute_db(
        "UPDATE scan_lock SET scan_id = NULL, owner_id = NULL, heartbeat = NULL, expires_at = NULL WHERE id = 1 AND scan_id = ?",
        (scan_id,), require_pg=True
    )
    log.info("[ScanLock] Released: scan_id=%s", scan_id)


def refresh_scan_lock_heartbeat(scan_id: str):
    """Refresh the heartbeat timestamp to keep the lock alive."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # P0.1D: require_pg=True — heartbeat must be consistent
    execute_db(
        "UPDATE scan_lock SET heartbeat = ? WHERE id = 1 AND scan_id = ?",
        (now, scan_id), require_pg=True
    )


def is_scan_lock_stale(threshold_seconds: int = 300) -> bool:
    """Check if the current scan lock is stale (heartbeat too old)."""
    lock = execute_db("SELECT * FROM scan_lock WHERE id = 1", fetch="one")
    if not lock or not lock.get("scan_id"):
        return True  # No lock held
    hb = lock.get("heartbeat")
    if not hb:
        return True
    try:
        hb_dt = datetime.strptime(str(hb)[:19], "%Y-%m-%d %H:%M:%S")
        age = (datetime.now() - hb_dt).total_seconds()
        return age > threshold_seconds
    except Exception:
        return True


# ── Scan Batches (Rule 5: Queue-based batch tracking) ─────────

def create_scan_batches(scan_id: str, batches: list):
    """Create batch records in DB for tracking.
    batches: list of lists of symbol strings.
    """
    for idx, batch_symbols in enumerate(batches):
        execute_db(
            """INSERT INTO scan_batches (scan_id, batch_index, status, symbol_count)
               VALUES (?, ?, 'PENDING', ?)
               ON CONFLICT (scan_id, batch_index) DO NOTHING""",
            (scan_id, idx, len(batch_symbols))
        )
    log.info("[Phase 5.5] Created %d batch records for scan %s", len(batches), scan_id)


def claim_next_batch(scan_id: str, batch_index: int, worker_id: str):
    """Mark a batch as claimed by a worker."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    execute_db(
        """UPDATE scan_batches SET status = 'RUNNING', worker_id = ?, started_at = ?
           WHERE scan_id = ? AND batch_index = ? AND status = 'PENDING'""",
        (worker_id, now, scan_id, batch_index)
    )


def complete_batch(scan_id: str, batch_index: int, symbols_processed: int):
    """Mark a batch as completed."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    execute_db(
        """UPDATE scan_batches SET status = 'COMPLETED', symbols_processed = ?,
           completed_at = ? WHERE scan_id = ? AND batch_index = ?""",
        (symbols_processed, now, scan_id, batch_index)
    )


MAX_BATCH_RETRIES = 3  # Max times a batch can be recovered before permanent failure

def recover_stale_batches(scan_id: str, stale_threshold_seconds: int = 300) -> list:
    """Reset RUNNING batches whose started_at is older than stale_threshold_seconds
    back to PENDING so they can be re-queued.

    Returns list of batch_index values that were recovered.
    Batches that have been retried MAX_BATCH_RETRIES times are marked
    FAILED_PERMANENTLY and NOT re-queued (prevents infinite crash loops).
    """
    from datetime import timedelta
    threshold_dt = (datetime.now() - timedelta(seconds=stale_threshold_seconds)
                    ).strftime("%Y-%m-%d %H:%M:%S")

    # Find stale batches: RUNNING and started_at older than threshold
    stale = execute_db(
        """SELECT batch_index, retry_count FROM scan_batches
           WHERE scan_id = ? AND status = 'RUNNING'
             AND started_at IS NOT NULL AND started_at < ?""",
        (scan_id, threshold_dt), fetch="all"
    )
    if not stale:
        return []

    recovered = []
    permanently_failed = []

    for row in stale:
        idx = row.get("batch_index")
        retries = (row.get("retry_count") or 0) + 1

        if idx is None:
            continue

        if retries > MAX_BATCH_RETRIES:
            # Exceeded max retries — mark permanently failed
            execute_db(
                """UPDATE scan_batches
                   SET status = 'FAILED_PERMANENTLY', retry_count = ?
                   WHERE scan_id = ? AND batch_index = ?""",
                (retries, scan_id, idx)
            )
            permanently_failed.append(idx)
            log.error("[Phase 5.5] Batch %d FAILED_PERMANENTLY after %d retries (scan %s)",
                      idx, retries, scan_id)
        else:
            # Reset to PENDING with incremented retry_count
            execute_db(
                """UPDATE scan_batches
                   SET status = 'PENDING', worker_id = NULL, started_at = NULL,
                       retry_count = ?
                   WHERE scan_id = ? AND batch_index = ? AND status = 'RUNNING'""",
                (retries, scan_id, idx)
            )
            recovered.append(idx)

    if recovered:
        log.info("[Phase 5.5] Recovered %d stale batches for scan %s: %s",
                 len(recovered), scan_id, recovered)
    if permanently_failed:
        log.warning("[Phase 5.5] %d batches FAILED_PERMANENTLY for scan %s: %s",
                    len(permanently_failed), scan_id, permanently_failed)
    return recovered


def get_batch_progress(scan_id: str) -> dict:
    """Get batch-level progress for a scan."""
    if not scan_id:
        return {}
    rows = execute_db(
        "SELECT * FROM scan_batches WHERE scan_id = ? ORDER BY batch_index",
        (scan_id,), fetch="all"
    )
    if not rows:
        return {}

    total = len(rows)
    completed = sum(1 for r in rows if r.get("status") == "COMPLETED")
    total_symbols = sum(r.get("symbol_count", 0) for r in rows)
    processed_symbols = sum(r.get("symbols_processed", 0) for r in rows)
    remaining = total_symbols - processed_symbols

    # Get universe version from resume state
    resume = execute_db(
        "SELECT universe_version FROM scan_resume_state WHERE scan_id = ?",
        (scan_id,), fetch="one"
    )
    version = resume.get("universe_version", "") if resume else ""

    return {
        "universe_total": total_symbols,
        "completed": processed_symbols,
        "remaining": remaining,
        "progress": round((processed_symbols / total_symbols) * 100, 1) if total_symbols > 0 else 0,
        "current_batch": completed,
        "total_batches": total,
        "universe_version": version,
    }


# ── Scan Resume State (Rule 10: lightweight recovery) ─────────

def save_scan_resume_state(scan_id: str, universe_version: str,
                           total_batches: int, current_batch_index: int):
    """Persist resume checkpoint. Only stores batch_index — no symbol JSON."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # P0.1D: require_pg=True — resume state must be consistent
    execute_db(
        """INSERT INTO scan_resume_state
           (scan_id, universe_version, total_batches, current_batch_index, status, last_heartbeat)
           VALUES (?, ?, ?, ?, 'running', ?)
           ON CONFLICT (scan_id) DO UPDATE SET
             current_batch_index = EXCLUDED.current_batch_index,
             last_heartbeat = EXCLUDED.last_heartbeat""",
        (scan_id, universe_version, total_batches, current_batch_index, now), require_pg=True
    )


def get_pending_resume() -> dict:
    """Get the most recent incomplete scan resume state."""
    row = execute_db(
        "SELECT * FROM scan_resume_state WHERE status = 'running' ORDER BY created_at DESC LIMIT 1",
        fetch="one"
    )
    return row


def clear_scan_resume_state(scan_id: str):
    """Mark scan as completed and clean up."""
    execute_db(
        "UPDATE scan_resume_state SET status = 'completed' WHERE scan_id = ?",
        (scan_id,)
    )


# ── Universe Catalog Helpers ──────────────────────────────────

def upsert_universe_catalog(symbols_data: list, set_synced_at: bool = True):
    """Bulk upsert into universe_catalog (Stock Master Registry).
    symbols_data: list of dicts with keys matching universe_catalog columns.
    set_synced_at: If True, updates last_synced_at to now. Phase 1 (skeleton insert)
                   should pass False so Phase 2 (yfinance) still picks them up.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Batch upserts to avoid pool starvation (was: 1 INSERT per symbol × 2346 symbols)
    BATCH_SIZE = 50
    use_pg = is_postgresql() and not pg_cooldown_active()

    if use_pg:
        pool = _get_pg_pool()
        if pool:
            conn = None
            try:
                from psycopg2.extras import RealDictCursor
                conn = pool.getconn()
                conn.autocommit = False
                with conn.cursor() as cur:
                    for i in range(0, len(symbols_data), BATCH_SIZE):
                        batch = symbols_data[i:i + BATCH_SIZE]
                        for s in batch:
                            sync_fail_count = s.get("sync_fail_count")
                            synced_at_value = now if set_synced_at else None
                            query_pg = """INSERT INTO universe_catalog
               (symbol, company_name, market_cap, market_cap_bucket, sector, industry,
                is_active, avg_volume_20d, avg_turnover_20d, instrument_type,
                exchange, price, last_synced_at, sync_fail_count)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (symbol) DO UPDATE SET
                 company_name = COALESCE(EXCLUDED.company_name, universe_catalog.company_name),
                 market_cap = CASE WHEN EXCLUDED.market_cap > 0 THEN EXCLUDED.market_cap ELSE universe_catalog.market_cap END,
                 market_cap_bucket = COALESCE(EXCLUDED.market_cap_bucket, universe_catalog.market_cap_bucket),
                 sector = CASE WHEN EXCLUDED.sector != '' THEN EXCLUDED.sector ELSE universe_catalog.sector END,
                 industry = CASE WHEN EXCLUDED.industry != '' THEN EXCLUDED.industry ELSE universe_catalog.industry END,
                 is_active = EXCLUDED.is_active,
                 avg_volume_20d = CASE WHEN EXCLUDED.avg_volume_20d > 0 THEN EXCLUDED.avg_volume_20d ELSE universe_catalog.avg_volume_20d END,
                 avg_turnover_20d = CASE WHEN EXCLUDED.avg_turnover_20d > 0 THEN EXCLUDED.avg_turnover_20d ELSE universe_catalog.avg_turnover_20d END,
                 instrument_type = COALESCE(EXCLUDED.instrument_type, universe_catalog.instrument_type),
                 exchange = COALESCE(EXCLUDED.exchange, universe_catalog.exchange),
                 price = CASE WHEN EXCLUDED.price > 0 THEN EXCLUDED.price ELSE universe_catalog.price END,
                 sync_fail_count = COALESCE(EXCLUDED.sync_fail_count, universe_catalog.sync_fail_count),
                 last_synced_at = CASE WHEN EXCLUDED.last_synced_at IS NOT NULL THEN EXCLUDED.last_synced_at ELSE universe_catalog.last_synced_at END"""
                            cur.execute(query_pg, (
                                s.get("symbol"), s.get("company_name"), s.get("market_cap"),
                                s.get("market_cap_bucket"), s.get("sector"), s.get("industry"),
                                s.get("is_active", True), s.get("avg_volume_20d", 0),
                                s.get("avg_turnover_20d", 0), s.get("instrument_type", "EQ"),
                                s.get("exchange", "NSE"), s.get("price", 0), synced_at_value,
                                sync_fail_count if sync_fail_count is not None else 0))
                        conn.commit()
                conn.autocommit = True
                log.info("[Phase 5.5] Upserted %d symbols into universe_catalog via PG batch (synced_at=%s)", len(symbols_data), set_synced_at)
                return
            except Exception as exc:
                log.warning("[Phase 5.5] PG batch upsert failed: %s, falling back to individual", exc)
                if conn:
                    try: conn.rollback()
                    except: pass
            finally:
                if conn:
                    try: pool.putconn(conn)
                    except: pass

    # Fallback: individual inserts (SQLite path or PG failure)
    for s in symbols_data:
        sync_fail_count = s.get("sync_fail_count")
        synced_at_value = now if set_synced_at else None
        execute_db(
            """INSERT INTO universe_catalog
               (symbol, company_name, market_cap, market_cap_bucket, sector, industry,
                is_active, avg_volume_20d, avg_turnover_20d, instrument_type,
                exchange, price, last_synced_at, sync_fail_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (symbol) DO UPDATE SET
                 company_name = COALESCE(EXCLUDED.company_name, universe_catalog.company_name),
                 market_cap = CASE WHEN EXCLUDED.market_cap > 0 THEN EXCLUDED.market_cap ELSE universe_catalog.market_cap END,
                 market_cap_bucket = COALESCE(EXCLUDED.market_cap_bucket, universe_catalog.market_cap_bucket),
                 sector = CASE WHEN EXCLUDED.sector != '' THEN EXCLUDED.sector ELSE universe_catalog.sector END,
                 industry = CASE WHEN EXCLUDED.industry != '' THEN EXCLUDED.industry ELSE universe_catalog.industry END,
                 is_active = EXCLUDED.is_active,
                 avg_volume_20d = CASE WHEN EXCLUDED.avg_volume_20d > 0 THEN EXCLUDED.avg_volume_20d ELSE universe_catalog.avg_volume_20d END,
                 avg_turnover_20d = CASE WHEN EXCLUDED.avg_turnover_20d > 0 THEN EXCLUDED.avg_turnover_20d ELSE universe_catalog.avg_turnover_20d END,
                 instrument_type = COALESCE(EXCLUDED.instrument_type, universe_catalog.instrument_type),
                 exchange = COALESCE(EXCLUDED.exchange, universe_catalog.exchange),
                 price = CASE WHEN EXCLUDED.price > 0 THEN EXCLUDED.price ELSE universe_catalog.price END,
                 sync_fail_count = COALESCE(EXCLUDED.sync_fail_count, universe_catalog.sync_fail_count),
                 last_synced_at = CASE WHEN EXCLUDED.last_synced_at IS NOT NULL THEN EXCLUDED.last_synced_at ELSE universe_catalog.last_synced_at END""",
            (s.get("symbol"), s.get("company_name"), s.get("market_cap"),
             s.get("market_cap_bucket"), s.get("sector"), s.get("industry"),
             s.get("is_active", True), s.get("avg_volume_20d", 0),
             s.get("avg_turnover_20d", 0), s.get("instrument_type", "EQ"),
             s.get("exchange", "NSE"), s.get("price", 0), synced_at_value,
             sync_fail_count if sync_fail_count is not None else 0)
        )
    log.info("[Phase 5.5] Upserted %d symbols into universe_catalog (synced_at=%s)", len(symbols_data), set_synced_at)


def get_universe_catalog_eligible() -> list:
    """Get all active stocks from universe_catalog for filtering."""
    return execute_db(
        """SELECT symbol, company_name, market_cap, market_cap_bucket, sector, industry,
                  avg_volume_20d, avg_turnover_20d, instrument_type, exchange, price, is_active,
                  last_synced_at
           FROM universe_catalog
           WHERE is_active = TRUE
           ORDER BY market_cap DESC NULLS LAST""",
        fetch="all"
    ) or []


def update_universe_catalog_metrics(symbol: str, avg_volume: float,
                                    avg_turnover: float, price: float):
    """Update computed metrics for a single symbol."""
    execute_db(
        """UPDATE universe_catalog
           SET avg_volume_20d = ?, avg_turnover_20d = ?, price = ?
           WHERE symbol = ?""",
        (avg_volume, avg_turnover, price, symbol)
    )


# ═══════════════════════════════════════════════════════════════
# Phase 5.6B/C: Liquidity Enrichment & Universe Governance Helpers
# ═══════════════════════════════════════════════════════════════

# ── Instrument Classification ─────────────────────────────────

_ETF_HEURISTIC_PATTERNS = [
    "BEES", "ETF", "LIQUID", "GOLDBEES", "SILVERBEES",
    "NIFTYBEES", "BANKBEES", "JUNIORBEES", "SETFNIF50",
]
_NAV_HEURISTIC_PATTERNS = ["NAV", "INAV"]

def classify_instrument_types():
    """Bulk-classify instrument_type in universe_catalog.
    Priority: Metadata (yfinance quoteType via last_synced_at) > Name Heuristics.
    Only applies heuristics for unsynced symbols (last_synced_at IS NULL).
    """
    # Get unsynced symbols where heuristics should apply
    unsynced = execute_db(
        """SELECT symbol, company_name, instrument_type
           FROM universe_catalog
           WHERE is_active = TRUE AND last_synced_at IS NULL""",
        fetch="all"
    ) or []

    if not unsynced:
        log.info("[Phase 5.6B/C] classify_instrument_types: no unsynced symbols to classify")
        return 0

    classified = 0
    for row in unsynced:
        sym = row.get("symbol", "")
        name = (row.get("company_name") or sym).upper()
        current_type = (row.get("instrument_type") or "EQ").upper()

        # Already classified by metadata — skip
        if current_type != "EQ":
            continue

        detected_type = None
        sym_upper = sym.upper()

        # ETF heuristics
        for pattern in _ETF_HEURISTIC_PATTERNS:
            if pattern in sym_upper or pattern in name:
                detected_type = "ETF"
                break

        # NAV/INAV heuristics (only if not already detected as ETF)
        if not detected_type:
            for pattern in _NAV_HEURISTIC_PATTERNS:
                # Check suffix or standalone presence — avoid matching GOLDIAM, SILVERTOUCH etc.
                if sym_upper.endswith(pattern) or f" {pattern}" in name or name.startswith(f"{pattern} "):
                    detected_type = pattern
                    break

        if detected_type:
            execute_db(
                "UPDATE universe_catalog SET instrument_type = ? WHERE symbol = ?",
                (detected_type, sym)
            )
            classified += 1

    log.info("[Phase 5.6B/C] classify_instrument_types: classified %d/%d unsynced symbols",
             classified, len(unsynced))
    return classified


# ── Candidate Universe Operations ─────────────────────────────

def get_candidate_universe() -> list:
    """Stage-1 query: active EQ symbols not excluded.
    Returns list of dicts with symbol, market_cap, price.
    Sorted by market_cap DESC so known large-caps get enriched first.
    No market_cap hard gate — liquidity worker uses Angel API (not yfinance)
    and universe_builder Stage-3 applies proper filters after enrichment.
    """
    return execute_db(
        """SELECT symbol, market_cap, price
           FROM universe_catalog
           WHERE is_active = TRUE
             AND (instrument_type = 'EQ' OR instrument_type IS NULL)
             AND COALESCE(liquidity_excluded, FALSE) = FALSE
           ORDER BY market_cap DESC""",
        fetch="all"
    ) or []


def freeze_candidate_universe(version: str) -> dict:
    """Freeze Stage-1 candidates into candidate_universe for the specified version.
    Returns dict with frozen_count and frozen_checksum.
    """
    import hashlib

    candidates = get_candidate_universe()
    if not candidates:
        log.warning("[Phase 5.6B/C] freeze_candidate_universe: no candidates found")
        return {"frozen_count": 0, "frozen_checksum": ""}

    # Clear any existing freeze for this version
    execute_db("DELETE FROM candidate_universe WHERE universe_version = ?", (version,))

    # Insert candidates
    for c in candidates:
        mcap = c.get("market_cap", 0)
        # Normalize market_cap: if in absolute rupees (> 10000), convert to Cr
        mcap_cr = mcap / 1e7 if mcap > 10000 else mcap
        execute_db(
            """INSERT INTO candidate_universe (universe_version, symbol, market_cap_cr)
               VALUES (?, ?, ?)
               ON CONFLICT (universe_version, symbol) DO NOTHING""",
            (version, c.get("symbol"), mcap_cr)
        )

    # Compute SHA256 checksum from sorted symbols (deterministic regardless of DB row order)
    symbols_sorted = sorted(c.get("symbol", "") for c in candidates)
    checksum = hashlib.sha256("|".join(symbols_sorted).encode()).hexdigest()
    frozen_count = len(candidates)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Store in metadata
    set_meta("candidate_frozen_count", str(frozen_count))
    set_meta("candidate_frozen_checksum", checksum)
    set_meta("candidate_frozen_version", version)
    set_meta("candidate_frozen_created_at", now)

    log.info("[Phase 5.6B/C] Frozen %d candidates for %s (sha256=%s)",
             frozen_count, version, checksum[:12])
    return {"frozen_count": frozen_count, "frozen_checksum": checksum}


def get_frozen_candidates(version: str) -> list:
    """Return list of symbols frozen for the specified version."""
    rows = execute_db(
        "SELECT symbol FROM candidate_universe WHERE universe_version = ? ORDER BY symbol",
        (version,), fetch="all"
    ) or []
    return [r.get("symbol") for r in rows if r.get("symbol")]


def verify_candidate_integrity(version: str) -> tuple:
    """Verify candidate universe hasn't been tampered with.
    Returns (is_valid, current_count, current_checksum).
    Uses SHA256 checksum on sorted symbols for deterministic verification.
    """
    import hashlib

    rows = execute_db(
        "SELECT symbol FROM candidate_universe WHERE universe_version = ? ORDER BY symbol",
        (version,), fetch="all"
    ) or []
    symbols = [r.get("symbol", "") for r in rows]

    current_count = len(symbols)
    current_checksum = hashlib.sha256("|".join(symbols).encode()).hexdigest()

    stored_count = int(get_meta("candidate_frozen_count") or 0)
    stored_checksum = get_meta("candidate_frozen_checksum") or ""

    is_valid = (current_count == stored_count and current_checksum == stored_checksum)

    if not is_valid:
        log.error("[Phase 5.6B/C] CANDIDATE INTEGRITY MISMATCH: "
                  "stored=%d/%s current=%d/%s",
                  stored_count, stored_checksum[:12],
                  current_count, current_checksum[:12])

    return is_valid, current_count, current_checksum


# ── Liquidity Tracking ────────────────────────────────────────

def get_liquidity_pending_symbols_v2(version: str, batch_size: int = 50) -> list:
    """Get symbols from candidate_universe for the version whose corresponding
    universe_catalog entries have liquidity_synced_at IS NULL or > 7 days old,
    and liquidity_sync_fail_count < 3.
    """
    cutoff_7d = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    rows = execute_db(
        """SELECT cu.symbol
           FROM candidate_universe cu
           JOIN universe_catalog uc ON cu.symbol = uc.symbol
           WHERE cu.universe_version = ?
             AND COALESCE(uc.liquidity_excluded, FALSE) = FALSE
             AND uc.liquidity_sync_fail_count < 3
             AND (uc.liquidity_synced_at IS NULL
                  OR uc.liquidity_synced_at < ?)
           ORDER BY uc.liquidity_synced_at ASC NULLS FIRST
           LIMIT ?""",
        (version, cutoff_7d, batch_size), fetch="all"
    ) or []
    return [r.get("symbol") for r in rows if r.get("symbol")]


def update_liquidity_metrics(symbol: str, avg_volume: float, avg_turnover: float, price: float):
    """Update liquidity metrics for a symbol after successful enrichment.
    Resets fail count and stamps liquidity_synced_at.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    execute_db(
        """UPDATE universe_catalog
           SET avg_volume_20d = ?,
               avg_turnover_20d = ?,
               price = ?,
               liquidity_synced_at = ?,
               liquidity_sync_fail_count = 0
           WHERE symbol = ?""",
        (avg_volume, avg_turnover, price, now, symbol)
    )


def increment_liquidity_sync_fail(symbol: str, failure_type: str):
    """Increment liquidity sync failure count for a symbol.
    If fail_count >= 3 AND failure_type == 'SYMBOL_NOT_SUPPORTED',
    mark as liquidity_excluded.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Increment fail count
    execute_db(
        """UPDATE universe_catalog
           SET liquidity_sync_fail_count = COALESCE(liquidity_sync_fail_count, 0) + 1
           WHERE symbol = ?""",
        (symbol,)
    )

    # Check if we should exclude
    row = execute_db(
        "SELECT liquidity_sync_fail_count FROM universe_catalog WHERE symbol = ?",
        (symbol,), fetch="one"
    )
    fail_count = int(row.get("liquidity_sync_fail_count", 0)) if row else 0

    if fail_count >= 3 and failure_type == "SYMBOL_NOT_SUPPORTED":
        execute_db(
            """UPDATE universe_catalog
               SET liquidity_excluded = TRUE,
                   liquidity_excluded_reason = ?,
                   liquidity_excluded_at = ?
               WHERE symbol = ?""",
            (f"SYMBOL_NOT_SUPPORTED (failed {fail_count} times)", now, symbol)
        )
        log.warning("[Phase 5.6B/C] %s excluded from liquidity enrichment: %s (fail_count=%d)",
                    symbol, failure_type, fail_count)


# ── Universe Health Metrics ───────────────────────────────────

def get_universe_health_metrics_v3(version: str) -> dict:
    """Return universe health metrics with formal coverage denominator.

    coverage_pct = done_candidates / (total_candidates - permanently_excluded) * 100

    Permanently excluded = liquidity_excluded = TRUE AND fail_count >= 3
                           AND failure_type == SYMBOL_NOT_SUPPORTED
    Temporarily failed = fail_count > 0 but < 3, remains in denominator.
    """
    # Total candidates for this version
    total_row = execute_db(
        "SELECT COUNT(*) as c FROM candidate_universe WHERE universe_version = ?",
        (version,), fetch="one"
    )
    total_candidates = int(total_row.get("c", 0)) if total_row else 0

    # Permanently excluded candidates
    excluded_row = execute_db(
        """SELECT COUNT(*) as c
           FROM candidate_universe cu
           JOIN universe_catalog uc ON cu.symbol = uc.symbol
           WHERE cu.universe_version = ?
             AND COALESCE(uc.liquidity_excluded, FALSE) = TRUE""",
        (version,), fetch="one"
    )
    excluded_count = int(excluded_row.get("c", 0)) if excluded_row else 0

    # Done candidates (liquidity_synced_at IS NOT NULL)
    done_row = execute_db(
        """SELECT COUNT(*) as c
           FROM candidate_universe cu
           JOIN universe_catalog uc ON cu.symbol = uc.symbol
           WHERE cu.universe_version = ?
             AND uc.liquidity_synced_at IS NOT NULL""",
        (version,), fetch="one"
    )
    done_candidates = int(done_row.get("c", 0)) if done_row else 0

    # Market cap coverage
    mcap_row = execute_db(
        """SELECT COUNT(*) as c
           FROM candidate_universe cu
           JOIN universe_catalog uc ON cu.symbol = uc.symbol
           WHERE cu.universe_version = ?
             AND uc.market_cap > 0""",
        (version,), fetch="one"
    )
    mcap_populated = int(mcap_row.get("c", 0)) if mcap_row else 0

    denominator = total_candidates - excluded_count
    liquidity_coverage_pct = (done_candidates / denominator * 100) if denominator > 0 else 0
    marketcap_coverage_pct = (mcap_populated / total_candidates * 100) if total_candidates > 0 else 0

    return {
        "total_candidates": total_candidates,
        "excluded_count": excluded_count,
        "done_candidates": done_candidates,
        "denominator": denominator,
        "liquidity_coverage_pct": round(liquidity_coverage_pct, 2),
        "marketcap_coverage_pct": round(marketcap_coverage_pct, 2),
        "mcap_populated": mcap_populated,
    }


# ── Universe Snapshot & Validation ────────────────────────────

def save_eligible_universe_with_snapshot(symbols_data: list, version: str):
    """Save eligible universe AND append to universe_snapshot for audit trail.
    Replaces the old save_eligible_universe for Phase 5.6B/C builds.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Clear old eligible universe
    execute_db("DELETE FROM eligible_universe WHERE 1=1")

    for s in symbols_data:
        # Insert into eligible_universe (active set)
        execute_db(
            """INSERT INTO eligible_universe
               (symbol, market_cap_cr, avg_volume_20d, avg_turnover_20d,
                price, eligibility_reason, universe_version, generated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (symbol) DO UPDATE SET
                 market_cap_cr = EXCLUDED.market_cap_cr,
                 avg_volume_20d = EXCLUDED.avg_volume_20d,
                 avg_turnover_20d = EXCLUDED.avg_turnover_20d,
                 price = EXCLUDED.price,
                 eligibility_reason = EXCLUDED.eligibility_reason,
                 universe_version = EXCLUDED.universe_version,
                 generated_at = EXCLUDED.generated_at""",
            (s["symbol"], s.get("market_cap_cr", 0), s.get("avg_volume_20d", 0),
             s.get("avg_turnover_20d", 0), s.get("price", 0),
             s.get("eligibility_reason", "FILTER_PASS"), version, now)
        )

        # Append to universe_snapshot (audit trail — never deleted)
        execute_db(
            """INSERT INTO universe_snapshot
               (universe_version, symbol, market_cap_cr, avg_volume_20d,
                avg_turnover_20d, price, eligibility_reason, generated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (version, s["symbol"], s.get("market_cap_cr", 0),
             s.get("avg_volume_20d", 0), s.get("avg_turnover_20d", 0),
             s.get("price", 0), s.get("eligibility_reason", "FILTER_PASS"), now)
        )

    set_meta("universe_version", version)
    set_meta("universe_stock_count", str(len(symbols_data)))
    set_meta("universe_generated_at", now)
    log.info("[Phase 5.6B/C] Saved eligible universe + snapshot: %d symbols, version=%s",
             len(symbols_data), version)


def save_validation_snapshot(version: str, candidate_count: int,
                             eligible_count: int, marketcap_coverage_pct: float,
                             liquidity_coverage_pct: float):
    """Write validation evidence to universe_build_validation_snapshot."""
    execute_db(
        """INSERT INTO universe_build_validation_snapshot
           (universe_version, candidate_count, eligible_count,
            marketcap_coverage_pct, liquidity_coverage_pct)
           VALUES (?, ?, ?, ?, ?)""",
        (version, candidate_count, eligible_count,
         marketcap_coverage_pct, liquidity_coverage_pct)
    )
    log.info("[Phase 5.6B/C] Validation snapshot saved: version=%s candidates=%d eligible=%d "
             "mcap_cov=%.1f%% liq_cov=%.1f%%",
             version, candidate_count, eligible_count,
             marketcap_coverage_pct, liquidity_coverage_pct)


# ── Atomic Version Activation ─────────────────────────────────

def activate_universe_version_transaction(version: str) -> bool:
    """Atomically activate a new universe version.
    Wraps the metadata switch in a single DB transaction.
    Uses explicit ROLLING_BACK state for crash recovery clarity.
    Returns True if activation succeeded.
    """
    global _pg_pool

    current_active = get_meta("active_universe_version") or ""

    if current_active == version:
        log.warning("[Phase 5.6B/C] Version %s is already active — skipping activation", version)
        return False

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Mark ROLLING_BACK state before attempting switch (crash recovery breadcrumb)
    set_meta("universe_state", "ACTIVATING")

    # Attempt PostgreSQL transactional switch
    if is_postgresql():
        pool = _get_pg_pool()
        if pool:
            conn = None
            try:
                from psycopg2.extras import RealDictCursor
                conn = pool.getconn()
                conn.autocommit = False  # Start transaction

                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    # Swap versions atomically
                    for key, val in [
                        ("previous_active_universe_version", current_active),
                        ("active_universe_version", version),
                        ("building_universe_version", ""),
                        ("scan_ready", "true"),
                        ("universe_state", "READY"),
                        ("universe_activated_at", now),
                    ]:
                        cur.execute(
                            """INSERT INTO scan_meta (key, value, updated_at) VALUES (%s, %s, %s)
                               ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, updated_at=EXCLUDED.updated_at""",
                            (key, val, now)
                        )

                conn.commit()
                log.info("[Phase 5.6B/C] ATOMIC VERSION SWITCH: %s → %s (PG transaction committed)",
                         current_active, version)

                # Update cache
                if _META_CACHE_ENABLED:
                    _meta_cache["previous_active_universe_version"] = (current_active, time.time())
                    _meta_cache["active_universe_version"] = (version, time.time())
                    _meta_cache["building_universe_version"] = ("", time.time())
                    _meta_cache["scan_ready"] = ("true", time.time())
                    _meta_cache["universe_state"] = ("READY", time.time())

                return True

            except Exception as exc:
                if conn:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                log.error("[Phase 5.6B/C] ATOMIC VERSION SWITCH FAILED — ROLLED BACK: %s", exc)
                # Recover: restore previous state
                set_meta("universe_state", "DEGRADED")
                set_meta("building_universe_version", "")
                return False
            finally:
                if conn:
                    try:
                        conn.autocommit = True
                        pool.putconn(conn)
                    except Exception:
                        pass

    # SQLite fallback: no true transaction but sequential writes
    try:
        set_meta("previous_active_universe_version", current_active)
        set_meta("active_universe_version", version)
        set_meta("building_universe_version", "")
        set_meta("scan_ready", "true")
        set_meta("universe_state", "READY")
        set_meta("universe_activated_at", now)
        log.info("[Phase 5.6B/C] VERSION SWITCH: %s → %s (SQLite sequential)",
                 current_active, version)
        return True
    except Exception as exc:
        log.error("[Phase 5.6B/C] VERSION SWITCH FAILED (SQLite): %s", exc)
        set_meta("universe_state", "DEGRADED")
        set_meta("building_universe_version", "")
        return False


# ── Exclusion Percentage Guard ────────────────────────────────

def check_exclusion_guard(version: str, max_exclusion_pct: float = 10.0) -> tuple:
    """Check if permanent exclusions exceed the safety threshold.
    Returns (is_safe, exclusion_pct, excluded_count, total_candidates).

    If excluded_count / total_candidates > max_exclusion_pct:
        universe_state → DEGRADED
    """
    total_row = execute_db(
        "SELECT COUNT(*) as c FROM candidate_universe WHERE universe_version = ?",
        (version,), fetch="one"
    )
    total_candidates = int(total_row.get("c", 0)) if total_row else 0

    excluded_row = execute_db(
        """SELECT COUNT(*) as c
           FROM candidate_universe cu
           JOIN universe_catalog uc ON cu.symbol = uc.symbol
           WHERE cu.universe_version = ?
             AND COALESCE(uc.liquidity_excluded, FALSE) = TRUE""",
        (version,), fetch="one"
    )
    excluded_count = int(excluded_row.get("c", 0)) if excluded_row else 0

    exclusion_pct = (excluded_count / total_candidates * 100) if total_candidates > 0 else 0
    is_safe = exclusion_pct <= max_exclusion_pct

    if not is_safe:
        log.error("[Phase 5.6B/C] EXCLUSION GUARD TRIGGERED: %.1f%% excluded (%d/%d) > %.1f%% max",
                  exclusion_pct, excluded_count, total_candidates, max_exclusion_pct)
        set_meta("universe_state", "DEGRADED")

    return is_safe, round(exclusion_pct, 2), excluded_count, total_candidates


# ── Snapshot Retention ────────────────────────────────────────

def cleanup_old_snapshots(keep_days: int = 90):
    """Remove universe_snapshot rows older than keep_days.
    Always keeps all rows for the active version regardless of age.
    Uses Python-side cutoff for PG+SQLite compatibility.
    """
    active_version = get_meta("active_universe_version") or ""
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d %H:%M:%S")

    deleted = execute_db(
        """DELETE FROM universe_snapshot
           WHERE generated_at < ?
             AND universe_version != ?""",
        (cutoff, active_version), fetch="rowcount"
    )

    if deleted and deleted > 0:
        log.info("[Phase 5.6B/C] Snapshot retention: deleted %d rows older than %d days "
                 "(kept active=%s)", deleted, keep_days, active_version)
    return deleted or 0



def get_incomplete_chunks(scan_id: str) -> dict:
    " Return chunk_name -> symbols_processed for incomplete chunks of a scan."
    rows = execute_db("SELECT chunk_name, symbols_processed FROM universe_chunk_runs WHERE scan_id = ? AND status != 'COMPLETED'", (scan_id,), fetch="all")
    if not rows: return {}
    return {r["chunk_name"]: r.get("symbols_processed", 0) for r in rows}

def get_chunk_run_states(scan_id: str) -> dict:
    """Return chunk_name -> (status, symbols_processed) for a scan."""
    rows = execute_db("SELECT chunk_name, status, symbols_processed FROM universe_chunk_runs WHERE scan_id = ?", (scan_id,), fetch="all")
    if not rows: return {}
    return {r["chunk_name"]: (r["status"], r.get("symbols_processed", 0)) for r in rows}


# --- Historical Cache API ---
def get_historical_cache(symbol_token: str, exchange: str, timeframe: str, allow_stale: bool = False):
    try:
        if allow_stale:
            row = execute_db(
                "SELECT payload_json FROM historical_cache WHERE symbol_token = ? AND exchange = ? AND timeframe = ?",
                (symbol_token, exchange, timeframe), fetch="one"
            )
        else:
            # PG uses NOW(), SQLite uses datetime('now') — execute_db handles placeholder translation
            # Use a portable approach: fetch and check expiry in Python
            row = execute_db(
                "SELECT payload_json, expires_at FROM historical_cache WHERE symbol_token = ? AND exchange = ? AND timeframe = ?",
                (symbol_token, exchange, timeframe), fetch="one"
            )
            if row:
                expires_at = row.get("expires_at")
                if expires_at:
                    from datetime import datetime as _dt
                    try:
                        if isinstance(expires_at, str):
                            exp_dt = _dt.fromisoformat(expires_at.replace("Z", "+00:00").replace("+00:00", ""))
                        else:
                            exp_dt = expires_at  # already datetime
                        if exp_dt < _dt.utcnow():
                            return None  # expired
                    except Exception:
                        pass  # if we can't parse, treat as valid
        if row:
            payload = row.get("payload_json")
            if isinstance(payload, str):
                return json.loads(payload)
            return payload  # already parsed (PG JSONB)
        return None
    except Exception as e:
        log.error("Failed to get historical_cache for %s: %s", symbol_token, e)
        return None

def set_historical_cache(symbol_token: str, exchange: str, timeframe: str, payload: list, ttl_hours: int = 24):
    try:
        payload_str = json.dumps(payload)
        if is_postgresql():
            execute_db(
                """INSERT INTO historical_cache (symbol_token, exchange, timeframe, last_refresh, expires_at, payload_json)
                   VALUES (?, ?, ?, NOW(), NOW() + interval '1 hour' * ?, ?)
                   ON CONFLICT (symbol_token, exchange, timeframe)
                   DO UPDATE SET last_refresh = EXCLUDED.last_refresh, expires_at = EXCLUDED.expires_at, payload_json = EXCLUDED.payload_json""",
                (symbol_token, exchange, timeframe, ttl_hours, payload_str)
            )
        else:
            execute_db(
                """INSERT INTO historical_cache (symbol_token, exchange, timeframe, last_refresh, expires_at, payload_json)
                   VALUES (?, ?, ?, datetime('now'), datetime('now', '+' || ? || ' hours'), ?)
                   ON CONFLICT(symbol_token, exchange, timeframe) DO UPDATE SET
                   last_refresh=excluded.last_refresh, expires_at=excluded.expires_at, payload_json=excluded.payload_json""",
                (symbol_token, exchange, timeframe, ttl_hours, payload_str)
            )
    except Exception as e:
        log.error("Failed to set historical_cache for %s: %s", symbol_token, e)


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 5.7: Immutable First Analysis Lock
# ═══════════════════════════════════════════════════════════════════════════════

def get_first_analysis(symbol: str) -> dict:
    """Get the locked first analysis for a symbol. Returns None if not yet analysed."""
    try:
        row = execute_db(
            "SELECT * FROM recommendation_history WHERE symbol = ? AND is_first_analysis = TRUE LIMIT 1",
            (symbol,), fetch="one"
        )
        return dict(row) if row else None
    except Exception:
        return None


def save_first_analysis(symbol: str, analysis_data: dict, scan_id: str = None):
    """
    Lock the first analysis for a symbol. This NEVER gets overwritten.
    Called only when get_first_analysis() returns None.
    """
    try:
        import json as _json
        execute_db(
            """INSERT INTO recommendation_history
               (symbol, scan_id, version, entry_low, entry_high, stop_loss, target_price,
                target1, target2, target3, risk_reward, score, grade,
                confidence_score, risk_score, technical_score, fundamental_score,
                price_at_analysis, is_first_analysis, data_snapshot)
               VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE, ?)
               ON CONFLICT (symbol, version) DO NOTHING""",
            (
                symbol, scan_id,
                analysis_data.get("entry_low"), analysis_data.get("entry_high"),
                analysis_data.get("stop_loss"), analysis_data.get("target_price"),
                analysis_data.get("target1"), analysis_data.get("target2"),
                analysis_data.get("target3"),
                analysis_data.get("risk_reward", 0),
                analysis_data.get("score", 0), analysis_data.get("grade", ""),
                analysis_data.get("confidence_score", 0),
                analysis_data.get("risk_score", 0),
                analysis_data.get("technical_score", 0),
                analysis_data.get("fundamental_score", 0),
                analysis_data.get("price", analysis_data.get("close", 0)),
                _json.dumps(analysis_data, default=str),
            )
        )
        log.info("[FIRST_ANALYSIS] Locked for %s (score=%s, grade=%s)",
                 symbol, analysis_data.get("score"), analysis_data.get("grade"))
    except Exception as exc:
        log.warning("[FIRST_ANALYSIS] Save failed for %s: %s", symbol, exc)


def save_rescan_analysis(symbol: str, analysis_data: dict, scan_id: str = None,
                          change_reason: str = None):
    """
    Store a rescan result as a new version. First analysis remains untouched.
    """
    try:
        import json as _json
        # Get next version number
        row = execute_db(
            "SELECT COALESCE(MAX(version), 0) as max_ver FROM recommendation_history WHERE symbol = ?",
            (symbol,), fetch="one"
        )
        next_version = (row.get("max_ver", 0) if row else 0) + 1

        execute_db(
            """INSERT INTO recommendation_history
               (symbol, scan_id, version, entry_low, entry_high, stop_loss, target_price,
                target1, target2, target3, risk_reward, score, grade,
                confidence_score, risk_score, technical_score, fundamental_score,
                price_at_analysis, is_first_analysis, change_reason, data_snapshot)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, FALSE, ?, ?)
               ON CONFLICT (symbol, version) DO NOTHING""",
            (
                symbol, scan_id, next_version,
                analysis_data.get("entry_low"), analysis_data.get("entry_high"),
                analysis_data.get("stop_loss"), analysis_data.get("target_price"),
                analysis_data.get("target1"), analysis_data.get("target2"),
                analysis_data.get("target3"),
                analysis_data.get("risk_reward", 0),
                analysis_data.get("score", 0), analysis_data.get("grade", ""),
                analysis_data.get("confidence_score", 0),
                analysis_data.get("risk_score", 0),
                analysis_data.get("technical_score", 0),
                analysis_data.get("fundamental_score", 0),
                analysis_data.get("price", analysis_data.get("close", 0)),
                change_reason,
                _json.dumps(analysis_data, default=str),
            )
        )
        log.info("[RESCAN_ANALYSIS] Saved version %d for %s (reason=%s)",
                 next_version, symbol, change_reason)
    except Exception as exc:
        log.warning("[RESCAN_ANALYSIS] Save failed for %s: %s", symbol, exc)


def get_analysis_history(symbol: str) -> list:
    """Get all analysis versions for a symbol, ordered by version."""
    try:
        rows = execute_db(
            "SELECT * FROM recommendation_history WHERE symbol = ? ORDER BY version ASC",
            (symbol,), fetch="all"
        )
        return [dict(r) for r in rows] if rows else []
    except Exception:
        return []
