"""
live_pipeline.py - the daily scoring_v1 LIVE pipeline (additive; legacy untouched).
==================================================================================
Runs the LOCKED engine (tuned) for an as_of date, then:
  1) MIN_ELIGIBLE_UNIVERSE guard — if the gated universe is too thin, score for the
     UI but SKIP auto paper-trades (a sub-72 universe makes the percentile score gate
     meaningless; logged, never silent).
  2) Builds a FULL result dict per ranked symbol — score (percentile 0-100), rank,
     composite_z, the c_* factor contributions, drivers/weaknesses, the two
     confidence tiers, AND real entry/SL/target (levels.py) + display fields
     (pct_1d/1w/1m, ADX, weekly_trend, sector, grade) so the existing top_picks UI
     renders with NO empty columns (MUST-FIX 3).
  3) Persists the full ranked set to scan_results_v2 under a unique execution scan_id
     (scan_v1_YYYYMMDD_HHMMSS) and tags those rows model_version='scoring_v1'
     (the UI toggle reads them via db.get_ui_scan_id; legacy is never touched).
  4) Persists the daily picks to recommendation_snapshots (model_version='scoring_v1'
     + composite_z/drivers/weaknesses/confidence tiers) as the comparison ledger.
  5) Auto paper-trades the top picks via execution_engine.submit_order
     (model_version='scoring_v1'): rank<=TOP_N (engine constant), AFTER an
     entry-quality filter (skip names >8% above the 20-DMA).
  6) Persists per-scan metadata + coverage (versions + gate coverage) and logs one
     INFO summary line.

PG-ONLY: import bootstrap FIRST so analytics hit the real PostgreSQL store.
"""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime

log = logging.getLogger("screener")

try:
    from . import bootstrap  # noqa: F401  (loads .env so db -> PostgreSQL)
    from . import adapter, gates, sector_map, levels
    from . import engine as _engine
except Exception:  # pragma: no cover - standalone fallback
    import bootstrap  # type: ignore  # noqa: F401
    import adapter, gates, sector_map, levels  # type: ignore
    import engine as _engine  # type: ignore

MODEL = "scoring_v1"
WEIGHT_MODE = "tuned"
MIN_ELIGIBLE_UNIVERSE = 150   # below this, score for UI but SKIP auto-submit (thin universe)

# Version/provenance stamped on every scoring_v1 artifact (Addition A).
ENGINE_VERSION = "scoring_v1-engine-1.0"
WEIGHT_VERSION = "tuned-1.0"
SPEC_VERSION = "marketos_scoring_final_spec-locked"
SCHEMA_VERSION = "model_version_v1"


def _git_commit() -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                      cwd=bootstrap._REPO_ROOT, stderr=subprocess.DEVNULL)
        return out.decode().strip()
    except Exception:
        return ""


def _config_hash() -> str:
    import hashlib
    blob = json.dumps({"fw": _engine.FACTOR_WEIGHTS, "sw": _engine.SUBFACTOR_WEIGHTS}, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def _grade_from_percentile(score) -> str:
    """Percentile -> letter grade band (scoring_v1 has no native grade)."""
    s = score or 0
    if s >= 95: return "A+"
    if s >= 85: return "A"
    if s >= 70: return "B"
    if s >= 50: return "C"
    return "D"


def _drivers_list(s):
    """Parse the engine's 'drivers'/'weaknesses' string -> list of factor names."""
    if not s:
        return []
    return [p.strip().split(" ")[0] for p in str(s).split(",") if p.strip()]


def _build_result(symbol, row, lv, disp, sector):
    """Assemble one UI/scan_results_v2 result dict from engine row + levels + display."""
    score = round(float(row.get("score") or 0), 1)
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
        # scoring_v1 attribution / confidence (display only)
        "rank": int(row.get("rank") or 0),
        "composite_z": round(float(row.get("composite_z") or 0), 4),
        "drivers": row.get("drivers", ""),
        "weaknesses": row.get("weaknesses", ""),
        "data_integrity": row.get("data_integrity", ""),
        "signal_agreement": row.get("signal_agreement", ""),
        "factor_contributions": {
            "momentum": round(float(row.get("c_momentum") or 0), 3),
            "trend": round(float(row.get("c_trend") or 0), 3),
            "smart_money": round(float(row.get("c_smart_money") or 0), 3),
            "sector_rs": round(float(row.get("c_sector_rs") or 0), 3),
            "earnings": round(float(row.get("c_earnings") or 0), 3),
            "risk": round(float(row.get("c_risk") or 0), 3),
        },
        # 0-100 per-factor universe percentiles for the Thesis Radar (display only)
        "factor_percentiles": {
            "momentum": round(float(row.get("pctl_momentum") or 0), 1),
            "trend": round(float(row.get("pctl_trend") or 0), 1),
            "smart_money": round(float(row.get("pctl_smart_money") or 0), 1),
            "sector_rs": round(float(row.get("pctl_sector_rs") or 0), 1),
            "earnings": round(float(row.get("pctl_earnings") or 0), 1),
            "risk": round(float(row.get("pctl_risk") or 0), 1),
        },
        # not applicable to scoring_v1 (legacy badges) — keep UI happy
        "high_conviction": False,
        "is_golden": False,
        "is_breakout": False,
    }
    if lv:
        res.update({
            "price": lv["price"], "entry_low": lv["entry_low"], "entry_high": lv["entry_high"],
            "stop_loss": lv["stop_loss"], "target1": lv["target1"], "target2": lv["target2"],
            "target3": lv["target3"], "target_price": lv["target_price"],
            "risk_reward": lv["risk_reward"], "atr_pct": lv["atr_pct"],
        })
    if disp:
        res.update(disp)
    # mirror contributions into the legacy *_score columns so existing UI bars render
    fc = res["factor_contributions"]
    res.setdefault("technical_score", fc["trend"])
    res.setdefault("smart_money_score", fc["smart_money"])
    res.setdefault("earnings_momentum_score", fc["earnings"])
    res.setdefault("sector_rotation_score", fc["sector_rs"])
    res.setdefault("risk_score", fc["risk"])
    return res


