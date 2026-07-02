import sys
import os
import logging
import urllib.request
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables before importing app modules like db
load_dotenv(Path(__file__).parent.parent / '.env')

# Make sure we can import the app modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
from universe import FNO_UNIVERSE

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

NSE_EQUITY_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"

def build_universe():
    log.info("Starting NSE Direct Universe Builder...")
    
    # Initialize connection pool safely without triggering full DDL migrations
    db._get_pg_pool()

    
    # 1. Download EQUITY_L.csv from NSE
    csv_path = Path(__file__).parent.parent / "cache" / "equity_l.csv"
    csv_path.parent.mkdir(exist_ok=True)
    
    log.info(f"Downloading active NSE equity list from {NSE_EQUITY_URL} ...")
    try:
        req = urllib.request.Request(NSE_EQUITY_URL, headers={'User-Agent': 'Mozilla/5.0'})
        csv_data = urllib.request.urlopen(req).read()
        with open(csv_path, 'wb') as f:
            f.write(csv_data)
        log.info("Download successful.")
    except Exception as e:
        log.error(f"Failed to download NSE equity list: {e}")
        return

    # 2. Parse CSV
    try:
        df = pd.read_csv(csv_path)
        # Clean column names (strip spaces)
        df.columns = df.columns.str.strip()
        symbols = df['SYMBOL'].str.strip().tolist()
        company_names = df['NAME OF COMPANY'].str.strip().tolist()
        log.info(f"Parsed {len(symbols)} active symbols from NSE.")
    except Exception as e:
        log.error(f"Failed to parse CSV: {e}")
        return

    # 3. Apply Heuristics for Chunking since NSE CSV doesn't provide Market Cap
    fno_list = list(FNO_UNIVERSE)
    nifty50_heuristic = set(fno_list[:50])
    fno_rest_heuristic = set(fno_list[50:])
    
    results = []
    rest_count = 0
    chunk_size = 250
    
    for i, symbol in enumerate(symbols):
        # Exclude indices or non-standard symbols if any slip through
        if len(symbol) > 15:
            continue
            
        company_name = company_names[i]
        
        if symbol in nifty50_heuristic:
            bucket = "Blue Chip (Nifty 50 Proxy)"
        elif symbol in fno_rest_heuristic:
            bucket = "Large/Mid Cap (F&O Proxy)"
        else:
            # Group the rest into generic broader market chunks of 250
            part = (rest_count // chunk_size) + 1
            bucket = f"Broader Market Part {part}"
            rest_count += 1
            
        results.append({
            "symbol": symbol,
            "company_name": company_name,
            "market_cap": 0.0, # Not provided by EQUITY_L
            "sector": "Unknown",
            "industry": "Unknown",
            "bucket": bucket
        })

    log.info("Saving to database with UPSERT logic (avoiding duplicates)...")
    
    # 4. Deactivation Governance (Risk 4)
    log.info("Marking existing catalog as inactive (Deactivation Governance)...")
    try:
        db.execute_db("UPDATE universe_catalog SET is_active = FALSE;")
    except Exception as e:
        log.error(f"Failed to reset active status: {e}")
    
    # 5. Database UPSERT logic
    if db.is_postgresql():
        log.info("Using PostgreSQL ON CONFLICT upsert...")
        query = """
            INSERT INTO universe_catalog 
            (symbol, company_name, market_cap, market_cap_bucket, sector, industry, is_active, last_scanned_at)
            VALUES (?, ?, ?, ?, ?, ?, TRUE, CURRENT_TIMESTAMP)
            ON CONFLICT (symbol) DO UPDATE SET 
                company_name = EXCLUDED.company_name,
                market_cap_bucket = EXCLUDED.market_cap_bucket,
                is_active = TRUE,
                last_scanned_at = CURRENT_TIMESTAMP;
        """
        success_count = 0
        for r in results:
            try:
                db.execute_db(query, (r["symbol"], r["company_name"], r["market_cap"], r["bucket"], r["sector"], r["industry"]))
                success_count += 1
            except Exception as e:
                log.error(f"Failed to insert {r['symbol']}: {e}")
        log.info(f"PostgreSQL UPSERT successful for {success_count} records.")
    else:
        log.info("Using SQLite INSERT OR REPLACE upsert...")
        query = """
            INSERT OR REPLACE INTO universe_catalog 
            (symbol, company_name, market_cap, market_cap_bucket, sector, industry, is_active, last_scanned_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, datetime('now'));
        """
        success_count = 0
        for r in results:
            try:
                db.execute_db(query, (r["symbol"], r["company_name"], r["market_cap"], r["bucket"], r["sector"], r["industry"]))
                success_count += 1
            except Exception as e:
                log.error(f"Failed to insert {r['symbol']}: {e}")
        log.info(f"SQLite UPSERT successful for {success_count} records.")
        
    log.info(f"Successfully cataloged {len(results)} stocks directly from NSE!")
    
if __name__ == "__main__":
    build_universe()
