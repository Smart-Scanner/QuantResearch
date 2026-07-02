"""
Angel One SmartAPI WebSocket Live Feed
Real-time tick data for Smart Screener
"""

import os
import json
import time
import random
import logging
import threading
from pathlib import Path
from datetime import datetime, date, timedelta, timezone

import pyotp
import requests
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2
from metrics.timer import timed
import angel_throttle as at
# yf_guard stubs — yfinance removed, Angel/Fyers only
try:
    from intelligence.yf_guard import yf_is_available, yf_record_failure, yf_record_success, get_yf_session, get_yf_ticker
except ImportError:
    def yf_is_available(): return False
    def yf_record_failure(**kw): pass
    def yf_record_success(): pass
    def get_yf_session(): return None
    def get_yf_ticker(s, **kw): return None

log = logging.getLogger("live_feed")

# --- jugaad_data fresh-fallback circuit breaker (additive, GOAL #2) ---
# When the Angel chain (live -> cache -> stale) yields insufficient fresh data,
# fetch_historical falls back to jugaad_data so the symbol still gets analyzed.
# A lightweight module-level circuit breaker disables the fallback after N
# consecutive failures within a process so a blocked NSE is not hammered.
JUGAAD_FALLBACK_MAX_FAILS = int(os.environ.get("JUGAAD_FALLBACK_MAX_FAILS", "5"))
_jugaad_fallback_ok = True
_jugaad_fallback_fails = 0
_jugaad_fallback_lock = threading.Lock()
# Index/non-EQ symbols jugaad's EQ stock_df cannot serve — skip them entirely so the
# macro/benchmark warmup (NIFTY/BANKNIFTY) never trips the circuit breaker.
_JUGAAD_SKIP_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYBEES",
                        "SENSEX", "BANKEX", "NIFTY50", "NIFTYNXT50"}


def _jugaad_pick_col(raw, *names):
    """Return the first matching column from a jugaad frame (handles both the
    friendly schema DATE/OPEN/... and the raw NSE schema CH_TIMESTAMP/CH_*)."""
    for n in names:
        if n in raw.columns:
            return raw[n]
    return None

ENV_FILE = Path(__file__).parent / ".env"
TOKEN_FILE = Path(__file__).parent / "cache" / "angel_tokens.json"

_angel_accounts = []
_active_account_idx = 0
_account_lock = threading.Lock()

_smart_api = None # For legacy reference, though we use get_smart_api()
_auth_token = None
_feed_token = None
_last_login = 0

_token_map = {}
_reverse_map = {}
_session_lock = threading.Lock()

_live_prices = {}
_prices_lock = threading.Lock()

_subscribers = set()
_ws_thread = None
_ws_running = False
_sws = None

_correlation_id = "smartscanner"
_WS_MODE = 2  # 2 = Quote Mode (contains open, high, low, close, volume)
MAX_WS_TOKENS_PER_SESSION = 1000
MAX_WS_BATCH_SIZE = 50

REST_GAP_SECONDS = 0.5
_hist_lock = threading.Lock()
_hist_last_call = 0.0

# Phase 4: Dynamic throttling state — tracks recent Angel API 429 failures
_angel_429_count = 0
_angel_429_window_start = 0.0
_ANGEL_429_WINDOW_SECS = 600  # 10 minute sliding window
_ANGEL_429_LOCK = threading.Lock()


def get_active_account():
    with _account_lock:
        if not _angel_accounts:
            return None
        return _angel_accounts[_active_account_idx]

def switch_account(reason=""):
    global _active_account_idx, _smart_api, _feed_token
    with _account_lock:
        if not _angel_accounts:
            return False
        _active_account_idx = (_active_account_idx + 1) % len(_angel_accounts)
        acct = _angel_accounts[_active_account_idx]
        _smart_api = acct["smart_api"]
        _feed_token = acct["feed_token"]
        log.warning("Switched to Angel Account %d due to: %s", acct['id'], reason)
        return True

def get_smart_api():
    acct = get_active_account()
    return acct["smart_api"] if acct else None

def _record_429():
    acct = get_active_account()
    if not acct: return
    now = time.time()
    if now - acct["429_window_start"] > _ANGEL_429_WINDOW_SECS:
        acct["429_count"] = 0
        acct["429_window_start"] = now
    acct["429_count"] += 1
    log.warning("Account %d hit 429 rate limit (count: %d).", acct['id'], acct['429_count'])
    if acct["429_count"] >= 3:
        switch_account(reason="Rate Limit Exceeded (3+ 429s)")

