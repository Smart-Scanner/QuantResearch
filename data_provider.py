import os
import time
import logging
from enum import Enum
from typing import Dict, List, Optional
from datetime import datetime, timedelta

import pyotp
from SmartApi import SmartConnect

import angel_throttle as at

class ProviderState(Enum):
    ACTIVE = "ACTIVE"
    COOLDOWN = "COOLDOWN"
    FAILED = "FAILED"

class ProviderStats:
    def __init__(self):
        self.success_count = 0
        self.failure_count = 0
        self.rate_limits_hit = 0
        self.avg_latency_ms = 0.0
        self.chunks_processed = 0
        self.symbols_processed = 0
        self.last_success_at = None
        self.consecutive_failures = 0
        self.cooldown_until = None
        
        # Internal for calculating moving average
        self._total_latency_ms = 0.0
        self._latency_samples = 0
        
    def record_success(self, latency_ms: float):
        self.success_count += 1
        self.symbols_processed += 1
        self.last_success_at = datetime.now().isoformat() + "Z"
        self.consecutive_failures = 0
        
        self._total_latency_ms += latency_ms
        self._latency_samples += 1
        self.avg_latency_ms = self._total_latency_ms / self._latency_samples
        
    def record_failure(self, is_429: bool = False):
        self.failure_count += 1
        self.consecutive_failures += 1
        if is_429:
            self.rate_limits_hit += 1

    def to_dict(self):
        return {
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "rate_limits_hit": self.rate_limits_hit,
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "chunks_processed": self.chunks_processed,
            "symbols_processed": self.symbols_processed,
            "last_success_at": self.last_success_at,
            "consecutive_failures": self.consecutive_failures,
            "cooldown_until": self.cooldown_until.isoformat() + "Z" if self.cooldown_until else None
        }

class BrokerProvider:
    def __init__(self, name: str, config: dict):
        self.name = name
        self.role = config.get("ROLE", "RESEARCH").upper()
        self.state = ProviderState.ACTIVE
        self.stats = ProviderStats()
        self.in_use = False  # Scheduler lock

    def login(self) -> bool:
        raise NotImplementedError()

    def fetch_historical(self, symboltoken: str, exchange: str = "NSE", fromdate: str = None, todate: str = None, interval: str = "ONE_DAY") -> Optional[list]:
        if self.role == "EXECUTION":
            msg = f"[{self.name}] FATAL: Cannot fetch historical data using an EXECUTION provider!"
            logging.critical(f"[ROLE_VIOLATION] {msg}")
            try:
                from db import audit_log
                audit_log("ROLE_VIOLATION", f"Provider {self.name}", f"historical_fetch for {symboltoken}")
            except Exception:
                pass
            raise RuntimeError(msg)
            
        if self.state == ProviderState.COOLDOWN:
            if self.stats.cooldown_until and datetime.now() > self.stats.cooldown_until:
                logging.info(f"[{self.name}] Cooldown expired. Recovering to ACTIVE.")
                self.state = ProviderState.ACTIVE
                self.stats.consecutive_failures = 0
                self.stats.cooldown_until = None
            else:
                return None
        
        if self.state == ProviderState.FAILED:
            return None

        return self._do_fetch(symboltoken, exchange, fromdate, todate, interval)

    def _do_fetch(self, symboltoken: str, exchange: str, fromdate: str = None, todate: str = None, interval: str = "ONE_DAY") -> Optional[list]:
        raise NotImplementedError()

    def _handle_failure(self, is_429: bool):
        self.stats.record_failure(is_429=is_429)
        if self.stats.consecutive_failures >= 5:
            logging.warning(f"[{self.name}] 5 consecutive failures! Triggering 60s COOLDOWN.")
            self.state = ProviderState.COOLDOWN
            self.stats.cooldown_until = datetime.now() + timedelta(seconds=60)


