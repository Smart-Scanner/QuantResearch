"""
Sector Rotation Engine — RRG (Relative Rotation Graph) Proxy
--------------------------------------------------------------
- Computes RS Ratio + RS Momentum for 12 Nifty sector indices vs NSEI
- Classifies into LEADING / IMPROVING / WEAKENING / LAGGING quadrants
- Cached globally, refreshed each scan (or every 1 hour)
"""

import time
import logging
import threading
from historical_service import get_daily_history
import pandas as pd
import pandas as pd

log = logging.getLogger("screener")

from constants.index_tokens import ANGEL_INDEX_TOKENS, BENCHMARK_TOKEN

BENCHMARK = BENCHMARK_TOKEN

# Map our sector display names → NSE ind_close_all "Index Name" values.
# Used ONLY when USE_BHAVCOPY_HISTORY is ON (flag-gated, additive).
NSE_INDEX_NAMES = {
    "NIFTY_50":      "Nifty 50",
    "Bank Nifty":    "Nifty Bank",
    "Nifty IT":      "Nifty IT",
    "Nifty Pharma":  "Nifty Pharma",
    "Nifty Auto":    "Nifty Auto",
    "Nifty FMCG":    "Nifty FMCG",
    "Nifty Metal":   "Nifty Metal",
    "Nifty Realty":  "Nifty Realty",
    "Nifty Energy":  "Nifty Energy",
    "Nifty Infra":   "Nifty Infrastructure",
    "Nifty PSU":     "Nifty PSE",
    "Nifty Media":   "Nifty Media",
    "Nifty Midcap":  "Nifty Midcap 100",
}
# Reverse map: Angel token → display name (for resolving NSE name from token).
_TOKEN_TO_NAME = {v: k for k, v in ANGEL_INDEX_TOKENS.items()}

NIFTY_SECTORS = {
    "Bank Nifty":    ANGEL_INDEX_TOKENS["Bank Nifty"],
    "Nifty IT":      ANGEL_INDEX_TOKENS["Nifty IT"],
    "Nifty Pharma":  ANGEL_INDEX_TOKENS["Nifty Pharma"],
    "Nifty Auto":    ANGEL_INDEX_TOKENS["Nifty Auto"],
    "Nifty FMCG":    ANGEL_INDEX_TOKENS["Nifty FMCG"],
    "Nifty Metal":   ANGEL_INDEX_TOKENS["Nifty Metal"],
    "Nifty Realty":  ANGEL_INDEX_TOKENS["Nifty Realty"],
    "Nifty Energy":  ANGEL_INDEX_TOKENS["Nifty Energy"],
    "Nifty Infra":   ANGEL_INDEX_TOKENS["Nifty Infra"],
    "Nifty PSU":     ANGEL_INDEX_TOKENS["Nifty PSU"],
    "Nifty Media":   ANGEL_INDEX_TOKENS["Nifty Media"],
    "Nifty Midcap":  ANGEL_INDEX_TOKENS["Nifty Midcap"],
}

# sector string → Nifty index name — ordered from most specific to least specific
# industrial/cement/defence removed from wrong mappings
SECTOR_TO_NIFTY = {
    # Banking / Finance
    "banking":          "Bank Nifty",
    "bank":             "Bank Nifty",
    "finance":          "Bank Nifty",
    "nbfc":             "Bank Nifty",
    # IT / Software
    "information technology": "Nifty IT",
    "technology":       "Nifty IT",
    "software":         "Nifty IT",
    "it":               "Nifty IT",
    # Pharma / Healthcare
    "pharmaceutical":   "Nifty Pharma",
    "healthcare":       "Nifty Pharma",
    "pharma":           "Nifty Pharma",
    "hospital":         "Nifty Pharma",
    # Auto
    "automobile":       "Nifty Auto",
    "automotive":       "Nifty Auto",
    "auto":             "Nifty Auto",
    # FMCG / Consumer
    "fmcg":             "Nifty FMCG",
    "consumer":         "Nifty FMCG",
    "household":        "Nifty FMCG",
    # Metals
    "metals":           "Nifty Metal",
    "steel":            "Nifty Metal",
    "mining":           "Nifty Metal",
    "aluminium":        "Nifty Metal",
    # Realty
    "real estate":      "Nifty Realty",
    "realty":           "Nifty Realty",
    # Energy
    "energy":           "Nifty Energy",
    "oil":              "Nifty Energy",
    "gas":              "Nifty Energy",
    # Infra (specific — railways/capital goods excluded)
    "infrastructure":   "Nifty Infra",
    "infra":            "Nifty Infra",
    # Media
    "media":            "Nifty Media",
    "entertainment":    "Nifty Media",
    "broadcast":        "Nifty Media",
}

sector_rotation_cache: dict = {}
_rrg_lock = threading.Lock()
_rrg_built_at: float = 0
_RRG_TTL = 3600  # 1 hour
_rrg_running = False  # Prevent concurrent scans


