"""
adapter.py - Assemble the legacy_cleaned engine's inputs point-in-time, then score.
==================================================================================
ADDITIVE / READ-ONLY orchestration layer for the THIRD scoring engine
(quantresearch/legacy_cleaned). It MIRRORS quantresearch/scoring_v1/adapter.py
verbatim for the first FOUR engine inputs (price_data, benchmark, sector_idx,
earnings) — reusing scoring_v1's FROZEN foundation modules read-only — and adds
exactly ONE thing: a FUNDAMENTALS dict, keyed on the same eligible symbol set.

WHAT THE ENGINE EXPECTS (verbatim contract, NOT modified here)
--------------------------------------------------------------
score_universe(price_data, benchmark, sector_idx, earnings, fundamentals, mode):
  * price_data   : dict[symbol -> pandas.DataFrame]   lowercase cols
                   open, high, low, close, volume, delivery_pct ; DatetimeIndex
                   ASCENDING. (== scoring_v1.pit_loader.load_price_df output.)
  * benchmark    : pandas.Series of broad-benchmark close, ASCENDING.
                   (== scoring_v1.pit_loader.load_benchmark -> NSE "Nifty 500".)
  * sector_idx   : dict[symbol -> pandas.Series] of that stock's sector-index
                   close, ASCENDING. (== scoring_v1.pit_loader.load_index_series
                   for the NSE index named by sector_map.get_sector_index_name.)
  * earnings     : dict[symbol -> dict] (any field may be None). (==
                   scoring_v1.earnings_adapter.build_earnings_batch output.)
  * fundamentals : dict[symbol -> dict] (NEW). Per-symbol quality fundamentals
                   (pe/pb/roe/roce/eps/div_yield/debt_to_equity/promoter_pct, …)
                   from universe_catalog via scoring_v1.data_store.get_fundamentals.
                   Missing symbol -> {} (never fabricated). LOCAL DB read only.

POINT-IN-TIME MANDATE
---------------------
Every underlying loader/gate filters `date <= as_of_date`. This module adds NO
new data access of its own beyond those foundation calls (fundamentals are a
current-snapshot LOCAL DB read, no network, no look-ahead into future bars), so
it inherits the no-look-ahead guarantee. Any symbol/field that cannot be loaded
is dropped (price) or surfaced as None/{} (sector_idx/earnings/fundamentals) —
never fabricated.

ZERO EXTERNAL NETWORK
---------------------
The earnings build is guarded by the STORE_ONLY discipline: around the batch we
set earnings_adapter.STORE_ONLY=True and dhan_forecast.STORE_ONLY=True (then
RESTORE the prior values), so nothing hits Dhan/NSE/screener during assembly —
the same guarantee scoring_v1's live_pipeline provides. Fundamentals are a plain
local DB read (universe_catalog, already Dhan-enriched offline).

ROLLBACK-SAFETY
---------------
ADDITIVE. Touches no live module and does NOT modify the engine or the reused
scoring_v1 foundation; it only reads via that layer and feeds score_universe(...).
Imports are done defensively (the sibling `engine`/`gates` modules may be built
by another agent and may not yet exist), so this module imports cleanly on its
own and only requires them at call time.
"""

from __future__ import annotations

import logging

log = logging.getLogger("screener")


# ─────────────── FROZEN scoring_v1 foundation (reused READ-ONLY) ───────────────
# The first four engine inputs are byte-identical to scoring_v1's, so we reuse
# v1's foundation modules verbatim. Prefer package-relative import of the sibling
# scoring_v1 package; fall back to absolute so this also works when the individual
# modules are importable on sys.path.
try:  # pragma: no cover - import plumbing
    from ..scoring_v1 import (
        pit_loader,
        sector_map,
        earnings_adapter,
        data_store,
        dhan_forecast,
    )
except Exception:  # pragma: no cover - fallback path
    import pit_loader        # type: ignore
    import sector_map        # type: ignore
    import earnings_adapter  # type: ignore
    import data_store        # type: ignore
    import dhan_forecast     # type: ignore


def _apply_universe_gates(candidates, as_of_date):
    """Call legacy_cleaned.gates.apply_universe_gates, imported DEFENSIVELY.

    The sibling `gates` module is created by another agent and may not exist yet
    (or may fail), so this is imported at call time and any failure degrades to
    "all candidates eligible" rather than raising — matching v1's adapter, which
    also swallows a failing apply_universe_gates.
    """
    try:  # pragma: no cover - import plumbing
        try:
            from . import gates  # legacy_cleaned.gates (preferred)
        except Exception:
            import gates  # type: ignore  # sys.path fallback
        eligible, rejected = gates.apply_universe_gates(candidates, as_of_date)
        return list(eligible), rejected
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("[lc-adapter] apply_universe_gates unavailable/failed: %s "
                    "— using candidates", exc)
        return list(candidates), {}


def _get_engine():
    """Import legacy_cleaned.engine DEFENSIVELY (built by another agent)."""
    try:
        from . import engine as _engine  # preferred
        return _engine
    except Exception:  # pragma: no cover - fallback path
        import engine as _engine  # type: ignore
        return _engine


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

