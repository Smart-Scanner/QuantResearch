"""Sprint 1 Phase 1.25: Performance Baseline Capture & Comparison

Captures first-hit and cached-hit API response times + payload sizes
for before/after optimization comparison.

NOTE on "first_hit" vs "true cold":
  first_hit_ms = the first request in THIS benchmark run.
  The server-side TTLCache may already be warm from prior requests.
  For a true cold measurement, restart the server or clear caches first.
  In practice, first_hit is still the most useful metric because it
  represents what a user experiences after cache expiry (TTL=10-15s).

Usage:
    python tests/perf_baseline.py capture [base_url]
    python tests/perf_baseline.py compare [base_url]

Default base_url: http://localhost:5050
"""

import sys
import time
import json
import urllib.request

BASE_URL = "http://localhost:5050"
BASELINE_FILE = "tests/performance_baseline.json"
COMPARISON_FILE = "tests/performance_comparison.json"

# Endpoints to benchmark — must match real production usage
ENDPOINTS = [
    {"name": "dashboard_html", "url": "/dashboard",             "key": "dashboard_html"},
    {"name": "dashboard_api",  "url": "/api/dashboard",          "key": "dashboard_api"},
    {"name": "results",        "url": "/api/results?limit=500",  "key": "results"},
    {"name": "status",         "url": "/api/status",             "key": "status"},
    {"name": "search_list",    "url": "/api/search-list",        "key": "search_list"},
    {"name": "top_picks_html", "url": "/top-picks",              "key": "top_picks_html"},
    {"name": "stock_detail",   "url": "/stock/RELIANCE",         "key": "stock_detail"},
]

# Sprint 1 success targets (ms) — from frozen roadmap
# search_list: 250ms realistic for Railway + 2300 stocks
TARGETS = {
    "dashboard_api": 1000,
    "results": 500,
    "status": 50,
    "search_list": 250,
}


def measure_endpoint(url: str) -> dict:
    """Measure first-hit and cached-hit response times + payload size.

    First request = first_hit (likely cache miss after TTL expiry).
    Next 2 requests = cached_hit (should hit server-side TTLCache).
    Returns {"first_hit_ms", "cached_hit_ms", "payload_kb"}.
    """
    full_url = BASE_URL + url

    # First hit (after TTL expiry — represents real user experience)
    first_hit_ms = -1
    payload_kb = 0
    try:
        t0 = time.perf_counter()
        with urllib.request.urlopen(full_url, timeout=30) as resp:
            body = resp.read()
        first_hit_ms = round((time.perf_counter() - t0) * 1000)
        payload_kb = round(len(body) / 1024, 1)
    except Exception:
        pass

    # Cached hits (next 2 — should hit server TTLCache)
    cached_times = []
    for _ in range(2):
        try:
            t0 = time.perf_counter()
            with urllib.request.urlopen(full_url, timeout=30) as resp:
                resp.read()
            cached_times.append(round((time.perf_counter() - t0) * 1000))
        except Exception:
            pass

    cached_hit_ms = round(sum(cached_times) / len(cached_times)) if cached_times else -1

    return {"first_hit_ms": first_hit_ms, "cached_hit_ms": cached_hit_ms, "payload_kb": payload_kb}


