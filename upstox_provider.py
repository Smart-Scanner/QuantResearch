"""
upstox_provider.py — Upstox Data Provider (v3 market-data)
==========================================================
Integrates Upstox as a RESEARCH data provider for historical OHLCV, so it can
run as a 3rd parallel provider alongside Angel PROVIDER_1 / PROVIDER_2.

Duck-typed to the BrokerProvider interface (data_provider.py) exactly like
fyers_provider.py — it implements name/role/state/in_use/stats and
login()/fetch_historical()/_do_fetch()/_handle_failure(), and returns candle
rows in the Angel-compatible shape: [[iso_ts, open, high, low, close, volume], ...].

Why Upstox for a single-user local tool:
  * Analytics access-token = generated once, NO daily re-auth (unlike Angel TOTP).
  * Daily historical candles cover ~1 year (matches DATA_LOOKBACK_DAYS).
  * Same account also exposes Company Fundamentals / Corporate Actions / News
    (wired separately under intelligence/).

Env vars:
  UPSTOX_ACCESS_TOKEN   = analytics/standard access token (Bearer)        [required]
  UPSTOX_API_KEY        = app api key (optional; only needed to mint tokens)
  UPSTOX_INSTRUMENTS_URL= override the NSE instruments dump (optional)
  DISABLE_UPSTOX=1      = skip Upstox discovery entirely

Instrument keys are ISIN-based ("NSE_EQ|INE...") so we load Upstox's NSE
instruments master once and map trading_symbol -> instrument_key.
"""

import os
import io
import gzip
import json
import time
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote

import requests

log = logging.getLogger("screener")

UPSTOX_BASE = "https://api.upstox.com/v2"   # non-deprecated endpoints (e.g. user/profile)
UPSTOX_V3 = "https://api.upstox.com/v3"     # market-data (v2 candle/quote/feed APIs are deprecated)
DEFAULT_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
_INSTR_CACHE_PATH = os.path.join(os.path.dirname(__file__), "cache", "upstox_instruments.json")


