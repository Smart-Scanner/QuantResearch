"""
screener_financials.py — Detailed financials fallback via screener.in
=====================================================================
Free, no-API-key source for quarterly + annual financial statements
(Sales, Net Profit, EPS, OPM, operating cash flow) used to revive the
Earnings Momentum / Financial Quality engines while the Upstox Company
Fundamentals API is unavailable.

This is the FALLBACK tier of intelligence/fundamentals.extract_detailed_financials:
    Upstox Company-Fundamentals (when ready)  ->  screener.in (this module)  ->  empty

Output schema (matches what get_earnings_momentum() consumes), latest-first:
  quarterly[]: {revenue, rev_growth_qoq, net_income, net_income_growth_qoq,
                eps, ebitda_margin, net_margin}
  yearly[]:    {revenue, net_income, eps, rev_growth_yoy, net_income_growth_yoy,
                fcf, fcf_conversion}

Note: scraping is structure-dependent and politeness-rate-limited; results are
cached 7 days by the caller, so live hits are infrequent (deep-scan only).
"""

import re
import logging
from typing import Optional

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("screener")

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}


def _to_float(s: str) -> Optional[float]:
    """Parse screener cell text -> float. Handles commas, %, parentheses, blanks."""
    if s is None:
        return None
    s = s.strip().replace(",", "").replace("%", "").replace("₹", "").strip()
    if s in ("", "-", "—"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return None


def _fetch_html(symbol: str) -> Optional[str]:
    """Fetch the screener company page; prefer consolidated, fall back to standalone."""
    import time as _time
    sym = symbol.upper().replace(".NS", "").strip()
    for path in (f"/company/{sym}/consolidated/", f"/company/{sym}/"):
        for attempt in range(2):  # 1 retry on TRANSIENT failure only (timeout/conn)
            try:
                r = requests.get(f"https://www.screener.in{path}", headers=_HEADERS, timeout=20)
                if r.status_code == 200 and "data-table" in r.text:
                    return r.text
                break  # non-200 / no table => a real miss for this path, don't retry
            except Exception as exc:
                log.debug("[screener] fetch failed %s (attempt %d): %s", path, attempt + 1, exc)
                _time.sleep(0.5)  # brief backoff, then one retry
    return None


def _parse_section(soup, section_id: str) -> dict:
    """Return {'periods': [labels...], 'rows': {label: [floats...]}} for a section table."""
    sec = soup.find("section", id=section_id)
    if not sec:
        return {"periods": [], "rows": {}}
    table = sec.find("table", class_="data-table") or sec.find("table")
    if not table:
        return {"periods": [], "rows": {}}

    head = table.find("thead")
    periods = []
    if head:
        ths = head.find_all("th")
        periods = [th.get_text(strip=True) for th in ths[1:]]  # skip first (label col)

    rows = {}
    body = table.find("tbody")
    if body:
        for tr in body.find_all("tr"):
            cells = tr.find_all("td")
            if not cells:
                continue
            label = cells[0].get_text(strip=True).rstrip("+").strip()
            if not label:
                continue
            rows[label] = [_to_float(td.get_text(strip=True)) for td in cells[1:]]
    return {"periods": periods, "rows": rows}


def _row(parsed: dict, *names):
    """First matching row (case-insensitive 'contains') as a list, else []."""
    rows = parsed.get("rows", {})
    for want in names:
        wl = want.lower()
        for label, vals in rows.items():
            if wl in label.lower():
                return vals
    return []


def _pct(curr, prev):
    if curr is None or prev is None or prev == 0:
        return None
    return (curr - prev) / abs(prev) * 100.0


def fetch_screener_financials(symbol: str) -> Optional[dict]:
    """Scrape + normalize quarterly/annual financials. Returns the detailed-financials
    dict (latest-first) or None when the page/tables are unavailable."""
    html = _fetch_html(symbol)
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")

    q = _parse_section(soup, "quarters")
    pl = _parse_section(soup, "profit-loss")
    cf = _parse_section(soup, "cash-flow")

    # ── Quarterly (screener lists oldest -> newest; we emit latest-first) ──
    q_sales = _row(q, "Sales", "Revenue")
    q_np = _row(q, "Net Profit")
    q_eps = _row(q, "EPS in Rs", "EPS")
    q_opm = _row(q, "OPM %")
    quarterly = []
    n = len(q_sales)
    for i in range(n):
        rev = q_sales[i]
        np_ = q_np[i] if i < len(q_np) else None
        quarterly.append({
            "revenue": rev,
            "rev_growth_qoq": _pct(rev, q_sales[i - 1]) if i > 0 else None,
            "net_income": np_,
            "net_income_growth_qoq": _pct(np_, q_np[i - 1]) if (i > 0 and i < len(q_np)) else None,
            "eps": q_eps[i] if i < len(q_eps) else None,
            "ebitda_margin": q_opm[i] if i < len(q_opm) else None,
            "net_margin": (np_ / rev * 100.0) if (np_ is not None and rev) else None,
        })
    quarterly.reverse()  # latest-first

    # ── Annual (align operating cash flow to P&L years by period label) ──
    pl_periods = pl.get("periods", [])
    pl_sales = _row(pl, "Sales", "Revenue")
    pl_np = _row(pl, "Net Profit")
    pl_eps = _row(pl, "EPS in Rs", "EPS")
    cfo_by_period = {}
    cfo_vals = _row(cf, "Cash from Operating Activity", "Operating Activity")
    for idx, label in enumerate(cf.get("periods", [])):
        if idx < len(cfo_vals):
            cfo_by_period[label] = cfo_vals[idx]

    yearly = []
    m = len(pl_sales)
    for i in range(m):
        rev = pl_sales[i]
        np_ = pl_np[i] if i < len(pl_np) else None
        label = pl_periods[i] if i < len(pl_periods) else None
        cfo = cfo_by_period.get(label)
        yearly.append({
            "revenue": rev,
            "net_income": np_,
            "eps": pl_eps[i] if i < len(pl_eps) else None,
            "rev_growth_yoy": _pct(rev, pl_sales[i - 1]) if i > 0 else None,
            "net_income_growth_yoy": _pct(np_, pl_np[i - 1]) if (i > 0 and i < len(pl_np)) else None,
            "fcf": cfo,  # operating cash flow as FCF proxy (capex not broken out on screener)
            "fcf_conversion": (cfo / np_ * 100.0) if (cfo is not None and np_) else None,
        })
    yearly.reverse()  # latest-first

    if not quarterly and not yearly:
        return None

    # Light financial-health read (the heavy fin_health path was lost with yfinance).
    fin_health_score, verdict, alerts = _fin_health(quarterly, yearly)
    return {
        "quarterly": quarterly,
        "yearly": yearly,
        "fin_health_score": fin_health_score,
        "fin_health_verdict": verdict,
        "fin_alerts": alerts,
        "source": "screener.in",
    }


def _fin_health(quarterly: list, yearly: list):
    """Minimal 0-100 health read from screener data (profitability + growth + margin)."""
    score, alerts = 0, []
    latest_q = quarterly[0] if quarterly else {}
    if latest_q.get("net_income") is not None:
        if latest_q["net_income"] > 0:
            score += 35
        else:
            alerts.append("Latest quarter net loss")
    if latest_q.get("net_margin") is not None and latest_q["net_margin"] > 8:
        score += 20
    if len(yearly) >= 2 and yearly[0].get("net_income_growth_yoy") is not None:
        g = yearly[0]["net_income_growth_yoy"]
        if g > 15:
            score += 25
        elif g > 0:
            score += 12
        else:
            alerts.append("Profit declining YoY")
    if yearly and yearly[0].get("fcf") is not None and yearly[0]["fcf"] > 0:
        score += 20
    score = min(100, score)
    verdict = "Strong" if score >= 70 else "Healthy" if score >= 50 else "Watch" if score >= 30 else "Stressed"
    return score, verdict, alerts
