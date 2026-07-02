"""
adapter.py - Assemble the locked engine's inputs point-in-time, then score.
===========================================================================
ADDITIVE / READ-ONLY orchestration layer. It wires the foundation modules
(pit_loader, sector_map, earnings_adapter, gates) into the EXACT input shape
that the LOCKED engine (quantresearch/scoring_v1/engine.py -> score_universe)
expects, then runs the engine and returns the ranked DataFrame.

WHAT THE ENGINE EXPECTS (verbatim contract, NOT modified here)
--------------------------------------------------------------
score_universe(price_data, benchmark, sector_idx, earnings, mode):
  * price_data : dict[symbol -> pandas.DataFrame]   lowercase cols
                 open, high, low, close, volume, delivery_pct ; DatetimeIndex
                 ASCENDING. (== pit_loader.load_price_df output.)
  * benchmark  : pandas.Series of broad-benchmark close, ASCENDING.
                 (== pit_loader.load_benchmark -> NSE "Nifty 500".)
  * sector_idx : dict[symbol -> pandas.Series] of that stock's sector-index
                 close, ASCENDING. (== pit_loader.load_index_series for the
                 NSE index named by sector_map.get_sector_index_name(symbol).)
  * earnings   : dict[symbol -> dict] (any field may be None). (==
                 earnings_adapter.build_earnings_batch output.)

POINT-IN-TIME MANDATE
---------------------
Every underlying loader/gate filters `date <= as_of_date`. This module adds
NO new data access of its own beyond those foundation calls, so it inherits
their no-look-ahead guarantee. Any symbol/field that cannot be loaded is
simply dropped (price) or surfaced as None (sector_idx/earnings) - never
fabricated.

ROLLBACK-SAFETY
---------------
ADDITIVE. Touches no live module and does NOT modify the engine; it only
reads via the foundation layer and feeds score_universe(...). Imports are
done defensively (relative-first, absolute fallback) so the module works both
as a package member and when the package root is on sys.path.
"""

from __future__ import annotations

import logging

log = logging.getLogger("screener")


# ─────────────────────── defensive foundation imports ───────────────────────
# Prefer relative (package) imports; fall back to absolute so this also works
# when quantresearch/scoring_v1 itself is importable on sys.path.
try:  # pragma: no cover - import plumbing
    from . import pit_loader, sector_map, earnings_adapter, gates
    from . import engine as _engine
except Exception:  # pragma: no cover - fallback path
    import pit_loader        # type: ignore
    import sector_map        # type: ignore
    import earnings_adapter  # type: ignore
    import gates             # type: ignore
    import engine as _engine  # type: ignore


def _is_empty_df(df) -> bool:
    """True if df is None or has no rows (pandas-free safe)."""
    if df is None:
        return True
    try:
        return len(df) == 0
    except Exception:
        # If it has an `empty` attribute (DataFrame/Series), use it.
        return bool(getattr(df, "empty", True))


# ─────────────────────────── input assembly ─────────────────────────────────