def get_dynamic_rest_gap() -> float:
    acct = get_active_account()
    if not acct: return REST_GAP_SECONDS
    now = time.time()
    if now - acct["429_window_start"] > _ANGEL_429_WINDOW_SECS:
        return REST_GAP_SECONDS
    count = acct["429_count"]
    if count <= 0: return REST_GAP_SECONDS
    elif count <= 2: return 1.0
    elif count <= 4: return 1.5
    else: return 2.0

# ── P0: Angel Reauth Storm Lock ─────────────────────────────────────────
# When AG8001 (Invalid Token) is detected by multiple threads simultaneously,
# they must NOT all call _login(). This lock + cooldown prevents login storms.
_reauth_lock = threading.Lock()
_last_reauth_time = 0.0
_last_reauth_success = False
_REAUTH_COOLDOWN_SECS = 60  # Minimum seconds between re-auth attempts


def force_reauth(reason: str = "unknown") -> bool:
    """Force a re-authentication with storm prevention.

    Only one thread can re-auth at a time. If another reauth happened
    within the last 60 seconds, the current request is skipped.
    Tracks success state for observability.

    Returns True if reauth succeeded, False otherwise.
    """
    global _last_reauth_time, _last_reauth_success
    with _reauth_lock:
        now = time.time()
        if now - _last_reauth_time < _REAUTH_COOLDOWN_SECS:
            log.info("[REAUTH_SKIP] reason=%s — last reauth was %.0fs ago (cooldown=%ds, last_success=%s)",
                     reason, now - _last_reauth_time, _REAUTH_COOLDOWN_SECS, _last_reauth_success)
            return _last_reauth_success

        log.warning("[REAUTH_START] reason=%s — initiating forced re-login", reason)
        _last_reauth_time = now

        # Increment reauth counter
        try:
            import db
            db.increment_mem_counter("angel_reauth_count")
        except Exception:
            pass

        # Force fresh login by clearing existing session
        pass
        _auth_token = None
        _feed_token = None
        _last_login = 0

        success = _login()
        _last_reauth_success = success

        log.info("[REAUTH_COMPLETE] reason=%s success=%s", reason, success)
        return success

_IST = timezone(timedelta(hours=5, minutes=30))

def _load_env():
    global _angel_accounts
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
        log.info("Loaded .env file from %s", ENV_FILE)

    # Parse Angel One accounts — only load LIVEFEED providers (not RESEARCH)
    # Provider 1&2 = RESEARCH (used by data_provider.py)
    # Provider 3 = LIVEFEED (used here for WebSocket + live prices)
    _angel_accounts = []
    
    # Check for numbered accounts — skip RESEARCH providers
    for i in range(1, 10):
        ak = os.environ.get(f"PROVIDER_{i}_API_KEY") or os.environ.get(f"ANGEL_API_KEY_{i}")
        if not ak:
            continue
        role = os.environ.get(f"PROVIDER_{i}_ROLE", "").upper()
        if role == "RESEARCH":
            log.info("Skipping PROVIDER_%d (ROLE=RESEARCH, reserved for data_provider)", i)
            continue
        _angel_accounts.append({
            "id": i,
            "api_key": ak,
            "client_id": os.environ.get(f"PROVIDER_{i}_CLIENT_ID", "") or os.environ.get(f"ANGEL_CLIENT_ID_{i}", ""),
            "mpin": os.environ.get(f"PROVIDER_{i}_MPIN", "") or os.environ.get(f"ANGEL_MPIN_{i}", ""),
            "totp_secret": os.environ.get(f"PROVIDER_{i}_TOTP_SECRET", "") or os.environ.get(f"PROVIDER_{i}_TOTP", "") or os.environ.get(f"ANGEL_TOTP_SECRET_{i}", ""),
            "smart_api": None,
            "last_login": 0,
            "429_count": 0,
            "429_window_start": 0.0,
            "login_failures": 0,
            "cooldown_until": 0.0,
            "circuit_broken": False,
            "circuit_error": "",
            "feed_token": None
        })
            
    # Fallback to single account
    if not _angel_accounts:
        ak = os.environ.get("PROVIDER_3_API_KEY") or os.environ.get("ANGEL_API_KEY")
        if ak:
            _angel_accounts.append({
                "id": 1,
                "api_key": ak,
                "client_id": os.environ.get("PROVIDER_3_CLIENT_ID", "") or os.environ.get("ANGEL_CLIENT_ID", ""),
                "mpin": os.environ.get("PROVIDER_3_MPIN", "") or os.environ.get("ANGEL_MPIN", ""),
                "totp_secret": os.environ.get("PROVIDER_3_TOTP_SECRET", "") or os.environ.get("PROVIDER_3_TOTP", "") or os.environ.get("ANGEL_TOTP_SECRET", ""),
                "smart_api": None,
                "last_login": 0,
                "429_count": 0,
                "429_window_start": 0.0,
                "login_failures": 0,
                "cooldown_until": 0.0,
                "circuit_broken": False,
                "circuit_error": "",
                "feed_token": None
            })

    log.info("Loaded %d Angel One accounts for load balancing", len(_angel_accounts))