def compute_rrg_quadrant(rs_ratio: float, rs_momentum: float) -> str:
    if rs_ratio > 100 and rs_momentum > 100:
        return "LEADING 🟢"
    elif rs_ratio < 100 and rs_momentum > 100:
        return "IMPROVING 🟡"
    elif rs_ratio > 100 and rs_momentum < 100:
        return "WEAKENING 🟠"
    else:
        return "LAGGING 🔴"


def scan_sector_rotation():
    """
    Compute RRG for all 12 Nifty sector indices vs NSEI benchmark.
    Populates global sector_rotation_cache.
    """
    global sector_rotation_cache, _rrg_built_at, _rrg_running

    now = time.time()
    if now - _rrg_built_at < _RRG_TTL and sector_rotation_cache:
        return
    if _rrg_running:
        return

    _rrg_running = True
    log.info("Computing sector rotation (RRG)...")
    results = {}

    try:
        def _fetch_to_series(token):
            # Flag-gated: source index history from the broker-free EOD store.
            # Default OFF → byte-identical to the Angel path. Falls through on miss.
            try:
                import bhavcopy_history
                if bhavcopy_history.USE_BHAVCOPY_HISTORY:
                    name = _TOKEN_TO_NAME.get(token)
                    nse_name = NSE_INDEX_NAMES.get(name) if name else None
                    if nse_name:
                        idx_df = bhavcopy_history.get_index_history(nse_name, days=180)
                        if idx_df is not None and not idx_df.empty:
                            s = idx_df.copy()
                            s["DATE"] = pd.to_datetime(s["DATE"])
                            s.set_index("DATE", inplace=True)
                            ser = s["CLOSE"].astype(float).squeeze()
                            if not ser.empty:
                                return ser
            except Exception as exc:
                log.debug("RRG store fetch failed for token %s: %s — falling back", token, exc)

            data = get_daily_history(token, days=180, exchange="NSE")
            if not data: return pd.Series(dtype=float)
            df = pd.DataFrame(data, columns=["timestamp", "Open", "High", "Low", "Close", "Volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df.set_index("timestamp", inplace=True)
            return df["Close"].squeeze()

        bench = _fetch_to_series(BENCHMARK)
        if bench.empty or len(bench) < 20:
            log.warning("Benchmark data empty, skipping RRG")
            _rrg_running = False
            return

        for name, ticker in NIFTY_SECTORS.items():
            try:
                sec = _fetch_to_series(ticker)
                if sec.empty or len(sec) < 20:
                    continue

                # Align indices
                aligned = pd.concat([sec, bench], axis=1, keys=["sec", "bench"]).dropna()
                if len(aligned) < 20:
                    continue

                rs = aligned["sec"] / aligned["bench"]
                rs_mean = rs.rolling(20).mean()
                rs_ratio = float(rs.iloc[-1] / rs_mean.iloc[-1] * 100) if float(rs_mean.iloc[-1]) > 0 else 100

                rs_mom_raw = float(rs.pct_change(5).iloc[-1])
                rs_momentum = rs_mom_raw * 100 + 100

                quad = compute_rrg_quadrant(rs_ratio, rs_momentum)

                week_chg = 0.0
                if len(aligned) >= 6:
                    week_chg = float(((aligned["sec"].iloc[-1] - aligned["sec"].iloc[-6]) /
                                      aligned["sec"].iloc[-6]) * 100)

                results[name] = {
                    "rs_ratio": round(rs_ratio, 2),
                    "rs_momentum": round(rs_momentum, 2),
                    "quadrant": quad,
                    "week_change": round(week_chg, 2),
                    "ticker": ticker,
                }
                log.debug("RRG %s: ratio=%.1f mom=%.1f", name, rs_ratio, rs_momentum)
            except Exception as exc:
                log.debug("RRG %s failed: %s", name, exc)

    except Exception as exc:
        log.warning("Benchmark download failed: %s", exc)
        _rrg_running = False
        return

    with _rrg_lock:
        sector_rotation_cache.clear()
        sector_rotation_cache.update(results)
        _rrg_built_at = time.time()

    _rrg_running = False
    log.info("RRG computed: %d sectors", len(results))


def get_sector_rotation_score(sector: str) -> tuple:
    """
    Returns (score, quadrant_string) for a given sector name.
    Looks up sector → Nifty index name → cached RRG data.
    """
    sector_l = sector.lower()
    matched_index = None
    # Match longest keyword first for specificity (e.g. 'banking' before 'bank')
    for key in sorted(SECTOR_TO_NIFTY.keys(), key=len, reverse=True):
        if key in sector_l:
            matched_index = SECTOR_TO_NIFTY[key]
            break

    if not matched_index:
        return 0, "UNKNOWN"  # No bad fit — better to leave unranked

    with _rrg_lock:
        data = sector_rotation_cache.get(matched_index, {})

    if not data:
        return 0, "UNKNOWN"

    quad = data.get("quadrant", "UNKNOWN")
    if "LEADING" in quad:
        score = 15
    elif "IMPROVING" in quad:
        score = 8
    elif "WEAKENING" in quad:
        score = -5
    else:  # LAGGING
        score = -10

    return score, quad


def get_rrg_data() -> dict:
    """Return full RRG cache for API endpoint."""
    with _rrg_lock:
        return dict(sector_rotation_cache)
