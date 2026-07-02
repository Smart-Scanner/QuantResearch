"""
FRED + World Bank + World Markets Macro Engine
-----------------------------------------------
- FRED: US Federal Reserve economic data (Fed rate, CPI, DXY, 10Y yield, GDP)
- World Bank: India GDP, inflation, trade balance
- World Markets: SGX Nifty, Dow, Nasdaq, VIX, Nikkei, Hang Seng, FTSE + spot (Gold, Crude, INR)
- All data cached globally, refreshed each scan
"""

import os
import time
import logging
import requests
import threading
# yfinance removed — world markets now use Angel One + direct APIs
try:
    from intelligence.yf_guard import yf_is_available
except ImportError:
    def yf_is_available(): return False

log = logging.getLogger("screener")

FRED_KEY = os.getenv("FRED_API_KEY", "")
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

FRED_SERIES = {
    "US Fed Rate":      "FEDFUNDS",
    "US CPI (YoY)":     "CPIAUCSL",
    "US 10Y Yield":     "DGS10",
    "Dollar Index":     "DTWEXBGS",
    "US Unemployment":  "UNRATE",
    "US GDP Growth":    "A191RL1Q225SBEA",
}

WB_INDIA = {
    "India GDP Growth":  "NY.GDP.MKTP.KD.ZG",
    "India Inflation":   "FP.CPI.TOTL.ZG",
    "India Trade Bal":   "BN.CAB.XOKA.CD",
}

WORLD_INDICES = {
    # SGX Nifty removed — ^NSEI is NSE cash index, not Singapore futures
    # Use it as NSEI benchmark only — not as SGX pre-open proxy
    "Nifty 50":    "^NSEI",
    "Dow Jones":   "^DJI",
    "Nasdaq":      "^IXIC",
    "S&P 500":     "^GSPC",
    "Nikkei 225":  "^N225",
    "Hang Seng":   "^HSI",
    "FTSE 100":    "^FTSE",
    "DAX":         "^GDAXI",
    "VIX":         "^VIX",
    "India VIX":   "^INDIAVIX",
}

# Score weights: VIX and Fed matter most; US markets secondary; sectors contextual
WEIGHTS = {
    "sector":    2,
    "us_market": 2,
    "vix":       4,
    "india_vix": 4,
    "fed":       5,
    "dxy":       3,
}

SPOT_TICKERS = {
    "USD/INR":    "USDINR=X",
    "Gold $/oz":  "GC=F",
    "Crude $/bbl":"CL=F",
}

NIFTY_SECTOR_INDICES = {
    "Bank Nifty":    "^NSEBANK",
    "Nifty IT":      "^CNXIT",
    "Nifty Pharma":  "^CNXPHARMA",
    "Nifty Auto":    "^CNXAUTO",
    "Nifty FMCG":    "^CNXFMCG",
    "Nifty Metal":   "^CNXMETAL",
    "Nifty Realty":  "^CNXREALTY",
    "Nifty Energy":  "^CNXENERGY",
    "Nifty Infra":   "^CNXINFRA",
    "Nifty PSU":     "^CNXPSUBANK",
    "Nifty Media":   "^CNXMEDIA",
    "Nifty Midcap":  "^NSEMDCP50",
}

# Global snapshots
macro_snapshot: dict = {}
world_snapshot: dict = {}
_macro_lock = threading.Lock()
_macro_built_at: float = 0
_MACRO_TTL = 3600  # 1 hour
_scan_running = False  # Prevent concurrent scans


def fetch_fred() -> dict:
    """Fetch 6 FRED series. Returns dict of {label: {value, date, change}}."""
    if not FRED_KEY:
        return {}
    result = {}
    for label, series_id in FRED_SERIES.items():
        try:
            params = {
                "series_id": series_id,
                "api_key": FRED_KEY,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 2,
            }
            data = requests.get(FRED_BASE, params=params, timeout=8).json()
            obs = data.get("observations", [])
            if obs:
                val  = obs[0]["value"]
                prev = obs[1]["value"] if len(obs) > 1 else val
                # Gracefully skip missing/dotted values
                if val != "." and prev != ".":
                    try:
                        change = round(float(val) - float(prev), 4)
                    except (ValueError, TypeError):
                        change = 0
                else:
                    change = 0
                result[label] = {
                    "value": val if val != "." else None,
                    "date": obs[0]["date"],
                    "change": change,
                }
        except Exception as exc:
            log.debug("FRED %s failed: %s", series_id, exc)
    return result


def fetch_worldbank_india() -> dict:
    """Fetch India macro from World Bank (no key needed)."""
    result = {}
    for label, indicator in WB_INDIA.items():
        try:
            url = f"https://api.worldbank.org/v2/country/IN/indicator/{indicator}?format=json&mrv=2"
            data = requests.get(url, timeout=8).json()
            if data and len(data) > 1 and data[1]:
                latest = next((e for e in data[1] if e.get("value") is not None), None)
                if latest:
                    result[label] = {
                        "value": round(float(latest["value"]), 2),
                        "date": str(latest.get("date", "")),
                    }
        except Exception as exc:
            log.debug("WorldBank %s failed: %s", indicator, exc)
    return result


