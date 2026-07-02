"""
Forex Factory Macro Events Engine
-----------------------------------
- Source: https://nfs.faireconomy.media/ff_calendar_thisweek.json
  (Free JSON endpoint, no API key, no scraping, no BeautifulSoup)
- Computes surprise scores: actual vs forecast delta
- Derives market regime: RISK_ON / MILD_BULLISH / NEUTRAL / MILD_BEARISH / RISK_OFF
- Applies sector-specific multipliers
"""

import os
import time
import logging
import requests
import threading
from datetime import datetime

log = logging.getLogger("screener")

FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# ──────────────────────────────────────────────────────────────
# Regime multipliers — NEUTRAL softened to 0.25 (not 0)
# ──────────────────────────────────────────────────────────────
REGIME_MULT = {
    "RISK_ON":      1.2,
    "MILD_BULLISH": 1.0,
    "NEUTRAL":      0.25,   # was 0 — neutral should dampen, not erase
    "MILD_BEARISH": -0.6,   # was -0.5
    "RISK_OFF":     -1.0,
}

# Normalized sector map — ensures Finance/Banking/Realty etc. match consistently
SECTOR_MAP = {
    "financial": "Finance", "finance": "Finance", "nbfc": "Finance",
    "banking": "Banking", "bank": "Banking",
    "it": "IT", "technology": "IT", "software": "IT", "tech": "IT",
    "pharma": "Pharma", "healthcare": "Pharma", "hospital": "Pharma",
    "auto": "Auto", "automobile": "Auto", "automotive": "Auto",
    "realty": "Realty", "real estate": "Realty", "construction": "Realty",
    "fmcg": "FMCG", "consumer": "FMCG",
    "metals": "Metals", "steel": "Metals", "mining": "Metals",
    "energy": "Energy", "oil": "Energy", "power": "Energy",
    "industrial": "Industrial", "capital goods": "Industrial",
    "infra": "Industrial", "infrastructure": "Industrial",
    "aviation": "Aviation", "logistics": "Logistics",
}

# High-impact events — title matched first, country secondary
HIGH_IMPACT_EVENTS = {
    "US CPI": {"surprise_bullish": 8, "surprise_bearish": -8,
               "sectors_bull": ["IT", "Pharma", "Banking", "Realty"],
               "sectors_bear": ["IT", "Realty", "Finance"]},
    "FOMC": {"surprise_bullish": 10, "surprise_bearish": -10,
             "sectors_bull": ["Banking", "Finance", "Realty", "Auto"],
             "sectors_bear": ["Finance", "Realty", "Auto"]},
    "Federal Funds Rate": {"surprise_bullish": 12, "surprise_bearish": -12,
                           "sectors_bull": ["Banking", "Realty", "Auto", "Finance"],
                           "sectors_bear": ["Finance", "Energy"]},
    "Non-Farm Payrolls": {"surprise_bullish": 5, "surprise_bearish": -5,
                          "sectors_bull": ["IT", "FMCG"],
                          "sectors_bear": []},
    "US GDP": {"surprise_bullish": 6, "surprise_bearish": -6,
               "sectors_bull": ["IT", "Metals", "Energy"],
               "sectors_bear": ["Metals", "Energy"]},
    "China PMI": {"surprise_bullish": 5, "surprise_bearish": -5,
                  "sectors_bull": ["Metals", "Mining", "Energy"],
                  "sectors_bear": ["Metals"]},
    "OPEC": {"surprise_bullish": 4, "surprise_bearish": -6,
             "sectors_bull": ["Energy"],
             "sectors_bear": ["Auto", "Aviation", "Logistics"]},
    "India CPI": {"surprise_bullish": 6, "surprise_bearish": -6,
                  "sectors_bull": ["Banking", "Realty", "Finance", "Auto"],
                  "sectors_bear": ["Banking", "Finance"]},
    "India GDP": {"surprise_bullish": 8, "surprise_bearish": -8,
                  "sectors_bull": ["Banking", "Finance", "Realty", "Industrial"],
                  "sectors_bear": []},
    "RBI Interest Rate": {"surprise_bullish": 12, "surprise_bearish": -8,
                          "sectors_bull": ["Banking", "Finance", "Realty", "Auto"],
                          "sectors_bear": ["Banking"]},
    "ECB Rate": {"surprise_bullish": 4, "surprise_bearish": -4,
                 "sectors_bull": ["IT", "Pharma"],
                 "sectors_bear": []},
}

# ──────────────────────────────────────────────────────────────
# Global cache
# ──────────────────────────────────────────────────────────────
_ff_cache: list = []
_ff_regime: str = "NEUTRAL"
_ff_score: int = 0
_ff_last_fetch: float = 0
_ff_lock = threading.Lock()
_FF_TTL = 3600 * 6  # 6 hours


