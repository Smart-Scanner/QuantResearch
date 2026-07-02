"""
dhan_forecast.py - earnings from Dhan/ScanX forecast API (PRIMARY, most recent).
================================================================================
Reverse-engineered from the ScanX company SPA (Playwright XHR intercept):
  POST https://ow-static-scanx.dhan.co/staticscanx/forecast
  payload {"Data": {"isin": "<ISIN>", "period": "Q"}}
  -> {"data": {"actuals":   {metric: {YYYYMM: value}},     # as-reported quarters
                "estimates": {metric: {YYYYMM: {low,mean,high}}}}}  # analyst consensus
metrics: revenue, net_income, ebitda, eps, roe, roa, price.

Why this is the primary source:
  * RECENT — actuals run to the latest reported quarter (e.g. 202603 = Mar-2026),
    unlike the NSE corporate-results API (stale at Jan-2025 here). So the earnings
    decay does NOT zero out the factor.
  * Has the quarterly SERIES -> YoY growth, acceleration, margin trend, EPS trend.
  * Has ANALYST CONSENSUS (estimates.eps.mean) -> real EPS-surprise (actual vs
    consensus), the engine's primary e_surprise path (was always None before).
  * ONE clean JSON call per ISIN. ISIN comes from universe_catalog.

Values are ₹-cr / per-share; growth & margin are ratios (unit-agnostic).
Reliability: per-request retry (local DNS is flaky; prod stable) + 7-day cache.
"""
from __future__ import annotations

import os
import json
import time
import logging
from datetime import datetime, timedelta

log = logging.getLogger("screener")

_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "cache", "dhan_forecast")
_CACHE_TTL = 7 * 86400
_EP = "https://ow-static-scanx.dhan.co/staticscanx/forecast"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://scanx.trade/", "Origin": "https://scanx.trade",
    "Content-Type": "application/json",
}
_REPORT_LAG_DAYS = 45  # quarter-end -> typical Indian result-announcement lag (decay anchor)

_isin_cache = None

# Stage-2 zero-network guarantee: when True, get_forecast reads ONLY the shared
# earnings_store (DB, local) and never touches the file-cache or the network. The
# research scan sets this so a Stage-2 run does ZERO external fetch.
STORE_ONLY = False


def _isin_for(symbol: str):
    """symbol -> ISIN from universe_catalog (loaded once)."""
    global _isin_cache
    if _isin_cache is None:
        try:
            import db
            rows = db.execute_db("SELECT symbol, isin FROM universe_catalog WHERE isin IS NOT NULL AND isin <> ''",
                                 fetch="all") or []
            _isin_cache = {r["symbol"].upper(): r["isin"] for r in rows}
        except Exception as exc:  # pragma: no cover
            log.warning("[dhan_forecast] isin map load failed: %s", exc)
            _isin_cache = {}
    return _isin_cache.get((symbol or "").upper().replace(".NS", "").strip())


def _post(isin):
    import requests
    for i in range(4):
        try:
            r = requests.post(_EP, headers=_HEADERS, json={"Data": {"isin": isin, "period": "Q"}}, timeout=18)
            if r.status_code == 200:
                return r.json()
        except Exception as exc:
            log.debug("[dhan_forecast] post failed (%d) %s: %s", i + 1, isin, exc)
        time.sleep(1.0 * (i + 1))
    return None


def get_forecast(isin: str) -> dict | None:
    """Raw {actuals, estimates} for an ISIN. None if unavailable.

    Read order (Stage-2 fast path first):
      1. shared earnings_store (DB, local, ZERO network) — populated by Stage-1.
      2. if STORE_ONLY: stop here (research scan guarantee: no file/network).
      3. file-cache (legacy 7-day) — byte-identical to the store's raw.
      4. network (Dhan) — only outside a research scan / when nothing stored yet.
    Same raw {actuals,estimates} regardless of source => build_dhan_earnings maps it
    identically => v1 scores unchanged.
    """
    if not isin:
        return None
    # 1) shared DB store
    try:
        from . import data_store
        raw = data_store.get_earnings_raw(isin)
        if raw and raw.get("actuals"):
            return raw
    except Exception as exc:
        log.debug("[dhan_forecast] store read failed %s: %s", isin, exc)
    # 2) Stage-2 guarantee: store-only, never fall through to file/network
    if STORE_ONLY:
        return None
    # 3) file-cache (legacy)
    cp = os.path.join(_CACHE_DIR, f"{isin}.json")
    try:
        if os.path.exists(cp) and (time.time() - os.path.getmtime(cp)) < _CACHE_TTL:
            return json.loads(open(cp, encoding="utf-8").read())
    except Exception:
        pass
    # 4) network
    d = _post(isin)
    data = (d or {}).get("data") if isinstance(d, dict) else None
    if not data or not data.get("actuals"):
        return None
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        open(cp, "w", encoding="utf-8").write(json.dumps(data))
    except Exception:
        pass
    return data


