"""API routes — scan, results, live prices, stock detail, export."""

import os

import csv
import io
import json
import re
import time
import hashlib
import logging
import threading
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from flask import Blueprint, jsonify, request, Response, make_response
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import MACD, EMAIndicator, SMAIndicator, ADXIndicator
from ta.volatility import BollingerBands, AverageTrueRange
from ta.volume import OnBalanceVolumeIndicator
from jugaad_data.nse import stock_df
import math

from metrics import counters

# ── Logger Initialization ──
log = logging.getLogger("api")
log.info("API logger initialized")

# ── Heavy fields that should only load in drawer via /api/stock/<symbol> ──
# These 12 fields account for ~92% of the /api/results payload (3.3MB of 3.6MB)
# but are never rendered in card/list views.
_HEAVY_FIELDS = frozenset({
    "chart_data",           # 1609 KB (50%) — sparkline arrays
    "signals",              #  618 KB (19%) — full signal history
    "fundamentals",         #  231 KB  (7%) — PE/PB/ROE details
    "trade",                #  187 KB  (6%) — entry/exit/SL details
    "order_book",           #   80 KB  (3%) — bid/ask data
    "seasonal",             #   67 KB  (2%) — seasonal patterns
    "news_sentiment",       #   63 KB  (2%) — news scores
    "support_resistance",   #   48 KB  (2%) — S/R levels
    "sector_rotation",      #   28 KB  (1%) — per-stock RRG
    "gdelt",                #   27 KB  (1%) — GDELT articles
    "macro_event",          #   22 KB  (1%) — macro events
    "earnings_signals",     #   19 KB  (1%) — earnings data
})


def _slim_result(stock: dict) -> dict:
    """Return a stock dict stripped of heavy drawer-only fields.

    Keeps all card-essential fields (~65 lightweight fields) like symbol, score,
    price, sector, risk_reward, confidence sub-scores, etc.
    """
    slim = {k: v for k, v in stock.items() if k not in _HEAVY_FIELDS}
    trade = stock.get("trade")
    if isinstance(trade, dict):
        slim["trade_summary"] = {
            "entry_low": trade.get("entry_low"),
            "entry_high": trade.get("entry_high"),
            "stop_loss": trade.get("stop_loss"),
            "target1": trade.get("target1"),
            "target2": trade.get("target2"),
            "target3": trade.get("target3"),
        }
    return slim


def _slim_results(results: list) -> list:
    """Strip heavy fields from a list of stock results."""
    return [_slim_result(r) for r in results]