_load_env()

def load_token_map():
    global _token_map, _reverse_map
    if TOKEN_FILE.exists():
        try:
            _token_map = json.loads(TOKEN_FILE.read_text())
            _reverse_map = {v: k for k, v in _token_map.items()}
            log.info("Loaded %d symbol tokens", len(_token_map))
            _inject_index_tokens()
            return
        except Exception as exc:
            log.warning("Token file load failed: %s", exc)
    refresh_token_map()

def refresh_token_map():
    global _token_map, _reverse_map
    try:
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        data = requests.get(url, timeout=30).json()
        nse_eq = [d for d in data if d.get("exch_seg") == "NSE" and d.get("symbol", "").endswith("-EQ")]
        _token_map = {d["symbol"].replace("-EQ", ""): d["token"] for d in nse_eq}
        _reverse_map = {v: k for k, v in _token_map.items()}
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(json.dumps(_token_map))
        log.info("Refreshed %d symbol tokens", len(_token_map))
    except Exception as exc:
        log.error("Token refresh failed: %s", exc)
    _inject_index_tokens()


def _inject_index_tokens():
    """Add NIFTY/BANKNIFTY and other well-known index tokens to the in-memory map.
    
    The Angel ScripMaster only contains -EQ (equity) symbols. Index tokens like
    NIFTY (26000) and BANKNIFTY (26009) are missing, which causes
    'Could not resolve token' errors and cascading failures in RRG/sector rotation.
    """
    global _token_map, _reverse_map
    try:
        from constants.index_tokens import ANGEL_INDEX_TOKENS
        INDEX_ALIASES = {
            "NIFTY": ANGEL_INDEX_TOKENS.get("NIFTY_50", "26000"),
            "NIFTY50": ANGEL_INDEX_TOKENS.get("NIFTY_50", "26000"),
            "NIFTY_50": ANGEL_INDEX_TOKENS.get("NIFTY_50", "26000"),
            "BANKNIFTY": ANGEL_INDEX_TOKENS.get("Bank Nifty", "26009"),
            "BANK_NIFTY": ANGEL_INDEX_TOKENS.get("Bank Nifty", "26009"),
            "NIFTYBEES": _token_map.get("NIFTYBEES", ""),  # Keep existing if present
        }
        added = 0
        for name, token in INDEX_ALIASES.items():
            if token and name not in _token_map:
                _token_map[name] = token
                _reverse_map[token] = name
                added += 1
        if added:
            log.info("Injected %d index tokens (NIFTY, BANKNIFTY, etc.)", added)
    except Exception as exc:
        log.warning("Failed to inject index tokens: %s", exc)


def get_token(symbol: str):
    import db
    if not _token_map:
        load_token_map()
    resolved = db.resolve_symbol(symbol)
    return _token_map.get(resolved.upper().replace(".NS", ""))

def get_symbol(token: str):
    return _reverse_map.get(str(token))

_circuit_lock = threading.Lock()

def reset_login_circuit_breaker(force_retry=True):
    with _circuit_lock:
        for acct in _angel_accounts:
            acct["login_failures"] = 1 if force_retry else 0
            acct["cooldown_until"] = 0.0
            acct["circuit_broken"] = False
            acct["circuit_error"] = ""
        log.info("Angel login circuit breaker has been reset for all %d accounts.", len(_angel_accounts))
        try:
            import db
            db.set_meta("angel_login_status", {
                "status": "reset",
                "failures": 1 if force_retry else 0,
                "message": "Reset (Single retry allowed)",
                "circuit_broken": False,
                "cooldown_until": 0.0,
                "error_details": ""
            })
        except Exception:
            pass

