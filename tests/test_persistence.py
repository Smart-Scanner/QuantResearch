import os, time
from datetime import date, timedelta
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
print("URL:", os.environ.get('DATABASE_URL'))

import db

# Insert dummy rows
db.execute_db("INSERT INTO scan_results(symbol, data, score, scan_date) VALUES ('TEST1', '{\"trade\":{}}', 70, '2023-10-10')")
db.execute_db("INSERT INTO scan_results(symbol, data, score, scan_date) VALUES ('TEST2', '{\"trade\":{}}', 60, '2023-10-10')")
rows = db.execute_db('SELECT COUNT(*) as c FROM scan_results', fetch='one')
print('Before clear:', rows['c'])

# Fake yesterday
yesterday = (date.today() - timedelta(days=1)).isoformat()
cache_dir = Path(db.__file__).parent / 'cache'
cache_dir.mkdir(exist_ok=True)
clear_tracker = cache_dir / 'last_clear_date.txt'
clear_tracker.write_text(yesterday)

# Run clear function
db.auto_clear_daily_cache()

# Verify count is SAME
rows = db.execute_db('SELECT COUNT(*) as c FROM scan_results', fetch='one')
print('After midnight cross:', rows['c'])

# Cleanup test data
db.execute_db("DELETE FROM scan_results WHERE symbol IN ('TEST1', 'TEST2')")
