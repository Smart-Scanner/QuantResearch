import os
import sys
import time
import logging
from datetime import datetime, timezone, timedelta

# Add parent dir to path so we can import db and intelligence
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

import db
from intelligence.fundamentals import get_fundamentals_yf

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("warm_fundamentals")

def main():
    log.info("Starting fundamentals warm-up job...")
    
    # 1. Get targets
    if db.is_postgresql():
        query = """
            SELECT s.symbol
            FROM stocks s
            LEFT JOIN fundamentals f ON s.symbol = f.symbol
            WHERE f.symbol IS NULL
               OR f.market_cap IS NULL
               OR f.pe IS NULL
               OR f.roe IS NULL
               OR f.updated_at < NOW() - INTERVAL '6 HOURS'
        """
    else:
        query = """
            SELECT s.symbol
            FROM stocks s
            LEFT JOIN fundamentals f ON s.symbol = f.symbol
            WHERE f.symbol IS NULL
               OR f.market_cap IS NULL
               OR f.pe IS NULL
               OR f.roe IS NULL
               OR f.updated_at < datetime('now', '-6 hours')
        """
        
    try:
        rows = db.execute_db(query, fetch="all")
        symbols = [r["symbol"] for r in rows if r.get("symbol")]
    except Exception as exc:
        log.error("Failed to query target symbols: %s", exc)
        return

    total = len(symbols)
    log.info("Found %d symbols requiring fundamental updates.", total)
    
    if total == 0:
        return

    processed = 0
    success = 0
    failed = 0
    skipped = 0
    
    for idx, symbol in enumerate(symbols, 1):
        try:
            # We explicitly bypass cache_only to force yfinance hit
            # This UPSERTs into the `fundamentals` table seamlessly.
            fund = get_fundamentals_yf(symbol, cache_only=False)
            if fund and fund.get("market_cap") is not None:
                success += 1
            else:
                failed += 1
                
        except Exception as exc:
            log.warning("Exception fetching %s: %s", symbol, exc)
            failed += 1
            
        processed += 1
        
        # Progress logging
        if processed % 50 == 0 or processed == total:
            log.info("Progress: %d/%d | Success: %d | Failed: %d | Skipped: %d", 
                     processed, total, success, failed, skipped)
                     
        # Respect yfinance rate limits
        time.sleep(1.0)
        
    log.info("Warm-up job completed. Total: %d, Success: %d, Failed: %d", total, success, failed)

if __name__ == "__main__":
    main()
