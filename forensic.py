import db

def print_forensics():
    # 1. Latest completed scan
    r = db.execute_db("SELECT scan_id, universe_version, candidate_count, processed_count FROM scan_runs WHERE status='COMPLETED' ORDER BY end_time DESC LIMIT 1", fetch="one")
    print(f"Latest Scan: {r['scan_id']}")
    print(f"Universe Version: {r['universe_version']}")
    print(f"Candidate Count: {r['candidate_count']}")
    print(f"Processed Count: {r['processed_count']}")
    
    # 2. Count eligible_universe
    e = db.execute_db("SELECT COUNT(*) as c FROM eligible_universe WHERE universe_version=?", (r['universe_version'],), fetch="one")
    print(f"Eligible Count in DB for {r['universe_version']}: {e['c']}")
    
if __name__ == "__main__":
    print_forensics()
