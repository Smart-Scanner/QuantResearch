"""
earnings_adapter.py  --  point-in-time earnings dict builder for scoring_v1.engine
==================================================================================
ADDITIVE / READ-ONLY adapter. Produces the EXACT earnings dict that the LOCKED
engine (quantresearch/scoring_v1/engine.py -> score_universe) consumes per symbol:

    {
      rev_growth_yoy, pat_growth_yoy, pat_growth_yoy_prev,
      opm_latest, opm_yago, eps_actual, eps_consensus, eps_trend,
      days_since_result
    }

Every field may be None; the engine treats None as neutral (winsorized z -> 0)
and decays the whole earnings block to zero when days_since_result is missing
or stale (EARN_STALE_DAYS = 75). So partial coverage is SAFE by construction.

DATA SOURCES (broker-free, read-only):
  * intelligence.fundamentals.extract_detailed_financials(symbol)
        -> {"quarterly": [...latest-first...], "yearly": [...latest-first...]}
        Each quarterly dict: revenue, rev_growth_qoq, net_income,
        net_income_growth_qoq, eps, ebitda_margin (== OPM%), net_margin.
        Each yearly dict: revenue, net_income, eps, rev_growth_yoy,
        net_income_growth_yoy, fcf, fcf_conversion.
        (Backed by the screener.in scrape / 7-day cache; per-symbol/deep-cache,
        so coverage is partial -- that's fine.)
  * intelligence.corporate_actions.get_corporate_actions(symbol)
        -> NSE result / board-meeting dates (the RESULT-ANNOUNCEMENT date, NOT
        the quarter-end). Used for days_since_result and as the point-in-time
        anchor.

POINT-IN-TIME MANDATE
---------------------
The normalized financials cache does NOT carry per-period date labels, so we
CANNOT reconstruct exactly which quarters were public on an arbitrary past
as_of_date from the cache alone. We therefore enforce PIT conservatively:

  1. days_since_result is computed ONLY from NSE result/board-meeting dates that
     are <= as_of_date (no look-ahead). If none is available -> None, and the
     engine decays the earnings factor to neutral.
  2. If the most-recent KNOWN result date is AFTER as_of_date (i.e. the latest
     cached quarter was announced after the as-of moment), that quarter was not
     public yet -> we drop the latest quarter from YoY/accel/OPM/EPS derivations
     to avoid look-ahead. If no result date is known at all, we still return the
     value fields (best-effort, common case = "as_of ~ today"), but with
     days_since_result=None the engine neutralizes them regardless.

Any field that cannot be computed -> None. Nothing is fabricated.
"""

from __future__ import annotations

import logging
from datetime import datetime, date

log = logging.getLogger("screener")

# Stage-2 zero-network guarantee: when True, build_earnings uses ONLY the Dhan
# earnings_store (via dhan_forecast, which is store-only too) and SKIPS the NSE /
# screener / corporate-actions network fallbacks. Dhan covers 100% of the gated
# universe, so those fallbacks never fire there anyway => scores stay byte-identical.
STORE_ONLY = False

# ----------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------

