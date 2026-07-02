# Legacy Scoring Pipeline — End-to-End Architecture (as built today)

Stock entry → scoring → results → frontend. This documents the **legacy** engine
(`model_version='legacy'`), which currently runs **backend-only** while `scoring_v1`
drives the UI (engine toggle `scan_meta.ui_reco_source`, default `scoring_v1`).

```
 TRIGGER ───────► UNIVERSE ───────► SCAN LOOP ───────► SCORING ───────► PERSIST ───────► FRONTEND
 (scheduler/      (gated, frozen     (workers fetch     (analyzer.py     (scan_results_v2  (routes → APIs
  manual/boot)     eligible set)      OHLCV per stock)   per stock)       + 10 side tables) → JS render + WS)
```

---

## STAGE 1 — Stock entry (trigger → universe → scan loop)

### 1a. Triggers
| Trigger | Where | Notes |
|---|---|---|
| Manual | `routes/api.py` `/api/scan`, `/api/force-scan` | builds `ScanContext`, spawns `run_full_scan()` thread |
| Auto (institutional, **default**) | `app.py` `_institutional_scan_loop()` | pre-open ~08:45 + EOD ~18:30 IST, weekdays |
| Auto (legacy) | `app.py` `_auto_scan_loop()` | every `AUTO_SCAN_INTERVAL` (60m); only when `SCAN_SCHEDULE_MODE=legacy` |
| Boot resume | `app.py` (`db.get_pending_resume()`) | resumes a scan left `running` after restart |
| EOD scoring_v1 hook | `app.py` `_run_shadow_logging()` | when `SCORING_V1_LIVE=1`, EOD also runs `live_pipeline.run_daily()` + shadow analytics |

Config: `SCAN_SCHEDULE_MODE`, `AUTO_SCAN_ENABLED_DEFAULT`, `USE_UNIVERSE_ENGINE`, `FULL_UNIVERSE`.

### 1b. Universe build (the tradable set)
- **Master sync** (`master_sync.py`, ~14-day cadence) → upserts all NSE EQ + Dhan enrichment (mcap/PE/PB/ROE/sector) into **`universe_catalog`**.
- **Universe builder** (`universe_builder.py`) → when Dhan coverage ≥80%, builds a **versioned `eligible_universe`** (UNIVERSE_v001…) with Stage-3 gates:
  - mcap > `UNIVERSE_MIN_MCAP_CR` (~1000cr) · 20d turnover > `UNIVERSE_MIN_AVG_TURNOVER_CR` (10cr, **primary liquidity gate**) · 20d vol > `UNIVERSE_MIN_AVG_VOLUME` (10k) · price > `UNIVERSE_MIN_PRICE` (50).
  - Skips ETF/INDEX/MF/SME/suspended; always includes open positions + watchlist.
- **Selection at scan time** (`universe.py` `get_fast_scan_universe`): `eligible_universe[active_version]` (frozen) → fallback `universe_catalog` EQ. `FULL_UNIVERSE=1` → full catalog (~2200); else curated (~573).

### 1c. Scan loop / orchestration — `scanner.py run_full_scan(context)`
- `ScanContext.create()` (`scan_context.py`) captures immutable scan_id/correlation_id/config; `acquire_scan_lock()` writes a **`scan_runs`** row (`status='running'`) + `current_scan_state`; 30s heartbeat worker.
- Warmup (`warmup_all`): GDELT+FinBERT, RRG, macro, world markets; `get_nifty50_benchmark()` → 1M return + regime.
- **Phase 1** (Angel One, chunked by mcap, ~2 workers): per symbol `live_feed.fetch_historical(200d)` → `fetch_and_analyze()` → batch save. `chunk_runs` = resume checkpoints.
- **Phase 2** (jugaad_data fallback, `MAX_WORKERS`): for Phase-1 misses. **Data-quality gate**: abort if >5% fail both.
- Finalize: sector heatmap, AI summary, final `save_results`, research snapshots, **submit PENDING paper orders** for score≥65/HC; audit trail; subscribe results to WS.
- Terminal: `transition_scan_state` → `completed`/`failed`; `finally` forces terminal (never stuck).

**Handoff:** each surviving symbol → `fetch_and_analyze()` → result dict.

---

## STAGE 2 — Scoring (`analyzer.py` `fetch_and_analyze`, ~L420-1349)

