"""
Sprint 1 Phase 0.75: Production Regression Gate
Run before any deployment to verify critical endpoints work.

Usage:
    python tests/regression_gate.py [base_url]

Default base_url: http://localhost:5050
Exit code 0 = all passed, 1 = failures detected.
"""

import sys
import time
import json
import urllib.request
import urllib.error

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:5050"

TESTS = [
    {
        "name": "Dashboard API",
        "url": "/api/dashboard",
        "method": "GET",
        "max_ms": 5000,
        "required_keys": ["status", "results", "total_analyzed"],
    },
    {
        "name": "Status API",
        "url": "/api/status",
        "method": "GET",
        "max_ms": 3000,
        "required_keys": ["scanning", "last_scan", "market_regime"],
    },
    {
        "name": "Search List API",
        "url": "/api/search-list",
        "method": "GET",
        "max_ms": 3000,
        "is_list": True,
    },
    {
        "name": "Results API (limit=500)",
        "url": "/api/results?limit=500",
        "method": "GET",
        "max_ms": 10000,
        "is_results_object": True,
        "validate_stock_fields": True,
    },
    {
        "name": "Health Check",
        "url": "/api/health",
        "method": "GET",
        "max_ms": 5000,
    },
    {
        "name": "Top Picks HTML",
        "url": "/top-picks",
        "method": "GET",
        "max_ms": 10000,
        "is_html": True,
    },
    {
        "name": "Stock Detail HTML",
        "url": "/stock/RELIANCE",
        "method": "GET",
        "max_ms": 10000,
        "is_html": True,
    },
]

# Required fields every stock result MUST have (data integrity gate)
REQUIRED_STOCK_FIELDS = ["symbol", "score", "sector"]


def run_test(test: dict) -> dict:
    """Run a single regression test. Returns result dict."""
    url = BASE_URL + test["url"]
    name = test["name"]
    start = time.perf_counter()
    
    try:
        req = urllib.request.Request(url, method=test.get("method", "GET"))
        with urllib.request.urlopen(req, timeout=30) as resp:
            elapsed_ms = round((time.perf_counter() - start) * 1000)
            status_code = resp.status
            body = resp.read().decode("utf-8")
            
            if status_code != 200:
                return {"name": name, "pass": False, "ms": elapsed_ms, "error": f"HTTP {status_code}"}
            
            # HTML page tests — just verify 200 + non-empty + has <html
            if test.get("is_html"):
                if len(body) < 100:
                    return {"name": name, "pass": False, "ms": elapsed_ms, "error": "Empty HTML response"}
                if "<html" not in body.lower()[:500]:
                    return {"name": name, "pass": False, "ms": elapsed_ms, "error": "Invalid HTML (no <html tag)"}
                max_ms = test.get("max_ms", 10000)
                if elapsed_ms > max_ms:
                    return {"name": name, "pass": False, "ms": elapsed_ms, "error": f"Too slow: {elapsed_ms}ms > {max_ms}ms"}
                return {"name": name, "pass": True, "ms": elapsed_ms}
            
            data = json.loads(body)
            
            # Check required keys
            if "required_keys" in test and isinstance(data, dict):
                missing = [k for k in test["required_keys"] if k not in data]
                if missing:
                    return {"name": name, "pass": False, "ms": elapsed_ms, "error": f"Missing keys: {missing}"}
            
            # Check if expected list
            if test.get("is_list") and not isinstance(data, list):
                return {"name": name, "pass": False, "ms": elapsed_ms, "error": "Expected list response"}
            
            # Check results object
            results_list = data
            if test.get("is_results_object"):
                if not isinstance(data, dict) or "results" not in data or not isinstance(data["results"], list):
                    return {"name": name, "pass": False, "ms": elapsed_ms, "error": "Expected dict with 'results' list"}
                results_list = data["results"]
                
                if len(results_list) == 0 and data.get("total_analyzed", 0) > 0:
                    return {"name": name, "pass": False, "ms": elapsed_ms, "error": "Results list empty despite total_analyzed > 0"}
            
            # Stock field validation — verify first 10 results have required fields
            if test.get("validate_stock_fields"):
                if not isinstance(results_list, list):
                    return {"name": name, "pass": False, "ms": elapsed_ms, "error": "Cannot validate stock fields on non-list"}
                sample = results_list[:10]
                for i, stock in enumerate(sample):
                    if not isinstance(stock, dict):
                        continue
                    missing = [f for f in REQUIRED_STOCK_FIELDS if f not in stock or stock[f] is None]
                    if missing:
                        return {"name": name, "pass": False, "ms": elapsed_ms,
                                "error": f"Stock[{i}] ({stock.get('symbol','?')}) missing: {missing}"}
            
            # Check response time
            max_ms = test.get("max_ms", 5000)
            if elapsed_ms > max_ms:
                return {"name": name, "pass": False, "ms": elapsed_ms, "error": f"Too slow: {elapsed_ms}ms > {max_ms}ms"}
            
            return {"name": name, "pass": True, "ms": elapsed_ms}
            
    except urllib.error.HTTPError as e:
        elapsed_ms = round((time.perf_counter() - start) * 1000)
        return {"name": name, "pass": False, "ms": elapsed_ms, "error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        elapsed_ms = round((time.perf_counter() - start) * 1000)
        return {"name": name, "pass": False, "ms": elapsed_ms, "error": str(e)[:200]}


def main():
    print(f"\n{'='*60}")
    print(f"  PRODUCTION REGRESSION GATE")
    print(f"  Target: {BASE_URL}")
    print(f"{'='*60}\n")
    
    results = []
    for test in TESTS:
        result = run_test(test)
        results.append(result)
        status = "✅ PASS" if result["pass"] else "❌ FAIL"
        print(f"  {status}  {result['name']:30s}  {result['ms']:>5d}ms", end="")
        if not result["pass"]:
            print(f"  — {result['error']}", end="")
        print()
    
    passed = sum(1 for r in results if r["pass"])
    failed = sum(1 for r in results if not r["pass"])
    
    print(f"\n{'='*60}")
    print(f"  Results: {passed} passed, {failed} failed")
    
    if failed > 0:
        print(f"\n  ⛔ DEPLOYMENT BLOCKED — {failed} test(s) failed")
        print(f"{'='*60}\n")
        
        # Save failure report
        report = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "base_url": BASE_URL,
            "passed": passed,
            "failed": failed,
            "results": results,
        }
        try:
            with open("tests/regression_report.json", "w") as f:
                json.dump(report, f, indent=2)
            print("  Report saved to tests/regression_report.json")
        except Exception:
            pass
        
        sys.exit(1)
    else:
        print(f"\n  ✅ ALL TESTS PASSED — Safe to deploy")
        print(f"{'='*60}\n")
        
        # Save baseline metrics
        baseline = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "base_url": BASE_URL,
        }
        for r in results:
            key = r["name"].lower().replace(" ", "_").replace("(", "").replace(")", "")
            baseline[f"{key}_ms"] = r["ms"]
        
        try:
            with open("tests/performance_baseline.json", "w") as f:
                json.dump(baseline, f, indent=2)
            print("  Baseline saved to tests/performance_baseline.json")
        except Exception:
            pass
        
        sys.exit(0)


if __name__ == "__main__":
    main()