def _as_date(d) -> date | None:
    """Coerce str ('YYYY-MM-DD') / datetime / date -> date. None on failure."""
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    s = str(d).strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y", "%d-%m-%Y", "%d %b %Y",
                "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s[:len(fmt) + 4], fmt).date()
        except (ValueError, TypeError):
            continue
    # last resort: leading ISO date
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _num(v):
    """Return float(v) if it is a real, finite number; else None."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return f


def _latest_result_date(symbol: str, as_of: date):
    """Most-recent NSE result/board-meeting announcement date <= as_of_date.

    Reads intelligence.corporate_actions (read-only). Uses ex_date/record_date as
    the available proxy for the announcement date (the public corporate-actions
    feed exposes those). Returns a `date` or None.

    NOTE: this anchors days_since_result to a genuine NSE event date, never to a
    quarter-end. If the feed is unreachable or empty -> None (engine neutralizes).
    """
    try:
        from intelligence.corporate_actions import get_corporate_actions
        actions = get_corporate_actions(symbol) or []
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("earnings_adapter: corp-actions unavailable for %s: %s", symbol, exc)
        return None

    best = None
    for a in actions:
        if (a.get("action_type") or "").lower() != "result":
            continue
        for key in ("ex_date", "record_date"):
            d = _as_date(a.get(key))
            if d is not None and d <= as_of:
                if best is None or d > best:
                    best = d
                break
    return best


# ----------------------------------------------------------------------------
# core builder
# ----------------------------------------------------------------------------

def build_earnings(symbol: str, as_of_date) -> dict:
    """Build the engine's point-in-time earnings dict for one symbol.

    Args:
        symbol:     NSE symbol (e.g. "TCS"); ".NS" suffix tolerated.
        as_of_date: str 'YYYY-MM-DD' | datetime | date. PIT cutoff (no look-ahead).

    Returns:
        dict with EXACTLY these keys (any may be None):
          rev_growth_yoy, pat_growth_yoy, pat_growth_yoy_prev,
          opm_latest, opm_yago, eps_actual, eps_consensus, eps_trend,
          days_since_result
    """
    as_of = _as_date(as_of_date) or date.today()

    empty = {
        "rev_growth_yoy": None,
        "pat_growth_yoy": None,
        "pat_growth_yoy_prev": None,
        "opm_latest": None,
        "opm_yago": None,
        "eps_actual": None,
        "eps_consensus": None,   # we have NO analyst estimates -> always None
        "eps_trend": None,
        "days_since_result": None,
    }

    clean = (symbol or "").upper().replace(".NS", "").strip()
    if not clean:
        return dict(empty)

    def _usable(d):
        return d and any(d.get(k) is not None for k in
                         ("rev_growth_yoy", "pat_growth_yoy", "eps_actual", "opm_latest"))

    # --- PRIMARY: Dhan/ScanX forecast — most RECENT actuals + ANALYST CONSENSUS ---
    # Quarterly actuals to the latest reported quarter (so the earnings decay stays
    # active) + estimates.eps.mean -> real eps_consensus (genuine actual-vs-consensus
    # surprise, the engine's primary e_surprise path).
    try:
        from . import dhan_forecast
        dh = dhan_forecast.build_dhan_earnings(clean, as_of)
        if _usable(dh):
            return {k: dh.get(k) for k in empty}  # exactly the 9 engine keys
    except Exception as exc:
        log.debug("earnings_adapter: Dhan forecast failed for %s: %s", clean, exc)

    # Stage-2 (research scan): Dhan-store only — never reach the network fallbacks.
    if STORE_ONLY:
        return dict(empty)

    # --- SECONDARY: official NSE Corporate Financial Results (authoritative) ---
    # As-filed Revenue/PAT/EPS + the REAL announcement date (broadCastDate).
    try:
        from . import nse_results
        nse = nse_results.build_nse_earnings(clean, as_of)
        if _usable(nse):
            return {k: nse.get(k) for k in empty}
    except Exception as exc:
        log.debug("earnings_adapter: NSE secondary failed for %s: %s", clean, exc)

    # --- result-announcement date (PIT anchor) ------------------------------
    result_dt = _latest_result_date(clean, as_of)
    days_since_result = (as_of - result_dt).days if result_dt is not None else None
    # Guard against a malformed (future) date slipping through.
    if days_since_result is not None and days_since_result < 0:
        days_since_result = None
        result_dt = None

    # --- detailed financials (latest-first lists; partial coverage OK) ------
    try:
        from intelligence.fundamentals import extract_detailed_financials
        detailed = extract_detailed_financials(clean) or {}
    except Exception as exc:
        log.debug("earnings_adapter: detailed financials failed for %s: %s", clean, exc)
        detailed = {}

    quarterly = detailed.get("quarterly") or []   # latest-first
    yearly = detailed.get("yearly") or []         # latest-first

    if not quarterly and not yearly:
        out = dict(empty)
        out["days_since_result"] = days_since_result
        return out

    # PIT guard: if we KNOW the latest result was announced after as_of_date,
    # the most-recent cached quarter was not public yet -> drop it so YoY/accel/
    # OPM/EPS are derived from quarters that WERE public. (If result_dt is None we
    # cannot prove this, so we keep the series; days_since_result=None will make
    # the engine neutralize the block anyway.)
    #
    # result_dt <= as_of by construction here, so the latest known result is in
    # the past -> no drop needed. We only drop if a result date was found that is
    # in the future (handled above by nulling it). This branch is kept explicit
    # for clarity and future-proofing.
    q = list(quarterly)

    # ---- eps_actual = latest quarterly EPS ---------------------------------
    eps_actual = _num(q[0].get("eps")) if q else None

    # ---- eps_trend = (eps_actual - mean(last 4 q EPS)) / |mean| ; None if <4 -
    eps_trend = None
    eps_series = [_num(item.get("eps")) for item in q[:4]]
    eps_series = [e for e in eps_series if e is not None]
    if len(eps_series) >= 4:
        mean4 = sum(eps_series[:4]) / 4.0
        if mean4 != 0:
            eps_trend = (eps_actual - mean4) / abs(mean4) if eps_actual is not None else None

    # ---- opm_latest / opm_yago (OPM == ebitda_margin on screener) ----------
    # opm_yago = the year-ago quarter (4 quarters back) OPM.
    opm_latest = _num(q[0].get("ebitda_margin")) if q else None
    opm_yago = _num(q[4].get("ebitda_margin")) if len(q) > 4 else None

    # ---- pat_growth_yoy (latest) & pat_growth_yoy_prev (prior quarter's YoY) -
    # Prefer the annual net_income_growth_yoy for the headline figure (matches the
    # engine's own e_growth source preference); fall back to a quarterly YoY from
    # the trailing-quarter series when annual is unavailable.
    pat_growth_yoy = None
    if yearly:
        pat_growth_yoy = _num(yearly[0].get("net_income_growth_yoy"))

    def _q_yoy(idx):
        """Quarterly net-income YoY: quarter[idx] vs quarter[idx+4]. None if missing."""
        if idx + 4 >= len(q):
            return None
        cur = _num(q[idx].get("net_income"))
        ago = _num(q[idx + 4].get("net_income"))
        if cur is None or ago is None or ago == 0:
            return None
        return (cur - ago) / abs(ago) * 100.0

    if pat_growth_yoy is None:
        pat_growth_yoy = _q_yoy(0)

    # prior quarter's YoY growth (for e_accel = pat_yoy - pat_yoy_prev)
    pat_growth_yoy_prev = None
    if len(yearly) >= 2:
        pat_growth_yoy_prev = _num(yearly[1].get("net_income_growth_yoy"))
    if pat_growth_yoy_prev is None:
        pat_growth_yoy_prev = _q_yoy(1)

    # ---- rev_growth_yoy (latest) -------------------------------------------
    rev_growth_yoy = None
    if yearly:
        rev_growth_yoy = _num(yearly[0].get("rev_growth_yoy"))
    if rev_growth_yoy is None and len(q) > 4:
        cur = _num(q[0].get("revenue"))
        ago = _num(q[4].get("revenue"))
        if cur is not None and ago not in (None, 0):
            rev_growth_yoy = (cur - ago) / abs(ago) * 100.0

    return {
        "rev_growth_yoy": rev_growth_yoy,
        "pat_growth_yoy": pat_growth_yoy,
        "pat_growth_yoy_prev": pat_growth_yoy_prev,
        "opm_latest": opm_latest,
        "opm_yago": opm_yago,
        "eps_actual": eps_actual,
        "eps_consensus": None,           # NO analyst estimates available
        "eps_trend": eps_trend,
        "days_since_result": days_since_result,
    }


_NONE_EARNINGS = {
    "rev_growth_yoy": None, "pat_growth_yoy": None, "pat_growth_yoy_prev": None,
    "opm_latest": None, "opm_yago": None, "eps_actual": None, "eps_consensus": None,
    "eps_trend": None, "days_since_result": None,
}


def build_earnings_batch(symbols, as_of_date, max_workers: int = 10,
                         overall_timeout: float = 600.0) -> dict:
    """Build the earnings dict for many symbols — CONCURRENT + bounded (reliability).

    Cache-first: build_earnings() reads the screener financials cache, so cached
    symbols return instantly. The live screener scrape (cache misses) is the slow,
    network-bound part — previously serial (one 20s request at a time => the run
    HUNG on ~hundreds of misses). Now: a ThreadPoolExecutor fans the per-symbol
    builds out (I/O-bound -> threads win), and an OVERALL deadline guarantees the
    batch RETURNS even if some scrapes stall (each request is already timeout=20).
    Symbols that error or miss the deadline map to the all-None dict (never dropped,
    never fabricated -> the engine neutralises them).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from concurrent.futures import TimeoutError as _FTimeout

    syms = list(symbols or [])
    out: dict[str, dict] = {s: dict(_NONE_EARNINGS) for s in syms}
    if not syms:
        return out

    done = errors = 0
    # NOTE: do NOT use `with ThreadPoolExecutor()` — its __exit__ does
    # shutdown(wait=True), which DRAINS ALL tasks regardless of the as_completed
    # timeout (so the deadline would be ineffective and the run could hang/be killed,
    # as observed). Instead shutdown(cancel_futures=True) on timeout -> the deadline
    # becomes a REAL wall-clock bound (running tasks finish their bounded request;
    # not-yet-started tasks are abandoned and stay neutral).
    ex = ThreadPoolExecutor(max_workers=max_workers)
    futs = {ex.submit(build_earnings, s, as_of_date): s for s in syms}
    try:
        for fut in as_completed(futs, timeout=overall_timeout):
            sym = futs[fut]
            try:
                out[sym] = fut.result()
                done += 1
            except Exception as exc:  # pragma: no cover - defensive
                errors += 1
                log.debug("earnings_adapter: build_earnings failed for %s: %s", sym, exc)
    except _FTimeout:
        pending = len(syms) - done - errors
        log.warning("[earnings] batch deadline (%.0fs) hit — %d/%d built, %d pending "
                    "-> cancelled + left neutral", overall_timeout, done, len(syms), pending)
    finally:
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except TypeError:  # pragma: no cover - py<3.9
            ex.shutdown(wait=False)

    if errors:
        log.info("[earnings] batch: %d symbols, %d built, %d errors", len(syms), done, errors)
    return out