def get_scan_duration() -> float:
    """Try to get last scan duration from multiple sources.
    Priority: /api/status -> /api/health -> fallback -1.
    """
    # Source 1: /api/status often has last_scan timestamp
    try:
        with urllib.request.urlopen(BASE_URL + "/api/health", timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            dur = data.get("last_scan_duration_min")
            if dur and float(dur) > 0:
                return round(float(dur), 1)
    except Exception:
        pass
    # Source 2: perf_baseline stored in scan_meta by scanner.py
    try:
        with urllib.request.urlopen(BASE_URL + "/api/debug/health", timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            perf = data.get("perf_baseline")
            if isinstance(perf, str):
                perf = json.loads(perf)
            if isinstance(perf, dict):
                dur = perf.get("scan_duration_min")
                if dur and float(dur) > 0:
                    return round(float(dur), 1)
    except Exception:
        pass
    return -1


def capture():
    """Capture current performance as baseline."""
    print(f"\n{'='*70}")
    print(f"  PERFORMANCE BASELINE CAPTURE")
    print(f"  Target: {BASE_URL}")
    print(f"{'='*70}\n")
    print(f"  {'Endpoint':20s}  {'1st Hit':>8s}  {'Cached':>8s}  {'Payload':>10s}")
    print(f"  {'─'*20}  {'─'*8}  {'─'*8}  {'─'*10}")

    baseline = {
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "base_url": BASE_URL,
        "phase": "before",
    }

    for ep in ENDPOINTS:
        m = measure_endpoint(ep["url"])
        baseline[f"{ep['key']}_first_hit_ms"] = m["first_hit_ms"]
        baseline[f"{ep['key']}_cached_hit_ms"] = m["cached_hit_ms"]
        baseline[f"{ep['key']}_payload_kb"] = m["payload_kb"]

        first_str = f"{m['first_hit_ms']}ms" if m["first_hit_ms"] > 0 else "FAIL"
        cached_str = f"{m['cached_hit_ms']}ms" if m["cached_hit_ms"] > 0 else "FAIL"
        size_str = f"{m['payload_kb']} KB"
        print(f"  {ep['name']:20s}  {first_str:>8s}  {cached_str:>8s}  {size_str:>10s}")

    # Scan duration
    scan_dur = get_scan_duration()
    baseline["scan_duration_min"] = scan_dur
    if scan_dur > 0:
        print(f"\n  Last scan duration: {scan_dur} min")
    else:
        print(f"\n  Last scan duration: unavailable")

    with open(BASELINE_FILE, "w") as f:
        json.dump(baseline, f, indent=2)

    print(f"\n  Baseline saved to {BASELINE_FILE}")
    print(f"{'='*70}\n")


def compare():
    """Compare current performance against saved baseline. Checks Sprint 1 targets."""
    try:
        with open(BASELINE_FILE) as f:
            baseline = json.load(f)
    except FileNotFoundError:
        print(f"\n  ❌ No baseline found at {BASELINE_FILE}")
        print(f"  Run: python tests/perf_baseline.py capture")
        sys.exit(1)

    print(f"\n{'='*80}")
    print(f"  SPRINT 1 PERFORMANCE COMPARISON")
    print(f"  Baseline from: {baseline.get('captured_at', '?')}")
    print(f"  Current:       {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*80}\n")
    print(f"  {'Endpoint':20s}  {'Before':>10s}  {'After':>10s}  {'Change':>8s}  {'Target':>8s}  {'Verdict'}")
    print(f"  {'─'*20}  {'─'*10}  {'─'*10}  {'─'*8}  {'─'*8}  {'─'*8}")

    comparison = {
        "compared_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    pass_count = 0
    fail_count = 0

    for ep in ENDPOINTS:
        m = measure_endpoint(ep["url"])
        before_ms = baseline.get(f"{ep['key']}_first_hit_ms", -1)
        after_ms = m["first_hit_ms"]
        before_payload = baseline.get(f"{ep['key']}_payload_kb", 0)
        after_payload = m["payload_kb"]

        # Save to comparison
        comparison[f"{ep['key']}_first_hit_ms_before"] = str(before_ms)
        comparison[f"{ep['key']}_first_hit_ms_after"] = str(after_ms)
        comparison[f"{ep['key']}_payload_kb_before"] = str(before_payload)
        comparison[f"{ep['key']}_payload_kb_after"] = str(after_payload)

        # Calculate change
        if before_ms > 0 and after_ms > 0:
            change_pct = round(((after_ms - before_ms) / before_ms) * 100)
            change_str = f"{change_pct:+d}%"
        else:
            change_str = "N/A"

        # Check against Sprint 1 target
        target = TARGETS.get(ep["key"])
        if target:
            target_str = f"{target}ms"
            if after_ms > 0 and after_ms <= target:
                verdict = "✅ PASS"
                pass_count += 1
            elif after_ms > 0:
                verdict = "❌ FAIL"
                fail_count += 1
            else:
                verdict = "❓ ERR"
        else:
            target_str = "—"
            verdict = "ℹ️"

        before_str = f"{before_ms}ms" if before_ms > 0 else "N/A"
        after_str = f"{after_ms}ms" if after_ms > 0 else "FAIL"
        print(f"  {ep['name']:20s}  {before_str:>10s}  {after_str:>10s}  {change_str:>8s}  {target_str:>8s}  {verdict}")

    # Payload comparison
    print(f"\n  {'Payload Size':20s}  {'Before':>10s}  {'After':>10s}  {'Change':>8s}")
    print(f"  {'─'*20}  {'─'*10}  {'─'*10}  {'─'*8}")
    for ep in ENDPOINTS:
        before_kb = baseline.get(f"{ep['key']}_payload_kb", 0)
        after_kb = float(comparison.get(f"{ep['key']}_payload_kb_after", 0))
        if before_kb > 0 and after_kb > 0:
            change_pct = round(((after_kb - before_kb) / before_kb) * 100)
            change_str = f"{change_pct:+d}%"
        else:
            change_str = "N/A"
        print(f"  {ep['name']:20s}  {before_kb:>8.1f}KB  {after_kb:>8.1f}KB  {change_str:>8s}")

    # Scan duration
    scan_dur = get_scan_duration()
    before_scan = baseline.get("scan_duration_min", -1)
    comparison["scan_duration_min_before"] = str(before_scan)
    comparison["scan_duration_min_after"] = str(scan_dur)
    if before_scan > 0 and scan_dur > 0:
        print(f"\n  Scan duration: {before_scan}min → {scan_dur}min")

    # Final verdict
    print(f"\n{'='*80}")
    if fail_count == 0 and pass_count > 0:
        print(f"  ✅ SPRINT 1 TARGETS MET: {pass_count}/{pass_count + fail_count} passed")
    elif fail_count > 0:
        print(f"  ❌ SPRINT 1 TARGETS NOT MET: {pass_count} passed, {fail_count} failed")
    else:
        print(f"  ℹ️  No targets to check (endpoints unreachable?)")
    print(f"{'='*80}\n")

    with open(COMPARISON_FILE, "w") as f:
        json.dump(comparison, f, indent=2)
    print(f"  Comparison saved to {COMPARISON_FILE}")


def main():
    global BASE_URL

    action = sys.argv[1] if len(sys.argv) > 1 else "capture"
    if len(sys.argv) > 2:
        BASE_URL = sys.argv[2]

    if action == "capture":
        capture()
    elif action == "compare":
        compare()
    else:
        print(f"Usage: python tests/perf_baseline.py [capture|compare] [base_url]")
        sys.exit(1)


if __name__ == "__main__":
    main()