def fetch_and_store(isin: str) -> bool:
    """Stage-1 ingestion: force a network fetch for an ISIN and persist the raw
    forecast into the shared earnings_store (+ file-cache, kept in sync). Idempotent.
    Returns True if data was fetched + stored."""
    if not isin:
        return False
    d = _post(isin)
    data = (d or {}).get("data") if isinstance(d, dict) else None
    if not data or not data.get("actuals"):
        return False
    try:  # keep the legacy file-cache in sync (so both paths return identical raw)
        os.makedirs(_CACHE_DIR, exist_ok=True)
        open(os.path.join(_CACHE_DIR, f"{isin}.json"), "w", encoding="utf-8").write(json.dumps(data))
    except Exception:
        pass
    try:
        from . import data_store
        data_store.put_earnings_raw(isin, data, source="dhan_forecast")
        return True
    except Exception as exc:
        log.warning("[dhan_forecast] store write failed %s: %s", isin, exc)
        return False


def _period_end(p: str):
    """'YYYYMM' (quarter end month) -> date of last day of that month."""
    try:
        y, m = int(p[:4]), int(p[4:6])
        nxt = datetime(y + (m // 12), (m % 12) + 1, 1)
        return (nxt - timedelta(days=1)).date()
    except Exception:
        return None


def build_dhan_earnings(symbol: str, as_of_date) -> dict | None:
    """Engine earnings dict from the Dhan forecast endpoint (PIT vs as_of). None if no data."""
    isin = _isin_for(symbol)
    if not isin:
        return None
    data = get_forecast(isin)
    if not data:
        return None
    act = data.get("actuals") or {}
    est = data.get("estimates") or {}
    rev, ni, eb, eps = act.get("revenue") or {}, act.get("net_income") or {}, act.get("ebitda") or {}, act.get("eps") or {}
    if not rev and not ni and not eps:
        return None

    as_of = datetime.strptime(str(as_of_date)[:10], "%Y-%m-%d").date()
    # PIT: actual quarters whose estimated announce date (period_end + lag) <= as_of
    periods = sorted(p for p in (set(rev) | set(ni) | set(eps))
                     if _period_end(p) and (_period_end(p) + timedelta(days=_REPORT_LAG_DAYS)) <= as_of)
    if not periods:
        periods = sorted(set(rev) | set(ni) | set(eps))  # fallback: all (live ~ latest)
    if not periods:
        return None

    def at(series, p):
        v = series.get(p)
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def yoy(series, idx):
        if len(periods) > idx + 4:
            cur, ago = at(series, periods[-1 - idx]), at(series, periods[-5 - idx])
            if cur is not None and ago not in (None, 0):
                return round((cur / ago - 1.0) * 100, 2)
        return None

    latest = periods[-1]
    rev_g = yoy(rev, 0)
    pat_g = yoy(ni, 0)
    pat_g_prev = yoy(ni, 1)

    def margin(p):
        r, e = at(rev, p), at(eb, p)
        return round(e / r * 100, 2) if (r and e is not None and r != 0) else None
    opm_latest = margin(latest)
    opm_yago = margin(periods[-5]) if len(periods) >= 5 else None

    eps_actual = at(eps, latest)
    last4 = [at(eps, p) for p in periods[-4:] if at(eps, p) is not None]
    eps_trend = None
    if eps_actual is not None and len(last4) >= 4:
        m = sum(last4) / len(last4)
        eps_trend = round((eps_actual - m) / abs(m), 4) if m else None
    # REAL analyst consensus for the latest reported quarter (estimates.eps[period].mean)
    eps_consensus = None
    try:
        ce = (est.get("eps") or {}).get(latest)
        if isinstance(ce, dict) and ce.get("mean") is not None:
            eps_consensus = round(float(ce["mean"]), 4)
    except Exception:
        eps_consensus = None

    dsr = None
    pe = _period_end(latest)
    if pe:
        dsr = (as_of - (pe + timedelta(days=_REPORT_LAG_DAYS))).days
        if dsr < 0:
            dsr = 0

    return {
        "rev_growth_yoy": rev_g, "pat_growth_yoy": pat_g, "pat_growth_yoy_prev": pat_g_prev,
        "opm_latest": opm_latest, "opm_yago": opm_yago,
        "eps_actual": eps_actual, "eps_consensus": eps_consensus, "eps_trend": eps_trend,
        "days_since_result": dsr, "_source": "dhan_forecast", "_latest_period": latest,
    }


if __name__ == "__main__":
    import sys
    for s in (sys.argv[1:] or ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ITC"]):
        print(s, "->", json.dumps(build_dhan_earnings(s, datetime.today().strftime("%Y-%m-%d")), default=str))