class AngelProvider(BrokerProvider):
    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self.api_key = config.get("API_KEY")
        self.client_id = config.get("CLIENT_ID")
        self.mpin = config.get("MPIN")
        self.totp_secret = config.get("TOTP")
        self.api = None

    def _reuse_cached_session(self, cached: dict) -> bool:
        """Rebuild a SmartConnect from a cached session WITHOUT generateSession.
        Returns True on success, marking the provider ACTIVE."""
        try:
            self.api = SmartConnect(api_key=self.api_key)
            self.api.setAccessToken(cached.get("jwt", ""))
            try:
                self.api.setRefreshToken(cached.get("refresh", ""))
            except Exception:
                pass
            feed = cached.get("feed", "")
            if feed:
                try:
                    self.api.feed_token = feed
                except Exception:
                    pass
            logging.info(f"[{self.name}] Reusing cached session (no generateSession).")
            self.state = ProviderState.ACTIVE
            return True
        except Exception as e:
            logging.error(f"[{self.name}] Failed to reuse cached session: {e}")
            self.api = None
            return False

    def login(self) -> bool:
        # 1) Cache-first: reuse a fresh, same-day session if available (NO login storm).
        try:
            cached = at.load_session(self.client_id)
        except Exception as e:
            logging.warning(f"[{self.name}] load_session error (ignored): {e}")
            cached = None
        if cached and self._reuse_cached_session(cached):
            return True

        # 2) Cache miss: serialize the actual login behind a cross-process lease.
        lease = None
        try:
            lease = at.acquire_login_lease(self.client_id, timeout=120)

            # Re-check cache after acquiring the lease — another worker may have
            # just logged in while we were waiting.
            try:
                cached = at.load_session(self.client_id)
            except Exception:
                cached = None
            if cached and self._reuse_cached_session(cached):
                return True

            # Login governor: do not hammer Angel auth if we're rate/attempt limited.
            try:
                allowed, wait_secs = at.login_allowed(self.client_id)
            except Exception as e:
                logging.warning(f"[{self.name}] login_allowed error (ignored): {e}")
                allowed, wait_secs = True, 0
            if not allowed:
                logging.warning(f"[{self.name}] Login not allowed yet (wait ~{wait_secs}s) — skipping.")
                # Leave state as-is so a later attempt can retry; do not mark FAILED.
                return False

            try:
                at.note_login_attempt(self.client_id)
            except Exception:
                pass

            self.api = SmartConnect(api_key=self.api_key)
            totp = pyotp.TOTP(self.totp_secret).now()
            at.global_login_gate()  # serialize cross-account logins (avoid per-IP login storm)
            res = self.api.generateSession(self.client_id, self.mpin, totp)
            if res and res.get("status"):
                # Extract tokens for caching/reuse.
                jwt = getattr(self.api, "access_token", None)
                if not jwt:
                    try:
                        jwt = res.get("data", {}).get("jwtToken", "")
                    except Exception:
                        jwt = ""
                try:
                    refresh = res.get("data", {}).get("refreshToken", "") or ""
                except Exception:
                    refresh = ""
                feed = ""
                try:
                    feed = self.api.getfeedToken() or ""
                except Exception:
                    feed = ""
                try:
                    at.save_session(self.client_id, jwt, refresh, feed)
                except Exception as e:
                    logging.warning(f"[{self.name}] save_session error (ignored): {e}")
                try:
                    at.note_login_result(self.client_id, ok=True)
                except Exception:
                    pass
                logging.info(f"[{self.name}] Logged in successfully.")
                self.state = ProviderState.ACTIVE
                return True
            else:
                logging.error(f"[{self.name}] Login failed: {res}")
                try:
                    at.note_login_result(self.client_id, ok=False,
                                         rate_limited=at.is_rate_limited(res))
                except Exception:
                    pass
                self.state = ProviderState.FAILED
                return False
        except Exception as e:
            logging.error(f"[{self.name}] Exception during login: {e}")
            try:
                at.note_login_result(self.client_id, ok=False,
                                     rate_limited=at.is_rate_limited(e))
            except Exception:
                pass
            self.state = ProviderState.FAILED
            return False
        finally:
            if lease is not None:
                try:
                    at.release_login_lease(lease)
                except Exception:
                    pass

    def _do_fetch(self, symboltoken: str, exchange: str, fromdate: str = None, todate: str = None, interval: str = "ONE_DAY") -> Optional[list]:
        start_time = time.time()
        try:
            if not self.api:
                logging.error(f"[{self.name}] Cannot fetch: api is None (login failed?)")
                return None
            if not fromdate:
                fromdate = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d %H:%M")
            if not todate:
                todate = datetime.now().strftime("%Y-%m-%d %H:%M")
            params = {
                "exchange": exchange,
                "symboltoken": symboltoken,
                "interval": interval,
                "fromdate": fromdate,
                "todate": todate
            }
            at.rest_acquire(self.client_id)
            res = self.api.getCandleData(params)

            # SELF-HEALING TOKEN: if the token is invalid/expired, clear the cached
            # session and attempt ONE governed re-login, then retry the call once.
            if (res and at.is_token_invalid(res.get("message", "") or res.get("errorcode", ""))):
                logging.warning(f"[{self.name}] Invalid/expired token detected — clearing session and re-logging in.")
                try:
                    at.clear_session(self.client_id)
                except Exception:
                    pass
                if self.login() and self.api:
                    at.rest_acquire(self.client_id)
                    res = self.api.getCandleData(params)

            latency_ms = (time.time() - start_time) * 1000

            if res and res.get("status") and res.get("data") is not None:
                data = res["data"]
                if len(data) > 0:
                    self.stats.record_success(latency_ms)
                    return data
                else:
                    # API returned SUCCESS but empty data (e.g., index tokens)
                    # This is "no data available", NOT an error
                    logging.debug(f"[{self.name}] No data for token={symboltoken} (SUCCESS but empty)")
                    return None
            elif res and res.get("errorcode") == "AB1019":
                logging.warning(f"[{self.name}] Rate limited (AB1019) for token {symboltoken}")
                self._handle_failure(is_429=True)
                return None
            else:
                err_code = res.get("errorcode", "?") if res else "no_response"
                err_msg = res.get("message", "") if res else "None"
                logging.warning(f"[{self.name}] Fetch FAILED for token={symboltoken}: errorcode={err_code} message={err_msg}")
                self._handle_failure(is_429=False)
                return None
        except Exception as e:
            msg = str(e)
            logging.error(f"[{self.name}] Exception fetching token={symboltoken}: {msg}")
            # SELF-HEALING TOKEN (exception path): clear session + ONE governed re-login + retry.
            if at.is_token_invalid(e):
                logging.warning(f"[{self.name}] Invalid/expired token (exception) — clearing session and re-logging in.")
                try:
                    at.clear_session(self.client_id)
                except Exception:
                    pass
                try:
                    if self.login() and self.api:
                        at.rest_acquire(self.client_id)
                        res = self.api.getCandleData(params)
                        if res and res.get("status") and res.get("data") is not None:
                            data = res["data"]
                            if len(data) > 0:
                                self.stats.record_success((time.time() - start_time) * 1000)
                                return data
                            return None
                except Exception as e2:
                    logging.error(f"[{self.name}] Re-login retry failed for token={symboltoken}: {e2}")
            if "Access denied" in msg or "access rate" in msg.lower():
                self._handle_failure(is_429=True)
            else:
                self._handle_failure(is_429=False)
            return None


