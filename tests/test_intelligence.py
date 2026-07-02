import os; os.environ["PYTHONIOENCODING"] = "utf-8"
from dotenv import load_dotenv; load_dotenv()
from intelligence.seasonal import get_seasonal_score
from intelligence.sector_rotation import scan_sector_rotation, get_sector_rotation_score, sector_rotation_cache
from intelligence.macro_events import scan_macro_events
from intelligence.news_gdelt_finbert import fetch_gdelt_india_bulk, score_headlines_finbert

# Test 1: Seasonal
score, seasons, reasons = get_seasonal_score("Pharma", "Healthcare")
print("Seasonal: score=%d, seasons=%d active, reasons=%s" % (score, len(seasons), reasons[:2]))

# Test 2: Sector Rotation (RRG)
print("Scanning sector rotation (downloading 6mo data for 12 sectors)...")
scan_sector_rotation()
s, q = get_sector_rotation_score("Pharma")
print("RRG Pharma: score=%d, quad=%s" % (s, q))
print("RRG cache: %d sectors" % len(sector_rotation_cache))
for name, data in list(sector_rotation_cache.items())[:4]:
    print("  %s: rs_ratio=%.1f rs_mom=%.1f -> %s" % (
        name, data.get("rs_ratio",0), data.get("rs_momentum",0), data.get("quadrant","?")))

# Test 3: Forex Factory
ff = scan_macro_events()
print("FF: regime=%s, events=%d" % (ff["regime"], len(ff["events"])))

# Test 4: GDELT
print("Fetching GDELT articles...")
arts = fetch_gdelt_india_bulk(hours_back=24)
print("GDELT articles: %d" % len(arts))
if arts:
    headlines = [a["title"] for a in arts[:5]]
    scores = score_headlines_finbert(headlines)
    for h, sc in zip(headlines, scores):
        print("  [%+.2f] %s" % (sc, h[:70]))

print("\nAll tests passed!")