def _login_account(acct):
    if not all([acct["api_key"], acct["client_id"], acct["mpin"], acct["totp_secret"]]):
        log.error("Account %d credentials missing", acct['id'])
        return False

    # ── Token-reuse: skip generateSession entirely if a fresh same-day session exists.
    # This is the primary defense against login storms — REST/WebSocket reuse the cached JWT.
    try:
        cached = at.load_session(acct["client_id"])
    except Exception:
        cached = None
    if cached and cached.get("jwt"):
        try:
            obj = SmartConnect(api_key=acct["api_key"])
            obj.setAccessToken(cached["jwt"])
            try:
                obj.setRefreshToken(cached.get("refresh", ""))
            except Exception:
                pass
            try:
                obj.feed_token = cached.get("feed", "")
            except Exception:
                pass
            acct["smart_api"] = obj
            acct["auth_token"] = cached["jwt"]
            acct["feed_token"] = cached.get("feed", "")
            acct["last_login"] = cached.get("login_ts", time.time())
            acct["login_failures"] = 0
            acct["cooldown_until"] = 0.0
            acct["circuit_broken"] = False
            acct["circuit_error"] = ""
            log.info("Angel One Account %d session reused from cache (no generateSession)", acct['id'])
            return True
        except Exception as exc:
            log.warning("Account %d cached-session reuse failed (%s) — falling back to login", acct['id'], exc)

    now = time.time()
    if acct["circuit_broken"]:
        log.warning("Account %d circuit broken. Error: %s", acct['id'], acct["circuit_error"])
        return False
    if now < acct["cooldown_until"]:
        log.warning("Account %d in cooldown for %d seconds", acct['id'], int(acct["cooldown_until"] - now))
        return False

    # ── Login governor: rate-limit fresh logins across processes (prevents login storms).
    try:
        allowed, wait_secs = at.login_allowed(acct["client_id"])
    except Exception:
        allowed, wait_secs = True, 0
    if not allowed:
        log.warning("Account %d login governed — not allowed yet (wait ~%ss)", acct['id'], int(wait_secs))
        return False

    # ── Login lease: only one logger-in per account at a time (cross-process).
    _lease = None
    try:
        _lease = at.acquire_login_lease(acct["client_id"], timeout=120)
    except Exception:
        _lease = None

    try:
        # Re-check cache after acquiring the lease — another holder may have just logged in.
        try:
            cached2 = at.load_session(acct["client_id"])
        except Exception:
            cached2 = None
        if cached2 and cached2.get("jwt"):
            try:
                obj = SmartConnect(api_key=acct["api_key"])
                obj.setAccessToken(cached2["jwt"])
                try:
                    obj.setRefreshToken(cached2.get("refresh", ""))
                except Exception:
                    pass
                try:
                    obj.feed_token = cached2.get("feed", "")
                except Exception:
                    pass
                acct["smart_api"] = obj
                acct["auth_token"] = cached2["jwt"]
                acct["feed_token"] = cached2.get("feed", "")
                acct["last_login"] = cached2.get("login_ts", time.time())
                acct["login_failures"] = 0
                acct["cooldown_until"] = 0.0
                acct["circuit_broken"] = False
                acct["circuit_error"] = ""
                log.info("Angel One Account %d session reused from cache after lease (no generateSession)", acct['id'])
                return True
            except Exception as exc:
                log.warning("Account %d post-lease cached reuse failed (%s) — proceeding to login", acct['id'], exc)

        try:
            at.note_login_attempt(acct["client_id"])
        except Exception:
            pass

        totp = pyotp.TOTP(acct["totp_secret"]).now()
        obj = SmartConnect(api_key=acct["api_key"])
        at.global_login_gate()  # serialize cross-account logins (avoid per-IP login storm)
        data = obj.generateSession(acct["client_id"], acct["mpin"], totp)
        if not data or not data.get("status"):
            err_msg = data.get("message") if data else "no response"
            log.error("Account %d login failed: %s", acct['id'], err_msg)
            acct["login_failures"] += 1
            if acct["login_failures"] == 1:
                acct["cooldown_until"] = now + 150
                acct["circuit_error"] = f"First login failed: {err_msg}"
            else:
                acct["circuit_broken"] = True
                acct["cooldown_until"] = now + 900
                acct["circuit_error"] = f"Multiple failures. Circuit broken. Error: {err_msg}"
            try:
                at.note_login_result(acct["client_id"], ok=False, rate_limited=at.is_rate_limited(err_msg))
            except Exception:
                pass
            return False

        acct["login_failures"] = 0
        acct["cooldown_until"] = 0.0
        acct["circuit_broken"] = False
        acct["circuit_error"] = ""
        acct["smart_api"] = obj
        acct["feed_token"] = obj.getfeedToken()
        acct["auth_token"] = data["data"]["jwtToken"]
        acct["last_login"] = time.time()
        try:
            at.save_session(
                acct["client_id"],
                jwt=data["data"]["jwtToken"],
                refresh=data["data"].get("refreshToken", ""),
                feed=obj.getfeedToken(),
            )
        except Exception as exc:
            log.debug("save_session failed for account %d: %s", acct['id'], exc)
        try:
            at.note_login_result(acct["client_id"], ok=True)
        except Exception:
            pass
        log.info("Angel One Account %d login successful", acct['id'])
        return True
    except Exception as exc:
        err_msg = str(exc)
        log.exception("Account %d login exception: %s", acct['id'], exc)
        acct["login_failures"] += 1
        if acct["login_failures"] == 1:
            acct["cooldown_until"] = now + 150
            acct["circuit_error"] = f"Exception: {err_msg}"
        else:
            acct["circuit_broken"] = True
            acct["cooldown_until"] = now + 900
            acct["circuit_error"] = f"Multiple exceptions. Circuit broken. Error: {err_msg}"
        try:
            at.note_login_result(acct["client_id"], ok=False, rate_limited=at.is_rate_limited(err_msg))
        except Exception:
            pass
        return False
    finally:
        if _lease is not None:
            try:
                at.release_login_lease(_lease)
            except Exception:
                pass

