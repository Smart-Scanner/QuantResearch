"""
live_pipeline.py - the daily legacy_cleaned LIVE pipeline (ADDITIVE; v1 + legacy untouched).
============================================================================================
This is the legacy_cleaned analogue of quantresearch/scoring_v1/live_pipeline.py. It MIRRORS
v1's pipeline + persistence pattern (scan_id generation, scan_results_v2 write path,
recommendation_snapshots ledger, per-factor percentiles in the result, auto paper-trade
submission via execution_engine, observability metadata + one INFO summary line), but wires
the legacy_cleaned foundation (adapter/engine/levels) and tags every artifact
model_version='legacy_cleaned'.

Key differences from v1 (by design — see legacy_cleaned/config.py):
  * factor set is technical / smart_money / sector_rs / earnings / fundamental (v1's
    momentum+trend are merged, a Fundamental quality factor is added, risk is dropped);
  * adapter.build_engine_inputs(as_of) returns a FIFTH value, `fundamentals`;
  * per-stock RENORMALIZED factor weights (renorm_weights) are surfaced on each row;
  * levels.compute_levels(df) returns a structure-based SL + resistance targets + a VARIED
    R:R and an `over_extended` flag; over-extended names are dropped from the buy list
    (still persisted for the UI).

ZERO-NETWORK RESEARCH GUARANTEE
-------------------------------
run_daily flips the Stage-2 STORE_ONLY flags (dhan_forecast.STORE_ONLY /
earnings_adapter.STORE_ONLY = True) so the scan reads ONLY the stored layer
(daily_bars / earnings_store / index_bars / universe_catalog) with no Dhan/NSE/screener
network. The flags are ALWAYS restored in a finally block.

PG-ONLY: import the shared bootstrap FIRST so analytics hit the real PostgreSQL store.
Imports are defensive (relative-first, absolute fallback) because the sibling
legacy_cleaned modules (adapter/engine/levels) are being built in parallel.
"""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime

log = logging.getLogger("screener")

# ── bootstrap FIRST (loads .env so db -> PostgreSQL). legacy_cleaned has no own
#    bootstrap module; the shared scoring_v1 bootstrap only loads env + require_pg,
#    which is engine-agnostic, so reuse it read-only (does NOT touch v1 data). ──
try:  # pragma: no cover - import plumbing
    from quantresearch.scoring_v1 import bootstrap  # noqa: F401
except Exception:  # pragma: no cover - fallback path
    try:
        from ..scoring_v1 import bootstrap  # type: ignore  # noqa: F401
    except Exception:
        bootstrap = None  # type: ignore

from . import config as _cfg

MODEL = _cfg.MODEL                       # "legacy_cleaned"
WEIGHT_MODE = "tuned"
MIN_ELIGIBLE_UNIVERSE = 150              # below this, score for UI but SKIP auto-submit (thin universe)

# Version/provenance stamped on every legacy_cleaned artifact.
ENGINE_VERSION = _cfg.ENGINE_VERSION     # "legacy_cleaned-engine-1.0"
WEIGHT_VERSION = _cfg.WEIGHT_VERSION      # "lc-tuned-1.0"
SPEC_VERSION = _cfg.SPEC_VERSION          # "legacy_cleaned-spec-1.0"
SCHEMA_VERSION = "model_version_v1"

# legacy_cleaned composite factors (config.FACTOR_WEIGHTS keys) — used to project
# c_* contributions and pctl_* percentiles into each result dict.
FACTORS = tuple(_cfg.FACTOR_WEIGHTS.keys())  # technical, smart_money, sector_rs, earnings, fundamental


def _git_commit() -> str:
    try:
        cwd = getattr(bootstrap, "_REPO_ROOT", None)
        out = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                      cwd=str(cwd) if cwd else None,
                                      stderr=subprocess.DEVNULL)
        return out.decode().strip()
    except Exception:
        return ""