class ProviderManager:
    def __init__(self):
        self.providers: Dict[str, BrokerProvider] = {}
        self._rr_index = 0  # round-robin cursor for RESEARCH providers

    def discover_providers(self):
        # ── Fyers Providers — skip if DISABLE_FYERS=1 or no FYERS_APP_ID ──
        fyers_disabled = os.getenv("DISABLE_FYERS", "1") == "1"
        fyers_app_id = os.getenv("FYERS_APP_ID", "")
        if not fyers_disabled and fyers_app_id:
            try:
                from fyers_provider import discover_fyers_providers
                fyers_providers = discover_fyers_providers()
                for name, provider in fyers_providers.items():
                    self.providers[name] = provider
                    logging.info(f"Registered Fyers provider: {name} (Role: RESEARCH)")
            except ImportError:
                logging.debug("fyers_provider module not available — skipping Fyers discovery")
            except Exception as e:
                logging.warning(f"Fyers discovery failed (non-fatal): {e}")
        else:
            logging.info("Fyers provider disabled (DISABLE_FYERS=1 or FYERS_APP_ID not set) — Angel Only mode")

        # ── Upstox Providers — skip if DISABLE_UPSTOX=1 or no UPSTOX_ACCESS_TOKEN ──
        # Registered BEFORE Angel so Upstox shares the RESEARCH round-robin pool
        # (Angel PROVIDER_1 + PROVIDER_2 + Upstox = 3-way parallel historical fetch).
        if os.getenv("DISABLE_UPSTOX", "0") != "1":
            try:
                from upstox_provider import discover_upstox_providers
                upstox_providers = discover_upstox_providers()
                for name, provider in upstox_providers.items():
                    self.providers[name] = provider
                    logging.info(f"Registered Upstox provider: {name} (Role: RESEARCH)")
                if not upstox_providers:
                    logging.info("Upstox provider not configured (no UPSTOX_ACCESS_TOKEN) — skipping")
            except ImportError:
                logging.debug("upstox_provider module not available — skipping Upstox discovery")
            except Exception as e:
                logging.warning(f"Upstox discovery failed (non-fatal): {e}")
        else:
            logging.info("Upstox provider disabled (DISABLE_UPSTOX=1)")

        # ── Angel Providers (fallback — existing logic) ──
        # Scan env for unique provider prefixes (by _TYPE or _API_KEY)
        prefixes = set()
        for key in os.environ:
            if key.startswith("PROVIDER_") and key.endswith("_TYPE"):
                prefix = key.replace("_TYPE", "")
                prefixes.add(prefix)

        # Fallback: if no _TYPE vars found, discover by _API_KEY
        # This handles the common case where .env has PROVIDER_1_API_KEY
        # but not PROVIDER_1_TYPE
        if not prefixes:
            for key in os.environ:
                if key.startswith("PROVIDER_") and key.endswith("_API_KEY"):
                    prefix = key.replace("_API_KEY", "")
                    prefixes.add(prefix)

        for prefix in sorted(list(prefixes)):
            ptype = os.getenv(f"{prefix}_TYPE", "ANGEL").upper()  # Default to ANGEL
            role = os.getenv(f"{prefix}_ROLE", "").upper()
            
            # Skip non-RESEARCH providers (LIVEFEED/EXECUTION are for live_feed.py)
            if role and role != "RESEARCH":
                logging.info(f"Skipping {prefix} (ROLE={role}, not RESEARCH)")
                continue
            
            config = {
                "ROLE": "RESEARCH",  # Only RESEARCH providers reach here
                "API_KEY": os.getenv(f"{prefix}_API_KEY", ""),
                "CLIENT_ID": os.getenv(f"{prefix}_CLIENT_ID", ""),
                "MPIN": os.getenv(f"{prefix}_MPIN", ""),
                "TOTP": os.getenv(f"{prefix}_TOTP_SECRET", "") or os.getenv(f"{prefix}_TOTP", "")
            }
            
            if ptype == "ANGEL":
                provider = AngelProvider(prefix, config)
                self.providers[prefix] = provider
                logging.info(f"Discovered {prefix} (Type: {ptype}, Role: RESEARCH)")
                
    def initialize_all(self):
        for name, p in self.providers.items():
            p.login()

    def acquire_active_provider(self, required_role="RESEARCH") -> Optional[BrokerProvider]:
        """
        Find an ACTIVE provider with the right role.

        RESEARCH providers: shared access, ROUND-ROBIN across all active providers
          (Angel PROVIDER_1 + PROVIDER_2 + Upstox) so historical fetch load is
          spread ~evenly instead of always hitting the first provider.
        EXECUTION providers: exclusive access (in_use mutex prevents concurrent orders).
        """
        now = datetime.now()
        active = []
        for name, p in self.providers.items():
            if not hasattr(p, 'state'):
                continue
            state_val = p.state.value if hasattr(p.state, 'value') else p.state
            # Recover expired cooldowns
            if state_val == "COOLDOWN" and p.stats.cooldown_until and now > p.stats.cooldown_until:
                logging.info(f"[{name}] Cooldown expired. Recovering to ACTIVE.")
                p.state = ProviderState.ACTIVE if hasattr(p.state, 'value') else "ACTIVE"
                p.stats.consecutive_failures = 0
                p.stats.cooldown_until = None
                state_val = "ACTIVE"
            if state_val == "ACTIVE" and p.role == required_role:
                active.append(p)

        if not active:
            return None

        if required_role == "RESEARCH":
            # Shared, read-only: round-robin to fan out across all active providers.
            idx = self._rr_index % len(active)
            self._rr_index = (self._rr_index + 1) % 1_000_000
            return active[idx]

        # EXECUTION: exclusive — first provider not currently in use.
        for p in active:
            if not p.in_use:
                p.in_use = True
                return p
        return None

    def release_provider(self, provider):
        """Release provider lock. Only meaningful for EXECUTION providers."""
        if hasattr(provider, 'in_use'):
            provider.in_use = False

    def get_telemetry(self) -> dict:
        telemetry = {}
        for name, p in self.providers.items():
            state_val = p.state.value if hasattr(p.state, 'value') else p.state
            telemetry[name] = {
                "state": state_val,
                "role": p.role,
                "in_use": p.in_use,
                **p.stats.to_dict()
            }
        return telemetry

provider_manager = ProviderManager()
provider_manager.discover_providers()
if provider_manager.providers:
    # STARTUP RELIABILITY: generateSession() (the Angel broker login) is a slow, occasionally
    # HANGING network call. Running it synchronously here — at module-import time — blocks
    # `import data_provider`, which blocks the entire app.py import, so gunicorn's worker never
    # finishes booting. Railway's health check then times out and SIGTERMs the container, producing
    # a restart loop where the app is never reachable ("Application not found" at the edge).
    # Fix: defer the login to a background daemon thread so the WSGI app imports immediately and the
    # web server is responsive at once. Functionality is preserved — _do_fetch() already guards
    # `if not self.api` (returns None until the async login sets it) and the auto-scan loop waits a
    # 60s startup grace, by which the login has long completed.
    import threading as _threading
    logging.info("Initializing %d data providers (login) in background...", len(provider_manager.providers))
    _threading.Thread(target=provider_manager.initialize_all, daemon=True, name="provider-login").start()