def _login():
    global _smart_api, _feed_token
    success = False
    for acct in _angel_accounts:
        if _login_account(acct):
            success = True
            
    if success:
        acct = get_active_account()
        if acct and acct["smart_api"]:
            _smart_api = acct["smart_api"]
            _feed_token = acct["feed_token"]
            
    return success

def ensure_session():
    with _session_lock:
        if not _angel_accounts:
            return False
        acct = get_active_account()
        if not acct or not acct["smart_api"] or (time.time() - acct["last_login"]) > 6 * 3600:
            return _login()
        return True

def is_market_open():
    now = datetime.now(_IST)
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return 555 <= mins <= 930

def get_live_prices(symbols=None):
    import db
    with _prices_lock:
        if symbols:
            res = {}
            for s in symbols:
                resolved = db.resolve_symbol(s)
                clean = resolved.upper().replace(".NS", "")
                if clean in _live_prices:
                    res[s] = _live_prices[clean].copy()
                    res[s]["symbol"] = s.upper()
            return res
        return {s: d.copy() for s, d in _live_prices.items()}

def get_live_price(symbol):
    import db
    resolved = db.resolve_symbol(symbol)
    clean = resolved.upper().replace(".NS", "")
    with _prices_lock:
        data = _live_prices.get(clean)
        if data:
            tick = data.copy()
            tick["symbol"] = symbol.upper()
            return tick

    # Fallback: return last scan price from DB so paper trades always have LTP
    try:
        scan_data = db.get_stock_from_results(clean)
        if scan_data and scan_data.get("price"):
            return {
                "symbol": symbol.upper(),
                "ltp": scan_data["price"],
                "change_pct": scan_data.get("pct_1w", 0),
                "last_update": scan_data.get("first_analysis_date", ""),
                "_source": "scan_fallback",
            }
    except Exception:
        pass

    return None

def seed_cache(symbols):
    """Fetch prices via REST API for symbols missing from WS cache and inject into _live_prices.
    Called once on paper-trades load so the GET /api/live-prices ticker always has data."""
    missing = []
    with _prices_lock:
        for s in symbols:
            clean = s.upper().replace(".NS", "")
            if clean not in _live_prices:
                missing.append(clean)
    if not missing:
        return
    try:
        fetched = fetch_ltp_bulk(missing)
        with _prices_lock:
            for sym, data in fetched.items():
                if sym not in _live_prices:
                    _live_prices[sym] = data
        if fetched:
            log.info("[SEED CACHE] Seeded %d/%d missing symbols into WS cache", len(fetched), len(missing))
    except Exception as exc:
        log.debug("[SEED CACHE] Failed: %s", exc)

