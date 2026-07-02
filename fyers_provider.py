"""
fyers_provider.py — Fyers API v3 Data Provider
================================================
Integrates Fyers as a RESEARCH data provider for historical OHLCV.
Uses fyers-apiv3 SDK for OAuth2 + historical candles.

Fyers advantages over Angel:
- 200 req/min (vs Angel's 5/sec with frequent 429s)
- 1 lakh requests/day
- Cleaner API, less rate limiting
- Better historical depth

Env vars required:
  FYERS_APP_ID       = your_app_id (e.g., "ABC123-100")
  FYERS_SECRET_KEY   = your_secret_key
  FYERS_REDIRECT_URI = https://your-app.com/fyers/callback (or http://localhost:8080)
  FYERS_ACCESS_TOKEN = pre-generated access token (optional, for headless auth)

Auth flow: App generates auth_code → exchange for access_token → use for API calls.
For server/headless: pre-generate access_token and set as env var.
"""

import os
import time
import logging
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger("screener")

# ─── Fyers Provider (extends BrokerProvider interface) ───────────────────────

class FyersProvider:
    """
    Fyers data provider compatible with ProviderManager interface.
    Fetches historical OHLCV candle data via Fyers API v3.
    """

    def __init__(self, name: str, config: dict):
        self.name = name
        self.role = config.get("ROLE", "RESEARCH").upper()
        self.app_id = config.get("APP_ID", "")
        self.secret_key = config.get("SECRET_KEY", "")
        self.redirect_uri = config.get("REDIRECT_URI", "https://www.aismartscan.in/")
        self.access_token = config.get("ACCESS_TOKEN", "")

        # State tracking (compatible with ProviderManager)
        self.state = "ACTIVE"
        self.in_use = False

        # Stats
        from data_provider import ProviderStats
        self.stats = ProviderStats()

        # SDK instance
        self._fyers = None
        self._last_login = None

    def login(self) -> bool:
        """
        Initialize Fyers API model with access token.
        If ACCESS_TOKEN is set in env, uses it directly (headless mode).
        Otherwise, generates a new one via OAuth2 flow.
        """
        try:
            from fyers_apiv3 import fyersModel
        except ImportError:
            log.error("[%s] fyers-apiv3 not installed. Run: pip install fyers-apiv3", self.name)
            self.state = "FAILED"
            return False

        if not self.app_id:
            log.error("[%s] FYERS_APP_ID not set", self.name)
            self.state = "FAILED"
            return False

        # Option 1: Use pre-generated access token (recommended for server)
        if self.access_token:
            try:
                cache_path = os.path.join(os.path.dirname(__file__), "cache")
                os.makedirs(cache_path, exist_ok=True)
                self._fyers = fyersModel.FyersModel(
                    token=self.access_token,
                    is_async=False,
                    client_id=self.app_id,
                    log_path=cache_path
                )
                # Test connection
                profile = self._fyers.get_profile()
                if profile and profile.get("s") == "ok":
                    self.state = "ACTIVE"
                    self._last_login = datetime.now()
                    log.info("[%s] ✅ Fyers login OK (pre-token) — %s",
                             self.name, profile.get("data", {}).get("name", "?"))
                    return True
                else:
                    log.warning("[%s] Fyers profile check failed: %s", self.name, profile)
                    self.state = "FAILED"
                    return False
            except Exception as exc:
                log.error("[%s] Fyers login failed: %s", self.name, exc)
                self.state = "FAILED"
                return False

        # Option 2: Generate access token via OAuth2
        try:
            from fyers_apiv3 import fyersModel

            session = fyersModel.SessionModel(
                client_id=self.app_id,
                secret_key=self.secret_key,
                redirect_uri=self.redirect_uri,
                response_type="code",
                grant_type="authorization_code",
            )
            auth_url = session.generate_authcode()
            log.warning("[%s] Fyers OAuth2 URL (open in browser): %s", self.name, auth_url)
            log.warning("[%s] After login, set FYERS_ACCESS_TOKEN env var with the token", self.name)
            self.state = "FAILED"
            return False

        except Exception as exc:
            log.error("[%s] Fyers OAuth2 setup failed: %s", self.name, exc)
            self.state = "FAILED"
            return False

    def fetch_historical(self, symboltoken: str, exchange: str = "NSE",
                         fromdate: str = None, todate: str = None,
                         interval: str = "ONE_DAY") -> Optional[list]:
        """
        Fetch historical candle data via Fyers API.
        Returns data in Angel-compatible format: [[timestamp, open, high, low, close, volume], ...]
        """
        if self.role == "EXECUTION":
            log.critical("[ROLE_VIOLATION] %s cannot fetch historical data!", self.name)
            raise RuntimeError(f"[{self.name}] ROLE_VIOLATION: EXECUTION provider cannot fetch historical!")

        if self.state != "ACTIVE" or not self._fyers:
            return None

        return self._do_fetch(symboltoken, exchange, fromdate, todate, interval)

    def _do_fetch(self, symboltoken: str, exchange: str,
                  fromdate: str = None, todate: str = None,
                  interval: str = "ONE_DAY") -> Optional[list]:
        """
        Internal fetch. Converts Angel token to Fyers symbol format and calls API.
        """
        start_time = time.time()

        try:
            # Convert Angel symboltoken to Fyers symbol format
            # Angel uses token numbers, Fyers uses "NSE:SYMBOL-EQ"
            # We need to reverse-lookup the symbol from the token
            import live_feed
            symbol_name = live_feed.get_symbol(symboltoken)
            if not symbol_name:
                log.debug("[%s] Cannot reverse-lookup symbol for token=%s", self.name, symboltoken)
                return None

            fyers_symbol = f"NSE:{symbol_name}-EQ"

            # Map Angel interval to Fyers resolution
            resolution_map = {
                "ONE_DAY": "D",
                "ONE_HOUR": "60",
                "FIFTEEN_MINUTE": "15",
                "FIVE_MINUTE": "5",
                "ONE_MINUTE": "1",
            }
            resolution = resolution_map.get(interval, "D")

            # Parse dates
            if fromdate:
                range_from = fromdate[:10]  # "YYYY-MM-DD HH:MM" → "YYYY-MM-DD"
            else:
                range_from = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")

            if todate:
                range_to = todate[:10]
            else:
                range_to = datetime.now().strftime("%Y-%m-%d")

            data = {
                "symbol": fyers_symbol,
                "resolution": resolution,
                "date_format": "1",  # epoch timestamps
                "range_from": range_from,
                "range_to": range_to,
                "cont_flag": "1",
            }

            response = self._fyers.history(data=data)
            latency_ms = (time.time() - start_time) * 1000

            if response and response.get("s") == "ok" and response.get("candles"):
                candles = response["candles"]
                # Fyers returns: [[epoch, open, high, low, close, volume], ...]
                # Angel format:  [[timestamp, open, high, low, close, volume], ...]
                # Convert epoch to ISO string for compatibility with historical_service
                result = []
                for c in candles:
                    ts = datetime.fromtimestamp(c[0]).strftime("%Y-%m-%dT%H:%M:%S+05:30")
                    result.append([ts, c[1], c[2], c[3], c[4], c[5]])

                self.stats.record_success(latency_ms)
                return result if result else None

            elif response and response.get("code") == -16:
                # Rate limit
                log.warning("[%s] Rate limited for %s", self.name, fyers_symbol)
                self._handle_failure(is_429=True)
                return None
            else:
                err_msg = response.get("message", "unknown") if response else "no_response"
                log.debug("[%s] Fetch failed for %s: %s", self.name, fyers_symbol, err_msg)
                self._handle_failure(is_429=False)
                return None

        except Exception as exc:
            log.error("[%s] Exception fetching %s: %s", self.name, symboltoken, exc)
            self._handle_failure(is_429=False)
            return None

    def _handle_failure(self, is_429: bool):
        """Handle fetch failure with cooldown logic."""
        self.stats.record_failure(is_429=is_429)
        if self.stats.consecutive_failures >= 10:  # Higher threshold for Fyers (more reliable)
            log.warning("[%s] 10 consecutive failures! Triggering 60s COOLDOWN.", self.name)
            self.state = "COOLDOWN"
            self.stats.cooldown_until = datetime.now() + timedelta(seconds=60)