def _config_hash() -> str:
    import hashlib
    blob = json.dumps({"fw": _cfg.FACTOR_WEIGHTS, "sw": _cfg.SUBFACTOR_WEIGHTS}, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def _grade_from_percentile(score) -> str:
    """Percentile -> letter grade band (legacy_cleaned has no native grade)."""
    s = score or 0
    if s >= 95: return "A+"
    if s >= 85: return "A"
    if s >= 70: return "B"
    if s >= 50: return "C"
    return "D"


def _f(row, key, default=0.0):
    """Row accessor tolerant of both pandas Series and dict rows."""
    try:
        v = row.get(key) if hasattr(row, "get") else row[key]
    except Exception:
        v = None
    return v if v is not None else default


def _build_result(symbol, row, lv, disp, sector):
    """Assemble one UI/scan_results_v2 result dict from an engine row + levels + display.

    Mirrors v1's result shape (score, composite_z, data_integrity, signal_agreement,
    c_* contributions, factor_percentiles/pctl_*, rank, drivers, weaknesses) PLUS the
    legacy_cleaned additions: renorm_weights, and the levels (entry band, structure SL,
    resistance targets, varied R:R, over_extended) and sector/price.
    """
    score = round(float(_f(row, "score", 0)), 1)

    # per-factor contributions (c_<factor>) and universe percentiles (pctl_<factor>)
    contributions = {fct: round(float(_f(row, f"c_{fct}", 0)), 3) for fct in FACTORS}
    percentiles = {fct: round(float(_f(row, f"pctl_{fct}", 0)), 1) for fct in FACTORS}

    # per-stock renormalized factor weights (legacy_cleaned drops+rescales missing factors).
    rw = _f(row, "renorm_weights", None)
    if rw is None:
        rw = {}
    elif isinstance(rw, str):          # engine emits renorm_weights as a JSON string
        try:
            rw = json.loads(rw)
        except Exception:
            rw = {}
    elif not isinstance(rw, dict):
        try:
            rw = dict(rw)
        except Exception:
            rw = {}

    res = {
        "symbol": symbol,
        "name": symbol,
        "sector": sector or "",
        "score": score,                                   # cross-sectional PERCENTILE 0-100
        "grade": _grade_from_percentile(score),
        "model_version": MODEL,
        "engine_version": ENGINE_VERSION,
        "weight_version": WEIGHT_VERSION,
        "spec_version": SPEC_VERSION,
        # legacy_cleaned attribution / confidence (display only)
        "rank": int(_f(row, "rank", 0)),
        "composite_z": round(float(_f(row, "composite_z", 0)), 4),
        "drivers": (_f(row, "drivers", "") or ""),
        "weaknesses": (_f(row, "weaknesses", "") or ""),
        "data_integrity": (_f(row, "data_integrity", "") or ""),
        "signal_agreement": (_f(row, "signal_agreement", "") or ""),
        "factor_contributions": contributions,
        # 0-100 per-factor universe percentiles for the Thesis Radar (display only)
        "factor_percentiles": percentiles,
        # per-stock renormalized factor weights (legacy_cleaned-specific provenance)
        "renorm_weights": rw,
        # not applicable to legacy_cleaned (legacy badges) — keep UI happy
        "high_conviction": False,
        "is_golden": False,
        "is_breakout": False,
    }
    # flat pctl_* aliases (mirror v1's per-factor percentile availability)
    for fct in FACTORS:
        res[f"pctl_{fct}"] = percentiles[fct]

    if lv:
        # levels dict from legacy_cleaned.levels.compute_levels: entry band, structure SL,
        # resistance targets, varied R:R, over_extended. Access tolerantly.
        def _lv(k, d=None):
            v = lv.get(k) if hasattr(lv, "get") else None
            return v if v is not None else d
        price = _lv("price", disp.get("price") if disp else None)
        t1 = _lv("target1")
        res.update({
            "price": price,
            "entry_low": _lv("entry_low"),
            "entry_high": _lv("entry_high"),
            "stop_loss": _lv("stop_loss"),
            "target1": t1,
            "target2": _lv("target2"),
            "target3": _lv("target3"),
            "target_price": _lv("target_price", t1),
            "risk_reward": _lv("risk_reward"),
            "over_extended": bool(_lv("over_extended", False)),
            "atr_pct": _lv("atr_pct"),
        })
    else:
        res["over_extended"] = False

    if disp:
        res.update(disp)

    # mirror contributions into the legacy *_score columns so the existing UI bars render.
    # legacy_cleaned merges momentum+trend into `technical`; map its factors onto the
    # display columns the top_picks template already knows.
    fc = res["factor_contributions"]
    res.setdefault("technical_score", fc.get("technical", 0))
    res.setdefault("smart_money_score", fc.get("smart_money", 0))
    res.setdefault("earnings_momentum_score", fc.get("earnings", 0))
    res.setdefault("sector_rotation_score", fc.get("sector_rs", 0))
    res.setdefault("fundamental_score", fc.get("fundamental", 0))
    return res


def _score_universe(price_data, benchmark, sector_idx, earnings, fundamentals):
    """Call legacy_cleaned.engine.score_universe defensively.

    The sibling engine is built in parallel; try the fundamentals-aware signature
    first, fall back to the v1-style signature if `fundamentals` is not yet accepted.
    """
    from . import engine as _engine  # imported here so a parallel build failure is localized
    try:
        return _engine.score_universe(
            price_data, benchmark=benchmark, sector_idx=sector_idx,
            earnings=earnings, fundamentals=fundamentals, mode=WEIGHT_MODE)
    except TypeError:
        # engine not yet fundamentals-aware -> mirror v1's call shape
        return _engine.score_universe(
            price_data, benchmark=benchmark, sector_idx=sector_idx,
            earnings=earnings, mode=WEIGHT_MODE)


def run_daily(as_of=None, submit_trades: bool = True, symbols=None) -> dict:
    """Run the legacy_cleaned live pipeline for `as_of` (default = latest store date).

    1) Flip Stage-2 STORE_ONLY flags (zero external network) — restore in finally.
    2) Build inputs via legacy_cleaned.adapter.build_engine_inputs(as_of) ->
       (price_data, benchmark, sector_idx, earnings, fundamentals); score via
       legacy_cleaned.engine.score_universe(...).
    3) Per scored row compute levels via legacy_cleaned.levels.compute_levels(df);
       SKIP over-extended names from the buy list (still persist the row).
    4) Persist to scan_results_v2 (model_version='legacy_cleaned',
       scan_id='scan_lc_YYYYMMDD_HHMMSS') via db.save_results, then TAG the rows;
       persist recommendation_snapshots (model_version='legacy_cleaned').
    5) If submit_trades: auto paper-trade the top-N non-over-extended picks via
       execution_engine.submit_order, tagged model_version='legacy_cleaned'.
    6) Log ONE INFO summary line + return the stats dict.
    """
    import db
    if bootstrap is not None:
        bootstrap.require_pg()

    # defensive foundation imports (siblings built in parallel)
    from . import adapter, levels

    if as_of is None:
        row = db.execute_db("SELECT MAX(date) AS d FROM daily_bars", fetch="one", require_pg=True)
        as_of = str(row["d"])[:10] if row and row.get("d") else None
    if not as_of:
        log.warning("[legacy_cleaned] daily_bars empty — aborting run_daily")
        return {"ok": False, "reason": "no_data"}

    # ── 1) STORE_ONLY zero-network enablement (restore in finally) ──────────────
    _dhan = _earn = None
    _prev_dhan = _prev_earn = None
    try:
        from . import dhan_forecast as _dhan  # type: ignore
    except Exception:
        try:
            from ..scoring_v1 import dhan_forecast as _dhan  # type: ignore
        except Exception:
            _dhan = None
    try:
        from . import earnings_adapter as _earn  # type: ignore
    except Exception:
        try:
            from ..scoring_v1 import earnings_adapter as _earn  # type: ignore
        except Exception:
            _earn = None

    try:
        if _dhan is not None:
            _prev_dhan = getattr(_dhan, "STORE_ONLY", None)
            _dhan.STORE_ONLY = True
        if _earn is not None:
            _prev_earn = getattr(_earn, "STORE_ONLY", None)
            _earn.STORE_ONLY = True

        # ── 2) build engine inputs (PIT, store-only) + score ────────────────────
        price_data, benchmark, sector_idx, earnings, fundamentals = \
            adapter.build_engine_inputs(as_of) if symbols is None else \
            adapter.build_engine_inputs(as_of, symbols=symbols)
        ranked = _score_universe(price_data, benchmark, sector_idx, earnings, fundamentals)
    finally:
        # restore flags no matter what
        if _dhan is not None:
            _dhan.STORE_ONLY = _prev_dhan if _prev_dhan is not None else False
        if _earn is not None:
            _earn.STORE_ONLY = _prev_earn if _prev_earn is not None else False

    if ranked is None or len(ranked) == 0:
        log.warning("[legacy_cleaned] 0 scored symbols for %s — aborting", as_of)
        return {"ok": False, "reason": "no_scores", "as_of": as_of}

    scored_n = len(ranked)

    # Per-factor cross-sectional percentiles (0-100) for the Thesis Radar. Display-only,
    # ADDITIVE — does NOT touch scoring/weights/rank. Compute here if the engine did not
    # already surface pctl_<factor> columns.
    for fct in FACTORS:
        pcol = f"pctl_{fct}"
        ccol = f"c_{fct}"
        if pcol not in ranked.columns and ccol in ranked.columns:
            try:
                ranked[pcol] = (ranked[ccol].rank(pct=True) * 100).round(1)
            except Exception:
                pass

    # coverage — sector / earnings / fundamentals presence over the scored set
    sector_cov = round(100 * len(sector_idx or {}) / max(1, scored_n), 1)
    earn_cov = round(100 * sum(1 for e in (earnings or {}).values()
                               if e and any(v is not None for v in e.values()))
                     / max(1, scored_n), 1)
    fund_cov = round(100 * sum(1 for fd in (fundamentals or {}).values()
                               if fd and any(v is not None for v in
                                             (fd.values() if hasattr(fd, "values") else [])))
                     / max(1, scored_n), 1)

    # ── 3) build full result dicts (levels + display + over_extended) ───────────
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    scan_id = f"scan_lc_{stamp}"
    results = []
    n_overext = 0
    for symbol, row in ranked.iterrows():
        df = price_data.get(symbol)
        lv = levels.compute_levels(df) if df is not None else None
        try:
            disp = levels.compute_display_fields(df) if (df is not None and
                        hasattr(levels, "compute_display_fields")) else {}
        except Exception:
            disp = {}
        sector = None
        try:
            if hasattr(adapter, "stocks_sector"):
                sector = adapter.stocks_sector(symbol)
        except Exception:
            sector = None
        if sector is None:
            try:
                import stocks
                sector = stocks.SECTORS.get(symbol)
            except Exception:
                sector = None
        res = _build_result(symbol, row, lv, disp, sector)
        # levels don't emit 'price' -> ensure a valid entry price (last close) so the
        # auto paper-trade submit path (execution_engine: price<=0 reject) works.
        if not res.get("price"):
            try:
                res["price"] = float(df["close"].iloc[-1]) if (df is not None and len(df)) else res.get("entry_low")
            except Exception:
                res["price"] = res.get("entry_low")
        if res.get("over_extended"):
            n_overext += 1
        results.append(res)

    # ── 4) persist to scan_results_v2 via db.save_results, then TAG as legacy_cleaned ──
    try:
        db.save_results(results, scan_id=scan_id, meta={"engine": MODEL, "as_of": as_of})
        db.execute_db("UPDATE scan_results_v2 SET model_version = 'legacy_cleaned' WHERE scan_id = ?",
                      (scan_id,), require_pg=True)
    except Exception as exc:
        log.error("[legacy_cleaned] save_results failed: %s", exc)

    # recommendation_snapshots ledger (model_version='legacy_cleaned')
    _save_snapshot(db, as_of, results)

    # ── 5) auto paper-trade top picks (rank<=TOP_N, non-over-extended) ──────────
    submitted, skipped_ext, skipped_thin = 0, 0, 0
    n_elig = scored_n  # legacy_cleaned adapter gates internally; scored == eligible-with-history
    if not submit_trades:
        pass
    elif n_elig < MIN_ELIGIBLE_UNIVERSE:
        skipped_thin = 1
        log.warning("[legacy_cleaned] eligible universe %d < MIN_ELIGIBLE_UNIVERSE %d — "
                    "scored for UI but SKIPPING auto paper-trades for %s",
                    n_elig, MIN_ELIGIBLE_UNIVERSE, as_of)
    else:
        import execution_engine
        # mirror v1: ensure the engine is marked running so submit_order accepts orders
        if hasattr(execution_engine, "_engine_running") and not execution_engine._engine_running:
            execution_engine._engine_running = True
        from execution_engine import submit_order
        top_n = getattr(_cfg, "TOP_N", 25)
        top = [r for r in results if 0 < r.get("rank", 1e9) <= top_n]
        for r in top:
            if r.get("over_extended"):
                skipped_ext += 1
                log.info("[legacy_cleaned] skip %s — over-extended", r["symbol"])
                continue
            if not r.get("target_price") or not r.get("stop_loss"):
                continue
            stock_data = dict(r)
            stock_data["model_version"] = MODEL  # tag legacy_cleaned for per-engine dedup
            if submit_order(stock_data, {"scan_id": scan_id, "source": MODEL}):
                submitted += 1

    # ── 6) metadata + coverage + one INFO summary line ──────────────────────────
    try:
        top_score = round(float(ranked["score"].max()), 1)
        median_score = round(float(ranked["score"].median()), 1)
    except Exception:
        top_score = median_score = 0.0

    meta = {
        "scan_id": scan_id, "as_of": as_of, "model_version": MODEL,
        "engine_version": ENGINE_VERSION, "weight_version": WEIGHT_VERSION,
        "spec_version": SPEC_VERSION, "schema_version": SCHEMA_VERSION,
        "git_commit": _git_commit(), "config_hash": _config_hash(),
        "eligible": n_elig, "scored": scored_n, "over_extended": n_overext,
        "sector_coverage_pct": sector_cov, "earnings_coverage_pct": earn_cov,
        "fundamentals_coverage_pct": fund_cov,
        "benchmark_present": benchmark is not None,
        "top_score": top_score, "median_score": median_score,
        "submitted": submitted, "skipped_overextended": skipped_ext,
        "skipped_thin_universe": bool(skipped_thin),
    }
    try:
        db.set_meta(f"legacy_cleaned_scan_meta:{scan_id}", json.dumps(meta, default=str))
        db.set_meta("legacy_cleaned_last_scan_id", scan_id)
    except Exception:
        pass

    log.info("[legacy_cleaned] %s | model=%s engine=%s weights=%s spec=%s schema=%s "
             "git=%s cfg=%s | Eligible %d / Scored %d (over-ext %d) | "
             "SectorCov %.0f%% / EarnCov %.0f%% / FundCov %.0f%% | Top %.0f / Median %.0f | "
             "submitted %d (skip ext %d, thin %s)",
             as_of, MODEL, ENGINE_VERSION, WEIGHT_VERSION, SPEC_VERSION, SCHEMA_VERSION,
             meta["git_commit"] or "-", meta["config_hash"], n_elig, scored_n, n_overext,
             sector_cov, earn_cov, fund_cov, top_score, median_score,
             submitted, skipped_ext, bool(skipped_thin))
    meta["ok"] = True
    return meta


def _save_snapshot(db, as_of, results):
    """Persist legacy_cleaned daily picks to recommendation_snapshots (comparison ledger)."""
    top = sorted(results, key=lambda r: r.get("rank", 1e9))[:50]
    for r in top:
        try:
            db.execute_db("""
                INSERT INTO recommendation_snapshots (
                    snapshot_date, symbol, rank, score, grade,
                    technical_score, fundamental_score, earnings_momentum_score, earnings_grade,
                    smart_money_score, risk_score, price, model_version, market_regime,
                    composite_z, drivers, weaknesses, data_integrity, signal_agreement
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(snapshot_date, symbol) DO UPDATE SET
                    rank=excluded.rank, score=excluded.score, model_version=excluded.model_version,
                    composite_z=excluded.composite_z, drivers=excluded.drivers,
                    weaknesses=excluded.weaknesses, data_integrity=excluded.data_integrity,
                    signal_agreement=excluded.signal_agreement
                WHERE recommendation_snapshots.model_version IN ('', 'legacy_cleaned')
            """, (
                as_of, r["symbol"], r.get("rank", 0), r.get("score", 0), r.get("grade", ""),
                r.get("technical_score", 0), r.get("fundamental_score", 0),
                r.get("earnings_momentum_score", 0), "",
                r.get("smart_money_score", 0), r.get("risk_score", 0), r.get("price", 0),
                MODEL, "",
                r.get("composite_z", 0), r.get("drivers", ""), r.get("weaknesses", ""),
                r.get("data_integrity", ""), r.get("signal_agreement", ""),
            ), require_pg=True)
        except Exception as exc:
            log.debug("[legacy_cleaned] snapshot save failed for %s: %s", r.get("symbol"), exc)


if __name__ == "__main__":  # pragma: no cover - manual run
    import sys
    if bootstrap is not None:
        bootstrap.require_pg()
    asof = sys.argv[1] if len(sys.argv) > 1 else None
    nosubmit = "--no-submit" in sys.argv
    out = run_daily(asof, submit_trades=not nosubmit)
    print(json.dumps(out, indent=2, default=str))
