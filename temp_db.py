def get_chunk_run_states(scan_id: str) -> dict:
    """Return chunk_name -> (status, symbols_processed) for a scan."""
    rows = execute_db("SELECT chunk_name, status, symbols_processed FROM universe_chunk_runs WHERE scan_id = ?", (scan_id,), fetch="all")
    if not rows: return {}
    return {r["chunk_name"]: (r["status"], r.get("symbols_processed", 0)) for r in rows}