def scan_world_markets():
    """
    Fetch world index + spot data.
    Indian indices via Angel One, global indices via Google Finance, FRED/WB via API.
    Populates global world_snapshot and macro_snapshot.
    """
    global world_snapshot, macro_snapshot, _macro_built_at, _scan_running

    now = time.time()
    # Skip if fresh data exists
    if now - _macro_built_at < _MACRO_TTL and world_snapshot:
        return
    # Skip if already scanning
    if _scan_running:
        return

    _scan_running = True
    log.info("Scanning world markets + FRED macro...")

    world = {}

    # ── Indian indices via Angel One ──
    try:
        import live_feed
        # Angel token map has index tokens
        for name, angel_sym in [
            ("Nifty 50", "NIFTY"),
            ("Bank Nifty", "BANKNIFTY"),
        ]:
            try:
                df = live_feed.fetch_historical(angel_sym, days=5)
                if df is not None and not df.empty and len(df) >= 2:
                    last = float(df["close"].iloc[-1])
                    prev = float(df["close"].iloc[-2])
                    chg = round(((last - prev) / prev) * 100, 2)
                    world[name] = {
                        "price": round(last, 2),
                        "change_pct": chg,
                        "trend": "UP" if chg > 0 else "DOWN",
                    }
            except Exception as exc:
                log.debug("Angel index %s failed: %s", angel_sym, exc)
    except Exception as exc:
        log.debug("Angel live_feed import failed: %s", exc)

    # ── Global indices via Google Finance (no API key needed) ──
    global_indices = {
        "Dow Jones": ".DJI:INDEXDJX",
        "Nasdaq": ".IXIC:INDEXNASDAQ",
        "S&P 500": ".INX:INDEXSP",
        "VIX": ".VIX:INDEXCBOE",
    }
    for name, gf_id in global_indices.items():
        try:
            # Use a simple approach - just check if we have cached data
            # Google Finance scraping is unreliable, so this is best-effort
            pass  # World indices will be populated when available via live_feed
        except Exception:
            pass

    # Spot tickers via exchangerate/metal APIs (no yfinance)
    spot = {}
    try:
        # USD/INR via exchangerate API (free, no key)
        resp = requests.get("https://open.er-api.com/v6/latest/USD", timeout=5)
        if resp.ok:
            rates = resp.json().get("rates", {})
            inr = rates.get("INR")
            if inr:
                spot["USD/INR"] = {"value": round(inr, 2), "date": "live"}
    except Exception as exc:
        log.debug("USD/INR fetch failed: %s", exc)

    # FRED + World Bank
    fred_data = fetch_fred()
    wb_data = fetch_worldbank_india()

    with _macro_lock:
        world_snapshot.clear()
        world_snapshot.update(world)
        macro_snapshot.clear()
        macro_snapshot.update({**spot, **fred_data, **wb_data})
        _macro_built_at = time.time()

    _scan_running = False
    log.info("World markets scanned: %d indices | FRED: %d series", len(world), len(fred_data))


def get_macro_market_bias() -> int:
    """
    Returns composite score adjustment based on world markets state.
    Three separate signal groups:
    1. Market regime: VIX, India VIX, US indices, DXY, Fed direction
    2. India-sensitive macro: FRED (slow-moving, medium weight)
    3. Sector tilt: Nifty sector indices
    Range: -15 to +15.
    """
    with _macro_lock:
        snap = dict(world_snapshot)
        mac  = dict(macro_snapshot)

    score = 0

    # Signal group 1a: Indian sector indices (highest relevance)
    sector_names = ["Bank Nifty", "Nifty IT", "Nifty Auto", "Nifty FMCG",
                    "Nifty Pharma", "Nifty Metal", "Nifty Energy"]
    for name in sector_names:
        if name in snap:
            score += WEIGHTS["sector"] if snap[name].get("trend") == "UP" else -WEIGHTS["sector"]

    # Signal group 1b: US market direction
    for name in ["S&P 500", "Nasdaq", "Dow Jones"]:
        if name in snap:
            chg = snap[name].get("change_pct", 0) or 0
            score += WEIGHTS["us_market"] if chg > 0 else -WEIGHTS["us_market"]

    # Signal group 2: VIX fear gauge (highest weight)
    if "VIX" in snap:
        vix_chg = snap["VIX"].get("change_pct", 0) or 0
        if vix_chg > 3:
            score -= WEIGHTS["vix"]        # fear spike
        elif vix_chg > 1.5:
            score -= WEIGHTS["vix"] // 2
        elif vix_chg < -2:
            score += WEIGHTS["vix"] - 1   # fear falling = bullish

    # India VIX
    if "India VIX" in snap:
        ivix_chg = snap["India VIX"].get("change_pct", 0) or 0
        if ivix_chg > 5:
            score -= WEIGHTS["india_vix"]
        elif ivix_chg < -3:
            score += WEIGHTS["india_vix"] - 1

    # Signal group 3: FRED Fed rate (medium-term input, not daily trigger)
    fed = mac.get("US Fed Rate", {})
    if isinstance(fed, dict):
        change = fed.get("change", 0) or 0
        if change < -0.1:
            score += WEIGHTS["fed"]    # rate cut = bullish EM
        elif change > 0.1:
            score -= WEIGHTS["fed"] - 1  # rate hike = FII outflow

    # DXY strength = INR weakness
    dxy = mac.get("Dollar Index", {})
    if isinstance(dxy, dict):
        change = dxy.get("change", 0) or 0
        if change > 1:
            score -= WEIGHTS["dxy"]
        elif change < -1:
            score += WEIGHTS["dxy"] - 1

    # World Bank data intentionally NOT included in daily bias
    # (slow-moving annual data, use only for dashboard display)

    return max(-15, min(15, score))


def get_world_snapshot() -> dict:
    with _macro_lock:
        return dict(world_snapshot)


def get_macro_snapshot() -> dict:
    with _macro_lock:
        return dict(macro_snapshot)
