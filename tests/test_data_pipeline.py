#!/usr/bin/env python3
"""
Local diagnostic script — tests the entire data pipeline end-to-end:
1. Provider discovery & login
2. Angel API getCandleData
3. historical_service flow
4. Token resolution
5. Universe candidate query
"""

import os
import sys
import logging

# Must load .env FIRST
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("test")

def test_env_vars():
    """Test 1: Verify PROVIDER env vars are set"""
    log.info("=" * 60)
    log.info("TEST 1: Environment Variables")
    log.info("=" * 60)
    
    for i in range(1, 4):
        api_key = os.environ.get(f"PROVIDER_{i}_API_KEY", "")
        role = os.environ.get(f"PROVIDER_{i}_ROLE", "(not set)")
        client = os.environ.get(f"PROVIDER_{i}_CLIENT_ID", "")
        totp = os.environ.get(f"PROVIDER_{i}_TOTP_SECRET", "") or os.environ.get(f"PROVIDER_{i}_TOTP", "")
        
        masked_key = api_key[:4] + "***" if api_key else "(missing)"
        masked_totp = totp[:4] + "***" if totp else "(missing)"
        
        log.info(f"  PROVIDER_{i}: API_KEY={masked_key} CLIENT={client} ROLE={role} TOTP={masked_totp}")
    
    db_url = os.environ.get("DATABASE_URL", "")
    log.info(f"  DATABASE_URL: {'SET (' + db_url[:30] + '...)' if db_url else 'NOT SET'}")
    

def test_provider_discovery():
    """Test 2: Provider discovery and login"""
    log.info("=" * 60)
    log.info("TEST 2: Provider Discovery & Login")
    log.info("=" * 60)
    
    from data_provider import provider_manager
    
    log.info(f"  Discovered providers: {list(provider_manager.providers.keys())}")
    
    for name, p in provider_manager.providers.items():
        log.info(f"  {name}: state={p.state.value} role={p.role} api={'OK' if p.api else 'NONE'}")
    
    # Try to acquire RESEARCH provider
    provider = provider_manager.acquire_active_provider(required_role="RESEARCH")
    if provider:
        log.info(f"  ✅ Acquired RESEARCH provider: {provider.name}")
        provider_manager.release_provider(provider)
    else:
        log.error(f"  ❌ No RESEARCH provider available!")
    
    return provider is not None


def test_raw_angel_api():
    """Test 3: Raw Angel API getCandleData"""
    log.info("=" * 60)
    log.info("TEST 3: Raw Angel API getCandleData")
    log.info("=" * 60)
    
    from data_provider import provider_manager
    from datetime import datetime, timedelta
    
    provider = provider_manager.acquire_active_provider(required_role="RESEARCH")
    if not provider:
        log.error("  ❌ No provider to test with")
        return False
    
    try:
        # Test with RELIANCE (token 2885)
        test_cases = [
            {"name": "RELIANCE", "token": "2885", "exchange": "NSE"},
            {"name": "NIFTY50", "token": "26000", "exchange": "NSE"},
            {"name": "INFY", "token": "1594", "exchange": "NSE"},
        ]
        
        fromdate = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d 09:15")
        todate = datetime.now().strftime("%Y-%m-%d 15:30")
        
        for tc in test_cases:
            params = {
                "exchange": tc["exchange"],
                "symboltoken": tc["token"],
                "interval": "ONE_DAY",
                "fromdate": fromdate,
                "todate": todate,
            }
            log.info(f"  Fetching {tc['name']} (token={tc['token']})...")
            
            try:
                res = provider.api.getCandleData(params)
                
                if res is None:
                    log.error(f"    ❌ {tc['name']}: Response is None")
                    continue
                
                status = res.get("status")
                message = res.get("message", "")
                errorcode = res.get("errorcode", "")
                data = res.get("data")
                
                log.info(f"    status={status} message={message} errorcode='{errorcode}'")
                log.info(f"    data type={type(data).__name__} length={len(data) if data else 0}")
                
                if data and len(data) > 0:
                    log.info(f"    ✅ First candle: {data[0]}")
                    log.info(f"    ✅ Last candle:  {data[-1]}")
                else:
                    log.warning(f"    ⚠️  Data is empty! Full response: {res}")
                    
            except Exception as e:
                log.error(f"    ❌ Exception: {e}")
        
        return True
        
    finally:
        provider_manager.release_provider(provider)


def test_token_resolution():
    """Test 4: Token resolution for common symbols"""
    log.info("=" * 60)
    log.info("TEST 4: Token Resolution")
    log.info("=" * 60)
    
    from live_feed import get_token
    
    test_symbols = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", 
                    "SBIN", "TATAMOTORS", "BHARTIARTL", "ITC", "WIPRO"]
    
    resolved = 0
    for sym in test_symbols:
        token = get_token(sym)
        if token:
            log.info(f"  ✅ {sym:15s} → token={token}")
            resolved += 1
        else:
            log.error(f"  ❌ {sym:15s} → FAILED to resolve")
    
    log.info(f"  Resolved {resolved}/{len(test_symbols)} symbols")
    return resolved > 0


