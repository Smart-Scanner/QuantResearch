"""Phase 1.5 Change Set A unit tests — canonical freshness + scan-id-versioned cache.

Isolated against a throwaway SQLite DB (DATABASE_URL cleared before importing db). Flag-gated
by PHASE15_CANONICAL_FRESHNESS; default OFF = legacy behaviour (byte-identical).

Run: python -m pytest test_phase15_canonical.py -v
"""
import os
import tempfile
import pathlib

import pytest

os.environ.pop("DATABASE_URL", None)
os.environ.pop("SUPABASE_DB_URL", None)
os.environ["PHASE15_CANONICAL_FRESHNESS"] = "0"

import db  # noqa: E402
_A_DB = pathlib.Path(tempfile.mkdtemp(prefix="phase15a_")) / "a.db"
db.DB_PATH = _A_DB
assert not db.is_postgresql()
db._init_sqlite()

import cache_layer  # noqa: E402


def _seed_scan(sid, end="2026-06-27 10:05:00"):
    db.execute_db("INSERT INTO scan_runs (scan_id,status,start_time,end_time,created_at) VALUES (?,?,?,?,?)",
                  (sid, "completed", "2026-06-27 10:00:00", end, end))


@pytest.fixture(autouse=True)
def _pin():
    db.DB_PATH = _A_DB
    db.execute_db("DELETE FROM scan_runs")
    db.execute_db("DELETE FROM scan_results_v2")
    if hasattr(db, "_meta_cache"):
        db._meta_cache.clear()
    cache_layer._cache_gen = None
    cache_layer._cache_gen_at = 0.0
    cache_layer._reset_compute_locks()
    for c in (cache_layer.results_cache, cache_layer.dashboard_cache, cache_layer.sector_cache,
              cache_layer.stats_cache, cache_layer.news_cache, cache_layer.search_cache):
        c.clear()
    os.environ["PHASE15_CANONICAL_FRESHNESS"] = "0"
    yield


# ── flag OFF = legacy unversioned behaviour ──────────────────────────────────
def test_flag_off_legacy_unversioned():
    calls = []
    v = cache_layer.get_or_compute(cache_layer.results_cache, "results", lambda: (calls.append(1) or "v1"))
    assert v == "v1"
    assert "results" in cache_layer.results_cache         # legacy key, no generation suffix
    v2 = cache_layer.get_or_compute(cache_layer.results_cache, "results", lambda: (calls.append(1) or "v2"))
    assert v2 == "v1" and len(calls) == 1                 # cache hit, no recompute


# ── flag ON = generation-keyed + a new scan invalidates structurally ─────────
def test_flag_on_versioned_key_and_generation_switch():
    _seed_scan("scan_g1", end="2026-06-27 10:05:00")
    os.environ["PHASE15_CANONICAL_FRESHNESS"] = "1"
    calls = []
    v = cache_layer.get_or_compute(cache_layer.results_cache, "results", lambda: (calls.append(1) or "g1"))
    assert v == "g1"
    assert "results:scan_g1" in cache_layer.results_cache  # versioned key
    v2 = cache_layer.get_or_compute(cache_layer.results_cache, "results", lambda: (calls.append(1) or "x"))
    assert v2 == "g1" and len(calls) == 1                  # same generation -> hit

    _seed_scan("scan_g2", end="2026-06-27 10:10:00")       # newer scan completes
    cache_layer._cache_gen = None; cache_layer._cache_gen_at = 0.0   # force micro-cache re-pull
    v3 = cache_layer.get_or_compute(cache_layer.results_cache, "results", lambda: (calls.append(1) or "g2"))
    assert v3 == "g2" and len(calls) == 2                  # stale generation is structurally a MISS
    assert "results:scan_g2" in cache_layer.results_cache


# ── only scan-derived caches are versioned (stats stays legacy) ──────────────
def test_scoped_versioning_stats_not_versioned():
    _seed_scan("scan_g1")
    os.environ["PHASE15_CANONICAL_FRESHNESS"] = "1"
    cache_layer.get_or_compute(cache_layer.stats_cache, "stats", lambda: "sv")
    assert "stats" in cache_layer.stats_cache              # event-derived -> NOT versioned
    assert "stats:scan_g1" not in cache_layer.stats_cache


# ── pinned scan_id threads through load_results + get_result_count ───────────
def test_pinned_scan_id():
    _seed_scan("scan_pin")
    db.execute_db("INSERT INTO scan_results_v2 (scan_id,symbol,data,score,scan_date,updated_at) VALUES (?,?,?,?,?,?)",
                  ("scan_pin", "AAA", '{"symbol":"AAA","score":50,"price":100}', 50, "2026-06-27", "2026-06-27 10:05:00"))
    assert db.get_result_count(scan_id="scan_pin") == 1
    assert db.get_result_count(scan_id="scan_other") == 0    # pinned to a different generation
    assert len(db.load_results(10, slim=False, scan_id="scan_pin")) == 1
    assert len(db.load_results(10, slim=False, scan_id="scan_other")) == 0


# ── canonical last_scan derived from scan_runs.end_time ──────────────────────
def test_get_last_scan_display_canonical():
    _seed_scan("scan_disp", end="2026-06-27 11:22:33")
    assert db.get_last_scan_display("scan_disp") == "2026-06-27 11:22:33"
    assert db.get_last_scan_display() == "2026-06-27 11:22:33"   # unpinned -> latest


def test_get_last_scan_display_fallback():
    db.execute_db("INSERT INTO scan_runs (scan_id,status,start_time,created_at) VALUES (?,?,?,?)",
                  ("scan_noend", "completed", "2026-06-27 10:00:00", "2026-06-27 10:00:00"))
    db.set_meta("last_scan", "LEGACY_META")
    assert db.get_last_scan_display("scan_noend") == "LEGACY_META"   # no end_time -> legacy fallback


# ── observability snapshot ───────────────────────────────────────────────────
def test_cache_generation_status():
    _seed_scan("scan_obs")
    os.environ["PHASE15_CANONICAL_FRESHNESS"] = "1"
    cache_layer.current_cache_generation()
    s = cache_layer.cache_generation_status()
    assert s["phase15_canonical"] is True
    assert s["cache_generation"] == "scan_obs"
    for k in ("cache_generation_switch", "cache_generation_hits", "generation_query_failed", "stale_generation_served"):
        assert k in s


# ── generation resolver fail-open to last-known ──────────────────────────────
def test_generation_failopen(monkeypatch):
    os.environ["PHASE15_CANONICAL_FRESHNESS"] = "1"
    cache_layer._cache_gen = "scan_last"; cache_layer._cache_gen_at = 0.0
    monkeypatch.setattr(db, "get_latest_completed_scan_id",
                        lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    assert cache_layer.current_cache_generation() == "scan_last"   # fail-open, never raises


# ── set_cache_generation PUSH advances generation + clears compute locks ─────
def test_set_cache_generation_push():
    os.environ["PHASE15_CANONICAL_FRESHNESS"] = "1"
    cache_layer.set_cache_generation("scan_push1")
    assert cache_layer.current_cache_generation() == "scan_push1"   # within 5s micro-cache
    cache_layer.set_cache_generation("scan_push2")
    assert cache_layer._cache_gen == "scan_push2"
