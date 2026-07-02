"""
nse_results.py - earnings from the OFFICIAL NSE Corporate Financial Results API.
================================================================================
Authoritative source (the filed quarterly results) — supersedes screener.in
scraping. Gives, per quarter, the as-filed Revenue / Net Profit / EPS PLUS the
REAL result-announcement date (broadCastDate) that drives the earnings-decay —
something yfinance/screener don't provide. Clean JSON (no raw-XBRL parsing).

Endpoints (cookie-warmed browser session, like nse_bhavcopy):
  list   : /api/corporates-financial-results?index=equities&symbol=SYM&period=Quarterly
           -> filings incl broadCastDate, fromDate/toDate, params, seqNumber,
              consolidated, industry, indAs, format
  detail : /api/corporates-financial-results-data?index=equities&params=..&seq_id=..
              &industry=..&ind=indAS&format=New  -> resultsData2 line items

resultsData2 codes used (values are ratios/EPS, so unit-agnostic):
  re_net_sale  = revenue from operations   (fallback re_total_inc)
  re_net_profit = PAT                        (fallback re_con_pro_loss / re_proloss_ord_act)
  re_basic_eps_for_cont_dic_opr = basic EPS  (fallback re_dilut_eps_for_cont_dic_opr)

Reliability: cookie warm-up + per-request retry (the local DNS is flaky; prod is
stable). 7-day per-symbol cache so a daily run mostly hits cache.
"""
from __future__ import annotations

import os
import json
import time
import logging
import urllib.parse as _up
from datetime import datetime

log = logging.getLogger("screener")

_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "cache", "nse_results")
_CACHE_TTL = 7 * 86400
_HOME = "https://www.nseindia.com/"
_LIST = "https://www.nseindia.com/api/corporates-financial-results"
_DATA = "https://www.nseindia.com/api/corporates-financial-results-data"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "application/json,text/plain,*/*", "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

_session = None


def _get_session():
    global _session
    if _session is None:
        import requests
        s = requests.Session()
        try:
            s.get(_HOME, headers=_HEADERS, timeout=12)
            time.sleep(1)
        except Exception as exc:
            log.debug("[nse_results] cookie warm-up failed: %s", exc)
        _session = s
    return _session


def _get(url, retries=4):
    s = _get_session()
    for i in range(retries):
        try:
            r = s.get(url, headers=_HEADERS, timeout=20)
            if r.status_code == 200:
                return r
            # 401/403 -> cookie expired: re-warm once
            if r.status_code in (401, 403):
                try:
                    s.get(_HOME, headers=_HEADERS, timeout=12); time.sleep(1)
                except Exception:
                    pass
        except Exception as exc:
            log.debug("[nse_results] GET failed (%d): %s", i + 1, exc)
        time.sleep(1.2 * (i + 1))
    return None


def _f(v):
    try:
        if v in (None, "", "-"):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_dt(s):
    """NSE date 'dd-Mon-yyyy ...' -> datetime (date part) or None."""
    if not s:
        return None
    try:
        return datetime.strptime(str(s).split(" ")[0], "%d-%b-%Y")
    except Exception:
        return None


def _cache_path(symbol):
    return os.path.join(_CACHE_DIR, f"{symbol.upper()}.json")