def test_historical_service():
    """Test 5: historical_service.get_daily_history"""
    log.info("=" * 60)
    log.info("TEST 5: historical_service.get_daily_history")
    log.info("=" * 60)
    
    from historical_service import get_daily_history
    
    # Test with RELIANCE token
    data = get_daily_history("2885", days=30, exchange="NSE")
    if data:
        log.info(f"  ✅ RELIANCE: Got {len(data)} candles")
        log.info(f"     First: {data[0]}")
        log.info(f"     Last:  {data[-1]}")
    else:
        log.error(f"  ❌ RELIANCE: No data returned!")
    
    return data is not None and len(data) > 0


def test_universe_candidates():
    """Test 6: Universe candidate query"""
    log.info("=" * 60)
    log.info("TEST 6: Universe Candidates")
    log.info("=" * 60)
    
    import db
    db.init_db()
    
    # Check total catalog
    total = db.execute_db("SELECT COUNT(*) as c FROM universe_catalog", fetch="one")
    active = db.execute_db("SELECT COUNT(*) as c FROM universe_catalog WHERE is_active = TRUE", fetch="one")
    equity = db.execute_db(
        "SELECT COUNT(*) as c FROM universe_catalog WHERE is_active = TRUE AND instrument_type = 'EQUITY'",
        fetch="one"
    )
    
    log.info(f"  Total catalog: {total.get('c', 0) if total else 0}")
    log.info(f"  Active:        {active.get('c', 0) if active else 0}")
    log.info(f"  Equity:        {equity.get('c', 0) if equity else 0}")
    
    # Check instrument type distribution
    dist = db.execute_db(
        "SELECT instrument_type, COUNT(*) as c FROM universe_catalog WHERE is_active = TRUE GROUP BY instrument_type ORDER BY c DESC",
        fetch="all"
    )
    if dist:
        log.info("  Instrument type distribution:")
        for row in dist:
            log.info(f"    {row.get('instrument_type', 'NULL'):20s} → {row.get('c', 0)}")
    
    # Check candidates
    candidates = db.get_candidate_universe()
    log.info(f"  Candidate universe size: {len(candidates) if candidates else 0}")
    
    # Check eligible universe
    eligible = db.execute_db(
        "SELECT COUNT(*) as c FROM eligible_universe", fetch="one"
    )
    log.info(f"  Eligible universe size: {eligible.get('c', 0) if eligible else 0}")
    
    return True


def test_fetch_historical_e2e():
    """Test 7: End-to-end fetch_historical via live_feed"""
    log.info("=" * 60)
    log.info("TEST 7: End-to-End live_feed.fetch_historical")
    log.info("=" * 60)
    
    import live_feed
    
    df = live_feed.fetch_historical("RELIANCE", days=30)
    if df is not None and not df.empty:
        log.info(f"  ✅ RELIANCE DataFrame: {len(df)} rows")
        log.info(f"     Columns: {list(df.columns)}")
        log.info(f"     Date range: {df['DATE'].min()} → {df['DATE'].max()}")
        log.info(f"     Volume range: {df['VOLUME'].min()} → {df['VOLUME'].max()}")
    else:
        log.error(f"  ❌ RELIANCE: No DataFrame returned!")
    
    return df is not None


if __name__ == "__main__":
    log.info("🔧 Smart Screener Data Pipeline Diagnostic")
    log.info("=" * 60)
    
    results = {}
    
    # Run tests in order
    test_env_vars()
    results["provider_discovery"] = test_provider_discovery()
    results["raw_angel_api"] = test_raw_angel_api()
    results["token_resolution"] = test_token_resolution()
    results["historical_service"] = test_historical_service()
    results["fetch_historical_e2e"] = test_fetch_historical_e2e()
    results["universe_candidates"] = test_universe_candidates()
    
    # Summary
    log.info("=" * 60)
    log.info("📊 RESULTS SUMMARY")
    log.info("=" * 60)
    for test_name, passed in results.items():
        status = "✅ PASS" if passed else "❌ FAIL"
        log.info(f"  {status}: {test_name}")
    
    all_passed = all(results.values())
    log.info("=" * 60)
    if all_passed:
        log.info("🎉 ALL TESTS PASSED — Pipeline is healthy!")
    else:
        failed = [k for k, v in results.items() if not v]
        log.error(f"⚠️ {len(failed)} TESTS FAILED: {', '.join(failed)}")
    
    sys.exit(0 if all_passed else 1)