# ─── Discovery + Registration ───────────────────────────────────────────────

def discover_fyers_providers() -> dict:
    """
    Discover Fyers providers from environment variables.
    
    Env pattern:
      FYERS_APP_ID=...
      FYERS_SECRET_KEY=...
      FYERS_ACCESS_TOKEN=...
    
    Returns dict of {name: FyersProvider}
    """
    providers = {}

    # Single Fyers account
    app_id = os.getenv("FYERS_APP_ID", "")
    if app_id:
        import db
        access_token = os.getenv("FYERS_ACCESS_TOKEN", "")
        if not access_token:
            try:
                access_token = db.get_meta("fyers_access_token") or ""
                if access_token:
                    log.info("[FyersDiscovery] Loaded FYERS_1 access token from database meta")
            except Exception as exc:
                log.debug("[FyersDiscovery] Could not load token from db: %s", exc)

        config = {
            "APP_ID": app_id,
            "SECRET_KEY": os.getenv("FYERS_SECRET_KEY", ""),
            "REDIRECT_URI": os.getenv("FYERS_REDIRECT_URI", "https://www.aismartscan.in/"),
            "ACCESS_TOKEN": access_token,
            "ROLE": "RESEARCH",
        }
        providers["FYERS_1"] = FyersProvider("FYERS_1", config)
        log.info("[FyersDiscovery] Found Fyers provider: FYERS_1 (app_id=%s...)", app_id[:8])

    # Multiple Fyers accounts (FYERS_2_APP_ID, FYERS_3_APP_ID, etc.)
    for i in range(2, 10):
        app_id = os.getenv(f"FYERS_{i}_APP_ID", "")
        if app_id:
            import db
            access_token = os.getenv(f"FYERS_{i}_ACCESS_TOKEN", "")
            if not access_token:
                try:
                    access_token = db.get_meta(f"fyers_{i}_access_token") or ""
                    if access_token:
                        log.info("[FyersDiscovery] Loaded %s access token from database meta", f"FYERS_{i}")
                except Exception as exc:
                    log.debug("[FyersDiscovery] Could not load token from db for FYERS_%d: %s", i, exc)

            config = {
                "APP_ID": app_id,
                "SECRET_KEY": os.getenv(f"FYERS_{i}_SECRET_KEY", ""),
                "REDIRECT_URI": os.getenv(f"FYERS_{i}_REDIRECT_URI", "https://www.aismartscan.in/"),
                "ACCESS_TOKEN": access_token,
                "ROLE": "RESEARCH",
            }
            name = f"FYERS_{i}"
            providers[name] = FyersProvider(name, config)
            log.info("[FyersDiscovery] Found Fyers provider: %s", name)

    return providers
