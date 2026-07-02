import psycopg2.pool
import os
from dotenv import load_dotenv

load_dotenv()
url = os.getenv('DATABASE_URL')
if not url:
    print("NO DB URL")
    exit()
# Use the app's own host-aware SSL policy instead of forcing sslmode=require,
# so this smoke test matches production (internal Coolify/Docker PG -> sslmode=disable).
import sys, os as _os
sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import db
url = db._normalize_pg_url(url)

try:
    p = psycopg2.pool.ThreadedConnectionPool(1, 1, url)
    print("Pool created, max=1")
    c1 = p.getconn()
    print("Got c1")
    try:
        print("Getting c2...")
        c2 = p.getconn()
        print("Got c2")
    except Exception as e:
        print("Error getting c2:", type(e).__name__, e)
except Exception as e:
    print("Init error:", type(e).__name__, e)