Model version `R2.1`. Input: `(symbol, nifty_1m, regime, ext_df)`. Output: one rich result dict.

### 2a. Layer 1 — 25 technical indicators (raw ~0–200 pts)
RSI, MACD, EMA-stack(9/21/50/200), Bollinger+squeeze, **Volume surge**, ATR sweet-spot, Stochastic, momentum(1W/2W/1M), **OBV**, 52w-pullback, VWAP, ADX, CCI, Williams%R, **MFI**, Keltner, **CMF**, Ichimoku, Supertrend, EMA21-dev, RS-vs-Nifty, **Delivery%**, Fibonacci, breakout, **VP-divergence**. (**bold** = also feed `smart_money_raw`, max 76: vol+20/OBV+10/MFI+10/CMF+8/delivery+18/VPdiv+10.) Then weekly-trend align (±8) and regime adj (bearish ×0.85, bullish +5).

### 2b. Layers 2–12 — intelligence (`run_all_layers`)
Multi-timeframe trends, support/resistance, fundamentals (`fund_score` 0–32), earnings momentum (0–100), sector rotation/RRG (Leading 90 / Improving 70 / Weakening 35 / Lagging 15), news sentiment (GDELT+FinBERT), GDELT spike, macro bias, corporate events.

### 2c. Composite score (0–100) — normalize each component, sum, rescale
| Component | Weight | Normalizer |
|---|---|---|
| Technical | **30%** | raw/200 × 30 |
| Earnings momentum | **15%** | earn100 × 0.15 |
| Fundamental | **10%** | fund/32 × 10 |
| Smart money | **10%** | raw/76 × 10 |
| Sector rotation | **10%** | quadrant→0-100 × 0.10 |
| News sentiment | **8%** | 4 + ns/15×4 |
| Catalyst (MarketAux) | **10%** | (mx+1)/2×10 |
| Macro | **5%** | 2.5 + raw/25×2.5 |
| News spike | **2%** | (spike−1)×0.4 |

`final = round(raw_sum / max_available_weight × 100)`, clamped 0–100. If **fundamentals fully missing**, `max_available_weight = 90` (re-weights the rest).

### 2d. Classification
- **Grade**: ≥90 Super · 70-89 Strong · 50-69 Moderate · <50 Weak.
- **Golden** (`is_golden`): score≥80 AND tech≥21 AND fund≥6 AND earn≥8 AND smart≥5 AND R:R≥2.2 AND risk≤45.
- **High-conviction** (`high_conviction`): score≥55 + ≥5 bullish signals + RSI 40-70 + delivery≥40% + ATR 1.5-5.5% + risk≤40 + vol≥1.0× + **R:R≥2.2**.
- **Risk score** (0–100): baseline 15 + penalties (RSI extremes, ATR>5%, near-high, low vol, 1M moves, below-EMA200, ADX<15).

### 2e. Trade levels (ATR + structure hybrid)
- **Stop**: nearest structural support below price, default `price − 2.0×ATR` (≥2% away).
- **Target**: base mult 2.0× risk (3.0× if weekly-up, 1.8× if weekly-down, +0.5× if ADX>25), capped vs Fib/R1/R2. T1=R1/target, T2≈+8%, T3≈+10%.
- **Entry band**: breakout = R1/Fib ±1-1.5%; pullback = nearest EMA9/S1/pivot within 3%.
- `trade{}` sub-dict carries entry_low/high, stop_loss, target1/2/3, rr1/2/3, risk_reward, booking_plan.
- **RO projection** (`recommendation_engine/`, `RE2_RO_PROJECT=1`) re-projects legacy trade levels at save time; `target_utils.resolve_targets()` = the read-side fallback chain (`trade.* → scan.* → scan.target_price`).

---

## STAGE 3 — Persistence (`db.py save_results`, ~L3821)

Write path: staleness guard → **RO projection** (legacy only; skipped for scoring_v1) → thesis lock (`recommendation_locks`) → sanitize+JSON → bulk UPSERT.

**Primary table — `scan_results_v2`** PK `(scan_id, symbol)`: indexed `score`, `high_conviction`, `sector`, `scan_date`; full `data` JSON blob (+ `slim_data` ~600B); `model_version` (canonicalized `legacy`/`scoring_v1`).

