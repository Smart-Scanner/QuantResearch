-- 1. Add missing fields to scan_runs
ALTER TABLE scan_runs ADD COLUMN IF NOT EXISTS degraded_data BOOLEAN DEFAULT FALSE;
ALTER TABLE scan_runs ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE scan_runs ADD COLUMN IF NOT EXISTS request_id TEXT;
ALTER TABLE scan_runs ADD COLUMN IF NOT EXISTS scanner_version TEXT;
ALTER TABLE scan_runs ADD COLUMN IF NOT EXISTS scoring_version TEXT;
ALTER TABLE scan_runs ADD COLUMN IF NOT EXISTS recommendation_version TEXT;
ALTER TABLE scan_runs ADD COLUMN IF NOT EXISTS config_snapshot JSONB;
ALTER TABLE scan_runs ADD COLUMN IF NOT EXISTS parent_scan_id TEXT;
ALTER TABLE scan_runs ADD COLUMN IF NOT EXISTS hash_chain TEXT;

-- 2. Add missing fields to score_audit
ALTER TABLE score_audit ADD COLUMN IF NOT EXISTS score_breakdown JSONB;
ALTER TABLE score_audit ADD COLUMN IF NOT EXISTS hash_chain TEXT;

-- 3. Add missing fields to paper_trades
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS weight_version TEXT DEFAULT '';
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS confidence_score REAL DEFAULT 0;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS entry_rank INTEGER DEFAULT 0;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS breadth_advances INTEGER DEFAULT 0;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS breadth_declines INTEGER DEFAULT 0;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS breadth_ratio REAL DEFAULT 0;

-- 4. Create missing tables
CREATE TABLE IF NOT EXISTS scan_state_transitions (
    id BIGSERIAL PRIMARY KEY,
    scan_id TEXT NOT NULL,
    old_state TEXT NOT NULL,
    new_state TEXT NOT NULL,
    reason TEXT,
    actor TEXT DEFAULT 'system',
    correlation_id TEXT,
    hash_chain TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_scan_state_transitions_scan_id ON scan_state_transitions(scan_id);

-- 5. Create New Immutable Research Governance Tables (Phase 5, 15, and Major Upgrades)
CREATE TABLE IF NOT EXISTS research_snapshots_v2 (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    recommendation TEXT NOT NULL,
    entry_low REAL NOT NULL,
    entry_high REAL NOT NULL,
    stop_loss REAL NOT NULL,
    target_1 REAL NOT NULL,
    target_2 REAL,
    target_3 REAL,
    risk_reward REAL NOT NULL,
    confidence REAL DEFAULT 0,
    confidence_breakdown JSONB, -- Future explainability
    research_thesis TEXT,
    cmp_at_generation REAL NOT NULL,
    
    -- ScanContext Governance
    scan_id TEXT NOT NULL,
    correlation_id TEXT,
    scanner_version TEXT,
    scoring_version TEXT,
    recommendation_version TEXT,
    config_snapshot JSONB,
    snapshot_hash TEXT NOT NULL,

    -- Universe and Chunk Metadata
    market_cap_bucket TEXT, -- BLUE_CHIP, LARGE_CAP, MID_CAP, SMALL_CAP, MICRO_CAP
    chunk_name TEXT,
    chunk_version TEXT,
    
    status TEXT DEFAULT 'ACTIVE', -- ACTIVE, SUPERSEDED
    outcome_status TEXT DEFAULT 'PENDING', -- PENDING, TARGET1_HIT, TARGET2_HIT, TARGET3_HIT, STOPLOSS_HIT, EXPIRED, CANCELLED
    generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(symbol, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_research_snapshot_scan_symbol ON research_snapshots_v2(scan_id, symbol);

CREATE INDEX IF NOT EXISTS idx_research_snapshots_v2_symbol ON research_snapshots_v2(symbol);
CREATE INDEX IF NOT EXISTS idx_research_snapshots_v2_status ON research_snapshots_v2(status);
CREATE INDEX IF NOT EXISTS idx_research_snapshots_v2_generated_at ON research_snapshots_v2(generated_at);
CREATE INDEX IF NOT EXISTS idx_research_snapshots_v2_version ON research_snapshots_v2(version);

CREATE TABLE IF NOT EXISTS research_advisories (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    research_snapshot_id BIGINT NOT NULL REFERENCES research_snapshots_v2(id) ON DELETE CASCADE,
    snapshot_version INTEGER NOT NULL,
    advisory_type TEXT NOT NULL, -- e.g. TRAIL_SL, BOOK_PROFIT, MOMENTUM_ALERT
    priority TEXT NOT NULL, -- INFO, WARNING, ACTION_REQUIRED, CRITICAL
    advisory_message TEXT NOT NULL,
    new_sl_level REAL,
    correlation_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_research_advisories_symbol ON research_advisories(symbol);
CREATE INDEX IF NOT EXISTS idx_research_advisories_snapshot_id ON research_advisories(research_snapshot_id);

-- 6. Create Universe Chunk Governance Table
CREATE TABLE IF NOT EXISTS universe_chunk_runs (
    id BIGSERIAL PRIMARY KEY,
    scan_id TEXT NOT NULL,
    chunk_name TEXT NOT NULL,
    status TEXT NOT NULL,
    total_stocks INTEGER DEFAULT 0,
    processed_stocks INTEGER DEFAULT 0,
    failed_stocks INTEGER DEFAULT 0,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    duration_seconds REAL
);

CREATE INDEX IF NOT EXISTS idx_universe_chunk_runs_scan_id ON universe_chunk_runs(scan_id);