class UpstoxProvider:
    """Upstox historical data provider compatible with ProviderManager."""

    def __init__(self, name: str, config: dict):
        self.name = name
        self.role = config.get("ROLE", "RESEARCH").upper()
        self.access_token = config.get("ACCESS_TOKEN", "")
        self.api_key = config.get("API_KEY", "")
        self.instruments_url = config.get("INSTRUMENTS_URL", DEFAULT_INSTRUMENTS_URL)

        # State tracking (string form, like FyersProvider)
        self.state = "ACTIVE"
        self.in_use = False

        from data_provider import ProviderStats
        self.stats = ProviderStats()

        # trading_symbol(upper) -> instrument_key, lazily loaded
        self._instruments: Optional[dict] = None
        self._instr_lock = threading.Lock()

    # ── auth ────────────────────────────────────────────────────────────────
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
        }

    def login(self) -> bool:
        """Validate the access token via the user-profile endpoint."""
        if not self.access_token:
            log.error("[%s] UPSTOX_ACCESS_TOKEN not set", self.name)
            self.state = "FAILED"
            return False
        try:
            r = requests.get(f"{UPSTOX_BASE}/user/profile", headers=self._headers(), timeout=15)
            if r.status_code == 200 and r.json().get("status") == "success":
                self.state = "ACTIVE"
                name = r.json().get("data", {}).get("user_name", "?")
                log.info("[%s] ✅ Upstox login OK — %s", self.name, name)
                return True
            log.warning("[%s] Upstox profile check failed: HTTP %s %s",
                        self.name, r.status_code, r.text[:160])
            self.state = "FAILED"
            return False
        except Exception as exc:
            log.error("[%s] Upstox login failed: %s", self.name, exc)
            self.state = "FAILED"
            return False

    # ── instrument map ──────────────────────────────────────────────────────
    def _ensure_instruments(self) -> dict:
        """Lazily load the NSE_EQ trading_symbol -> instrument_key map (cached)."""
        if self._instruments is not None:
            return self._instruments
        with self._instr_lock:
            if self._instruments is not None:
                return self._instruments
            mapping = self._download_instruments() or self._load_cached_instruments() or {}
            self._instruments = mapping
            log.info("[%s] Upstox instrument map ready (%d NSE_EQ symbols)", self.name, len(mapping))
            return mapping

    def _download_instruments(self) -> Optional[dict]:
        try:
            r = requests.get(self.instruments_url, timeout=30)
            if r.status_code != 200:
                log.warning("[%s] instruments download HTTP %s", self.name, r.status_code)
                return None
            raw = r.content
            try:
                raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            except OSError:
                pass  # already decompressed
            records = json.loads(raw)
            mapping = {}
            for it in records:
                if it.get("segment") == "NSE_EQ" and it.get("instrument_type") == "EQ":
                    sym = (it.get("trading_symbol") or "").upper().strip()
                    key = it.get("instrument_key")
                    if sym and key:
                        mapping[sym] = key
            if mapping:
                try:
                    os.makedirs(os.path.dirname(_INSTR_CACHE_PATH), exist_ok=True)
                    with open(_INSTR_CACHE_PATH, "w", encoding="utf-8") as f:
                        json.dump(mapping, f)
                except Exception:
                    pass
            return mapping or None
        except Exception as exc:
            log.warning("[%s] instruments download failed: %s", self.name, exc)
            return None

    def _load_cached_instruments(self) -> Optional[dict]:
        try:
            with open(_INSTR_CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _instrument_key(self, symbol: str) -> Optional[str]:
        if not symbol:
            return None
        return self._ensure_instruments().get(symbol.upper().strip())

    # ── historical fetch ─────────────────────────────────────────────────────
    def fetch_historical(self, symboltoken: str, exchange: str = "NSE",
                         fromdate: str = None, todate: str = None,
                         interval: str = "ONE_DAY") -> Optional[list]:
        if self.role == "EXECUTION":
            log.critical("[ROLE_VIOLATION] %s cannot fetch historical data!", self.name)
            raise RuntimeError(f"[{self.name}] ROLE_VIOLATION: EXECUTION provider cannot fetch historical!")
        if self.state != "ACTIVE":
            return None
        return self._do_fetch(symboltoken, exchange, fromdate, todate, interval)

    def _do_fetch(self, symboltoken: str, exchange: str,
                  fromdate: str = None, todate: str = None,
                  interval: str = "ONE_DAY") -> Optional[list]:
        start = time.time()
        try:
            # Angel uses numeric tokens; reverse-lookup the symbol, then map to
            # the Upstox ISIN-based instrument_key.
            import live_feed
            symbol = live_feed.get_symbol(symboltoken)
            if not symbol:
                log.debug("[%s] cannot reverse-lookup symbol for token=%s", self.name, symboltoken)
                return None
            instrument_key = self._instrument_key(symbol)
            if not instrument_key:
                log.debug("[%s] no Upstox instrument_key for %s", self.name, symbol)
                return None

            # v3 uses (unit, interval) pairs; the v2 candle endpoints are deprecated.
            unit_map = {"ONE_DAY": ("days", "1"), "ONE_HOUR": ("hours", "1"),
                        "THIRTY_MINUTE": ("minutes", "30"), "ONE_MINUTE": ("minutes", "1")}
            unit, ivl_num = unit_map.get(interval, ("days", "1"))

            to_date = (todate[:10] if todate else datetime.now().strftime("%Y-%m-%d"))
            from_date = (fromdate[:10] if fromdate
                         else (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d"))

            # v3 path order: .../{instrument_key}/{unit}/{interval}/{to_date}/{from_date}; key has a '|'.
            url = (f"{UPSTOX_V3}/historical-candle/"
                   f"{quote(instrument_key, safe='')}/{unit}/{ivl_num}/{to_date}/{from_date}")
            r = requests.get(url, headers=self._headers(), timeout=20)
            latency_ms = (time.time() - start) * 1000

            if r.status_code == 429:
                log.warning("[%s] rate limited for %s", self.name, symbol)
                self._handle_failure(is_429=True)
                return None
            if r.status_code != 200:
                log.debug("[%s] fetch HTTP %s for %s", self.name, r.status_code, symbol)
                self._handle_failure(is_429=False)
                return None

            payload = r.json()
            candles = (payload.get("data") or {}).get("candles") or []
            if not candles:
                return None  # success but empty (no data) — not an error

            # Upstox candle: [ts(ISO8601), open, high, low, close, volume, oi]
            # Angel/Fyers expect oldest-first [ts, open, high, low, close, volume].
            result = [[c[0], c[1], c[2], c[3], c[4], c[5]] for c in candles]
            result.sort(key=lambda row: row[0])  # ISO8601 sorts chronologically (robust to API order)
            self.stats.record_success(latency_ms)
            return result or None

        except Exception as exc:
            log.error("[%s] exception fetching token=%s: %s", self.name, symboltoken, exc)
            self._handle_failure(is_429=False)
            return None

    def _handle_failure(self, is_429: bool):
        self.stats.record_failure(is_429=is_429)
        if self.stats.consecutive_failures >= 10:
            log.warning("[%s] 10 consecutive failures! Triggering 60s COOLDOWN.", self.name)
            self.state = "COOLDOWN"
            self.stats.cooldown_until = datetime.now() + timedelta(seconds=60)


# ─── Discovery + Registration ───────────────────────────────────────────────

def _resolve_token(env_name: str, meta_key: str) -> str:
    token = os.getenv(env_name, "")
    if token:
        return token
    try:
        import db
        return db.get_meta(meta_key) or ""
    except Exception:
        return ""


def discover_upstox_providers() -> dict:
    """Discover Upstox provider(s) from env / db meta. Returns {name: UpstoxProvider}.

    Primary account: UPSTOX_ACCESS_TOKEN (or db meta 'upstox_access_token').
    Extra accounts: UPSTOX_2_ACCESS_TOKEN, UPSTOX_3_ACCESS_TOKEN, ...
    Returns {} when no token is configured (Upstox simply not used).
    """
    providers = {}
    instruments_url = os.getenv("UPSTOX_INSTRUMENTS_URL", DEFAULT_INSTRUMENTS_URL)

    token = _resolve_token("UPSTOX_ACCESS_TOKEN", "upstox_access_token")
    if token:
        providers["UPSTOX_1"] = UpstoxProvider("UPSTOX_1", {
            "ACCESS_TOKEN": token,
            "API_KEY": os.getenv("UPSTOX_API_KEY", ""),
            "INSTRUMENTS_URL": instruments_url,
            "ROLE": "RESEARCH",
        })
        log.info("[UpstoxDiscovery] Found Upstox provider: UPSTOX_1")

    for i in range(2, 10):
        token_i = _resolve_token(f"UPSTOX_{i}_ACCESS_TOKEN", f"upstox_{i}_access_token")
        if token_i:
            name = f"UPSTOX_{i}"
            providers[name] = UpstoxProvider(name, {
                "ACCESS_TOKEN": token_i,
                "API_KEY": os.getenv(f"UPSTOX_{i}_API_KEY", ""),
                "INSTRUMENTS_URL": instruments_url,
                "ROLE": "RESEARCH",
            })
            log.info("[UpstoxDiscovery] Found Upstox provider: %s", name)

    return providers