def sanitize_nan(obj):
    if isinstance(obj, dict):
        return {k: sanitize_nan(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize_nan(x) for x in obj]
    elif isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    return obj


def safe_float(v, default=None):
    if v is None or pd.isna(v):
        return default
    try:
        val = float(v)
        if math.isnan(val) or math.isinf(val):
            return default
        return round(val, 2)
    except Exception:
        return default

def _req_engine():
    """Optional client-side engine override (model_version) for read endpoints.

    Reads ?engine= (query) or JSON body "engine". Returns a validated non-empty str, else
    None. None means "use the server-side ui_reco_source default" — unchanged behavior. This
    NEVER blends engines; it only selects which single engine the read resolves against.
    """
    eng = request.args.get("engine")
    if not eng:
        body = request.get_json(silent=True) or {}
        if isinstance(body, dict):
            eng = body.get("engine")
    if isinstance(eng, str):
        eng = eng.strip()
        if eng:
            return eng
    return None


from config import TOP_N_RESULTS, DASHBOARD_MAX_RESULTS, DATA_LOOKBACK_DAYS
from stocks import SECTORS
from universe import get_universe_stats
from scanner import scan_state, run_full_scan
from scan_context import ScanContext
from analyzer import fetch_and_analyze, yf_guard_status
from routes.auth import admin_required
from intelligence.fundamentals import extract_detailed_financials, safe_load_json
from metrics.timer import timed
import live_feed
import db
import cache_layer
from target_utils import resolve_targets

api_bp = Blueprint("api", __name__)

APP_VERSION = os.getenv("APP_VERSION", "v5")

# ─── Phase 10: Detail Page Indicator Cache ───
DETAIL_CACHE_DIR = Path("cache/detail")
DETAIL_CACHE_TTL = 15 * 60  # 15 minutes


def get_cached_indicator_series(symbol: str) -> dict | None:
    """Load cached indicator series if fresh + scan-valid."""
    path = DETAIL_CACHE_DIR / f"{symbol.upper()}.json"
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > DETAIL_CACHE_TTL:
        return None
    data = safe_load_json(path)
    if data is None:
        return None
    # Scan freshness check: if DB has newer data, invalidate
    cached_scan_at = data.get("_last_scan_at", "")
    db_stock = db.get_stock(symbol.upper())
    if db_stock and db_stock.get("updated_at", "") > cached_scan_at:
        return None
    return data


def save_indicator_series(symbol: str, data: dict):
    """Save indicator series to cache."""
    DETAIL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data["_last_scan_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    path = DETAIL_CACHE_DIR / f"{symbol.upper()}.json"
    path.write_text(json.dumps(data, default=str), encoding="utf-8")


@api_bp.route("/api/scan", methods=["POST"])
@admin_required
def start_scan():
    # Phase 0A: Check via DB for active scan (not in-memory singleton)
    active, active_scan_id = db.is_scan_active()
    if active:
        # Section 4: HTTP 409 Conflict with active scan info
        return jsonify({
            "error": "scan_already_active",
            "scan_id": active_scan_id,
            "status": "already_scanning",
        }), 409

    # Phase 1: Create ScanContext at ingress with full attribution
    from flask import session as flask_session
    ctx = ScanContext.create(
        trigger_source="manual",
        user_id=str(flask_session.get("user_id", "unknown")),
        session_id=str(flask_session.get("session_id", "unknown")),
        mode="manual",
    )
    threading.Thread(target=run_full_scan, args=(ctx,), daemon=True).start()
    return jsonify({
        "status": "started",
        "scan_id": ctx.scan_id,
        "correlation_id": ctx.correlation_id,
    })


def _ui_last_scan_display():
    """Last-scan display for the ACTIVE UI engine. Derive the timestamp from the active
    engine's scan_id stamp (works for BOTH engines), but only emit it after VALIDATING it
    parses to a real date/time — guards against the corrupt '8284-35-28 26:05' that a
    malformed stamp produced. Falls back to the canonical scan_runs value otherwise, so the
    dashboard/status never shows '—' (scoring_v1) nor an impossible date (legacy)."""
    try:
        from datetime import datetime as _dt
        m = re.search(r"(\d{8})_(\d{6})", db.get_ui_scan_id() or "")
        if m:
            d, t = m.group(1), m.group(2)
            # strptime rejects month 13+, hour 24+, etc. — only a real timestamp survives.
            _dt.strptime(d + t, "%Y%m%d%H%M%S")
            return f"{d[:4]}-{d[4:6]}-{d[6:8]} {t[:2]}:{t[2:4]}:{t[4:6]}"
    except Exception:
        pass
    return db.get_last_scan_display()


@api_bp.route("/api/status")
def scan_status():
    def _compute():
        t0 = time.time()
        state = scan_state.status()

        # Phase C: Lightweight aggregation using indexed columns only
        # Avoids full-table JSONB scan that was causing 5-37s response times
        use_pg = db.is_postgresql() and not db.pg_cooldown_active()
        try:
            # Engine-aware: count for the ACTIVE UI engine (get_ui_scan_id), not the
            # legacy-only latest-completed scan, so hc/golden counts match the shown engine.
            scan_id = db.get_ui_scan_id() if hasattr(db, 'get_ui_scan_id') else None
            if use_pg and scan_id:
                agg = db.execute_db("""
                    SELECT
                        COALESCE(SUM(high_conviction), 0) as hc_count,
                        COALESCE(SUM(CASE WHEN (data->>'is_golden')::text IN ('true','1') THEN 1 ELSE 0 END), 0) as golden_count,
                        COUNT(*) as total_count
                    FROM scan_results_v2 WHERE scan_id = ?
                """, (scan_id,), fetch="one")
            elif use_pg:
                agg = db.execute_db("""
                    SELECT
                        COALESCE(SUM(high_conviction), 0) as hc_count,
                        COALESCE(SUM(CASE WHEN (data->>'is_golden')::text IN ('true','1') THEN 1 ELSE 0 END), 0) as golden_count,
                        COUNT(*) as total_count
                    FROM scan_results
                """, fetch="one")
            else:
                raise Exception("use sqlite")
            # adv/dec counts are too expensive on remote PG — estimate from cached results instead
            if agg and isinstance(agg, dict):
                agg.setdefault("adv_count", 0)
                agg.setdefault("dec_count", 0)
            else:
                agg = {"hc_count": 0, "golden_count": 0, "adv_count": 0, "dec_count": 0}
        except Exception:
            log.exception("[STATUS PG QUERY FAILED]")
            agg = db.execute_db("""
                SELECT
                    COALESCE(SUM(high_conviction), 0) as hc_count,
                    COALESCE(SUM(CASE WHEN json_extract(data, '$.is_golden') IN (1, 'true') THEN 1 ELSE 0 END), 0) as golden_count,
                    COALESCE(SUM(CASE WHEN CAST(json_extract(data, '$.change_pct') AS REAL) > 0 OR CAST(json_extract(data, '$.price_change_pct') AS REAL) > 0 THEN 1 ELSE 0 END), 0) as adv_count,
                    COALESCE(SUM(CASE WHEN CAST(json_extract(data, '$.change_pct') AS REAL) < 0 OR CAST(json_extract(data, '$.price_change_pct') AS REAL) < 0 THEN 1 ELSE 0 END), 0) as dec_count
                FROM scan_results
            """, fetch="one")

        hc_count = agg.get("hc_count", 0) if isinstance(agg, dict) else 0
        golden_count = agg.get("golden_count", 0) if isinstance(agg, dict) else 0
        adv_count = agg.get("adv_count", 0) if isinstance(agg, dict) else 0
        dec_count = agg.get("dec_count", 0) if isinstance(agg, dict) else 0

        db_time = round((time.time() - t0) * 1000)
        log.info("[STATUS PERF] cache_hit=false | db_time=%dms | query_count=2 | total_time=%dms", db_time, db_time)

        # Phase 5, Section 28: Performance budget check
        if db_time > 100:
            log.warning("[PERFORMANCE] Status endpoint budget exceeded: %d ms (budget: 100ms)", db_time)

        log.info("[STATUS DEBUG] state=%s", state)
        log.info("[STATUS DEBUG] agg=%s type=%s", agg, type(agg))

        return {
            "scanning": state.get("scanning", False),
            "status": state.get("status", "IDLE"),
            "status_source": state.get("status_source", "unknown"),
            "failed_reason": state.get("failed_reason", ""),
            "scan_id": state.get("scan_id", ""),
            "resume_version": state.get("resume_version"),
            "last_attempt": state.get("last_attempt", ""),
            "progress_updated_at": state.get("progress_updated_at", ""),
            "last_successful_scan": state.get("last_successful_scan", ""),
            "is_terminal": state.get("is_terminal", True),
            "progress": state.get("progress", 0),
            "total": state.get("total", 0),
            "last_scan": _ui_last_scan_display(),  # engine-aware (scoring_v1 from scan_id stamp)
            "market_regime": db.get_meta("market_regime", "unknown"),
            "login_status": db.get_meta("angel_login_status", {}),
            "hc_count": hc_count,
            "golden_count": golden_count,
            "adv_count": adv_count,
            "dec_count": dec_count,
        }

    result = cache_layer.get_status_cache(_compute)

    # Phase 5.5: Inject batch-level progress when universe engine is active
    from config import USE_UNIVERSE_ENGINE, MAX_SCAN_WORKERS
    if USE_UNIVERSE_ENGINE and result.get("scanning"):
        try:
            scan_id = db.get_meta("current_scan_id")
            batch_progress = db.get_batch_progress(scan_id)
            if batch_progress:
                result = dict(result)  # make mutable copy
                result.update({
                    "universe_total": batch_progress.get("universe_total", 0),
                    "completed": batch_progress.get("completed", 0),
                    "remaining": batch_progress.get("remaining", 0),
                    "batch_progress": batch_progress.get("progress", 0),
                    "current_batch": batch_progress.get("current_batch", 0),
                    "total_batches": batch_progress.get("total_batches", 0),
                    "worker_count": MAX_SCAN_WORKERS,
                    "universe_version": batch_progress.get("universe_version", ""),
                })
        except Exception:
            pass
    
    # Inject professional progress message
    if result.get("scanning"):
        try:
            progress_msg = db.get_meta("scan_progress_message", "")
            if progress_msg:
                if isinstance(result, dict):
                    result = dict(result)
                result["progress_message"] = progress_msg
        except Exception:
            pass

    # last_scan must stay fresh + engine-aware: the status cache is held INDEFINITELY while
    # idle, so a warmup-time value would otherwise stick. Recompute it outside the cache.
    try:
        result = dict(result)
        result["last_scan"] = _ui_last_scan_display()
    except Exception:
        pass

    return jsonify(result)


@api_bp.route("/api/engines/coverage")
def get_engines_coverage():
    """Per-engine data-quality coverage (scored / universe / sector% / earnings%) for the Comparison page."""
    try:
        return jsonify({"engines": db.engine_coverage()})
    except Exception:
        log.exception("[ENGINES] coverage failed")
        return jsonify({"engines": []})


@api_bp.route("/api/engines")
def get_engines():
    """Data-driven engine list for the dynamic engine switcher.

    Returns {"engines": [...], "active": <ui_reco_source or 'scoring_v1'>}. Each engine:
    {id, label, is_default, sort_order}. Engines auto-discover from scan_results_v2 +
    the curated engine_registry (see db.list_engines). Never blends engine data.
    """
    try:
        engines = db.list_engines()
    except Exception:
        log.exception("[ENGINES] list_engines failed")
        engines = []
    active = db.get_meta("ui_reco_source") or "scoring_v1"
    return jsonify({"engines": engines, "active": active})


@api_bp.route("/api/search-list")
def get_search_list():
    t0 = time.time()
    def _compute():
        results = db.get_all_results()
        search_list = []
        for r in results:
            search_list.append({
                "symbol": r.get("symbol"),
                "sector": r.get("sector", ""),
                "score": r.get("score", 0),
                "price": r.get("price", 0.0),
                "high_conviction": bool(r.get("high_conviction", False)),
                "is_golden": bool(r.get("is_golden", False))
            })
        return search_list
    data = cache_layer.get_or_compute(cache_layer.search_cache, "search-list", _compute)
    total_ms = round((time.time() - t0) * 1000)
    log.info("[SEARCH PERF] total_time=%dms", total_ms)
    return jsonify(data)



@api_bp.route("/api/results")
def get_results():
    t_start = time.perf_counter()
    sort_by = request.args.get("sort", "score")
    order = request.args.get("order", "desc")
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 25, type=int)
    per_page = min(per_page, 200)  # Safety cap per request
    engine = _req_engine()  # optional client override; None -> server default (unchanged)

    timings = {"cache_hit": True, "load_results": 0.0, "status": 0.0, "universe": 0.0, "meta": 0.0}

    def _compute_results():
        timings["cache_hit"] = False
        # Freshness (canonical, graduated to default): pin the scan generation ONCE per
        # compute so every read below (results, count, last_scan display) resolves the SAME
        # generation — no intra-request mixing, and the displayed timestamp matches its rows.
        # GOAL #1 (live partial results): get_display_scan_id() returns the ACTIVE scan_id
        # (once it has >=1 saved row) so an in-progress scan's partial board shows live;
        # when idle it returns get_latest_completed_scan_id() — identical to before.
        _gen = db.get_ui_scan_id(engine)  # engine toggle (default scoring_v1); MUST-FIX 2 isolation

        t0 = time.perf_counter()
        results = db.load_results(5000, slim=True, scan_id=_gen)  # Load all results (no artificial cap)
        timings["load_results"] = round((time.perf_counter() - t0) * 1000, 2)
        
        t0 = time.perf_counter()
        state = scan_state.status()
        timings["status"] = round((time.perf_counter() - t0) * 1000, 2)
        
        t0 = time.perf_counter()
        uni_stats = get_universe_stats()
        universe_size = uni_stats.get("total_symbols", 2200)
        timings["universe"] = round((time.perf_counter() - t0) * 1000, 2)
        
        t0 = time.perf_counter()
        last_scan = db.get_last_scan_display(_gen)
        nifty50_1m = db.get_meta("nifty50_1m", 0)
        summary = db.get_meta("summary", "")
        heatmap = db.get_meta("heatmap", [])
        regime = db.get_meta("market_regime", "unknown")
        login_status = db.get_meta("angel_login_status", {})
        total_analyzed = db.get_result_count(scan_id=_gen)
        timings["meta"] = round((time.perf_counter() - t0) * 1000, 2)
        
        return {
            "results": results,
            "total_analyzed": total_analyzed,
            "universe_size": universe_size,
            "last_scan": last_scan,
            "errors": state.get("errors", 0),
            "nifty50_1m": nifty50_1m,
            "summary": summary,
            "heatmap": heatmap,
            "market_regime": regime,
            "login_status": login_status,
        }

    # GOAL #1 (live partial results): while a scan is ACTIVE, BYPASS the results cache and
    # compute fresh from the DB on every poll, so partial batches appear live. When idle,
    # keep the EXACT cached behavior (byte-identical to before). Also bypass when the client
    # requests a specific engine (cache key "results" is engine-agnostic — serving it for an
    # explicit engine would leak the default engine's board).
    _scan_active, _ = db.is_scan_active()
    if _scan_active or engine:
        data = _compute_results()
    else:
        data = cache_layer.get_or_compute(cache_layer.results_cache, "results", _compute_results)

    t_slim_start = time.perf_counter()
    raw_results = data.get("results", [])
    slim_results = _slim_results(raw_results)
    # recommendation_locks holds LEGACY thesis locks only. Merging them onto a
    # scoring_v1 board overwrites that engine's own entry/SL/target with stale legacy
    # values, so the displayed levels stop reconciling with the shown R:R. Skip the
    # merge when the active engine isn't legacy (its own levels are already on the row).
    # Honor an explicit client engine; else fall back to the server toggle (unchanged).
    _ui_engine = engine or db.get_meta("ui_reco_source", "scoring_v1")
    if _ui_engine != "legacy":
        # scoring_v1 board: its own levels are on the row; legacy locks don't apply.
        for r in slim_results:
            r["thesis_status"] = "NONE"
    else:
        try:
            locks = db.execute_db(
                "SELECT symbol, thesis_status, entry_low, entry_high, stop_loss, target1 FROM recommendation_locks",
                fetch="all"
            )
            locks_map = {}
            if locks:
                for row in locks:
                    if isinstance(row, dict):
                        locks_map[row["symbol"].upper()] = row
                    else:
                        locks_map[row[0].upper()] = {
                            "symbol": row[0],
                            "thesis_status": row[1],
                            "entry_low": row[2],
                            "entry_high": row[3],
                            "stop_loss": row[4],
                            "target1": row[5]
                        }
                for r in slim_results:
                    sym = r.get("symbol", "").upper()
                    if sym in locks_map:
                        lock = locks_map[sym]
                        r["thesis_status"] = lock.get("thesis_status", "ACTIVE")
                        if lock.get("thesis_status") == "ACTIVE":
                            r["locked_entry_low"] = lock.get("entry_low")
                            r["locked_entry_high"] = lock.get("entry_high")
                            r["locked_stop_loss"] = lock.get("stop_loss")
                            r["locked_target1"] = lock.get("target1")
                    else:
                        r["thesis_status"] = "NONE"
        except Exception as exc:
            log.warning("Failed to merge recommendation locks: %s", exc)
    # Subscribe the visible top of the board to the WS live feed so CMP ticks flow.
    # GET /api/live-prices (polled by the board) only reads the tick cache; symbols
    # must be subscribed first. The scoring_v1 picks aren't in the legacy warmup set.
    try:
        _top_syms = [r.get("symbol", "").upper().replace(".NS", "")
                     for r in slim_results[:60] if r.get("symbol")]
        if _top_syms:
            live_feed.subscribe(_top_syms)
    except Exception:
        pass

    # Board enrichment: conviction rank (D/S tiers, for sorting), first-recommended date
    # ("kab recommendation aayi"), and the scan generation timestamp (header date-stamp).
    def _tier_rank(v):
        return {"H": 3, "M": 2, "L": 1}.get(str(v or "")[:1].upper(), 0)
    _first_rec = {}
    try:
        _snap = db.execute_db(
            "SELECT symbol, MIN(snapshot_date) AS fd FROM recommendation_snapshots "
            "WHERE model_version = 'scoring_v1' GROUP BY symbol", fetch="all") or []
        for row in _snap:
            sym = (row["symbol"] if isinstance(row, dict) else row[0])
            fd = (row["fd"] if isinstance(row, dict) else row[1])
            if sym:
                _first_rec[sym.upper()] = (str(fd)[:10] if fd else None)
    except Exception as exc:
        log.warning("first-recommended lookup failed: %s", exc)
    for r in slim_results:
        # conviction_rank: data integrity weighted over signal agreement (D:H S:H = 33)
        r["conviction_rank"] = _tier_rank(r.get("data_integrity")) * 10 + _tier_rank(r.get("signal_agreement"))
        r["first_recommended"] = _first_rec.get((r.get("symbol") or "").upper())

    _gen_at = None
    try:
        _m = re.search(r"(\d{8})_(\d{6})", db.get_ui_scan_id(engine) or "")
        if _m:
            _d, _t = _m.group(1), _m.group(2)
            # Validate it's a REAL date/time. Legacy ids (scan_manual_<unixts>_<n>)
            # otherwise mis-parse to garbage like "8284-35-28 26:05".
            from datetime import datetime as _dt
            _dt.strptime(_d + _t, "%Y%m%d%H%M%S")
            _gen_at = f"{_d[:4]}-{_d[4:6]}-{_d[6:8]} {_t[:2]}:{_t[2:4]}"
    except Exception:
        _gen_at = None
    if not _gen_at:  # fall back to the canonical display (valid for legacy unix-ts ids)
        _gen_at = data.get("last_scan")
    slim = {**data, "results": slim_results, "scan_generated_at": _gen_at}
    t_slim_ms = round((time.perf_counter() - t_slim_start) * 1000, 2)

    t_sort_start = time.perf_counter()
    valid_sorts = [
        "score", "price", "rsi", "adx", "volume_ratio", "pct_1w", "pct_1m",
        "delivery_pct", "risk_score", "rs_vs_nifty", "risk_reward", "target_pct",
        "atr_pct", "stoch_k", "bb_position", "conviction_rank",
    ]
    all_results = slim["results"]
    if sort_by in valid_sorts and sort_by != "score":
        all_results = sorted(all_results, key=lambda x: x.get(sort_by) or 0, reverse=(order == "desc"))

    # Pagination
    total_results = len(all_results)
    total_pages = max(1, (total_results + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    paginated_results = all_results[start_idx:end_idx]

    res_dict = {**slim, "results": paginated_results}
    res_dict["pagination"] = {
        "page": page,
        "per_page": per_page,
        "total_results": total_results,
        "total_pages": total_pages,
    }
    res_dict["metrics"] = {
        "target_resolved_trade": counters.get("target_resolved_trade"),
        "target_resolved_scan": counters.get("target_resolved_scan"),
        "target_missing": counters.get("target_missing"),
        "signal_compare_match": counters.get("signal_compare_match"),
        "signal_compare_mismatch": counters.get("signal_compare_mismatch"),
    }
    t_sort_ms = round((time.perf_counter() - t_sort_start) * 1000, 2)

    t_serialize_start = time.perf_counter()
    resp = jsonify(sanitize_nan(res_dict))
    t_serialize_ms = round((time.perf_counter() - t_serialize_start) * 1000, 2)

    total_ms = round((time.perf_counter() - t_start) * 1000, 2)

    if not timings["cache_hit"]:
        print(f"[RESULTS PERF] load_results={timings['load_results']} ms | status={timings['status']} ms | universe={timings['universe']} ms | meta={timings['meta']} ms | slim={t_slim_ms} ms | sort={t_sort_ms} ms | serialize={t_serialize_ms} ms | total={total_ms} ms")
        logging.getLogger("screener").info("[RESULTS PERF] load_results=%s ms | status=%s ms | universe=%s ms | meta=%s ms | slim=%s ms | sort=%s ms | serialize=%s ms | total=%s ms", timings['load_results'], timings['status'], timings['universe'], timings['meta'], t_slim_ms, t_sort_ms, t_serialize_ms, total_ms)
        # Phase 5, Section 28: Dashboard performance budget check
        if timings['load_results'] > 150:
            log.warning("[PERFORMANCE] Dashboard load_results budget exceeded: %.1f ms (budget: 150ms)", timings['load_results'])
        if total_ms > 500:
            log.warning("[PERFORMANCE] Dashboard total response budget exceeded: %.1f ms (budget: 500ms)", total_ms)

    return resp



@api_bp.route("/api/export/csv")
@admin_required
def export_csv():
    output = io.StringIO()
    writer = csv.writer(output)
    headers = [
        "Rank", "Symbol", "Sector", "Score", "High Conviction", "Price",
        "Target", "Target%", "StopLoss(ATR)", "SL%", "R:R",
        "RSI", "ADX", "MACD", "Volume", "Weekly Trend",
        "1W%", "2W%", "1M%", "Delivery%", "Fib Level",
        "Risk Score", "RS vs Nifty", "Breakout", "Accumulation",
        "Support S1", "Resistance R1",
    ]
    writer.writerow(headers)
    for i, r in enumerate(db.load_results(TOP_N_RESULTS)):
        sr = r.get("support_resistance", {})
        writer.writerow([
            i + 1, r["symbol"], r["sector"], r["score"],
            "YES" if r.get("high_conviction") else "",
            r["price"], r["target_price"], r.get("target_pct", ""),
            r["stop_loss"], r.get("stop_loss_pct", ""), r.get("risk_reward", ""),
            r["rsi"], r.get("adx", ""), r["macd_signal"], r["volume_ratio"],
            r.get("weekly_trend", ""),
            r["pct_1w"], r["pct_2w"], r["pct_1m"], r.get("delivery_pct", ""),
            r.get("fib_level", ""), r.get("risk_score", ""), r.get("rs_vs_nifty", ""),
            "YES" if r.get("is_breakout") else "", "YES" if r.get("vp_divergence") else "",
            sr.get("s1", ""), sr.get("r1", ""),
        ])
    output.seek(0)
    return Response(
        output.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename=nifty250_v4_{date.today()}.csv"},
    )


@api_bp.route("/api/stock/<symbol>")
@timed("detail_page_response")
def stock_data(symbol):
    """Return extended indicator series for the detail page.
    Phase 10: Uses indicator series cache (15min TTL, scan-invalidated).
    Financials split to /api/stock/<symbol>/financials for async loading.
    """
    import urllib.parse
    clean = urllib.parse.unquote(symbol).strip().upper().replace(".NS", "")
    engine = _req_engine()  # optional client override; None -> server default (unchanged)
    # GOAL #1 (live partial results): resolve against the display generation so a symbol's
    # in-progress scan result shows during a scan; idle = latest-completed (unchanged).
    cached_db = db.get_stock(clean, scan_id=db.get_ui_scan_id(engine))  # match the active engine view
    # scoring_v1 AND legacy_cleaned rows carry their OWN levels (scoring_v1: capped entry/SL/
    # target; legacy_cleaned: swing-low structure SL + resistance targets + varied R:R). But
    # db.get_stock -> _ensure_trade_populated synthesizes a legacy ATR-style 'trade' sub-dict
    # (no 'trade' on the row) whose levels would WIN resolve_targets' fallback chain (trade.*
    # is preferred over scan.*), overriding the engine's own levels and breaking the displayed
    # R:R. Strip only the stale level keys (keep booking_plan etc.) so the engine's own
    # scan.* levels are shown. legacy engine is untouched (its 'trade' levels are canonical).
    if cached_db and cached_db.get("model_version") in ("scoring_v1", "legacy_cleaned") and isinstance(cached_db.get("trade"), dict):
        _stale = ("target1", "target2", "target3", "target_1", "target_2", "target_3",
                  "stop_loss", "entry_low", "entry_high", "entry")
        cached_db = dict(cached_db)
        cached_db["trade"] = {k: v for k, v in cached_db["trade"].items() if k not in _stale}

    # Phase 10: Try indicator series cache first
    # P0-3: Inject data_unavailable flag
    from symbol_utils import check_symbol_exists
    _data_unavailable = not check_symbol_exists(clean)

    series_cache = get_cached_indicator_series(clean)
    if series_cache is not None:
        # Warm cache hit — serve directly, add scan data
        result = series_cache.copy()
        result.pop("_last_scan_at", None)
        if cached_db:
            result["scan"] = _build_scan_dict(cached_db)
        # D1-A: Inject normalized targets from single source of truth
        result["targets"] = resolve_targets(cached_db, symbol=clean)
        result["contracts"] = {"targets_contract_version": 2}
        # Phase 10: financials loaded async
        result["financials_detailed"] = {
            "loading": True,
            "endpoint": f"/api/stock/{clean}/financials"
        }
        _freshness = time.time() - Path(DETAIL_CACHE_DIR / f"{clean}.json").stat().st_mtime
        result["data_freshness_seconds"] = round(_freshness)
        result["data_unavailable"] = _data_unavailable
        resp = make_response(jsonify(sanitize_nan(result)))
        resp.headers["Cache-Control"] = "max-age=900"
        return resp

    try:
        # Try Angel One first, fallback to jugaad_data
        df = live_feed.fetch_historical(clean, days=DATA_LOOKBACK_DAYS)
        if df is None or df.empty or len(df) < 50:
            end_date = date.today()
            start_date = end_date - timedelta(days=DATA_LOOKBACK_DAYS)
            df = stock_df(symbol=clean, from_date=start_date, to_date=end_date)

        if df.empty or len(df) < 50:
            return jsonify({"error": "Insufficient data"}), 404

        df = df.sort_values("DATE").reset_index(drop=True)
        close = df["CLOSE"].astype(float)
        high = df["HIGH"].astype(float)
        low = df["LOW"].astype(float)
        volume = df["VOLUME"].astype(float)
        delivery_pct = (df["DELIVERY %"].astype(float)
                        if "DELIVERY %" in df.columns
                        else pd.Series([50.0] * len(df)))

        rsi_series = RSIIndicator(close, window=14).rsi()
        macd_ind = MACD(close)
        macd_line_s = macd_ind.macd()
        macd_sig_s = macd_ind.macd_signal()
        macd_hist_s = macd_ind.macd_diff()
        ema_9_s = EMAIndicator(close, window=9).ema_indicator()
        ema_21_s = EMAIndicator(close, window=21).ema_indicator()
        sma_50_s = SMAIndicator(close, window=50).sma_indicator()
        ema_200_s = EMAIndicator(close, window=min(200, len(close) - 1)).ema_indicator()
        bb = BollingerBands(close, window=20, window_dev=2)
        bb_upper_s = bb.bollinger_hband()
        bb_lower_s = bb.bollinger_lband()
        bb_mid_s = bb.bollinger_mavg()
        stoch = StochasticOscillator(high, low, close)
        stoch_k_s = stoch.stoch()
        stoch_d_s = stoch.stoch_signal()
        adx_s = ADXIndicator(high, low, close, window=14).adx()
        atr_s = AverageTrueRange(high, low, close, window=14).average_true_range()
        obv_s = OnBalanceVolumeIndicator(close, volume).on_balance_volume()
        avg_vol_s = volume.rolling(20).mean()

        def safe_list(series):
            return [safe_float(v) for v in series]

        dates = [
            r["DATE"].strftime("%Y-%m-%d") if hasattr(r["DATE"], "strftime")
            else str(r["DATE"])[:10]
            for _, r in df.iterrows()
        ]

        ohlcv = []
        for _, row in df.iterrows():
            ohlcv.append({
                "date": row["DATE"].strftime("%Y-%m-%d") if hasattr(row["DATE"], "strftime") else str(row["DATE"])[:10],
                "o": safe_float(row.get("OPEN", row["CLOSE"])),
                "h": safe_float(row["HIGH"]),
                "l": safe_float(row["LOW"]),
                "c": safe_float(row["CLOSE"]),
                "v": int(row.get("VOLUME", 0)) if not pd.isna(row.get("VOLUME")) else 0,
            })

        sector = SECTORS.get(clean, "Other")
        current_price = safe_float(close.iloc[-1])

        result = {
            "symbol": clean, "sector": sector,
            "price": current_price,
            "high_52w": safe_float(high.max()),
            "low_52w": safe_float(low.min()),
            "dates": dates, "ohlcv": ohlcv,
            "close": safe_list(close), "volume": [int(v) if not pd.isna(v) else 0 for v in volume],
            "delivery": safe_list(delivery_pct),
            "rsi": safe_list(rsi_series),
            "macd_line": safe_list(macd_line_s), "macd_signal": safe_list(macd_sig_s),
            "macd_hist": safe_list(macd_hist_s),
            "ema_9": safe_list(ema_9_s), "ema_21": safe_list(ema_21_s),
            "sma_50": safe_list(sma_50_s), "ema_200": safe_list(ema_200_s),
            "bb_upper": safe_list(bb_upper_s), "bb_lower": safe_list(bb_lower_s),
            "bb_mid": safe_list(bb_mid_s),
            "stoch_k": safe_list(stoch_k_s), "stoch_d": safe_list(stoch_d_s),
            "adx": safe_list(adx_s), "atr": safe_list(atr_s),
            "obv": safe_list(obv_s), "avg_volume": safe_list(avg_vol_s),
            "market_regime": db.get_meta("market_regime", "unknown"),
            "nifty50_1m": db.get_meta("nifty50_1m", 0),
        }

        # Phase 10: Save to indicator cache
        try:
            save_indicator_series(clean, result.copy())
        except Exception:
            pass

        if cached_db:
            result["scan"] = _build_scan_dict(cached_db)

        # D1-A: Inject normalized targets from single source of truth
        result["targets"] = resolve_targets(cached_db, symbol=clean)
        result["contracts"] = {"targets_contract_version": 2}

        # Phase 10: Financials loaded async via separate endpoint
        result["financials_detailed"] = {
            "loading": True,
            "endpoint": f"/api/stock/{clean}/financials"
        }
        result["data_freshness_seconds"] = 0
        result["data_unavailable"] = _data_unavailable

        # Phase 5.7: Immutable First Analysis + History
        try:
            first_analysis = db.get_first_analysis(clean)
            if first_analysis:
                result["first_analysis"] = {
                    "entry_low": first_analysis.get("entry_low"),
                    "entry_high": first_analysis.get("entry_high"),
                    "stop_loss": first_analysis.get("stop_loss"),
                    "target_price": first_analysis.get("target_price"),
                    "risk_reward": first_analysis.get("risk_reward"),
                    "score": first_analysis.get("score"),
                    "grade": first_analysis.get("grade"),
                    "confidence_score": first_analysis.get("confidence_score"),
                    "risk_score": first_analysis.get("risk_score"),
                    "price_at_analysis": first_analysis.get("price_at_analysis"),
                    "analysis_date": str(first_analysis.get("analysis_timestamp", "")),
                    "locked": True,
                }
            analysis_history = db.get_analysis_history(clean)
            if analysis_history:
                result["analysis_history"] = [{
                    "version": h.get("version"),
                    "score": h.get("score"),
                    "grade": h.get("grade"),
                    "price_at_analysis": h.get("price_at_analysis"),
                    "risk_reward": h.get("risk_reward"),
                    "analysis_date": str(h.get("analysis_timestamp", "")),
                    "is_first": h.get("is_first_analysis", False),
                    "change_reason": h.get("change_reason"),
                } for h in analysis_history]
        except Exception:
            pass

        # ── Thesis Lock Data ──
        # recommendation_locks are LEGACY thesis locks. On a scoring_v1 board they would
        # override the detail page's entry/SL/target with stale legacy values (breaking the
        # displayed R:R). Only attach the lock when the active engine is legacy.
        if db.get_meta("ui_reco_source", "scoring_v1") == "legacy":
            try:
                locked_thesis = db.get_locked_thesis(clean)
                if locked_thesis:
                    result["locked_thesis"] = locked_thesis
                    result["recommended_price"] = locked_thesis.get("recommended_price")
                    result["thesis_updates"] = locked_thesis.get("updates", [])
            except Exception:
                pass

        resp = make_response(jsonify(sanitize_nan(result)))
        resp.headers["Cache-Control"] = "max-age=900"
        return resp
    except Exception as exc:
        logging.getLogger("screener").warning("Stock detail fetch failed for %s: %s", clean, exc)
        return jsonify({"error": str(exc)}), 500


# ─────────── Price-chart OHLC (self-rendered chart data source) ───────────
# The stock-detail chart is TradingView Lightweight Charts fed by THIS endpoint.
# TradingView's free EMBED widget has no NSE data (licensing), and no broker offers a
# drop-in chart widget — so we self-render candles, which also lets us draw our
# engine-aware entry/SL/target levels ON the chart. Primary source: Yahoo Finance v8
# chart (free, no key, NSE intraday via <SYM>.NS), server-cached; EOD fallback to our
# own daily bars if Yahoo is unavailable (e.g. blocked from a datacenter IP).
# Read-only; touches no scoring.
_OHLC_CACHE: dict = {}
_YF_INTERVAL = {"5m": ("5m", "5d"), "15m": ("15m", "1mo"), "60m": ("60m", "3mo"), "1d": ("1d", "2y")}
_OHLC_TTL = {"5m": 180, "15m": 300, "60m": 600, "1d": 3600}
_IST_OFFSET = 19800  # +5:30 so Lightweight Charts' UTC time axis reads IST wall-clock


def _fetch_yahoo_ohlc(sym, yf_int, yf_range, daily):
    import requests
    from datetime import datetime as _dt
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}.NS"
    try:
        resp = requests.get(url, params={"interval": yf_int, "range": yf_range},
                            headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        res = ((resp.json().get("chart", {}) or {}).get("result") or [None])[0]
        if not res:
            return []
        ts = res.get("timestamp") or []
        q = (res.get("indicators", {}).get("quote") or [{}])[0]
        o, h, l, c, v = (q.get("open") or [], q.get("high") or [], q.get("low") or [],
                         q.get("close") or [], q.get("volume") or [])
        out = []
        for i, t in enumerate(ts):
            if i >= len(c) or c[i] is None:
                continue
            tval = (_dt.utcfromtimestamp(int(t)).strftime("%Y-%m-%d") if daily
                    else int(t) + _IST_OFFSET)
            out.append({"t": tval,
                        "o": safe_float(o[i]) if i < len(o) else None,
                        "h": safe_float(h[i]) if i < len(h) else None,
                        "l": safe_float(l[i]) if i < len(l) else None,
                        "c": safe_float(c[i]),
                        "v": int(v[i]) if (i < len(v) and v[i] is not None) else 0})
        return out
    except Exception as exc:
        logging.getLogger("screener").debug("[ohlc] yahoo fetch failed %s: %s", sym, exc)
        return []


def _fetch_eod_ohlc_fallback(sym):
    """EOD fallback to our own daily bars — used only if Yahoo is unavailable."""
    try:
        df = live_feed.fetch_historical(sym, days=250)
    except Exception:
        df = None
    if df is None or getattr(df, "empty", True):
        return []
    out = []
    for _, row in df.iterrows():
        d = row.get("DATE")
        ds = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
        vol = row.get("VOLUME")
        out.append({"t": ds, "o": safe_float(row.get("OPEN", row.get("CLOSE"))),
                    "h": safe_float(row.get("HIGH")), "l": safe_float(row.get("LOW")),
                    "c": safe_float(row.get("CLOSE")),
                    "v": int(vol) if (vol is not None and not pd.isna(vol)) else 0})
    return out


@api_bp.route("/api/stock/ohlc/<symbol>")
def stock_ohlc(symbol):
    """OHLC candles for the detail chart. interval=5m|15m|60m|1d (Yahoo .NS, cached, EOD fallback)."""
    import urllib.parse
    import time as _time
    clean = urllib.parse.unquote(symbol).strip().upper().replace(".NS", "")
    interval = (request.args.get("interval") or "1d").lower()
    if interval not in _YF_INTERVAL:
        interval = "1d"
    ck = (clean, interval)
    now = _time.time()
    hit = _OHLC_CACHE.get(ck)
    if hit and (now - hit[0]) < _OHLC_TTL.get(interval, 300):
        return jsonify(hit[1])
    yf_int, yf_range = _YF_INTERVAL[interval]
    candles = _fetch_yahoo_ohlc(clean, yf_int, yf_range, daily=(interval == "1d"))
    source, delayed = "yahoo", True
    if not candles:  # Yahoo blocked/empty -> our own EOD daily bars
        candles = _fetch_eod_ohlc_fallback(clean)
        source, delayed, interval = "eod", False, "1d"
    payload = {"symbol": clean, "interval": interval, "source": source,
               "delayed": delayed, "candles": candles}
    if candles:
        _OHLC_CACHE[ck] = (now, payload)
    resp = make_response(jsonify(sanitize_nan(payload)))
    resp.headers["Cache-Control"] = "max-age=120"
    return resp


def _build_scan_dict(cached: dict) -> dict:
    """Build scan sub-dict from cached DB result.

    Tolerant of lean result dicts (e.g. scoring_v1) that omit legacy-only fields:
    every access uses .get() so a missing key never 500s the detail page.
    """
    return {
        "score": cached.get("score", 0), "risk_score": cached.get("risk_score", 0),
        "sector": cached.get("sector"),
        "is_golden": cached.get("is_golden", False),
        "first_analysis_date": cached.get("first_analysis_date"),
        "rescan_count": cached.get("rescan_count"),
        # Factor sub-scores for drawer radar chart + confidence calc
        "technical_score": cached.get("technical_score", 0),
        "fundamental_score": cached.get("fundamental_score", 0),
        "earnings_momentum_score": cached.get("earnings_momentum_score", 0),
        "smart_money_score": cached.get("smart_money_score", 0),
        "smart_money_100": cached.get("smart_money_100", 0),
        "sector_rotation_score": cached.get("sector_rotation_score", 0),
        "news_sentiment_score": cached.get("news_sentiment_score", 0),
        "macro_score": cached.get("macro_score", 0),
        "risk_reward": cached.get("risk_reward", 0),
        "target_price": cached.get("target_price"), "target_pct": cached.get("target_pct"),
        "stop_loss": cached.get("stop_loss"), "stop_loss_pct": cached.get("stop_loss_pct"),
        "signals": cached.get("signals", []),
        "high_conviction": cached.get("high_conviction", False),
        "is_breakout": cached.get("is_breakout", False),
        "weekly_trend": cached.get("weekly_trend", "flat"),
        "below_ema200": cached.get("below_ema200", False),
        "vp_divergence": cached.get("vp_divergence", False),
        "fib_level": cached.get("fib_level"),
        "fib_support": cached.get("fib_support"),
        "fib_resistance": cached.get("fib_resistance"),
        "support_resistance": cached.get("support_resistance", {}),
        "pct_1w": cached.get("pct_1w"), "pct_2w": cached.get("pct_2w"),
        "pct_1m": cached.get("pct_1m"), "rs_vs_nifty": cached.get("rs_vs_nifty"),
        "delivery_pct": cached.get("delivery_pct"),
        "delivery_trend": cached.get("delivery_trend"),
        "bb_position": cached.get("bb_position"),
        "vwap_position": cached.get("vwap_position"),
        "grade": cached.get("grade", "Weak"),
        "trade": cached.get("trade", {}),
        "mtf_trends": cached.get("mtf_trends", {}),
        "mtf_score": cached.get("mtf_score", 0),
        "seasonal": cached.get("seasonal", {}),
        "order_book": cached.get("order_book", {}),
        "sector_rotation": cached.get("sector_rotation", {}),
        "gdelt": cached.get("gdelt", {}),
        "news_sentiment": cached.get("news_sentiment", {}),
        "macro_event": cached.get("macro_event", {}),
        "macro_bias": cached.get("macro_bias", 0),
        "events": cached.get("events", []),
        "fundamentals": cached.get("fundamentals", {}),
        "composite_layer_score": cached.get("composite_layer_score", 0),
        "supports": cached.get("supports", []),
        "resistances": cached.get("resistances", []),
        # scoring_v1 attribution (Thesis Radar + provenance)
        "factor_percentiles": cached.get("factor_percentiles", {}),
        "factor_contributions": cached.get("factor_contributions", {}),
        "drivers": cached.get("drivers", ""),
        "weaknesses": cached.get("weaknesses", ""),
        "data_integrity": cached.get("data_integrity", ""),
        "signal_agreement": cached.get("signal_agreement", ""),
    }


@api_bp.route("/api/stock/<symbol>/financials")
@timed("financial_detail_response")
def stock_financials(symbol):
    """Phase 10: Separate financial endpoint for async loading."""
    import urllib.parse
    clean = urllib.parse.unquote(symbol).strip().upper().replace(".NS", "")
    try:
        cached_db = db.get_stock(clean)
        recent_news_titles = []
        try:
            rows = db.execute_db("SELECT title FROM news_articles WHERE symbol = ?", (clean,), fetch="all")
            if rows:
                recent_news_titles = [r["title"] for r in rows if r.get("title")]
        except Exception:
            pass

        upcoming_events = cached_db.get("events", []) if cached_db else []
        data = extract_detailed_financials(clean, upcoming_events, recent_news_titles)

        # ETag for client-side caching
        etag = hashlib.md5(json.dumps(data, default=str, sort_keys=True).encode()).hexdigest()
        resp = make_response(jsonify(data))
        resp.headers["ETag"] = etag
        resp.headers["Cache-Control"] = "max-age=900"
        return resp
    except Exception as exc:
        logging.getLogger("screener").warning("Financials fetch failed for %s: %s", clean, exc)
        return jsonify({
            "yearly": [], "quarterly": [],
            "fin_health_score": 0, "fin_health_verdict": "Stressed",
            "fin_alerts": [f"Error: {str(exc)}"],
            "hindi_explanation": {
                "company_strength": "--", "revenue_status": "--", "profit_status": "--",
                "debt_status": "--", "cash_flow_status": "--", "entry_advice": "--", "suitability": "--"
            }
        }), 500


@api_bp.route("/api/live-prices", methods=["GET", "POST"])
def live_prices():
    # GET: return all cached WS prices (used by symbol_workspace, top_picks)
    if request.method == "GET":
        all_prices = live_feed.get_live_prices()
        result = {}
        for sym, data in all_prices.items():
            price = data.get("ltp", 0)
            if not price:
                continue
            result[sym] = {
                "price": price, "ltp": price,
                "open": data.get("open", 0), "high": data.get("high", 0),
                "low": data.get("low", 0), "close": data.get("close", 0),
                "change": data.get("change", 0), "change_pct": data.get("change_pct", 0),
                "volume": data.get("volume", 0), "last_update": data.get("last_update", ""),
            }
        return jsonify(result)

    # POST: filter by requested symbols (used by index.html, stock_detail.html)
    body = request.get_json(silent=True) or {}
    symbols = body.get("symbols", [])
    if not symbols:
        return jsonify({"error": "No symbols provided"}), 400

    symbols = [s.upper().replace(".NS", "") for s in symbols[:500]]
    live_feed.subscribe(symbols)

    ws_prices = live_feed.get_live_prices(symbols)
    missing = [s for s in symbols if s not in ws_prices]
    if missing:
        rest_prices = live_feed.fetch_ltp_bulk(missing[:20])
        ws_prices.update(rest_prices)

    result = {}
    # Batch load all scan data in one query instead of N+1 per-symbol calls
    scan_map = db.get_stocks_map(list(ws_prices.keys()))
    for sym, data in ws_prices.items():
        price = data.get("ltp", 0)
        if not price:
            continue
        entry = {
            "price": price, "open": data.get("open", 0),
            "high": data.get("high", 0), "low": data.get("low", 0),
            "close": data.get("close", 0), "change": data.get("change", 0),
            "change_pct": data.get("change_pct", 0),
            "volume": data.get("volume", 0), "last_update": data.get("last_update", ""),
        }
        scan_data = scan_map.get(sym)
        if scan_data:
            entry["scan_price"] = scan_data.get("price")
        result[sym] = entry

    return jsonify({
        "prices": result, "source": "angel_one",
        "market_open": live_feed.is_market_open(),
        "ws_connected": live_feed._ws_running,
    })


@api_bp.route("/api/custom-stocks", methods=["GET"])
def get_custom_stocks():
    return jsonify({"stocks": db.get_custom_stocks()})


@api_bp.route("/api/custom-stocks", methods=["POST"])
def add_custom_stock():
    body = request.get_json(silent=True) or {}
    symbol = body.get("symbol", "").upper().replace("NSE:", "").replace(".NS", "").strip()
    if not symbol:
        return jsonify({"error": "Symbol required"}), 400

    # Rate limit: 10s cooldown between custom scans (process-local TTLCache)
    if "last_scan" in cache_layer.custom_scan_limiter:
        return jsonify({"error": "Too fast. Wait 10 seconds between additions."}), 429

    # Cap total custom stocks
    existing = db.get_custom_stocks()
    if len(existing) >= 50:
        return jsonify({"error": "Maximum 50 custom stocks allowed."}), 400

    cache_layer.custom_scan_limiter["last_scan"] = True  # auto-expires in 10s

    db.add_custom_stock(symbol, "NSE", body.get("note", ""))
    if os.environ.get("PHASE15_CACHE_GAPS") == "1":   # C-2: a new custom stock changes the search universe
        try:
            cache_layer.search_cache.clear()
            from metrics import counters as _c
            _c.inc("search_cache_invalidations")
        except Exception:
            pass
    try:
        nifty_1m = db.get_meta("nifty50_1m", 0)
        regime = db.get_meta("market_regime", "unknown")
        result = fetch_and_analyze(symbol, nifty_1m, regime, scan_mode="deep")
        if result:
            result["custom"] = True
            db.save_results([result])
            live_feed.subscribe([symbol])
            return jsonify({"status": "ok", "symbol": symbol, "score": result["score"], "scanned": True})
        else:
            return jsonify({"status": "ok", "symbol": symbol, "scanned": False, "message": "Added but no data available"})
    except Exception as exc:
        return jsonify({"status": "ok", "symbol": symbol, "scanned": False, "message": str(exc)})


@api_bp.route("/api/custom-stocks/<symbol>", methods=["DELETE"])
def remove_custom_stock(symbol):
    clean = symbol.upper().replace("NSE:", "").replace(".NS", "")
    return jsonify({"status": "ok", "removed": db.remove_custom_stock(clean)})


def get_git_commit_sha():
    import subprocess
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"]).decode("utf-8").strip()
    except Exception:
        return "unknown"

@api_bp.route("/api/health")
def health():
    """Detailed health check and telemetry for Mission Control."""
    try:
        from data_provider import provider_manager
        telemetry = provider_manager.get_telemetry()
    except Exception as e:
        telemetry = {"error": str(e)}

    return jsonify({
        "status": "ok",
        "version": APP_VERSION,
        "ts": int(time.time()),
        "providers": telemetry
    })


@api_bp.route("/api/debug/health")
@admin_required
def debug_health():
    """Full diagnostics endpoint — admin only."""
    try:
        db_info = db.db_stats()
    except Exception:
        db_info = {}
    state = scan_state.status()
    try:
        uni = get_universe_stats()
    except Exception:
        uni = {}
    try:
        yf_info = yf_guard_status()
    except Exception:
        yf_info = {}
    from metrics import timer
    try:
        from scanner import get_marketaux_queue_depth, get_marketaux_overflow_count
        mx_depth = get_marketaux_queue_depth()
        mx_overflow = get_marketaux_overflow_count()
    except Exception:
        mx_depth = 0
        mx_overflow = 0
    try:
        from metrics import counters
        app_counters = counters.get_all()
    except Exception:
        app_counters = {}
    try:
        dlq_count = db.dlq_entry_count()
    except Exception:
        dlq_count = 0
    return jsonify({
        "status": "ok",
        "version": APP_VERSION,
        "git_commit_sha": get_git_commit_sha(),
        "build_date": "2026-06-06",
        "ts": int(time.time()),
        "universe": uni,
        "db_results": db_info.get("results", 0),
        "db_size_kb": db_info.get("db_size_kb", 0),
        "scanning": state["scanning"],
        "market_regime": db.get_meta("market_regime", "unknown"),
        "ws_connected": live_feed._ws_running,
        "live_symbols": len(live_feed._subscribers),
        "perf_timings": timer.get_report(),
        "marketaux_queue_depth": mx_depth,
        "marketaux_overflow_count": mx_overflow,
        "counters": app_counters,
        "dlq_entries": dlq_count,
        **db_info,
        **yf_info,
    })


@api_bp.route("/api/operations")
def operations():
    """Phase 1.5 (Change Set D): public operations/health probe.

    Flag-gated (PHASE15_OPS_ENDPOINT; OFF => 404, production-identical). No auth, NO secrets,
    never cached (Cache-Control: no-store). Body is db.scan_health() (always HTTP 200 when enabled,
    even on degraded DB — verdict carries the failure so monitoring is never blinded).
    """
    import os
    if os.environ.get("PHASE15_OPS_ENDPOINT") != "1":
        return jsonify({"error": "not_enabled"}), 404
    data = db.scan_health()
    try:                                   # Change Set A-5: cache-generation observability (additive)
        import cache_layer
        data["cache"] = cache_layer.cache_generation_status()
    except Exception:
        pass
    resp = jsonify(data)
    resp.headers["Cache-Control"] = "no-store"
    return resp


_PHASE15_FE_NOSTORE_PATHS = ("/api/status", "/api/live-prices", "/api/results", "/api/dashboard")


@api_bp.after_request
def _phase15_fe_no_store(resp):
    """Change Set E-1: prevent browser/CDN caching of freshness-critical endpoints so the
    frontend always sees the latest generation (flag-gated; OFF = no header change)."""
    try:
        import os
        from flask import request
        if os.environ.get("PHASE15_FE_SYNC") == "1" and request.path in _PHASE15_FE_NOSTORE_PATHS:
            resp.headers["Cache-Control"] = "no-store"
    except Exception:
        pass
    return resp


@api_bp.route("/api/debug/perf-baseline")
@admin_required
def perf_baseline():
    try:
        import json
        baseline_str = db.get_meta("perf_baseline")
        if baseline_str:
            return jsonify(json.loads(baseline_str))
        return jsonify({"error": "No baseline captured yet"}), 404
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@api_bp.route("/api/debug/yf-guard")
@admin_required
def yf_guard_status_endpoint():
    """Return yfinance circuit breaker state (admin only)."""
    try:
        status = yf_guard_status()
        return jsonify({"ok": True, **status})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@api_bp.route("/api/universe")
def universe_info():
    """Return active scan universe stats and symbol list (read-only)."""
    try:
        import json
        from pathlib import Path
        stats = get_universe_stats()
        # Also return first 100 symbols from cache if available
        active_file = Path(__file__).parent.parent / "cache" / "active_universe.json"
        symbols_preview = []
        if active_file.exists():
            try:
                data = json.loads(active_file.read_text())
                symbols_preview = data.get("symbols", [])[:100]
            except Exception:
                pass
        return jsonify({
            "ok": True,
            **stats,
            "symbols_preview": symbols_preview,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@api_bp.route("/api/stock/history/<symbol>")
def stock_score_history(symbol):
    return jsonify({"symbol": symbol.upper(), "history": db.get_score_history(symbol.upper(), days=30)})


# ═══════════════════════════════════════════════════════════════
# INTELLIGENCE API ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@api_bp.route("/api/macro")
def get_macro():
    """Returns FRED macro data + world market indices + spot prices."""
    try:
        from intelligence import get_world_snapshot, get_macro_snapshot, scan_world_markets
        world = get_world_snapshot()
        macro = get_macro_snapshot()
        if not world or not macro:
            threading.Thread(target=scan_world_markets, daemon=True).start()
        return jsonify({
            "world": world,
            "macro": macro,
            "regime": db.get_meta("market_regime", "unknown"),
        })
    except Exception as exc:
        return jsonify({"error": str(exc), "world": {}, "macro": {}})


@api_bp.route("/api/sector-rotation")
def get_sector_rotation():
    """Returns RRG data for all 12 Nifty sector indices."""
    def _compute():
        try:
            from intelligence.sector_rotation import get_rrg_data, scan_sector_rotation
            sectors = get_rrg_data()
            if not sectors:
                threading.Thread(target=scan_sector_rotation, daemon=True).start()
            return {"sectors": sectors}
        except Exception as exc:
            return {"error": str(exc), "sectors": {}}
    return jsonify(cache_layer.get_or_compute(cache_layer.sector_cache, "sector", _compute))


@api_bp.route("/api/seasonal")
def get_seasonal():
    """Returns current month's active seasons and sector boost map."""
    try:
        from datetime import datetime
        import pytz
        from intelligence.seasonal import INDIA_SEASONS, SECTOR_SEASONAL_BOOST
        IST = pytz.timezone("Asia/Kolkata")
        month = datetime.now(IST).month
        active = INDIA_SEASONS.get(month, [])
        boosted = {k: month in months for k, months in SECTOR_SEASONAL_BOOST.items()}
        return jsonify({
            "month": month,
            "active_seasons": active,
            "sector_boosts": {k: v for k, v in boosted.items() if v},
        })
    except Exception as exc:
        return jsonify({"error": str(exc)})


@api_bp.route("/api/macro-events")
def get_macro_events():
    """Returns Forex Factory calendar events and macro regime."""
    try:
        from intelligence.macro_events import get_ff_events, get_ff_regime, scan_macro_events
        events = get_ff_events()
        regime = get_ff_regime()
        if not events:
            threading.Thread(target=scan_macro_events, daemon=True).start()
        return jsonify({
            "events": events,
            "regime": regime,
        })
    except Exception as exc:
        return jsonify({"error": str(exc), "events": [], "regime": "NEUTRAL"})


@api_bp.route("/api/news/headlines")
def get_headlines():
    """Returns global macro headlines from NewsAPI (quota-guarded)."""
    try:
        from intelligence.news_sentiment import get_global_headlines
        return jsonify({"headlines": get_global_headlines()})
    except Exception as exc:
        return jsonify({"error": str(exc), "headlines": []})


@api_bp.route("/api/debug/macro-state")
def debug_macro_state():
    """Debug: inspect in-process macro and sector rotation state directly."""
    try:
        import intelligence.macro as _macro
        import intelligence.sector_rotation as _rrg
        return jsonify({
            "world_len": len(_macro.world_snapshot),
            "macro_len": len(_macro.macro_snapshot),
            "built_at": _macro._macro_built_at,
            "scan_running": _macro._scan_running,
            "world_keys": list(_macro.world_snapshot.keys())[:5],
            "macro_keys": list(_macro.macro_snapshot.keys())[:5],
            "rrg_sectors": len(_rrg.sector_rotation_cache),
            "rrg_running": _rrg._rrg_running,
            "rrg_built_at": _rrg._rrg_built_at,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)})


@api_bp.route("/api/stocks")
def get_all_scanned_stocks():
    """Return all scanned stocks (slimmed — heavy fields loaded via /api/stock/<symbol>)."""
    # GOAL #1 (live partial results): bind to the display generation so an in-progress
    # scan's partial rows show live; idle returns latest-completed (unchanged). No cache
    # here today, so this read is always fresh — no cache-bypass branch needed.
    _gen = db.get_display_scan_id()
    return jsonify({"stocks": db.load_results(DASHBOARD_MAX_RESULTS, slim=True, scan_id=_gen)})


@api_bp.route("/api/top-candidates")
@admin_required
def get_top_candidates():
    """Return candidates divided into Swing, News-based, Breakouts, Underdogs, and Golden Stocks."""
    results = db.load_results(TOP_N_RESULTS, scan_id=db.get_ui_scan_id())  # follow the active engine
    candidates = [r for r in results if (r.get("volume_ratio", 0.0) > 1.5 or r.get("is_breakout", False) or r.get("gdelt", {}).get("spike", 1.0) > 3.0)]
    
    # 1. Golden Candidates (Top 20)
    golden_c = [r for r in results if r.get("is_golden", False)]
    golden = sorted(golden_c, key=lambda x: x.get("score", 0), reverse=True)[:20]

    # 2. Swing Candidates (Top 20)
    swing = sorted(candidates, key=lambda x: x.get("score", 0), reverse=True)[:20]
    
    # 3. News Candidates (Top 20)
    news_c = [c for c in candidates if (len(c.get("gdelt", {}).get("articles", [])) > 0 or c.get("news_sentiment_score", 15.0) != 15.0)]
    news = sorted(news_c, key=lambda x: x.get("news_sentiment_score", 0.0), reverse=True)[:20]
    
    # 4. Breakout Candidates (Top 20)
    breakout_c = [c for c in candidates if c.get("is_breakout", False)]
    breakout = sorted(breakout_c, key=lambda x: x.get("score", 0), reverse=True)[:20]
    
    # 5. Underdog Candidates (Top 20)
    underdog_c = []
    for c in candidates:
        mcap = c.get("fundamentals", {}).get("market_cap")
        is_small = mcap is None or mcap < 50000000000  # < 5000 Cr
        is_spiked = c.get("gdelt", {}).get("spike", 1.0) > 3.0 or c.get("volume_ratio", 1.0) > 2.0
        if is_small and is_spiked:
            underdog_c.append(c)
    underdog = sorted(underdog_c, key=lambda x: x.get("score", 0), reverse=True)[:20]
    
    return jsonify({
        "golden": golden,
        "swing": swing,
        "news": news,
        "breakout": breakout,
        "underdog": underdog
    })


@api_bp.route("/api/golden")
def get_golden_list():
    """Return top golden stocks."""
    golden = db.load_golden_results(100, scan_id=db.get_ui_scan_id())  # follow the active engine
    return jsonify({"golden": golden})


@api_bp.route("/api/high-conviction")
def get_high_conviction():
    """Return top high conviction stocks."""
    hc = db.load_high_conviction_results(100, scan_id=db.get_ui_scan_id())  # follow the active engine
    return jsonify({"high_conviction": hc})


@api_bp.route("/api/watchlist/details", methods=["POST"])
def get_watchlist_details():
    """Return stock metadata for a list of watchlist symbols."""
    body = request.get_json(silent=True) or {}
    symbols = body.get("symbols", [])
    if not symbols:
        return jsonify({"stocks": {}})
    stocks_map = db.get_stocks_map(symbols)
    return jsonify({"stocks": stocks_map})


@api_bp.route("/api/news")
def get_recent_news():
    """Return recent news articles. Cached 60s."""
    def _compute():
        return db.execute_db("SELECT symbol, title, url, source, raw_score as score, scanned_at FROM news_articles ORDER BY scanned_at DESC LIMIT 100", fetch="all")
    return jsonify({"news": cache_layer.get_or_compute(cache_layer.news_cache, "news", _compute)})


@api_bp.route("/api/sentiment")
def get_sentiments():
    """Return recent news sentiment scores. Cached 60s."""
    def _compute():
        return db.execute_db("SELECT symbol, gdelt_sentiment, gdelt_spike, final_sentiment_score as score, updated_at FROM sentiment_scores ORDER BY updated_at DESC LIMIT 100", fetch="all")
    return jsonify({"sentiments": cache_layer.get_or_compute(cache_layer.news_cache, "sentiment", _compute)})


@api_bp.route("/api/breakouts")
def get_breakouts():
    """Return stocks currently triggering breakout alerts."""
    breakouts = db.load_breakout_results(100)
    return jsonify({"breakouts": breakouts})



@api_bp.route("/api/underdogs")
@admin_required
def get_underdog_list():
    """Return top underdog swing candidate picks."""
    results = db.load_results(TOP_N_RESULTS)
    underdog_c = []
    for c in results:
        mcap = c.get("fundamentals", {}).get("market_cap")
        is_small = mcap is None or mcap < 50000000000
        is_spiked = c.get("gdelt", {}).get("spike", 1.0) > 3.0 or c.get("volume_ratio", 1.0) > 2.0
        if is_small and is_spiked:
            underdog_c.append(c)
    underdog = sorted(underdog_c, key=lambda x: x.get("score", 0), reverse=True)[:20]
    return jsonify({"underdogs": underdog})


@api_bp.route("/api/market-overview")
@admin_required
def get_market_overview():
    """Return macro and global indexes data."""
    try:
        from intelligence import get_world_snapshot, get_macro_snapshot
        from intelligence.macro_events import get_ff_events, get_ff_regime
        return jsonify({
            "world": get_world_snapshot(),
            "macro": get_macro_snapshot(),
            "events": get_ff_events(),
            "regime": get_ff_regime(),
            "nifty50_1m": db.get_meta("nifty50_1m", 0),
            "market_regime": db.get_meta("market_regime", "unknown"),
        })
    except Exception as exc:
        return jsonify({"error": str(exc)})


# ═══════════════════════════════════════════════════════════════
# RELEASE 4 — PAPER TRADE & EXECUTION ENGINE API ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@api_bp.route("/api/admin/model-comparison")
def admin_model_comparison():
    """INTERNAL: legacy vs scoring_v1 PER-TRADE quality (not portfolio return; counts differ)."""
    return jsonify({
        "note": "per-trade quality only — win-rate / avg return_pct / avg alpha_pct / "
                "max_dd / avg days_held by engine. Trade counts differ; NOT portfolio return.",
        "by_model_version": db.get_model_comparison(),
    })


@api_bp.route("/api/admin/ui-engine", methods=["GET", "POST"])
def admin_ui_engine():
    """INTERNAL: read/flip the UI engine toggle (recommendations + paper-trade views).
    Default 'scoring_v1'; set to 'legacy' for side-by-side review."""
    if request.method == "POST":
        eng = (request.get_json(silent=True) or {}).get("engine") or request.form.get("engine")
        if eng in ("scoring_v1", "legacy"):
            db.set_meta("ui_reco_source", eng)
    return jsonify({"ui_reco_source": db.get_meta("ui_reco_source") or "scoring_v1"})


@api_bp.route("/api/paper-trades/stats")
def get_paper_trades_stats():
    """Aggregated paper-trade outcome stats for the Outcome Intelligence page.

    Thin wrapper over db.get_paper_trade_stats() (win-rate, avg return/alpha,
    profit factor, expectancy, conviction + factor attribution, by-model-version,
    by-sector). Returns an empty-but-valid shape (never 500) so the page renders.
    """
    engine = _req_engine()  # optional client override; None -> server default (unchanged)
    try:
        stats = db.get_paper_trade_stats(model_version=engine)
    except Exception as exc:
        logging.getLogger("screener").warning("paper-trade stats failed: %s", exc)
        stats = {"closed_trades": 0, "open_trades": 0, "win_rate": 0}
    return jsonify(sanitize_nan(stats))


@api_bp.route("/api/paper-trades")
def get_paper_trades():
    """Return all paper trades (open + closed) with live P&L, stats, and portfolio totals."""
    try:
        engine = _req_engine()  # optional client override; None -> server default (unchanged)
        limit = request.args.get("limit", 200, type=int)
        trades = db.get_all_paper_trades(limit, model_version=engine)

        # Portfolio-level tracking
        total_invested = 0
        total_current_value = 0
        total_pnl = 0
        total_target_profit = 0
        total_sl_risk = 0

        # WebSocket-only price fetch: subscribe + seed cache for missing symbols + get all prices
        open_symbols = [t.get("symbol", "").upper().replace(".NS", "") for t in trades if t.get("status") == "OPEN"]
        if open_symbols:
            live_feed.subscribe(open_symbols)
            live_feed.seed_cache(open_symbols)  # One-time REST fetch for symbols not in WS cache
        ws_price_map = live_feed.get_live_prices(open_symbols) if open_symbols else {}

        # Inject live P&L + calculated fields for all trades
        for trade in trades:
            entry = trade.get("entry_price", 0) or 0
            qty = trade.get("quantity", 0) or 0
            target = trade.get("target_price", 0) or 0
            sl = trade.get("stop_loss", 0) or 0

            # Always calculate static fields
            trade["invested_amount"] = round(entry * qty, 2) if entry and qty else 0
            trade["target_profit_amount"] = round((target - entry) * qty, 2) if target and entry and qty else 0
            trade["sl_loss_amount"] = round((entry - sl) * qty, 2) if sl and entry and qty else 0
            trade["target_pct"] = round(((target - entry) / entry) * 100, 1) if target and entry else 0
            trade["sl_pct"] = round(((sl - entry) / entry) * 100, 1) if sl and entry else 0

            if trade.get("status") == "OPEN":
                sym = trade.get("symbol", "").upper().replace(".NS", "")
                ltp = 0
                live = ws_price_map.get(sym)
                if live:
                    ltp = live.get("ltp", 0)
                    trade["current_price"] = ltp
                    trade["day_change_pct"] = live.get("change_pct", 0)

                if entry and ltp:
                    trade["live_return_pct"] = round(((ltp - entry) / entry) * 100, 2)
                    trade["live_pnl"] = round((ltp - entry) * qty, 2) if qty else 0
                    trade["current_value"] = round(ltp * qty, 2) if qty else 0
                else:
                    trade["live_return_pct"] = 0
                    trade["live_pnl"] = 0
                    trade["current_value"] = trade["invested_amount"]

                # Accumulate portfolio totals (open trades only)
                total_invested += trade["invested_amount"]
                total_current_value += trade.get("current_value", trade["invested_amount"])
                total_pnl += trade.get("live_pnl", 0)
                total_target_profit += trade.get("target_profit_amount", 0)
                total_sl_risk += trade.get("sl_loss_amount", 0)

            elif trade.get("status") == "CLOSED":
                exit_price = trade.get("exit_price", 0) or 0
                trade["exit_value"] = round(exit_price * qty, 2) if exit_price and qty else 0
                trade["realized_pnl"] = round((exit_price - entry) * qty, 2) if exit_price and entry and qty else 0

        open_count = sum(1 for t in trades if t.get("status") == "OPEN")

        # P0-5: Failure Isolation for Stats
        stats = {"ok": False}
        try:
            def _get_stats():
                try:
                    return {"ok": True, **db.get_paper_trade_stats(model_version=engine)}
                except Exception as exc:
                    return {"error": str(exc), "ok": False}
            # The shared stats cache (key "stats") is engine-agnostic; when the client
            # explicitly requests an engine, bypass it so the stats match the trades shown
            # (no cross-engine blend). When engine is None, keep the cached path unchanged.
            stats = _get_stats() if engine else cache_layer.get_or_compute(cache_layer.stats_cache, "stats", _get_stats)
        except Exception as exc:
            import logging
            logging.getLogger("api").exception("[PAPER TRADES API] Failed to fetch stats inline")

        # P0-7: Market & Scan State
        market_open = live_feed.is_market_open()
        scan_active, _ = db.is_scan_active()

        # Release 4: Execution Engine stats
        engine_stats = {}
        try:
            from execution_engine import get_engine_stats
            engine_stats = get_engine_stats()
        except Exception:
            pass

        return jsonify({
            "trades": trades,
            "total": len(trades),
            "open": open_count,
            "closed": len(trades) - open_count,
            "stats": stats,
            "market_open": market_open,
            "scan_running": scan_active,
            "engine": engine_stats,
            # Portfolio-level totals (open positions only)
            "portfolio": {
                "total_invested": round(total_invested, 2),
                "total_current_value": round(total_current_value, 2),
                "total_pnl": round(total_pnl, 2),
                "total_pnl_pct": round((total_pnl / total_invested) * 100, 2) if total_invested > 0 else 0,
                "total_target_profit": round(total_target_profit, 2),
                "total_sl_risk": round(total_sl_risk, 2),
                "open_positions": open_count,
            },
        })
    except Exception as exc:
        return jsonify({"error": str(exc), "trades": []})


@api_bp.route("/api/paper-trades", methods=["POST"])
def create_manual_paper_trade():
    """Open a MANUAL paper trade from user input.

    Body JSON: {symbol, entry_price, quantity, stop_loss, target_price}.
    Entry date/time default to now. Virtual capital is auto-defaulted; the
    user is not asked for a budget. The symbol is subscribed to the live feed
    so live P&L and SL/target evaluation work just like auto trades.
    """
    try:
        payload = request.get_json(silent=True) or {}

        symbol = str(payload.get("symbol", "")).strip().upper().replace(".NS", "")
        if not symbol:
            return jsonify({"ok": False, "error": "symbol is required"}), 400

        try:
            entry_price = float(payload.get("entry_price") or 0)
        except (TypeError, ValueError):
            entry_price = 0
        if entry_price <= 0:
            return jsonify({"ok": False, "error": "entry_price must be > 0"}), 400

        def _num(key):
            v = payload.get(key)
            try:
                return float(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None

        quantity = payload.get("quantity")
        try:
            quantity = int(quantity) if quantity not in (None, "") else 0
        except (TypeError, ValueError):
            quantity = 0

        stock_data = {
            "symbol": symbol,
            "price": entry_price,
            "quantity": quantity if quantity > 0 else None,
            "stop_loss": _num("stop_loss"),
            "target_price": _num("target_price"),
        }

        nifty_price = None
        try:
            nifty_meta = db.get_meta("nifty50_price")
            if nifty_meta:
                nifty_price = float(nifty_meta)
        except Exception:
            pass
        market_regime = db.get_meta("market_regime", "unknown")

        trade_id = db.create_paper_trade(
            stock_data, nifty_price=nifty_price,
            market_regime=market_regime, source="MANUAL",
        )
        if not trade_id:
            return jsonify({
                "ok": False,
                "error": "Could not create trade (an OPEN position may already exist).",
            }), 409

        # Subscribe so live P&L + SL/target evaluate just like auto trades
        try:
            live_feed.subscribe([symbol])
            live_feed.seed_cache([symbol])
        except Exception:
            pass

        try:
            import cache_layer
            cache_layer.invalidate_stats()
        except Exception:
            pass

        return jsonify({"ok": True, "id": trade_id, "symbol": symbol, "source": "MANUAL"})
    except Exception as exc:
        log.exception("[PAPER TRADES API] manual create failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@api_bp.route("/api/paper-orders")
def get_paper_orders():
    """Return all paper orders with full lifecycle status."""
    try:
        status_filter = request.args.get("status", None)
        limit = request.args.get("limit", 100, type=int)

        if status_filter:
            orders = db.execute_db(
                "SELECT * FROM paper_orders WHERE status = ? ORDER BY order_created_at DESC LIMIT ?",
                (status_filter.upper(), limit), fetch="all"
            ) or []
        else:
            orders = db.execute_db(
                "SELECT * FROM paper_orders ORDER BY order_created_at DESC LIMIT ?",
                (limit,), fetch="all"
            ) or []

        # Inject live prices for PENDING orders
        for order in orders:
            if order.get("status") == "PENDING":
                live = live_feed.get_live_price(order.get("symbol", ""))
                if live:
                    order["current_price"] = live.get("ltp", 0)

        pending_count = sum(1 for o in orders if o.get("status") == "PENDING")
        filled_count = sum(1 for o in orders if o.get("status") == "FILLED")

        return jsonify({
            "orders": orders,
            "total": len(orders),
            "pending": pending_count,
            "filled": filled_count,
        })
    except Exception as exc:
        return jsonify({"error": str(exc), "orders": []})


@api_bp.route("/api/paper-trades/engine-stats")
def get_execution_engine_stats():
    """Return execution engine real-time telemetry."""
    try:
        from execution_engine import get_engine_stats, _pending_orders, _active_positions, _state_lock
        stats = get_engine_stats()
        with _state_lock:
            stats["pending_orders"] = sum(len(v) for v in _pending_orders.values())
            stats["active_positions"] = sum(len(v) for v in _active_positions.values())
            stats["pending_symbols"] = list(_pending_orders.keys())[:20]
            stats["active_symbols"] = list(_active_positions.keys())[:20]
        return jsonify({"ok": True, **stats})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})


_dashboard_loaded = False


import functools, traceback
def catch_err(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            with open('logs/dash_err.txt', 'w') as errf:
                errf.write(traceback.format_exc())
            return {'error': str(e)}, 500
    return wrapper

@api_bp.route("/api/dashboard")
@catch_err
def get_dashboard():

    """Single composite endpoint for the V3 dashboard.

    Returns status + results summary + heatmap + sector + paper stats
    in ONE request instead of 5+. Cached for 10s.
    """
    global _dashboard_loaded
    is_first_load = not _dashboard_loaded
    if is_first_load:
        _dashboard_loaded = True
        log.info("[DASHBOARD FIRST LOAD] First API call to /api/dashboard")

    t_start = time.perf_counter()
    timings = {"was_computed": False}
    engine = _req_engine()  # optional client override; None -> server default (unchanged)

    def _compute():
        timings["was_computed"] = True
        # GOAL #1 (live partial results): bind to the ACTIVE scan when one is running
        # (so the board fills live), else the latest-completed scan. Pinned ONCE per compute
        # so results/count and the last_scan display all use the same generation. Idle =
        # identical to before (get_display_scan_id() == get_latest_completed_scan_id()).
        _gen = db.get_ui_scan_id(engine)  # engine toggle (default scoring_v1); MUST-FIX 2 isolation

        # Status
        t0 = time.perf_counter()
        state = scan_state.status()
        timings["status_ms"] = round((time.perf_counter() - t0) * 1000, 2)

        # Results (pre-sorted by score)
        t0 = time.perf_counter()
        results = db.load_results(DASHBOARD_MAX_RESULTS, slim=True, scan_id=_gen)
        total_analyzed = db.get_result_count(scan_id=_gen)
        timings["load_ms"] = round((time.perf_counter() - t0) * 1000, 2)

        # Universe size (mirror /api/results so the UI can show "Universe N / Scored M")
        try:
            universe_size = get_universe_stats().get("total_symbols", 2200)
        except Exception:
            universe_size = 2200

        # Sector rotation
        t0 = time.perf_counter()
        try:
            from intelligence.sector_rotation import get_rrg_data
            sectors = get_rrg_data() or []
        except Exception:
            sectors = []
        timings["sector_ms"] = round((time.perf_counter() - t0) * 1000, 2)

        # Paper trade stats
        t0 = time.perf_counter()
        try:
            paper_stats = db.get_paper_trade_stats()
        except Exception:
            paper_stats = {}
        timings["stats_ms"] = round((time.perf_counter() - t0) * 1000, 2)

        status = {
            "scanning": state["scanning"],
            "progress": state["progress"],
            "total": state["total"],
            "last_scan": _ui_last_scan_display(),
            "market_regime": db.get_meta("market_regime", "unknown"),
        }

        return {
            "status": status,
            "results": _slim_results(results),
            "total_analyzed": total_analyzed,
            "universe_size": universe_size,
            "sector_rotation": {"sectors": sectors},
            "paper_stats": paper_stats,
        }

    # GOAL #1: while a scan is ACTIVE, bypass the 10s dashboard cache so the board
    # reflects newly-saved batches each poll; when idle, use the cache exactly as before.
    # Also bypass when the client requests a specific engine (the cache key "dashboard" is
    # engine-agnostic, so serving it for an explicit engine would leak the default engine).
    _scan_active, _ = db.is_scan_active()
    if _scan_active or engine:
        data = _compute()
    else:
        data = cache_layer.get_or_compute(cache_layer.dashboard_cache, "dashboard", _compute)

    t0 = time.perf_counter()
    resp = jsonify(data)
    serialize_ms = round((time.perf_counter() - t0) * 1000, 2)
    total_ms = round((time.perf_counter() - t_start) * 1000, 2)

    if timings["was_computed"]:
        log.info("[DASHBOARD CACHE MISS] total=%.1fms | status=%.1fms load=%.1fms sector=%.1fms stats=%.1fms jsonify=%.1fms",
                 total_ms, timings.get("status_ms", 0), timings.get("load_ms", 0),
                 timings.get("sector_ms", 0), timings.get("stats_ms", 0), serialize_ms)
    else:
        log.info("[DASHBOARD CACHE HIT] total=%.1fms | jsonify=%.1fms", total_ms, serialize_ms)

    if is_first_load:
        log.info("[DASHBOARD FIRST LOAD COMPLETE] %.1fms", total_ms)

    return resp


@api_bp.route("/api/paper-trades/equity-curve")
def get_equity_curve():
    """Return equity curve data for charting."""
    try:
        days = request.args.get("days", 90, type=int)
        curve = db.get_equity_curve(days)
        return jsonify({"curve": curve, "days": days})
    except Exception as exc:
        return jsonify({"error": str(exc), "curve": []})


# ─── Phase 0: Trust & Observability Endpoints ───

@api_bp.route("/api/score-history/<symbol>")
def score_history(symbol):
    """Return score audit trail for a symbol.
    
    Answers: Why did score change? What components moved? Which data source?
    """
    try:
        limit = request.args.get("limit", 30, type=int)
        rows = db.execute_db("""
            SELECT scan_id, scan_time,
                   technical_score, earnings_momentum_score,
                   fundamental_score, smart_money_score, sector_rotation_score,
                   news_sentiment_score, news_spike_score, macro_score, catalyst_score,
                   final_score, data_source, source_reason,
                   provider_latency_ms, data_staleness_hours, scan_version
            FROM score_audit WHERE symbol=?
            ORDER BY scan_time DESC LIMIT ?
        """, (symbol.upper(), limit), fetch="all")

        # Compute deltas if we have at least 2 scans
        history = rows or []
        delta = None
        if history and len(history) >= 2:
            latest = history[0]
            prev = history[1]
            component_keys = [
                "technical_score", "earnings_momentum_score", "fundamental_score",
                "smart_money_score", "sector_rotation_score", "news_sentiment_score",
                "news_spike_score", "macro_score", "catalyst_score"
            ]
            delta = {
                "final_score_change": round((latest.get("final_score") or 0) - (prev.get("final_score") or 0), 2),
                "components": {
                    k: round((latest.get(k) or 0) - (prev.get(k) or 0), 2)
                    for k in component_keys
                },
                "source_changed": latest.get("data_source") != prev.get("data_source"),
                "version_changed": latest.get("scan_version") != prev.get("scan_version"),
            }

        return jsonify({
            "symbol": symbol.upper(),
            "history": history,
            "delta": delta,
            "count": len(history),
        })
    except Exception as exc:
        return jsonify({"symbol": symbol.upper(), "history": [], "delta": None, "error": str(exc)})

@api_bp.route("/api/research-history/<symbol>")
def api_research_history(symbol):
    """Retrieve the full timeline of research snapshots for a symbol."""
    try:
        history = db.get_research_history(symbol.upper())
        return jsonify({
            "symbol": symbol.upper(),
            "history": history,
            "count": len(history)
        })
    except Exception as exc:
        return jsonify({"symbol": symbol.upper(), "history": [], "error": str(exc)})

@api_bp.route("/api/research-advisories", methods=["GET", "POST"])
def api_research_advisories():
    """Get active advisories or create a new one."""
    if request.method == "POST":
        data = request.json or {}
        symbol = data.get("symbol")
        adv_type = data.get("advisory_type")
        adv_text = data.get("advisory_text")
        priority = data.get("priority", "MEDIUM")
        
        if not symbol or not adv_type or not adv_text:
            return jsonify({"error": "Missing required fields"}), 400
            
        try:
            adv_id = db.create_research_advisory(symbol.upper(), adv_type, adv_text, priority)
            return jsonify({"success": True, "advisory_id": adv_id})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
    else:
        symbol = request.args.get("symbol")
        try:
            advisories = db.get_research_advisories(symbol=symbol.upper() if symbol else None)
            return jsonify({"advisories": advisories})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500


@api_bp.route("/api/health/scan")
def scan_health():
    """Operational health of the scanner.
    
    Returns last scan time, age, data source, scan version, market regime, and status.
    """
    try:
        from config import SCAN_VERSION
    except ImportError:
        SCAN_VERSION = "unknown"

    last_scan = db.get_meta("last_scan")
    scan_age = None
    if last_scan:
        from datetime import datetime as dt
        try:
            last_dt = dt.strptime(last_scan, "%Y-%m-%d %H:%M IST")
            scan_age = round((dt.now() - last_dt).total_seconds() / 60, 1)
        except Exception:
            pass

    # Get latest scan_audit for extra context
    latest_audit = None
    try:
        latest_audit = db.execute_db(
            "SELECT scan_id, duration_ms, stocks_scanned, stocks_succeeded, stocks_failed, data_source, scan_version "
            "FROM scan_audit ORDER BY start_time DESC LIMIT 1",
            fetch="one"
        )
    except Exception:
        pass

    return jsonify({
        "last_scan": last_scan,
        "scan_age_minutes": scan_age,
        "scan_version": SCAN_VERSION,
        "market_regime": db.get_meta("market_regime", "unknown"),
        "status": "healthy" if scan_age and scan_age < 120 else "stale",
        "latest_audit": latest_audit,
    })