def run_daily(as_of_date=None, submit_trades: bool = True, symbols=None,
              neutralize_earnings: bool = False) -> dict:
    """Run the scoring_v1 live pipeline for `as_of_date` (default = latest store date).

    symbols: optional candidate list (default = full PIT universe).
    neutralize_earnings: skip the network-bound earnings fetch (earnings neutral for
        all symbols, identically) — for fast full-universe runs. EOD prod = False.
    """
    import db
    bootstrap.require_pg()

    if as_of_date is None:
        row = db.execute_db("SELECT MAX(date) AS d FROM daily_bars", fetch="one", require_pg=True)
        as_of_date = str(row["d"])[:10] if row and row.get("d") else None
    if not as_of_date:
        log.warning("[scoring_v1] daily_bars empty — aborting run_daily")
        return {"ok": False, "reason": "no_data"}

    # ── 1) gate the universe ONCE, then build engine inputs (no double gating) ──
    candidates = list(symbols) if symbols else adapter.pit_loader.list_symbols_with_history(as_of_date)
    eligible, rejected = gates.apply_universe_gates(candidates, as_of_date)
    n_elig = len(eligible)
    # STAGE-2 zero-external-fetch: when the shared earnings_store is populated (Stage-1
    # ran), read earnings ONLY from it — no Dhan/NSE/screener network during the scan.
    # Guarded by store-non-empty so v1 never degrades if Stage-1 hasn't run yet.
    import os as _os
    if _os.getenv("SCORING_V1_STAGE2_STORE_ONLY", "1") == "1":
        try:
            from . import data_store, dhan_forecast, earnings_adapter
            if data_store.earnings_coverage().get("rows", 0) > 0:
                dhan_forecast.STORE_ONLY = True
                earnings_adapter.STORE_ONLY = True
        except Exception:
            pass
    price_data, benchmark, sector_idx, earnings = adapter.build_engine_inputs(
        as_of_date, symbols=eligible, pre_gated=True, fetch_earnings=not neutralize_earnings)
    ranked = _engine.score_universe(price_data, benchmark=benchmark, sector_idx=sector_idx,
                                    earnings=earnings, mode=WEIGHT_MODE)
    if ranked is None or len(ranked) == 0:
        log.warning("[scoring_v1] 0 scored symbols for %s — aborting", as_of_date)
        return {"ok": False, "reason": "no_scores", "as_of": as_of_date}

    scored_n = len(ranked)
    # Per-factor cross-sectional percentiles (0-100) for the Thesis Radar. Display-only and
    # ADDITIVE — does NOT touch scoring/weights/rank. Ranking c_<factor> equals ranking the
    # factor_z (weights are positive constants), so each is the factor's universe percentile.
    for _f in ("momentum", "trend", "smart_money", "sector_rs", "earnings", "risk"):
        _col = f"c_{_f}"
        if _col in ranked.columns:
            ranked[f"pctl_{_f}"] = (ranked[_col].rank(pct=True) * 100).round(1)
    # coverage (Addition B) — from what the gates + adapter already computed
    sector_cov = round(100 * len(sector_idx) / max(1, scored_n), 1)
    earn_cov = round(100 * sum(1 for e in earnings.values()
                               if e and any(v is not None for v in e.values())) / max(1, scored_n), 1)
    from collections import Counter
    rej_reasons = Counter(r.split(":")[0] + ":" + r.split(":")[1].split("=")[0]
                          if ":" in r else r for r in rejected.values())

    # ── 2) build full result dicts (MUST-FIX 3 full fields) ──
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    scan_id = f"scan_v1_{stamp}"
    results = []
    for symbol, row in ranked.iterrows():
        df = price_data.get(symbol)
        lv = levels.compute_levels(df) if df is not None else None
        disp = levels.compute_display_fields(df) if df is not None else {}
        sector = (adapter.stocks_sector(symbol) if hasattr(adapter, "stocks_sector") else None)
        if sector is None:
            try:
                import stocks
                sector = stocks.SECTORS.get(symbol)
            except Exception:
                sector = None
        results.append(_build_result(symbol, row, lv, disp, sector))

    # ── 3) persist to scan_results_v2 under the execution scan_id, then TAG ──
    try:
        db.save_results(results, scan_id=scan_id, meta={"engine": MODEL, "as_of": as_of_date})
        db.execute_db("UPDATE scan_results_v2 SET model_version = 'scoring_v1' WHERE scan_id = ?",
                      (scan_id,), require_pg=True)
    except Exception as exc:
        log.error("[scoring_v1] save_results failed: %s", exc)

    # ── 4) recommendation_snapshots ledger (model_version='scoring_v1' + analytics) ──
    _save_snapshot(db, as_of_date, results)

    # ── 5) auto paper-trade top picks (rank<=TOP_N) after entry-quality filter ──
    submitted, skipped_ext, skipped_thin = 0, 0, 0
    if not submit_trades:
        pass
    elif n_elig < MIN_ELIGIBLE_UNIVERSE:
        skipped_thin = 1
        log.warning("[scoring_v1] eligible universe %d < MIN_ELIGIBLE_UNIVERSE %d — "
                    "scored for UI but SKIPPING auto paper-trades for %s",
                    n_elig, MIN_ELIGIBLE_UNIVERSE, as_of_date)
    else:
        from execution_engine import submit_order
        top = [r for r in results if 0 < r.get("rank", 1e9) <= _engine.TOP_N]
        for r in top:
            df = price_data.get(r["symbol"])
            if df is not None and levels.is_overextended(df):
                skipped_ext += 1
                log.info("[scoring_v1] skip %s — overextended (>8%% above 20-DMA)", r["symbol"])
                continue
            if not r.get("target_price") or not r.get("stop_loss"):
                continue
            if submit_order(dict(r), {"scan_id": scan_id, "source": "scoring_v1"}):
                submitted += 1

    # ── 6) metadata + coverage + one INFO line ──
    meta = {
        "scan_id": scan_id, "as_of": as_of_date, "model_version": MODEL,
        "engine_version": ENGINE_VERSION, "weight_version": WEIGHT_VERSION,
        "spec_version": SPEC_VERSION, "schema_version": SCHEMA_VERSION,
        "engine_git_commit": _git_commit(), "config_hash": _config_hash(),
        "universe": len(candidates), "eligible": n_elig, "scored": scored_n,
        "sector_coverage_pct": sector_cov, "earnings_coverage_pct": earn_cov,
        "benchmark_present": benchmark is not None,
        "rejected_by_reason": dict(rej_reasons),
        "top_score": round(float(ranked["score"].max()), 1),
        "median_score": round(float(ranked["score"].median()), 1),
        "submitted": submitted, "skipped_overextended": skipped_ext,
        "skipped_thin_universe": bool(skipped_thin),
    }
    try:
        db.set_meta(f"scoring_v1_scan_meta:{scan_id}", json.dumps(meta))
        db.set_meta("scoring_v1_last_scan_id", scan_id)
    except Exception:
        pass
    log.info("[scoring_v1] %s | Universe %d / Eligible %d / Scored %d | "
             "SectorCov %.0f%% / EarnCov %.0f%% | Top %.0f / Median %.0f | "
             "submitted %d (skip ext %d, thin %s)",
             as_of_date, len(candidates), n_elig, scored_n, sector_cov, earn_cov,
             meta["top_score"], meta["median_score"], submitted, skipped_ext, bool(skipped_thin))
    meta["ok"] = True
    return meta


def _save_snapshot(db, as_of_date, results):
    """Persist scoring_v1 daily picks to recommendation_snapshots (comparison ledger)."""
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
            """, (
                as_of_date, r["symbol"], r.get("rank", 0), r.get("score", 0), r.get("grade", ""),
                r.get("technical_score", 0), 0, r.get("earnings_momentum_score", 0), "",
                r.get("smart_money_score", 0), r.get("risk_score", 0), r.get("price", 0),
                "scoring_v1", "",
                r.get("composite_z", 0), r.get("drivers", ""), r.get("weaknesses", ""),
                r.get("data_integrity", ""), r.get("signal_agreement", ""),
            ), require_pg=True)
        except Exception as exc:
            log.debug("[scoring_v1] snapshot save failed for %s: %s", r.get("symbol"), exc)


if __name__ == "__main__":  # pragma: no cover - manual run
    import sys
    bootstrap.require_pg()
    asof = sys.argv[1] if len(sys.argv) > 1 else None
    nosubmit = "--no-submit" in sys.argv
    out = run_daily(asof, submit_trades=not nosubmit)
    print(json.dumps(out, indent=2, default=str))
