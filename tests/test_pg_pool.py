import psycopg2.pool
import os
from dotenv import load_dotenv

load_dotenv()
url = os.getenv('DATABASE_URL')
if not url:
    print("NO DB URL")
    exit()
url = url.replace('postgres://', 'postgresql://')
url += '&sslmode=require' if '?' in url else '?sslmode=require'

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
