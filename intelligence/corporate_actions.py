"""
corporate_actions.py — NSE corporate actions (dividends / bonus / splits / buyback / AGM)
==========================================================================================
NSE-ONLY (no BSE). Free, no API key. Reuses the repo's NSE session-cookie pattern
(homepage warm-up -> JSON API with browser headers).

Bulk-fetches the equity corporate-actions calendar once (cached 6h) and indexes by
symbol, so a full scan does NOT make one HTTP call per stock.

Feeds:
  * extract_detailed_financials() smart cache invalidation — a dividend/result/board
    event near the cache date forces a financials refresh.
  * the research/intelligence events surface (upcoming ex-dates).
"""

import re
import time
import logging
import threading
from datetime import datetime, timedelta

import requests

log = logging.getLogger("screener")

_NSE_HOME = "https://www.nseindia.com"
_CA_URL = "https://www.nseindia.com/api/corporates-corporateActions"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-actions",
}

_CACHE_TTL = 6 * 3600
_cache = {}          # {symbol: [action dicts]}
_cache_ts = 0.0
_lock = threading.Lock()

_DIV = re.compile(r"divid", re.I)
_BONUS = re.compile(r"bonus", re.I)
_SPLIT = re.compile(r"split|sub-?division|face\s*value", re.I)
_BUYBACK = re.compile(r"buy\s*-?\s*back", re.I)
_RIGHTS = re.compile(r"rights", re.I)
_AGM = re.compile(r"\bAGM\b|annual general", re.I)
_RESULT = re.compile(r"result|board meeting|quarterly", re.I)


def classify_action(subject: str) -> str:
    s = subject or ""
    if _DIV.search(s):
        return "dividend"
    if _BONUS.search(s):
        return "bonus"
    if _SPLIT.search(s):
        return "split"
    if _BUYBACK.search(s):
        return "buyback"
    if _RIGHTS.search(s):
        return "rights"
    if _RESULT.search(s):
        return "result"
    if _AGM.search(s):
        return "agm"
    return "other"


def _parse_date(s):
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d", "%d-%m-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _normalize(item: dict) -> dict:
    subject = (item.get("subject") or "").strip()
    return {
        "symbol": (item.get("symbol") or "").upper().strip(),
        "company": item.get("comp") or "",
        "action_type": classify_action(subject),
        "subject": subject[:200],
        "ex_date": _parse_date(item.get("exDate")),
        "record_date": _parse_date(item.get("recDate")),
        "series": item.get("series") or "",
        "face_value": item.get("faceVal") or "",
    }


def _nse_session() -> requests.Session:
    s = requests.Session()
    try:
        s.get(_NSE_HOME, headers=_HEADERS, timeout=10)
        time.sleep(0.5)  # NSE needs a beat after the cookie handshake
    except Exception:
        pass
    return s


def fetch_all_corporate_actions(from_date: str = None, to_date: str = None,
                                force: bool = False) -> dict:
    """Bulk-fetch NSE equity corporate actions for a window; returns {symbol: [actions]}.

    Default window = today-30d .. today+60d (recent + upcoming). Cached 6h.
    Dates are DD-MM-YYYY (NSE format). On failure, returns the last good cache (or {}).
    """
    global _cache, _cache_ts
    now = time.time()
    if not force and _cache and (now - _cache_ts) < _CACHE_TTL:
        return _cache

    today = datetime.now()
    fd = from_date or (today - timedelta(days=30)).strftime("%d-%m-%Y")
    td = to_date or (today + timedelta(days=60)).strftime("%d-%m-%Y")
    url = f"{_CA_URL}?index=equities&from_date={fd}&to_date={td}"

    try:
        s = _nse_session()
        r = s.get(url, headers=_HEADERS, timeout=12)
        if r.status_code != 200:
            log.debug("[corp-actions] NSE HTTP %s", r.status_code)
            return _cache or {}
        data = r.json()
        rows = data.get("data") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return _cache or {}
        idx = {}
        for it in rows:
            a = _normalize(it)
            if a["symbol"]:
                idx.setdefault(a["symbol"], []).append(a)
        with _lock:
            _cache = idx
            _cache_ts = time.time()
        log.info("[corp-actions] NSE: %d actions across %d symbols", len(rows), len(idx))
        return idx
    except Exception as exc:
        log.debug("[corp-actions] fetch failed: %s", exc)
        return _cache or {}


def get_corporate_actions(symbol: str) -> list:
    """All cached corporate actions for one NSE symbol (latest window)."""
    sym = (symbol or "").upper().replace(".NS", "").strip()
    return fetch_all_corporate_actions().get(sym, [])


def get_upcoming_events(symbol: str) -> list:
    """Upcoming corporate events as [{event, date, type}] (ex-date >= today),
    in the shape extract_detailed_financials() expects for smart invalidation."""
    today = datetime.now().strftime("%Y-%m-%d")
    out = []
    for a in get_corporate_actions(symbol):
        d = a.get("ex_date") or a.get("record_date")
        if d and d >= today:
            out.append({"event": a.get("subject") or a.get("action_type"),
                        "date": d, "type": a.get("action_type")})
    out.sort(key=lambda e: e["date"])
    return out