def _on_data(wsapp, message):
    try:
        if not isinstance(message, dict):
            return
        token = str(message.get("token", ""))
        symbol = get_symbol(token)
        if not symbol:
            return

        ltp = float(message.get("last_traded_price", 0)) / 100
        close_price = float(message.get("closed_price", 0)) / 100
        open_price = float(message.get("open_price_of_the_day", 0)) / 100
        high_price = float(message.get("high_price_of_the_day", 0)) / 100
        low_price = float(message.get("low_price_of_the_day", 0)) / 100
        volume = int(message.get("volume_trade_for_the_day", 0))

        change = ltp - close_price if close_price > 0 else 0
        change_pct = round((change / close_price) * 100, 2) if close_price > 0 else 0

        tick_time = datetime.now(_IST)

        with _prices_lock:
            _live_prices[symbol] = {
                "symbol": symbol,
                "ltp": round(ltp, 2),
                "open": round(open_price, 2),
                "high": round(high_price, 2),
                "low": round(low_price, 2),
                "close": round(close_price, 2),
                "change": round(change, 2),
                "change_pct": change_pct,
                "volume": volume,
                "last_update": tick_time.isoformat(timespec="seconds"),
            }

        # Release 4: Fire execution engine tick (sub-second SL/Target evaluation)
        if ltp > 0:
            try:
                from execution_engine import on_tick
                on_tick(symbol, round(ltp, 2), tick_time)
            except Exception:
                pass  # Never block WebSocket thread

    except Exception as exc:
        log.debug("Tick parse error: %s", exc)

def _on_open(wsapp):
    log.info("WebSocket connected")
    # Symbols queued before the socket was live are already in _subscribers, so the
    # set-dedup in subscribe() would make new_syms empty and never send the actual
    # WS subscribe frame. Push the low-level subscribe directly so every pending
    # symbol is (re)subscribed on connect/reconnect and ticks actually flow.
    if _subscribers:
        _subscribe_symbols(list(_subscribers))

def _on_error(wsapp, error):
    log.warning("WebSocket error: %s", error)

def _on_close(wsapp, close_status_code=None, close_msg=None):
    global _ws_running
    log.info("WebSocket closed: %s %s", close_status_code, close_msg)
    _ws_running = False

def _subscribe_symbols(symbols):
    global _sws
    if not _sws:
        return

    clean_symbols = []
    for sym in symbols:
        s = sym.upper().replace(".NS", "")
        if get_token(s):
            clean_symbols.append(s)

    if not clean_symbols:
        return

    if len(clean_symbols) > MAX_WS_TOKENS_PER_SESSION:
        clean_symbols = clean_symbols[:MAX_WS_TOKENS_PER_SESSION]

    try:
        for i in range(0, len(clean_symbols), MAX_WS_BATCH_SIZE):
            batch_syms = clean_symbols[i:i + MAX_WS_BATCH_SIZE]
            batch_tokens = [get_token(s) for s in batch_syms if get_token(s)]
            if not batch_tokens:
                continue
            token_list = [{"exchangeType": 1, "tokens": batch_tokens}]
            _sws.subscribe(_correlation_id, _WS_MODE, token_list)
        log.info("Subscribed to %d symbols", len(clean_symbols))
    except Exception as exc:
        log.error("Subscribe error: %s", exc)

def subscribe(symbols):
    import db
    global _subscribers
    new_syms = set()
    for s in symbols:
        resolved = db.resolve_symbol(s)
        clean = resolved.upper().replace(".NS", "")
        if clean not in _subscribers and get_token(clean):
            _subscribers.add(clean)
            new_syms.add(clean)
    if _ws_running and new_syms:
        _subscribe_symbols(new_syms)

def start_websocket():
    global _ws_thread, _ws_running, _sws
    if _ws_running:
        return
    if not ensure_session():
        log.error("Cannot start WebSocket: login failed")
        return
    def _run():
        global _sws, _ws_running
        load_token_map()
        _ws_running = True
        while _ws_running:
            try:
                acct = get_active_account()
                _sws = SmartWebSocketV2(acct.get("auth_token", ""), acct["api_key"], acct["client_id"], acct.get("feed_token", ""))
                _sws.on_data = _on_data
                _sws.on_open = _on_open
                _sws.on_error = _on_error
                _sws.on_close = _on_close
                log.info("Starting WebSocket connection...")
                _sws.connect()
            except Exception as exc:
                log.error("WebSocket crashed: %s", exc)
            if _ws_running:
                time.sleep(5)

    _ws_thread = threading.Thread(target=_run, daemon=True)
    _ws_thread.start()
    log.info("WebSocket thread started")

