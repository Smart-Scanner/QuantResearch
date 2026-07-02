from dotenv import load_dotenv
load_dotenv()

from intelligence.news_sentiment import fetch_news_sentiment
from intelligence.news_cache import get_cache_stats
import time
import logging

logging.basicConfig(level=logging.INFO)

nifty_50 = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "ITC", 
    "SBIN", "BHARTIARTL", "HINDUNILVR", "BAJFINANCE", "L&T", 
    "KOTAKBANK", "AXISBANK", "ASIANPAINT", "MARUTI", "SUNPHARMA"
]

print(f"--- Running Phase A: Nifty 50 subset ({len(nifty_50)} symbols) ---")

for sym in nifty_50:
    start = time.time()
    # query_marketaux=True to simulate a deep scan / top pick enrichment
    score, items, breakdown = fetch_news_sentiment(sym, query_marketaux=True, scan_mode="deep")
    elapsed = time.time() - start
    print(f"{sym:12} | Score: {score:5} | Articles: {sum(b['count'] for b in breakdown.values()):2} | Took {elapsed:.2f}s | Breakdown: {list(breakdown.keys())}")

print("\n--- Cache & Telemetry Stats ---")
print(get_cache_stats())