Side tables written per scan: `score_history`, `technical_indicators`, `sentiment_scores`, `fundamentals`, `final_scores`, `stocks`, `news_articles`. Lifecycle/outcome: **`scan_runs`** (status + `end_time` = canonical freshness clock), `current_scan_state`, `recommendation_snapshots` (daily ledger, model-tagged), `recommendation_locks` (legacy thesis freeze), `paper_orders`/`paper_trades`/`paper_portfolio_daily` (model-tagged).

`_canon_model_version(mv)` → `scoring_v1` else `legacy`. Read functions: `get_ui_scan_id()` (engine-aware — scoring_v1 = latest by `updated_at`; legacy = `get_display_scan_id`→`get_latest_completed_scan_id`), `load_results(limit, slim, scan_id)` (score-desc), `get_stock(symbol, scan_id)`, `get_last_scan_display`, `get_result_count`, `get_meta`.

---

## STAGE 4 — Frontend delivery (`routes/pages.py`, `routes/api.py`, `templates/`)

Pages (all extend `_app_base.html`): `/dashboard`→`v3/dashboard.html`, `/top-picks`→`v3/top_picks.html`, `/stock|/symbol/<s>`→`symbol_workspace.html`, `/market`→`v3/market_intel.html`, `/mission-control`, `/outcome`, `/paper-trades-view`, `/golden|/hc|/breakouts`→**redirect to /top-picks** under scoring_v1.

Key APIs:
- **`/api/results`** — board list: `db.load_results` (slim, score-desc) → `_slim_results()` strips 12 `_HEAVY_FIELDS` (~92% payload cut) → legacy `recommendation_locks` merged **only when engine=legacy** → subscribes top-60 to WS → adds `conviction_rank`/`first_recommended`/`scan_generated_at` → sort+paginate.
- **`/api/stock/<s>`** — detail: OHLCV + indicator series (15m cache) + `_build_scan_dict()` (`.get()`-tolerant) + `resolve_targets()` + first_analysis/history; legacy `locked_thesis` + stale RO `trade` stripped for scoring_v1.
- **`/api/dashboard`** — composite (status + top-100 + sector RRG + paper_stats).
- **`/api/status`** — progress + counts (indexed-only; `last_scan` recomputed outside the indefinite idle cache).
- **`/api/live-prices`** — GET = all WS-cached ticks; POST = subscribe+fetch.
- **`/api/paper-trades`**, **`/api/paper-trades/stats`** — ledger + outcome aggregates.

Render: `top_picks.html` `renderTable()` + `updateLivePrices()` (**5s poll** of `/api/live-prices` → CMP + 1D% + entry-status live); `symbol_workspace.html` candlestick (lightweight-charts) + Thesis Radar (scoring_v1 `factor_percentiles`, legacy normalized breakdown) + 5s price poll; `dashboard.html` KPIs/heatmap/brief + live breadth + 7s re-poll while scanning.

---

## Cross-cutting
- **Engine isolation**: everything is `model_version`-scoped; `ui_reco_source` flips the UI between legacy and scoring_v1. Legacy + scoring_v1 coexist on the same symbol (dedup/cooldown scoped per engine).
- **Live partial results (GOAL #1)**: while scanning, `/api/results` + `/api/dashboard` bypass cache and pin one scan generation via `get_ui_scan_id()`.
- **Slim vs heavy**: list endpoints serve `slim_data`; heavy fields load only in the detail drawer.
- **Caching**: results/dashboard ~10s (bypass while scanning); detail series 15m (scan-invalidated); status held indefinitely while idle (last_scan patched fresh); live-prices uncached.

### Legacy vs scoring_v1 (where they diverge)
- Legacy = 25 indicators + 12 layers → weighted composite (above). scoring_v1 = locked 6-factor cross-sectional z→percentile (`quantresearch/scoring_v1/engine.py`).
- Legacy levels = ATR+structure+RO projection (1.5–2R, structural targets). scoring_v1 = `levels.py` (2×ATR capped at 8%, fixed 2/3/4R).
- Legacy has golden/HC/breakout + thesis locks. scoring_v1 has data-integrity / signal-agreement tiers + drivers.
- Both share Stages 1, 3, 4 (entry, persistence, frontend) — only Stage 2 (scoring) + level computation differ.
