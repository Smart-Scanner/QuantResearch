"""angel_throttle.py — Angel SmartAPI REST rate-limiting + login governance.

Single authority that keeps every Angel One account under its REST rate limit and
prevents login storms. Shared by the two Angel surfaces:
  * data_provider.py  — RESEARCH pool (getCandleData / historical)
  * live_feed.py      — LIVEFEED pool (ltpData) + login for the WebSocket

The live WebSocket DATA stream is EXEMPT from REST limits and must NOT be routed
through this module (only its one-time auth/login is governed here).

Guarantees, keyed PER ANGEL ACCOUNT (by client_id / provider name):
  1. REST calls are spaced >= ANGEL_REST_MIN_INTERVAL apart  (default 0.5s => <=2 req/s/account).
     Two accounts therefore run independently (up to ~4 req/s total), exactly as intended.
  2. generateSession (login) is throttled (min interval) and backed off exponentially on
     "exceeding access rate" / "Access denied" responses.
  3. The session token (jwt/refresh/feed) is cached to disk after a successful login and
     reused on the next process start, so app restarts and standalone scripts do NOT re-login.
  4. A cross-process file lease ensures only ONE process performs a login per account at a
     time; the others wait and then reuse the freshly cached token.

Single-process note: production is `gunicorn --workers 1` and run_local is one process, so
the in-memory limiter is authoritative within the box; the disk token-cache + file lease
additionally coordinate ACROSS separate process starts (e.g. web app + a CLI scan).
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

# ── Tunables (env-overridable) ─────────────────────────────────────────────
REST_MIN_INTERVAL = float(os.getenv("ANGEL_REST_MIN_INTERVAL", "0.5"))   # 0.5s => 2 req/s per account
LOGIN_MIN_INTERVAL = float(os.getenv("ANGEL_LOGIN_MIN_INTERVAL", "30"))  # >=30s between login attempts/account
SESSION_MAX_AGE = int(os.getenv("ANGEL_SESSION_MAX_AGE", "28800"))       # reuse cached token up to 8h (same day)
LOGIN_BACKOFF_CAP = float(os.getenv("ANGEL_LOGIN_BACKOFF_CAP", "900"))   # cap exponential cooldown at 15 min
_CACHE_DIR = Path(os.getenv("ANGEL_CACHE_DIR", "cache"))


# ── Per-account REST rate limiter (thread-safe min-interval) ───────────────
class PerKeyRateLimiter:
    """Calls sharing a key are spaced >= min_interval apart. Monotonic clock
    (immune to NTP/wall-clock steps). Thread-safe; one lock per key so different
    accounts never block each other."""

    def __init__(self, min_interval: float = REST_MIN_INTERVAL):
        self._min = float(min_interval)
        self._last: dict[str, float] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            lk = self._locks.get(key)
            if lk is None:
                lk = threading.Lock()
                self._locks[key] = lk
            return lk

    def acquire(self, key: str, min_interval: Optional[float] = None) -> float:
        """Block until this key is allowed to make a call. Returns seconds slept."""
        gap = self._min if min_interval is None else float(min_interval)
        key = key or "_default"
        with self._lock_for(key):
            now = time.monotonic()
            last = self._last.get(key, 0.0)
            wait = gap - (now - last)
            if wait > 0:
                time.sleep(wait)
            self._last[key] = time.monotonic()
            return wait if wait > 0 else 0.0


# Module singleton — ONE keyspace shared by both Angel subsystems so the same
# account is bounded even if it were ever used by both (it is not today, but this
# makes the guarantee structural, not config-dependent).
angel_rest_limiter = PerKeyRateLimiter(REST_MIN_INTERVAL)


def rest_acquire(account_key, min_interval: Optional[float] = None) -> float:
    """Throttle one Angel REST call for the given account. Call IMMEDIATELY before
    the REST request (getCandleData / ltpData). Never wrap the WebSocket stream."""
    return angel_rest_limiter.acquire(str(account_key or "_default"), min_interval)


# ── Error classification ───────────────────────────────────────────────────
def is_rate_limited(exc_or_msg) -> bool:
    s = str(exc_or_msg).lower()
    return (
        "exceeding access rate" in s
        or "access denied" in s
        or "access rate" in s
        or "ab1019" in s
        or "too many requests" in s
    )


def is_token_invalid(exc_or_msg) -> bool:
    """A reused/expired session token was rejected -> caller should re-login."""
    s = str(exc_or_msg).lower()
    return (
        "token missing" in s
        or "ag8003" in s
        or "ag8001" in s
        or "invalid token" in s
        or "token expired" in s
        or "invalid session" in s
    )


# ── Session token cache (reuse across process starts) ──────────────────────
def _session_path(client_id: str) -> Path:
    safe = "".join(c for c in str(client_id) if c.isalnum() or c in "-_") or "acct"
    return _CACHE_DIR / f"angel_session_{safe}.json"


def save_session(client_id: str, jwt: str, refresh: str = "", feed: str = "") -> None:
    """Persist a successful login so the next process can reuse it (no generateSession)."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "client_id": str(client_id),
            "jwt": jwt or "",
            "refresh": refresh or "",
            "feed": feed or "",
            "login_ts": time.time(),
        }
        p = _session_path(client_id)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(p)  # atomic on same filesystem
    except Exception:
        pass