def stop_websocket():
    global _ws_running, _sws
    _ws_running = False
    if _sws:
        try:
            _sws.close_connection()
        except Exception:
            pass

def _rest_gap():
    global _hist_last_call
    with _hist_lock:
        now = time.time()
        # Phase 4: Use dynamic gap that adapts to recent 429 failures
        gap = get_dynamic_rest_gap()
        wait = gap - (now - _hist_last_call)
        if wait > 0:
            time.sleep(wait)
        _hist_last_call = time.time()

def fetch_ltp_bulk(symbols: list[str]) -> dict:
    if not ensure_session():
        return {}
    results = {}
    _reauth_attempted = False  # P0: Only attempt reauth once per batch
    for sym in symbols:
        _rest_gap()
        clean = sym.upper().replace(".NS", "")
        token = get_token(clean)
        if not token:
            continue
        try:
            at.rest_acquire(get_active_account().get('client_id'))
            data = get_smart_api().ltpData("NSE", f"{clean}-EQ", token)

            # P0: AG8001 detection — reauth once per batch, then retry
            if not _reauth_attempted and data and data.get("errorcode") == "AG8001":
                log.warning("[AG8001] Invalid Token in LTP for %s — forcing reauth", clean)
                _reauth_attempted = True
                if force_reauth(reason=f"AG8001_fetch_ltp_{clean}"):
                    _rest_gap()
                    at.rest_acquire(get_active_account().get('client_id'))
                    data = get_smart_api().ltpData("NSE", f"{clean}-EQ", token)

            if data.get("status") and data.get("data"):
                d = data["data"]
                ltp = float(d.get("ltp", 0))
                close_price = float(d.get("close", 0))
                change = ltp - close_price if close_price else 0
                change_pct = round((change / close_price) * 100, 2) if close_price else 0
                results[clean] = {
                    "symbol": clean,
                    "ltp": ltp,
                    "open": float(d.get("open", 0)),
                    "high": float(d.get("high", 0)),
                    "low": float(d.get("low", 0)),
                    "close": close_price,
                    "change": round(change, 2),
                    "change_pct": change_pct,
                    "last_update": datetime.now().strftime("%H:%M:%S"),
                }
        except Exception as exc:
            log.debug("LTP fetch failed for %s: %s", clean, exc)
    return results

@timed("fetch_historical")
def fetch_historical(symbol: str, days: int = 90):
    import pandas as pd
    from historical_service import get_daily_history

    clean = symbol.upper().replace(".NS", "")

    # OPT-IN (USE_BHAVCOPY_HISTORY): source daily candles (incl "DELIVERY %")
    # from the bhavcopy history store instead of Angel. Lazy import to avoid
    # import cycles; any error / insufficient data falls through to the existing
    # Angel-then-jugaad path UNCHANGED. Flag OFF => byte-identical to before.
    try:
        import bhavcopy_history
        if bhavcopy_history.USE_BHAVCOPY_HISTORY:
            store_df = bhavcopy_history.get_history(clean, days)
            if store_df is not None and len(store_df) >= 50:
                return store_df
    except Exception as exc:
        log.error("bhavcopy_history lookup failed for %s: %s", clean, exc)

    token = get_token(clean)
    if not token:
        log.warning(f"Could not resolve token for {clean}")
        return None

    df = None
    try:
        data = get_daily_history(token, days=days, exchange="NSE")
        if data:
            rows = [{
                "DATE": pd.Timestamp(c[0]),
                "OPEN": float(c[1]),
                "HIGH": float(c[2]),
                "LOW": float(c[3]),
                "CLOSE": float(c[4]),
                "VOLUME": int(c[5]),
            } for c in data]

            df = pd.DataFrame(rows)
            if not df.empty:
                df["DATE"] = pd.to_datetime(df["DATE"]).dt.tz_localize(None)
            else:
                df = None
    except Exception as exc:
        log.error("Historical exception for %s via historical_service: %s", clean, exc)
        df = None

    # SUCCESS PATH UNTOUCHED: if the Angel chain returned enough fresh rows
    # (the analyzer's 50-row minimum), return it exactly as before.
    if df is not None and len(df) >= 50:
        return df

    # FRESH FALLBACK (GOAL #2): Angel chain (live/cache/stale) gave None or
    # insufficient rows. Try jugaad_data so the symbol still gets analyzed
    # instead of being silently lost. Guarded by an in-process circuit breaker.
    fallback_df = _fetch_historical_jugaad_fallback(clean, days)
    if fallback_df is not None and not fallback_df.empty:
        return fallback_df

    return df


