import time
import threading
import functools

_lock = threading.Lock()
_timings = {}

def timed(label: str):
    """Decorator: records duration and success/failure count for an operation."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            start = time.monotonic()
            success = True
            try:
                return fn(*args, **kwargs)
            except Exception:
                success = False
                raise
            finally:
                ms = round((time.monotonic() - start) * 1000)
                _record(label, ms, success)
        return wrapper
    return decorator

def _record(label: str, ms: int, success: bool):
    with _lock:
        if label not in _timings:
            _timings[label] = {
                "count": 0,
                "total_ms": 0,
                "failures": 0,
                "max_ms": 0,
                "min_ms": 999999
            }
        t = _timings[label]
        t["count"] += 1
        t["total_ms"] += ms
        t["max_ms"] = max(t["max_ms"], ms)
        t["min_ms"] = min(t["min_ms"], ms)
        if not success:
            t["failures"] += 1

def get_report() -> dict:
    with _lock:
        return {
            k: {**v, "avg_ms": round(v["total_ms"] / v["count"]) if v["count"] else 0}
            for k, v in _timings.items()
        }

def reset():
    with _lock:
        _timings.clear()