def coverage_stats(earnings_map: dict) -> dict:
    """Summarize field coverage across a build_earnings_batch() result.

    Returns counts + per-field non-None coverage fractions. Pure reporting; no
    side effects. Useful to confirm that partial coverage is within expectation.
    """
    fields = ("rev_growth_yoy", "pat_growth_yoy", "pat_growth_yoy_prev",
              "opm_latest", "opm_yago", "eps_actual", "eps_consensus",
              "eps_trend", "days_since_result")
    n = len(earnings_map) or 0
    per_field = {f: 0 for f in fields}
    any_data = 0
    for d in earnings_map.values():
        has_any = False
        for f in fields:
            if d.get(f) is not None:
                per_field[f] += 1
                if f != "eps_consensus":  # consensus is always None by design
                    has_any = True
        if has_any:
            any_data += 1
    return {
        "symbols": n,
        "symbols_with_any_earnings": any_data,
        "any_earnings_pct": round(100.0 * any_data / n, 1) if n else 0.0,
        "field_counts": per_field,
        "field_pct": {f: (round(100.0 * c / n, 1) if n else 0.0)
                      for f, c in per_field.items()},
    }


if __name__ == "__main__":  # lightweight self-check (no network needed if cached)
    import json as _json
    test_syms = ["TCS", "RELIANCE", "INFY", "HDFCBANK", "ITC"]
    em = build_earnings_batch(test_syms, datetime.today().strftime("%Y-%m-%d"))
    print(_json.dumps(em, indent=2, default=str))
    print("COVERAGE:", _json.dumps(coverage_stats(em), indent=2))
