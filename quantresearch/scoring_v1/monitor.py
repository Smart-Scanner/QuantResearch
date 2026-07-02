"""
monitor.py - Factor-correlation MONITORING for scoring_v1 (NOT part of the score).
================================================================================
ADDITIVE / READ-ONLY monitoring layer. This module wires the LOCKED engine's
`factor_correlation_monitor` (quantresearch/scoring_v1/engine.py) into a runnable
monthly-cadence health check. It NEVER feeds back into scoring, ranking, sizing,
gating, confidence, or any decision path - it only OBSERVES the realized factor_z
behaviour across a set of as-of dates and warns when two factors become too
collinear (|rho| > engine.CORR_WARN, i.e. 0.85), which would mean the composite
is effectively double-counting a single underlying signal.

Spec section 9 (Monitoring) mandate (verbatim engine contract):
    engine.factor_correlation_monitor(factor_z_history) -> (corr_matrix, warnings)
    where factor_z_history is a DataFrame [dates x 6 factor_z columns] and
    warnings is the list of (factorA, factorB, rho) pairs with |rho| > 0.85.

HOW factor_z PER DATE IS RECOVERED
----------------------------------
adapter.run_scoring(as_of, mode) returns a ranked, per-symbol DataFrame whose
attribution columns are the factor CONTRIBUTIONS:
        c_<factor> = factor_z[<factor>] * factor_weight[<factor>]
(engine.score_universe: `contrib = {f: factor_z[f] * fw[f] ...}`). We re-derive
the per-symbol factor_z exactly by inverting that:
        factor_z[<factor>] = c_<factor> / factor_weight[<factor>]
using the SAME weights the engine used (engine._weights(mode) -> fw). For the
correlation monitor we then need ONE factor_z observation per (date, factor):
we take the cross-sectional MEAN of factor_z across the scored universe on each
date. (The engine's own contributions are already cross-sectional z-scores; the
date-over-date series of their universe means is the natural input for a
factor-vs-factor correlation over time, which is what section 9 asks for.)

NOTE on earnings: c_earnings already carries the per-symbol earnings DECAY
(engine multiplies factor_z["earnings"] by the freshness decay BEFORE building
contrib). Inverting c_earnings therefore recovers the post-decay factor_z, which
is the factor as it ACTUALLY entered the composite - the correct thing to
monitor. This is documented as a (benign) recovery detail, not a deviation.

POINT-IN-TIME / PG SAFETY
-------------------------
This module performs NO scoring of its own; it calls adapter.run_scoring, which
inherits the foundation layer's strict `date <= as_of` point-in-time guarantee.
The CLI calls bootstrap.require_pg() FIRST so the monitor can only ever run
against the real PostgreSQL point-in-time store (never the empty SQLite
fallback).

USAGE
-----
    # programmatic
    from quantresearch.scoring_v1 import bootstrap
    bootstrap.require_pg()
    from quantresearch.scoring_v1 import monitor
    corr, warnings, hist = monitor.run_correlation_monitor(
        ["2025-12-31", "2026-01-30", "2026-02-27"], mode="tuned")

    # CLI - explicit date list
    python quantresearch/scoring_v1/monitor.py --dates 2025-12-31,2026-01-30,2026-02-27

    # CLI - monthly range (start,end,step-in-days); monthly cadence = step 21
    python quantresearch/scoring_v1/monitor.py --start 2025-12-01 --end 2026-05-31 --step 21
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pandas as pd

log = logging.getLogger("screener")


# ─────────────────────── defensive foundation imports ───────────────────────
# Prefer relative (package) imports; fall back to absolute so this also works
# when quantresearch/scoring_v1 itself is importable on sys.path.
try:  # pragma: no cover - import plumbing
    from . import adapter as _adapter
    from . import engine as _engine
except Exception:  # pragma: no cover - fallback path
    import adapter as _adapter   # type: ignore
    import engine as _engine     # type: ignore


# Canonical factor order (== engine.FACTOR_WEIGHTS keys). These are the 6
# factor_z columns the correlation monitor operates on.
FACTORS = list(_engine.FACTOR_WEIGHTS.keys())


# ───────────────────────────── date helpers ─────────────────────────────────

def _to_iso(d) -> str:
    """Normalize datetime/date/'YYYY-MM-DD' -> 'YYYY-MM-DD'."""
    if isinstance(d, str):
        return d[:10]
    try:
        return d.strftime("%Y-%m-%d")
    except Exception:
        return str(d)[:10]


def _date_range(start, end, step_days: int) -> list[str]:
    """Inclusive list of ISO dates from start to end stepping `step_days` calendar days."""
    s = datetime.strptime(_to_iso(start), "%Y-%m-%d")
    e = datetime.strptime(_to_iso(end), "%Y-%m-%d")
    if step_days <= 0:
        raise ValueError("step_days must be >= 1")
    out: list[str] = []
    cur = s
    while cur <= e:
        out.append(cur.strftime("%Y-%m-%d"))
        cur = cur + timedelta(days=step_days)
    return out


# ─────────────────────── factor_z recovery (per date) ───────────────────────

def _factor_z_means_for_date(as_of, mode: str = "tuned", symbols=None):
    """
    Run the engine for one as-of date and return the cross-sectional MEAN of each
    factor_z across the scored universe.

    Recovery: factor_z[f] = c_<f> / factor_weight[f]  (inverts engine's
    contrib = factor_z * fw). Uses engine._weights(mode) so the inversion uses
    EXACTLY the weights the engine applied.

    Returns:
        dict[factor -> float] (one mean per factor), or None if no symbol scored
        on this date (so the caller can skip the date cleanly).
    """
    ranked = _adapter.run_scoring(as_of, mode=mode, symbols=symbols)
    if ranked is None or len(ranked) == 0:
        log.warning("[monitor] no symbols scored for as_of=%s (mode=%s) - skipping",
                    _to_iso(as_of), mode)
        return None

    fw, _sw = _engine._weights(mode)  # same weights the engine used
    means: dict[str, float] = {}
    for f in FACTORS:
        col = f"c_{f}"
        w = fw.get(f)
        if col not in ranked.columns or not w:
            # Should not happen given the engine contract, but stay defensive:
            # a missing factor column / zero weight -> NaN (kept out of corr).
            means[f] = float("nan")
            continue
        factor_z_series = ranked[col].astype(float) / float(w)
        means[f] = float(factor_z_series.mean(skipna=True))
    return means


# ─────────────────────────── public entry point ─────────────────────────────

def build_factor_z_history(as_of_dates, mode: str = "tuned", symbols=None) -> pd.DataFrame:
    """
    Build the factor_z history DataFrame [dates x 6 factor_z] by calling
    adapter.run_scoring across `as_of_dates` and recovering per-date, per-factor
    cross-sectional means.

    Args:
        as_of_dates: iterable of datetime/date/'YYYY-MM-DD'. PIT cutoffs.
        mode:        'tuned' (default) | 'equal' - passed to the engine.
        symbols:     optional candidate symbol list (passed through).

    Returns:
        pandas.DataFrame indexed by ISO date string, columns = FACTORS
        (momentum, trend, smart_money, sector_rs, earnings, risk). Dates on which
        nothing scored are skipped. Empty DataFrame (with the factor columns) if
        no date produced a scored universe.
    """
    rows: dict[str, dict] = {}
    for d in (as_of_dates or []):
        iso = _to_iso(d)
        means = _factor_z_means_for_date(iso, mode=mode, symbols=symbols)
        if means is None:
            continue
        rows[iso] = means
        log.info("[monitor] factor_z means @ %s: %s", iso,
                 {k: round(v, 4) for k, v in means.items()})

    if not rows:
        return pd.DataFrame(columns=FACTORS)
    hist = pd.DataFrame.from_dict(rows, orient="index")[FACTORS]
    hist.index.name = "as_of_date"
    return hist.sort_index()


def run_correlation_monitor(as_of_dates, mode: str = "tuned", symbols=None):
    """
    MONITORING ONLY (never enters the score). Build the factor_z history across
    the given as-of dates and run the LOCKED engine.factor_correlation_monitor on
    it.

    Args:
        as_of_dates: iterable of datetime/date/'YYYY-MM-DD'. Monthly cadence is
                     the intended use, but any list works.
        mode:        'tuned' (default) | 'equal'.
        symbols:     optional candidate symbol list (passed through).

    Returns:
        (corr_matrix, warnings, history)
          corr_matrix : pandas.DataFrame [6 x 6] factor-vs-factor correlation
                        (empty DataFrame if < 2 usable dates).
          warnings    : list[(factorA, factorB, rho)] with |rho| > engine.CORR_WARN.
          history     : the factor_z history DataFrame used (for inspection).
    """
    history = build_factor_z_history(as_of_dates, mode=mode, symbols=symbols)
    if len(history) < 2:
        log.warning(
            "[monitor] need >= 2 scored dates for a correlation matrix; got %d. "
            "Correlation is undefined - returning empty matrix.", len(history))
        return pd.DataFrame(), [], history

    corr, warn = _engine.factor_correlation_monitor(history)
    return corr, warn, history


# ────────────────────────── readable report ─────────────────────────────────

def _format_report(corr, warnings, history, mode: str) -> str:
    """Render a human-readable monitoring report (pure string; no I/O)."""
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("scoring_v1 FACTOR-CORRELATION MONITOR  (MONITORING ONLY - not in score)")
    lines.append("=" * 72)
    lines.append(f"mode           : {mode}")
    lines.append(f"dates scored   : {len(history)}")
    if len(history):
        lines.append(f"window         : {history.index.min()} .. {history.index.max()}")
    lines.append(f"warn threshold : |rho| > {_engine.CORR_WARN}")
    lines.append("")

    lines.append("factor_z history (cross-sectional mean per date):")
    if len(history):
        lines.append(history.round(4).to_string())
    else:
        lines.append("  (no dates produced a scored universe)")
    lines.append("")

    lines.append("correlation matrix (factor_z over time):")
    if corr is not None and len(corr):
        lines.append(corr.round(2).to_string())
    else:
        lines.append("  (undefined - need >= 2 scored dates)")
    lines.append("")

    if warnings:
        lines.append(f"WARNINGS ({len(warnings)} pair(s) with |rho| > {_engine.CORR_WARN}):")
        for a, b, rho in warnings:
            lines.append(f"  ! {a:<12} <-> {b:<12}  rho = {rho:+.2f}")
        lines.append("")
        lines.append("ACTION: high collinearity means the composite may be double-counting")
        lines.append("a single underlying signal. This is a REVIEW SIGNAL ONLY; per locked")
        lines.append("spec it must NOT auto-adjust weights, score, or rank.")
    else:
        lines.append(f"OK: no factor pair exceeds |rho| > {_engine.CORR_WARN}.")
    lines.append("=" * 72)
    return "\n".join(lines)


def print_report(corr, warnings, history, mode: str = "tuned") -> None:
    """Print the readable monitoring report to stdout."""
    print(_format_report(corr, warnings, history, mode))


# ───────────────────────────────── CLI ──────────────────────────────────────

def _parse_args(argv=None):
    import argparse
    p = argparse.ArgumentParser(
        description="scoring_v1 factor-correlation monitor (monitoring only; "
                    "never enters the score). Runs on the REAL PG point-in-time store.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dates", help="comma-separated as-of dates, e.g. "
                                   "2025-12-31,2026-01-30,2026-02-27")
    g.add_argument("--start", help="range start (YYYY-MM-DD); use with --end/--step")
    p.add_argument("--end", help="range end (YYYY-MM-DD); required with --start")
    p.add_argument("--step", type=int, default=21,
                   help="range step in CALENDAR days (default 21 ~ monthly cadence)")
    p.add_argument("--mode", choices=("tuned", "equal"), default="tuned",
                   help="weight mode passed to the engine (default tuned)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    """CLI entry point. MUST be run via this module so require_pg() guards it."""
    args = _parse_args(argv)

    if args.dates:
        dates = [d.strip() for d in args.dates.split(",") if d.strip()]
    else:
        if not args.end:
            raise SystemExit("--start requires --end")
        dates = _date_range(args.start, args.end, args.step)

    log.info("[monitor] running correlation monitor on %d date(s), mode=%s",
             len(dates), args.mode)
    corr, warnings, history = run_correlation_monitor(dates, mode=args.mode)
    print_report(corr, warnings, history, mode=args.mode)
    # Non-zero exit if any warning, so the monthly job can alert on collinearity.
    return 1 if warnings else 0


if __name__ == "__main__":
    # MANDATORY PG GUARD - load .env (-> PostgreSQL) and refuse the SQLite fallback.
    import sys, os
    sys.path.insert(0, r"d:\Gulshan\QuantResearch")
    from quantresearch.scoring_v1 import bootstrap  # FIRST import - loads .env
    bootstrap.require_pg()                           # raises if not on live PG

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    raise SystemExit(main())