def build_engine_inputs(as_of_date, symbols=None, pre_gated=False, fetch_earnings=True):
    """
    Assemble (price_data, benchmark, sector_idx, earnings) for `as_of_date`,
    strictly point-in-time, ready to hand to engine.score_universe(...).

    Steps (per the build mandate):
      1) symbols -> apply hard universe + quality gates -> eligible only.
      2) price_data = {sym: load_price_df(sym, as_of)} for eligible (drop empty).
      3) benchmark = load_benchmark(as_of)  (NSE "Nifty 500").
      4) sector_idx = {sym: load_index_series(sector_index, as_of)} for eligible
         symbols whose sector maps to a non-None NSE index AND whose index
         series actually loads (empty series dropped).
      5) earnings = build_earnings_batch(eligible, as_of).

    Args:
        as_of_date: datetime | date | 'YYYY-MM-DD'. PIT cutoff (no look-ahead).
        symbols:    optional iterable of symbols to consider. When None, the
                    candidate universe is pit_loader.list_symbols_with_history.

    Returns:
        (price_data, benchmark, sector_idx, earnings)
          price_data : dict[str -> DataFrame]   (may be empty)
          benchmark  : pandas.Series | None
          sector_idx : dict[str -> Series]      (subset of eligible)
          earnings   : dict[str -> dict]        (one per eligible symbol)
    """
    # ── 1) candidate universe -> hard gates -> eligible ──────────────────────
    if symbols is None:
        candidates = pit_loader.list_symbols_with_history(as_of_date)
    else:
        candidates = list(symbols)

    if pre_gated:
        eligible = list(candidates)  # caller already ran apply_universe_gates (avoid double work)
    else:
        try:
            eligible, _rejected = gates.apply_universe_gates(candidates, as_of_date)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("[adapter] apply_universe_gates failed: %s — using candidates", exc)
            eligible = list(candidates)

    # ── 2) price_data (drop None/empty) ──────────────────────────────────────
    # Bulk-load all eligible bars in ONE query, then assemble price_data in ELIGIBLE
    # ORDER so the engine's rank(method="first") tie-break is unchanged => byte-identical.
    _bulk_px = pit_loader.load_price_df_bulk(eligible, as_of_date)
    price_data = {}
    for sym in eligible:
        df = _bulk_px.get(sym)
        if not _is_empty_df(df):
            price_data[sym] = df

    # ── 3) benchmark (NSE Nifty 500) ─────────────────────────────────────────
    benchmark = pit_loader.load_benchmark(as_of_date)

    # ── 4) sector_idx (only mapped sectors with a loadable series) ───────────
    # Cache index series per NSE index_name so we hit the store once per index,
    # not once per symbol sharing that index.
    sector_idx = {}
    _index_cache = {}
    for sym in eligible:
        idx_name = sector_map.get_sector_index_name(sym)
        if not idx_name:
            continue  # unmapped sector -> engine treats sector_rs as neutral
        if idx_name not in _index_cache:
            _index_cache[idx_name] = pit_loader.load_index_series(idx_name, as_of_date)
        series = _index_cache[idx_name]
        if not _is_empty_df(series):
            sector_idx[sym] = series

    # ── 5) earnings (point-in-time; any field may be None) ───────────────────
    # fetch_earnings=False -> earnings={} (engine neutralises the 12% factor for ALL
    # symbols identically); used for fast full-universe runs where the network-bound
    # screener scrape is the bottleneck. EOD production runs fetch real earnings.
    if fetch_earnings:
        # Warm the earnings_store bulk cache (ONE query) so the per-symbol build is
        # local-instant; clear it after so nothing leaks across runs.
        try:
            from . import data_store, dhan_forecast
            data_store.warm_earnings_cache([dhan_forecast._isin_for(s) for s in eligible])
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("[adapter] earnings cache warm failed: %s", exc)
        try:
            earnings = earnings_adapter.build_earnings_batch(eligible, as_of_date)
        finally:
            try:
                from . import data_store
                data_store.clear_earnings_cache()
            except Exception:
                pass
    else:
        earnings = {}

    return price_data, benchmark, sector_idx, earnings


# ──────────────────────────── run the engine ────────────────────────────────

def run_scoring(as_of_date, mode="tuned", symbols=None):
    """
    Build the point-in-time engine inputs for `as_of_date` and run the LOCKED
    engine, returning the ranked DataFrame (composite-z descending).

    Args:
        as_of_date: datetime | date | 'YYYY-MM-DD'. PIT cutoff.
        mode:       'tuned' (default) | 'equal' — passed through to the engine.
        symbols:    optional candidate symbol list (see build_engine_inputs).

    Returns:
        pandas.DataFrame as produced by engine.score_universe (empty DataFrame
        if no symbol survives the >=126-bar eligibility floor).
    """
    price_data, benchmark, sector_idx, earnings = build_engine_inputs(
        as_of_date, symbols=symbols
    )
    return _engine.score_universe(
        price_data,
        benchmark=benchmark,
        sector_idx=sector_idx,
        earnings=earnings,
        mode=mode,
    )


# ─────────────────────────── reporting helper ───────────────────────────────

def inputs_summary(as_of_date, symbols=None) -> dict:
    """
    Build the engine inputs and report coverage counts (pure reporting; no
    side effects). Useful to confirm PIT assembly before/without scoring.

    Returns a dict:
      {
        as_of_date, eligible_symbols, price_data_size, benchmark_rows,
        sector_idx_coverage, earnings_with_any, earnings_total,
      }
    """
    price_data, benchmark, sector_idx, earnings = build_engine_inputs(
        as_of_date, symbols=symbols
    )

    try:
        bench_rows = 0 if _is_empty_df(benchmark) else int(len(benchmark))
    except Exception:
        bench_rows = 0

    try:
        cov = earnings_adapter.coverage_stats(earnings)
        earnings_any = cov.get("symbols_with_any_earnings", 0)
    except Exception:
        earnings_any = 0

    return {
        "as_of_date": str(as_of_date),
        "eligible_symbols": len(earnings),  # earnings is keyed on eligible set
        "price_data_size": len(price_data),
        "benchmark_rows": bench_rows,
        "sector_idx_coverage": len(sector_idx),
        "earnings_with_any": earnings_any,
        "earnings_total": len(earnings),
    }


if __name__ == "__main__":  # pragma: no cover - manual smoke aid
    import json
    from datetime import date
    print(json.dumps(inputs_summary(date.today().strftime("%Y-%m-%d")),
                     indent=2, default=str))