def _safe_float(val):
    """Parse numeric string like '4.50%' or '0.25' to float."""
    if val is None:
        return None
    try:
        return float(str(val).replace("%", "").replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def scan_macro_events() -> dict:
    """
    Fetch Forex Factory calendar (one GET call).
    Returns {regime, score, events} and caches globally.
    """
    global _ff_cache, _ff_regime, _ff_score, _ff_last_fetch

    now = time.time()
    if now - _ff_last_fetch < _FF_TTL and _ff_cache:
        return {"regime": _ff_regime, "score": _ff_score, "events": _ff_cache}

    log.info("Fetching Forex Factory calendar...")
    events = []
    total_score = 0

    try:
        resp = requests.get(FF_CALENDAR_URL, timeout=8)
        if resp.status_code != 200:
            log.warning("Forex Factory returned %d", resp.status_code)
            return {"regime": "NEUTRAL", "score": 0, "events": []}

        calendar = resp.json()
        for item in calendar:
            title = item.get("title", "")
            impact = item.get("impact", "").lower()
            actual_raw = item.get("actual")
            forecast_raw = item.get("forecast")
            country_raw = item.get("country", "").upper().strip()
            country = country_raw[:3]  # normalize to 3-char

            # Accept USD, US, INR, IN, CNY, CN, EUR, EU
            if impact not in ("high",) or country not in (
                "USD", "US", "INR", "IN", "CNY", "CN", "EUR", "EU"
            ):
                continue

            actual = _safe_float(actual_raw)
            forecast = _safe_float(forecast_raw)

            surprise = None
            surprise_dir = None
            event_score = 0

            if actual is not None and forecast is not None:
                surprise = actual - forecast
                # For CPI/inflation: lower actual = positive surprise (better for markets)
                is_inflation = any(x in title for x in ["CPI", "Inflation", "PPI"])
                if is_inflation:
                    surprise_dir = "bullish" if surprise < 0 else "bearish"
                else:
                    surprise_dir = "bullish" if surprise > 0 else "bearish"

                # Match to known events
                for key, cfg in HIGH_IMPACT_EVENTS.items():
                    if key.lower() in title.lower():
                        # India events get 1.5x multiplier
                        mult = 1.5 if country == "INR" else 1.0
                        if surprise_dir == "bullish":
                            event_score = round(cfg["surprise_bullish"] * mult)
                        else:
                            event_score = round(cfg["surprise_bearish"] * mult)
                        break

            total_score += event_score
            events.append({
                "title": title,
                "country": country,
                "impact": impact,
                "actual": actual_raw,
                "forecast": forecast_raw,
                "surprise_dir": surprise_dir,
                "score": event_score,
                "date": item.get("date", ""),
                "time": item.get("time", ""),
            })

        # Derive regime
        if total_score >= 15:
            regime = "RISK_ON"
        elif total_score >= 5:
            regime = "MILD_BULLISH"
        elif total_score <= -15:
            regime = "RISK_OFF"
        elif total_score <= -5:
            regime = "MILD_BEARISH"
        else:
            regime = "NEUTRAL"

        with _ff_lock:
            _ff_cache = events
            _ff_regime = regime
            _ff_score = total_score
            _ff_last_fetch = time.time()

        log.info("Forex Factory: %d events | regime: %s | score: %+d", len(events), regime, total_score)
        return {"regime": regime, "score": total_score, "events": events}

    except Exception as exc:
        log.warning("Forex Factory failed: %s", exc)
        return {"regime": "NEUTRAL", "score": 0, "events": []}


def get_macro_event_score(sector: str = "") -> tuple:
    """
    Returns (score_adjustment, regime) for a given sector.
    Sector is normalized before matching so Finance/Banking/Realty etc. work consistently.
    Regime multiplier applied AFTER sector bonuses are added.
    """
    with _ff_lock:
        regime = _ff_regime
        base_score = _ff_score

    # Normalize input sector to canonical form
    sec_lower = sector.lower()
    norm_sector = SECTOR_MAP.get(sec_lower, sector)  # try exact
    if norm_sector == sector:  # try partial match
        for key, val in SECTOR_MAP.items():
            if key in sec_lower:
                norm_sector = val
                break

    # Sector-specific adjustments from event surprise matches
    sector_bonus = 0
    for event in _ff_cache:
        if event.get("surprise_dir") == "bullish" and event["score"] > 0:
            for key, cfg in HIGH_IMPACT_EVENTS.items():
                if key.lower() in event["title"].lower():
                    if norm_sector in cfg.get("sectors_bull", []):
                        sector_bonus += 3
        elif event.get("surprise_dir") == "bearish" and event["score"] < 0:
            for key, cfg in HIGH_IMPACT_EVENTS.items():
                if key.lower() in event["title"].lower():
                    if norm_sector in cfg.get("sectors_bear", []):
                        sector_bonus -= 3

    # Apply regime multiplier to base_score only, then add sector bonus
    mult = REGIME_MULT.get(regime, 0.25)
    total = round(base_score * mult * 0.1 + sector_bonus)

    return total, regime


def get_ff_regime() -> str:
    """Return current macro regime string."""
    with _ff_lock:
        return _ff_regime


def get_ff_events() -> list:
    """Return cached Forex Factory events."""
    with _ff_lock:
        return list(_ff_cache)