def load_session(client_id: str, max_age: int = SESSION_MAX_AGE) -> Optional[dict]:
    """Return cached {jwt,refresh,feed,login_ts} if still fresh (within max_age AND
    same calendar day — Angel tokens expire around end of trading day), else None."""
    try:
        p = _session_path(client_id)
        if not p.exists():
            return None
        data = json.loads(p.read_text())
        ts = float(data.get("login_ts", 0))
        age = time.time() - ts
        if age < 0 or age > max_age:
            return None
        if time.strftime("%Y%m%d", time.localtime(ts)) != time.strftime("%Y%m%d"):
            return None
        if not data.get("jwt"):
            return None
        return data
    except Exception:
        return None


def clear_session(client_id: str) -> None:
    try:
        p = _session_path(client_id)
        if p.exists():
            p.unlink()
    except Exception:
        pass


# ── Login governor: per-account min-interval + exponential backoff ─────────
_login_state: dict[str, dict] = {}
_login_guard = threading.Lock()


def login_allowed(client_id: str) -> tuple[bool, float]:
    """(allowed, wait_secs). False while the account is in backoff or before the
    min interval since the last attempt has elapsed."""
    with _login_guard:
        st = _login_state.get(str(client_id), {})
        now = time.monotonic()
        cu = st.get("cooldown_until", 0.0)
        if now < cu:
            return False, cu - now
        last = st.get("last_attempt", 0.0)
        if last:
            wait = LOGIN_MIN_INTERVAL - (now - last)
            if wait > 0:
                return False, wait
        return True, 0.0


def note_login_attempt(client_id: str) -> None:
    with _login_guard:
        _login_state.setdefault(str(client_id), {})["last_attempt"] = time.monotonic()


def note_login_result(client_id: str, ok: bool, rate_limited: bool = False) -> float:
    """Record outcome; on failure set an exponential cooldown. Returns cooldown secs."""
    with _login_guard:
        st = _login_state.setdefault(str(client_id), {})
        if ok:
            st["fail_count"] = 0
            st["cooldown_until"] = 0.0
            return 0.0
        n = st.get("fail_count", 0) + 1
        st["fail_count"] = n
        base = 60.0 if rate_limited else 15.0
        backoff = min(LOGIN_BACKOFF_CAP, base * (2 ** (n - 1)))
        st["cooldown_until"] = time.monotonic() + backoff
        return backoff


# ── Global cross-account login gate ─────────────────────────────────────────
# The per-account governor (above) spaces repeated attempts for the SAME account,
# but on a fresh-day cold start P1/P2/P3 all generateSession near-simultaneously
# and trip Angel's per-IP login rate limit ("Access denied because of exceeding
# access rate"). This gate serializes logins ACROSS all accounts with a minimum
# gap, so the three startup logins happen one-at-a-time. Call immediately before
# every generateSession (RESEARCH + LIVEFEED). Cheap (only fires at login).
GLOBAL_LOGIN_GAP = float(os.getenv("ANGEL_GLOBAL_LOGIN_GAP", "6"))
_global_login_lock = threading.Lock()
_global_last_login = 0.0


def global_login_gate() -> None:
    """Block until GLOBAL_LOGIN_GAP seconds have elapsed since the last Angel login
    of ANY account, then mark now as the last login. Serializes cross-account logins
    to avoid the per-IP login storm."""
    global _global_last_login
    with _global_login_lock:
        now = time.monotonic()
        if _global_last_login:
            wait = GLOBAL_LOGIN_GAP - (now - _global_last_login)
            if wait > 0:
                time.sleep(wait)
        _global_last_login = time.monotonic()


# ── Cross-process login lease (file lock; Windows msvcrt / POSIX fcntl) ─────
def acquire_login_lease(client_id: str, timeout: float = 120.0):
    """Best-effort cross-process exclusive lock so only one process logs in per
    account at a time. Returns an opaque handle for release_login_lease(), or None
    if it could not be acquired in time (caller may proceed; the in-process governor
    still protects against storms)."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        lock_path = _session_path(client_id).with_name(
            _session_path(client_id).stem.replace("angel_session_", "angel_login_") + ".lock"
        )
        f = open(lock_path, "a+")
        deadline = time.monotonic() + timeout
        if os.name == "nt":
            import msvcrt
            while True:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    return f
                except OSError:
                    if time.monotonic() > deadline:
                        f.close()
                        return None
                    time.sleep(0.25)
        else:
            import fcntl
            while True:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return f
                except OSError:
                    if time.monotonic() > deadline:
                        f.close()
                        return None
                    time.sleep(0.25)
    except Exception:
        return None


def release_login_lease(handle) -> None:
    if handle is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
    finally:
        try:
            handle.close()
        except Exception:
            pass