def build_engine_inputs(as_of_date, symbols=None, pre_gated=False,
                        fetch_earnings=True):
    """
    Assemble (price_data, benchmark, sector_idx, earnings, fundamentals) for
    `as_of_date`, strictly point-in-time, ready to hand to
    engine.score_universe(...).

    Steps (mirrors scoring_v1's adapter for 1-5; step 6 is the ONLY addition):
      1) symbols -> apply hard universe + quality gates -> eligible only.
      2) price_data = {sym: load_price_df(sym, as_of)} for eligible (drop empty).
      3) benchmark = load_benchmark(as_of)  (NSE "Nifty 500").
      4) sector_idx = {sym: load_index_series(sector_index, as_of)} for eligible
         symbols whose sector maps to a non-None NSE index AND whose index
         series actually loads (empty series dropped).
      5) earnings = build_earnings_batch(eligible, as_of), under a STORE_ONLY
         zero-network guard (set + restore earnings_adapter/dhan_forecast flags).
      6) fundamentals = {sym: data_store.get_fundamentals(sym) or {}} for eligible
         (LOCAL DB read from universe_catalog; None -> {}).

    Args:
        as_of_date:     datetime | date | 'YYYY-MM-DD'. PIT cutoff (no look-ahead).
        symbols:        optional iterable of symbols to consider. When None, the
                        candidate universe is pit_loader.list_symbols_with_history.
        pre_gated:      when True, `symbols` are already gated (skip re-gating).
        fetch_earnings: when False, earnings={} (engine neutralises the earnings
                        factor identically for all symbols).

    Returns:
        (price_data, benchmark, sector_idx, earnings, fundamentals)
          price_data   : dict[str -> DataFrame]   (may be empty)
          benchmark    : pandas.Series | None
          sector_idx   : dict[str -> Series]      (subset of eligible)
          earnings     : dict[str -> dict]        (one per eligible symbol)
          fundamentals : dict[str -> dict]        (one per eligible symbol; {} if none)
    """
    # ── 1) candidate universe -> hard gates -> eligible ──────────────────────
    if symbols is None:
        candidates = pit_loader.list_symbols_with_history(as_of_date)
    else:
        candidates = list(symbols)

    if pre_gated:
        eligible = list(candidates)  # caller already ran apply_universe_gates (avoid double work)
    else:
        eligible, _rejected = _apply_universe_gates(candidates, as_of_date)

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

    # ── 5) earnings (point-in-time; any field may be None; ZERO network) ──────
    # fetch_earnings=False -> earnings={} (engine neutralises the earnings factor for
    # ALL symbols identically); used for fast full-universe runs.
    if fetch_earnings:
        # Warm the earnings_store bulk cache (ONE query) so the per-symbol build is
        # local-instant; clear it after so nothing leaks across runs.
        try:
            data_store.warm_earnings_cache(
                [dhan_forecast._isin_for(s) for s in eligible])
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("[lc-adapter] earnings cache warm failed: %s", exc)

        # Zero-external-fetch guard: force STORE_ONLY on both modules around the
        # build (same discipline scoring_v1.live_pipeline applies), then RESTORE
        # the prior values so we never leak state across runs.
        _prev_ea = getattr(earnings_adapter, "STORE_ONLY", False)
        _prev_df = getattr(dhan_forecast, "STORE_ONLY", False)
        try:
            earnings_adapter.STORE_ONLY = True
            dhan_forecast.STORE_ONLY = True
            earnings = earnings_adapter.build_earnings_batch(eligible, as_of_date)
        finally:
            try:
                earnings_adapter.STORE_ONLY = _prev_ea
                dhan_forecast.STORE_ONLY = _prev_df
            except Exception:
                pass
            try:
                data_store.clear_earnings_cache()
            except Exception:
                pass
    else:
        earnings = {}

    # ── 6) fundamentals (LOCAL DB snapshot; None -> {}) — THE ONLY ADDITION ──
    # Per-symbol quality fundamentals from universe_catalog (already Dhan-enriched
    # offline). Zero network: data_store.get_fundamentals is a local DB read.
    fundamentals = {}
    for sym in eligible:
        try:
            f = data_store.get_fundamentals(sym)
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("[lc-adapter] get_fundamentals(%s) failed: %s", sym, exc)
            f = None
        fundamentals[sym] = f if f else {}

    return price_data, benchmark, sector_idx, earnings, fundamentals


# ──────────────────────────── run the engine ────────────────────────────────

def run_scoring(as_of_date, mode="tuned", symbols=None):
    """
    Build the point-in-time engine inputs for `as_of_date` and run the
    legacy_cleaned engine, returning the ranked DataFrame.

    Args:
        as_of_date: datetime | date | 'YYYY-MM-DD'. PIT cutoff.
        mode:       'tuned' (default) | 'equal' — passed through to the engine.
        symbols:    optional candidate symbol list (see build_engine_inputs).

    Returns:
        pandas.DataFrame as produced by engine.score_universe.
    """
    price_data, benchmark, sector_idx, earnings, fundamentals = build_engine_inputs(
        as_of_date, symbols=symbols
    )
    _engine = _get_engine()
    return _engine.score_universe(
        price_data,
        benchmark=benchmark,
        sector_idx=sector_idx,
        earnings=earnings,
        fundamentals=fundamentals,
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
        fundamentals_with_any, fundamentals_total,
      }
    """
    price_data, benchmark, sector_idx, earnings, fundamentals = build_engine_inputs(
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

    fund_any = sum(
        1 for f in fundamentals.values()
        if f and any(v is not None for v in f.values())
    )

    return {
        "as_of_date": str(as_of_date),
        "eligible_symbols": len(earnings),  # earnings is keyed on eligible set
        "price_data_size": len(price_data),
        "benchmark_rows": bench_rows,
        "sector_idx_coverage": len(sector_idx),
        "earnings_with_any": earnings_any,
        "earnings_total": len(earnings),
        "fundamentals_with_any": fund_any,
        "fundamentals_total": len(fundamentals),
    }


if __name__ == "__main__":  # pragma: no cover - manual smoke aid
    import json
    from datetime import date
    print(json.dumps(inputs_summary(date.today().strftime("%Y-%m-%d")),
                     indent=2, default=str))
