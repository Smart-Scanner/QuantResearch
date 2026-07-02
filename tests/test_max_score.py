from dotenv import load_dotenv
load_dotenv()
import db
row = db.execute_db('SELECT MAX(score) as m FROM scan_results', fetch='one')
print(f'Max score right now: {row.get("m")}')
rows = db.execute_db('SELECT COUNT(*) as c FROM scan_results', fetch='one')
print(f'Total rows right now: {rows.get("c")}')