def get_quarterly(symbol: str, n: int = 6) -> list:
    """Latest `n` quarterly results (newest first), each:
       {fromDate,toDate,broadcast(iso),revenue,pat,eps,net_margin}. Cached 7 days."""
    symbol = symbol.upper().replace(".NS", "").strip()
    cp = _cache_path(symbol)
    try:
        if os.path.exists(cp) and (time.time() - os.path.getmtime(cp)) < _CACHE_TTL:
            return json.loads(open(cp, encoding="utf-8").read())
    except Exception:
        pass

    r = _get(f"{_LIST}?index=equities&symbol={_up.quote(symbol)}&period=Quarterly")
    if not r:
        return []
    try:
        filings = r.json()
    except Exception:
        return []
    # prefer Consolidated; fall back to Non-Consolidated. newest first by toDate.
    q = [x for x in filings if x.get("period") == "Quarterly"]
    def _key(x):
        return _parse_dt(x.get("toDate")) or datetime(1900, 1, 1)
    cons = sorted([x for x in q if x.get("consolidated") == "Consolidated"], key=_key, reverse=True)
    noncons = sorted([x for x in q if x.get("consolidated") != "Consolidated"], key=_key, reverse=True)
    chosen = cons if len(cons) >= 2 else noncons
    # de-dup by toDate (keep first/newest per period)
    seen, picks = set(), []
    for x in chosen:
        td = x.get("toDate")
        if td in seen:
            continue
        seen.add(td); picks.append(x)
        if len(picks) >= n:
            break

    out = []
    for x in picks:
        params = x.get("params"); seq = x.get("seqNumber")
        if not params or not seq:
            continue
        q = {"index": "equities", "params": params, "seq_id": seq,
             "industry": x.get("industry") or "-", "ind": "indAS", "format": x.get("format") or "New"}
        rr = _get(f"{_DATA}?{_up.urlencode(q)}")
        rd = {}
        if rr:
            try:
                rd = (rr.json() or {}).get("resultsData2") or {}
            except Exception:
                rd = {}
        rev = _f(rd.get("re_net_sale")) or _f(rd.get("re_total_inc"))
        pat = _f(rd.get("re_net_profit")) or _f(rd.get("re_con_pro_loss")) or _f(rd.get("re_proloss_ord_act"))
        eps = _f(rd.get("re_basic_eps_for_cont_dic_opr")) or _f(rd.get("re_dilut_eps_for_cont_dic_opr"))
        nm = (pat / rev) if (rev and pat is not None and rev != 0) else None
        bc = _parse_dt(x.get("broadCastDate"))
        out.append({
            "fromDate": x.get("fromDate"), "toDate": x.get("toDate"),
            "broadcast": bc.strftime("%Y-%m-%d") if bc else None,
            "revenue": rev, "pat": pat, "eps": eps, "net_margin": nm,
        })
        time.sleep(0.2)  # be gentle to NSE

    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        open(cp, "w", encoding="utf-8").write(json.dumps(out))
    except Exception:
        pass
    return out


def build_nse_earnings(symbol: str, as_of_date) -> dict | None:
    """Engine earnings dict from NSE filings (point-in-time vs as_of). None if no data."""
    rows = get_quarterly(symbol, n=6)
    if not rows:
        return None
    as_of = str(as_of_date)[:10]
    # PIT: only quarters announced on/before as_of
    rows = [r for r in rows if (r.get("broadcast") or "9999") <= as_of] or rows
    if not rows:
        return None

    def col(k):
        return [r.get(k) for r in rows]
    rev, pat, eps = col("revenue"), col("pat"), col("net_margin"),
    rev = col("revenue"); pat = col("pat"); nm = col("net_margin"); ep = col("eps")

    def yoy(series, i0, i4):
        if len(series) > i4 and series[i0] is not None and series[i4] not in (None, 0):
            return round((series[i0] / series[i4] - 1.0) * 100, 2)
        return None

    rev_g = yoy(rev, 0, 4)
    pat_g = yoy(pat, 0, 4)
    pat_g_prev = yoy(pat, 1, 5)
    opm_latest = round(nm[0] * 100, 2) if nm and nm[0] is not None else None
    opm_yago = round(nm[4] * 100, 2) if len(nm) > 4 and nm[4] is not None else None
    eps_actual = ep[0] if ep else None
    last4 = [e for e in ep[:4] if e is not None]
    eps_trend = None
    if eps_actual is not None and len(last4) >= 4:
        m = sum(last4) / len(last4)
        eps_trend = round((eps_actual - m) / abs(m), 4) if m else None
    dsr = None
    bc = rows[0].get("broadcast")  # already ISO 'YYYY-MM-DD' from get_quarterly
    if bc:
        try:
            dsr = (datetime.strptime(as_of, "%Y-%m-%d") - datetime.strptime(bc, "%Y-%m-%d")).days
            if dsr < 0:
                dsr = None
        except Exception:
            dsr = None

    return {
        "rev_growth_yoy": rev_g, "pat_growth_yoy": pat_g, "pat_growth_yoy_prev": pat_g_prev,
        "opm_latest": opm_latest, "opm_yago": opm_yago,
        "eps_actual": eps_actual, "eps_consensus": None, "eps_trend": eps_trend,
        "days_since_result": dsr, "_source": "nse",
    }


if __name__ == "__main__":
    import sys
    for sym in (sys.argv[1:] or ["RELIANCE", "TCS", "INFY"]):
        print(sym, "->", json.dumps(build_nse_earnings(sym, datetime.today().strftime("%Y-%m-%d")), default=str))