def _fetch_historical_jugaad_fallback(clean: str, days: int):
    """Fresh alternative source when the Angel chain yields insufficient data.

    Reshapes jugaad_data.nse.stock_df into the SAME
    DATE/OPEN/HIGH/LOW/CLOSE/VOLUME (+ "DELIVERY %") DataFrame fetch_historical
    normally returns, so the analyzer stays provider-agnostic. Disabled after
    JUGAAD_FALLBACK_MAX_FAILS consecutive failures (circuit breaker); resets on
    any success.
    """
    global _jugaad_fallback_ok, _jugaad_fallback_fails

    import pandas as pd

    # Indices / non-EQ symbols can't be served by jugaad's EQ history — skip WITHOUT
    # counting as a failure (so benchmark/macro warmup never trips the breaker).
    if (clean or "").upper() in _JUGAAD_SKIP_SYMBOLS:
        return None

    with _jugaad_fallback_lock:
        if not _jugaad_fallback_ok:
            return None

    try:
        from jugaad_data.nse import stock_df
        from datetime import date as _date

        to_date = _date.today()
        from_date = to_date - timedelta(days=days)
        raw = stock_df(symbol=clean, from_date=from_date, to_date=to_date, series="EQ")

        if raw is None or raw.empty:
            raise ValueError("jugaad_data returned empty frame")

        # jugaad may return friendly columns (DATE/OPEN/...) OR raw NSE columns (CH_*).
        c_date = _jugaad_pick_col(raw, "DATE", "CH_TIMESTAMP")
        c_open = _jugaad_pick_col(raw, "OPEN", "CH_OPENING_PRICE")
        c_high = _jugaad_pick_col(raw, "HIGH", "CH_TRADE_HIGH_PRICE")
        c_low = _jugaad_pick_col(raw, "LOW", "CH_TRADE_LOW_PRICE")
        c_close = _jugaad_pick_col(raw, "CLOSE", "CH_CLOSING_PRICE")
        c_vol = _jugaad_pick_col(raw, "VOLUME", "CH_TOT_TRADED_QTY")
        c_deliv = _jugaad_pick_col(raw, "DELIVERY %", "COP_DELIV_PERC", "%DLY QT TO TRD QT")
        if any(c is None for c in (c_date, c_open, c_high, c_low, c_close, c_vol)):
            raise ValueError(f"jugaad columns unrecognized: {list(raw.columns)[:10]}")

        out = pd.DataFrame({
            "DATE": pd.to_datetime(c_date, errors="coerce").dt.tz_localize(None),
            "OPEN": pd.to_numeric(c_open, errors="coerce").astype(float),
            "HIGH": pd.to_numeric(c_high, errors="coerce").astype(float),
            "LOW": pd.to_numeric(c_low, errors="coerce").astype(float),
            "CLOSE": pd.to_numeric(c_close, errors="coerce").astype(float),
            "VOLUME": pd.to_numeric(c_vol, errors="coerce").fillna(0).astype("int64"),
        })
        if c_deliv is not None:
            out["DELIVERY %"] = pd.to_numeric(c_deliv, errors="coerce")

        out = out.dropna(subset=["DATE", "CLOSE"]).sort_values("DATE").reset_index(drop=True)

        with _jugaad_fallback_lock:
            _jugaad_fallback_fails = 0

        log.info(
            "Used jugaad_data FRESH fallback for %s (%d rows) — Angel chain returned insufficient fresh data",
            clean, len(out),
        )
        return out if not out.empty else None
    except Exception as exc:
        with _jugaad_fallback_lock:
            _jugaad_fallback_fails += 1
            if _jugaad_fallback_fails >= JUGAAD_FALLBACK_MAX_FAILS:
                _jugaad_fallback_ok = False
                log.warning(
                    "jugaad_data fallback CIRCUIT-BROKEN after %d consecutive failures "
                    "(JUGAAD_FALLBACK_MAX_FAILS=%d) — disabling for this process. Last error: %s",
                    _jugaad_fallback_fails, JUGAAD_FALLBACK_MAX_FAILS, exc,
                )
            else:
                log.info("jugaad_data fallback failed for %s (%d/%d): %s",
                         clean, _jugaad_fallback_fails, JUGAAD_FALLBACK_MAX_FAILS, exc)
        return None
