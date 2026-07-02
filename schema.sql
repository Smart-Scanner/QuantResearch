-- ==============================================================================
-- Smart Screener PostgreSQL Schema
-- Consolidated migration script for all tables, indexes, constraints, and triggers
-- ==============================================================================

-- ------------------------------------------------------------------------------
-- 1. Main Application Tables (from db.py)
-- ------------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS scan_results (
    symbol TEXT PRIMARY KEY,
    data JSONB NOT NULL,
    score INTEGER DEFAULT 0,
    high_conviction INTEGER DEFAULT 0,
    sector TEXT DEFAULT '',
    scan_date TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    slim_data JSONB
);

CREATE TABLE IF NOT EXISTS scan_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS score_history (
    symbol TEXT NOT NULL,
    score INTEGER NOT NULL,
    price REAL NOT NULL,
    rsi REAL,
    scan_date TEXT NOT NULL,
    PRIMARY KEY (symbol, scan_date)
);

CREATE TABLE IF NOT EXISTS custom_stocks (
    symbol TEXT PRIMARY KEY,
    exchange TEXT DEFAULT 'NSE',
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS portfolios (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS positions (
    id SERIAL PRIMARY KEY,
    portfolio_id INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    trade_type TEXT DEFAULT 'BUY',
    quantity INTEGER NOT NULL DEFAULT 1,
    buy_price REAL NOT NULL,
    buy_date TEXT NOT NULL,
    sell_price REAL,
    sell_date TEXT,
    stop_loss REAL,
    target REAL,
    status TEXT DEFAULT 'OPEN',
    notes TEXT DEFAULT '',
    scan_analysis TEXT DEFAULT 'Hold (Position Active)',
    last_scan_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS stocks (
    symbol TEXT PRIMARY KEY,
    name TEXT,
    sector TEXT,
    industry TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS news_articles (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    source TEXT,
    age_hours REAL,
    raw_score REAL,
    scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sentiment_scores (
    symbol TEXT NOT NULL,
    scan_date TEXT NOT NULL,
    gdelt_sentiment REAL,
    gdelt_spike REAL,
    gdelt_freshness REAL,
    final_sentiment_score REAL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, scan_date)
);

CREATE TABLE IF NOT EXISTS technical_indicators (
    symbol TEXT NOT NULL,
    scan_date TEXT NOT NULL,
    rsi REAL,
    adx REAL,
    macd_signal TEXT,
    volume_ratio REAL,
    atr_pct REAL,
    stoch_k REAL,
    stoch_d REAL,
    pct_1w REAL,
    pct_2w REAL,
    pct_1m REAL,
    bb_position REAL,
    dist_from_high REAL,
    rs_vs_nifty REAL,
    vwap_position REAL,
    is_breakout BOOLEAN,
    vp_divergence BOOLEAN,
    weekly_trend TEXT,
    below_ema200 BOOLEAN,
    high_52w REAL,
    low_52w REAL,
    pullback_pct REAL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, scan_date)
);

CREATE TABLE IF NOT EXISTS fundamentals (
    symbol TEXT PRIMARY KEY,
    pe REAL,
    pb REAL,
    fwd_pe REAL,
    roe REAL,
    roa REAL,
    revenue_growth REAL,
    earnings_growth REAL,
    debt_to_equity REAL,
    promoter_pct REAL,
    market_cap REAL,
    free_cash_flow REAL,
    total_revenue REAL,
    capex REAL,
    eps_fwd REAL,
    eps_trail REAL,
    fund_score INTEGER,
    detailed_json JSONB,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS macro_events (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    country TEXT,
    impact TEXT,
    actual TEXT,
    forecast TEXT,
    surprise_dir TEXT,
    score REAL,
    event_date TEXT,
    event_time TEXT,
    scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS final_scores (
    symbol TEXT NOT NULL,
    scan_date TEXT NOT NULL,
    news_sentiment_score REAL,
    news_spike_score REAL,
    technical_score REAL,
    fundamental_score REAL,
    macro_score REAL,
    marketaux_score REAL,
    final_score REAL,
    grade TEXT,
    high_conviction BOOLEAN,
    bear_play BOOLEAN,
    is_golden BOOLEAN DEFAULT FALSE,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, scan_date)
);

CREATE TABLE IF NOT EXISTS scan_runs (
    scan_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL DEFAULT 'manual',
    status TEXT NOT NULL DEFAULT 'running',
    phase TEXT DEFAULT '',
    start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    end_time TIMESTAMP,
    processed_count INTEGER DEFAULT 0,
    failed_count INTEGER DEFAULT 0,
    deferred_count INTEGER DEFAULT 0,
    candidate_count INTEGER DEFAULT 0,
    duration_seconds REAL,
    error_message TEXT,
    universe_version TEXT,
    degraded_data BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS current_scan_state (
    id INTEGER PRIMARY KEY DEFAULT 1,
    scan_id TEXT,
    mode TEXT DEFAULT '',
    status TEXT DEFAULT 'idle',
    phase TEXT DEFAULT '',
    start_time TIMESTAMP,
    processed_count INTEGER DEFAULT 0,
    failed_count INTEGER DEFAULT 0,
    candidate_count INTEGER DEFAULT 0,
    cancel_requested INTEGER DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS symbol_state (
    symbol TEXT PRIMARY KEY,
    last_price_update TIMESTAMP,
    last_technical_update TIMESTAMP,
    last_news_update TIMESTAMP,
    last_sentiment_update TIMESTAMP,
    last_financial_update TIMESTAMP,
    last_deep_scan TIMESTAMP,
    price_change_pct REAL DEFAULT 0.0,
    prev_score INTEGER DEFAULT 0,
    needs_deep_scan INTEGER DEFAULT 0,
    deep_scan_reason TEXT DEFAULT '',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    sector TEXT DEFAULT '',
    entry_date TEXT NOT NULL,
    entry_price REAL NOT NULL,
    target_price REAL,
    stop_loss REAL,
    virtual_capital REAL DEFAULT 25000,
    quantity INTEGER DEFAULT 0,
    score_at_entry INTEGER DEFAULT 0,
    grade_at_entry TEXT DEFAULT '',
    technical_score REAL DEFAULT 0,
    fundamental_score REAL DEFAULT 0,
    earnings_momentum_score REAL DEFAULT 0,
    earnings_grade TEXT DEFAULT '',
    smart_money_score REAL DEFAULT 0,
    sector_rotation_score REAL DEFAULT 0,
    catalyst_score REAL DEFAULT 0,
    news_sentiment_score REAL DEFAULT 0,
    risk_score REAL DEFAULT 0,
    risk_reward REAL DEFAULT 0,
    model_version TEXT DEFAULT '',
    market_regime TEXT DEFAULT '',
    nifty_entry REAL,
    high_conviction INTEGER DEFAULT 0,
    is_golden INTEGER DEFAULT 0,
    signals_json TEXT DEFAULT '[]',
    earnings_signals_json TEXT DEFAULT '[]',
    exit_date TEXT,
    exit_price REAL,
    exit_reason TEXT,
    nifty_exit REAL,
    days_held INTEGER DEFAULT 0,
    return_pct REAL,
    alpha_pct REAL,
    max_drawdown_pct REAL DEFAULT 0,
    max_runup_pct REAL DEFAULT 0,
    status TEXT DEFAULT 'OPEN',
    position_size_pct REAL DEFAULT 20.0,
    weight_version TEXT DEFAULT '',
    confidence_score REAL DEFAULT 0,
    entry_rank INTEGER DEFAULT 0,
    breadth_advances INTEGER DEFAULT 0,
    breadth_declines INTEGER DEFAULT 0,
    breadth_ratio REAL DEFAULT 0,
    probability_bucket TEXT,
    expected_return_bucket TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS recommendation_snapshots (
    id SERIAL PRIMARY KEY,
    snapshot_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    rank INTEGER NOT NULL,
    score INTEGER DEFAULT 0,
    grade TEXT DEFAULT '',
    technical_score REAL DEFAULT 0,
    fundamental_score REAL DEFAULT 0,
    earnings_momentum_score REAL DEFAULT 0,
    earnings_grade TEXT DEFAULT '',
    smart_money_score REAL DEFAULT 0,
    risk_score REAL DEFAULT 0,
    price REAL DEFAULT 0,
    model_version TEXT DEFAULT '',
    market_regime TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (snapshot_date, symbol)
);

CREATE TABLE IF NOT EXISTS paper_portfolio_daily (
    date TEXT PRIMARY KEY,
    portfolio_value REAL DEFAULT 0,
    invested_value REAL DEFAULT 0,
    open_positions INTEGER DEFAULT 0,
    closed_today INTEGER DEFAULT 0,
    total_closed INTEGER DEFAULT 0,
    win_count INTEGER DEFAULT 0,
    loss_count INTEGER DEFAULT 0,
    total_return_pct REAL DEFAULT 0,
    nifty_level REAL DEFAULT 0,
    model_version TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS score_audit (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    scan_id TEXT NOT NULL,
    scan_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    technical_score NUMERIC,
    earnings_momentum_score NUMERIC,
    fundamental_score NUMERIC,
    smart_money_score NUMERIC,
    sector_rotation_score NUMERIC,
    news_sentiment_score NUMERIC,
    news_spike_score NUMERIC,
    macro_score NUMERIC,
    catalyst_score NUMERIC,
    final_score NUMERIC NOT NULL,
    data_source TEXT,
    source_reason TEXT,
    provider_latency_ms INTEGER,
    data_staleness_hours REAL,
    scan_version TEXT,
    score_breakdown JSONB,
    UNIQUE (symbol, scan_id)
);

CREATE TABLE IF NOT EXISTS scan_audit (
    id BIGSERIAL PRIMARY KEY,
    scan_id TEXT,
    start_time TIMESTAMP,
    end_time TIMESTAMP,
    duration_ms BIGINT,
    stocks_scanned INTEGER,
    stocks_succeeded INTEGER,
    stocks_failed INTEGER,
    data_source TEXT,
    scan_version TEXT,
    scan_mode TEXT DEFAULT 'manual'
);

-- ------------------------------------------------------------------------------
-- 2. Auth & Subscription Tables (from auth_db.py)
-- ------------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
    id              SERIAL PRIMARY KEY,
    google_sub      TEXT UNIQUE,
    email           TEXT NOT NULL UNIQUE,
    name            TEXT,
    picture_url     TEXT,
    phone           TEXT,
    status          TEXT NOT NULL CHECK (status IN ('pending','approved','rejected','suspended')),
    is_admin        INTEGER NOT NULL DEFAULT 0,
    trial_started_at TEXT,
    sub_plan_id     INTEGER,
    sub_started_at  TEXT,
    sub_expires_at  TEXT,
    created_at      TEXT NOT NULL,
    approved_at     TEXT,
    approved_by     TEXT,
    last_login_at   TEXT
);

CREATE TABLE IF NOT EXISTS devices (
    id                  SERIAL PRIMARY KEY,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    visitor_id          TEXT NOT NULL,
    confidence_score    REAL,
    user_agent          TEXT,
    ip                  TEXT,
    status              TEXT NOT NULL CHECK (status IN ('pending','active','revoked')),
    first_seen_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL,
    approved_at         TEXT,
    approved_by         TEXT,
    revoked_at          TEXT,
    revoked_by          TEXT,
    revoked_reason      TEXT
);

CREATE TABLE IF NOT EXISTS subscription_plans (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    duration_days   INTEGER NOT NULL CHECK (duration_days > 0),
    price_inr       INTEGER NOT NULL CHECK (price_inr >= 0),
    is_active       INTEGER NOT NULL DEFAULT 1,
    qr_image_path   TEXT,
    upi_id          TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payment_submissions (
    id                  SERIAL PRIMARY KEY,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    plan_id             INTEGER,
    utr                 TEXT NOT NULL,
    screenshot_path     TEXT,
    note                TEXT,
    status              TEXT NOT NULL CHECK (status IN ('pending','approved','rejected')),
    submitted_at        TEXT NOT NULL,
    reviewed_at         TEXT,
    reviewed_by         TEXT,
    review_note         TEXT
);

-- ------------------------------------------------------------------------------
-- 3. Indexes & Constraints
-- ------------------------------------------------------------------------------

-- App Indexes
CREATE INDEX IF NOT EXISTS idx_scan_results_score ON scan_results(score DESC);
CREATE INDEX IF NOT EXISTS idx_scan_results_hc ON scan_results(high_conviction) WHERE high_conviction = 1;
CREATE INDEX IF NOT EXISTS idx_scan_results_golden ON scan_results(((data->>'is_golden')::text)) WHERE (data->>'is_golden')::text IN ('true','1');
CREATE INDEX IF NOT EXISTS idx_scan_results_breakout ON scan_results(((data->>'is_breakout')::text)) WHERE (data->>'is_breakout')::text IN ('true','1');
CREATE INDEX IF NOT EXISTS idx_score_history_date ON score_history(scan_date DESC);
CREATE INDEX IF NOT EXISTS idx_news_articles_date ON news_articles(scanned_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_articles_symbol ON news_articles(symbol);
CREATE INDEX IF NOT EXISTS idx_scan_runs_status ON scan_runs(status);
CREATE INDEX IF NOT EXISTS idx_paper_trades_status ON paper_trades(status);
CREATE INDEX IF NOT EXISTS idx_paper_trades_return ON paper_trades(return_pct);
CREATE INDEX IF NOT EXISTS idx_paper_trades_symbol ON paper_trades(symbol);
CREATE INDEX IF NOT EXISTS idx_paper_trades_entry ON paper_trades(entry_date);
CREATE INDEX IF NOT EXISTS idx_paper_trades_model ON paper_trades(model_version);
CREATE INDEX IF NOT EXISTS idx_rec_snap_date ON recommendation_snapshots(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_score_audit_symbol ON score_audit(symbol);
CREATE INDEX IF NOT EXISTS idx_score_audit_time ON score_audit(scan_time DESC);

-- Phase 6: State transition audit trail (hash-chained)
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
CREATE INDEX IF NOT EXISTS idx_sst_scan_id ON scan_state_transitions(scan_id);
CREATE INDEX IF NOT EXISTS idx_sst_created ON scan_state_transitions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_scan_audit_time ON scan_audit(start_time DESC);

-- Auth Indexes
CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);
CREATE INDEX IF NOT EXISTS idx_users_email  ON users(email);

CREATE UNIQUE INDEX IF NOT EXISTS uniq_devices_visitor_active ON devices(visitor_id) WHERE status='active';
CREATE UNIQUE INDEX IF NOT EXISTS uniq_devices_user_active ON devices(user_id) WHERE status='active';
CREATE UNIQUE INDEX IF NOT EXISTS uniq_devices_user_pending ON devices(user_id) WHERE status='pending';
CREATE INDEX IF NOT EXISTS idx_devices_user ON devices(user_id);

CREATE INDEX IF NOT EXISTS idx_plans_active ON subscription_plans(is_active);

CREATE UNIQUE INDEX IF NOT EXISTS uniq_payments_user_pending ON payment_submissions(user_id) WHERE status='pending';
CREATE INDEX IF NOT EXISTS idx_payments_status ON payment_submissions(status);
CREATE INDEX IF NOT EXISTS idx_payments_user ON payment_submissions(user_id);

-- Phase A: Composite index for paper trade status+date queries (~318ms → <20ms)
CREATE INDEX IF NOT EXISTS idx_paper_trades_status_date ON paper_trades(status, entry_date);

-- ------------------------------------------------------------------------------
-- 4. Seed Data
-- ------------------------------------------------------------------------------

INSERT INTO current_scan_state (id, status, cancel_requested, updated_at)
VALUES (1, 'idle', 0, CURRENT_TIMESTAMP)
ON CONFLICT (id) DO NOTHING;

INSERT INTO settings (key, value, updated_at)
VALUES ('trial_duration_days', '3', CURRENT_TIMESTAMP::text)
ON CONFLICT (key) DO NOTHING;
