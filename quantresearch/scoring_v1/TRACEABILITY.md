# scoring_v1 - Spec -> Code Traceability

Maps every section of `marketos_scoring_final_spec.md` (the locked spec; its
mechanism is embodied verbatim in `engine.py`, ported from
`marketos_scoring_engine_final.py`) to the exact code location implementing it in
`quantresearch/scoring_v1/`.

All paths are relative to `quantresearch/scoring_v1/`. "engine" = `engine.py`.

> ADDITIVE / READ-ONLY package. The engine is LOCKED (do not re-tune weights,
> change formulas, add factors, or change normalization). Everything else
> (gates, loaders, adapters, monitor) only assembles point-in-time inputs for the
> engine and observes its output. Runs in SHADOW; does not touch the live
> `analyzer.py` path.

---

## Section 1 - Gates (universe + quality)

| Spec item | Code location | Notes |
|---|---|---|
| Universe + quality hard gates run UPSTREAM of scoring | `gates.py :: apply_universe_gates(symbols, as_of_date)` -> `(eligible, rejected)` | Engine itself enforces ONLY the >=126-bar floor (see Section 1b). |
| NSE EQ instrument (instrument_type='EQ', is_active) | `gates.py :: apply_universe_gates` (instrument/active block, ~L370-382); catalog via `_load_catalog` (~L143) | Structural; non-EQ / inactive -> reject. |
| market_cap >= 1000 cr | `gates.py :: apply_universe_gates` mcap block (~L384-396); threshold `UNIVERSE_MIN_MCAP_CR` (config.py L131) | Structural: absent market_cap -> reject. |
| 20-day MEDIAN turnover >= 10 cr | `gates.py :: _last_n_turnovers` (~L182) + median block (~L407-418); `TURNOVER_WINDOW_BARS=20` (L75), `UNIVERSE_MIN_AVG_TURNOVER_CR` (config L132) | MEDIAN not mean. Turnover stored in LAKHS -> `/LAKHS_PER_CRORE (100)` (see Deviation b). |
| price >= 50 (last close <= as_of) | `gates.py :: _last_close` (~L200) + price block (~L398-405); `UNIVERSE_MIN_PRICE` (config L134) | |
| data coverage >= 0.90 | `gates.py :: _coverage_bar_count` (~L223), `_market_trading_dates` (~L263), `_expected_trading_days` (~L291) + coverage block (~L426-439); `UNIVERSE_MIN_DATA_COVERAGE` (config L135) | Fill-rate vs REAL trading calendar over listed period (see Deviation c). |
| listed >= 180 days | `gates.py :: _first_bar_date` (~L245) + listing block (~L441-444); `UNIVERSE_MIN_LISTING_DAYS` (config L136) | listing proxy = earliest daily_bars date. |
| QUALITY: ASM/GSM surveillance | `gates.py :: _quality_reject_reason` (~L307) -> `asm_gsm.is_under_surveillance` (`asm_gsm.py` L268); warmed by `asm_gsm.get_surveillance_sets` (L226) | Only wired quality signal. |
| QUALITY: suspended / pledge>30% / distress | `gates.py :: _quality_reject_reason` TODO stubs (~L321-324) | Not in pipeline -> MISSING -> PASS (never excludes), per spec quality policy. |
| missing-field policy (universe=reject-if-structural-absent; quality=missing->pass) | `gates.py :: GATE_THRESHOLDS["missing_field_policy"]` (~L109) + per-gate handling | |
| thresholds snapshot for reporting | `gates.py :: GATE_THRESHOLDS` (~L92) | |

### Section 1b - >=126-bar eligibility floor (engine-enforced)

| Spec item | Code location |
|---|---|
| >=126-bar eligibility floor (the ONLY gate inside the engine) | `engine.MIN_HISTORY_DAYS = 126` (L49); applied in `engine.score_universe` (`if len(df) < MIN_HISTORY_DAYS: continue`, L247) |
| Point-in-time input assembly feeding the gated universe -> engine | `adapter.py :: build_engine_inputs` (~L73), `run_scoring` (~L144) |
| Candidate universe / price / index / benchmark loaders | `pit_loader.py :: list_symbols_with_history` (L219), `load_price_df` (L85), `load_index_series` (L158), `load_benchmark` (L209) |

---

## Section 2 - Factor weights

| Spec item | Code location | Value |
|---|---|---|
| 6 scored factors + weights (sum 100) | `engine.FACTOR_WEIGHTS` (L35) | momentum 26, trend 20, smart_money 18, sector_rs 14, earnings 12, risk 10 |
| Fundamental is a GATE, not scored | `engine.FACTOR_WEIGHTS` comment (L35) + `gates.py` (Section 1) | not a factor |
| sub-factor weights per block (each sums 100) | `engine.SUBFACTOR_WEIGHTS` (L40-47) | see engine |
| WEIGHT_MODE {equal, tuned} | `engine._weights(mode)` (L224); both modes plumbed through `adapter.run_scoring(..., mode)` (L144) and `monitor` (mode arg) | equal = uniform; tuned = spec weights normalized |

---

## Section 3 - Feature Registry (per factor)

All raw features computed in `engine.compute_symbol_features(df, sec_idx, bench, earn)` (L124). All features "higher = better".

| Factor | Sub-features (engine key) | Code location |
|---|---|---|
| Momentum | m_rs_rank, m_mom_1m3m, m_52w_prox, m_rvol, m_fip | `compute_symbol_features` L130-140; `m_rs_rank` cross-sectional in `score_universe` L257 |
| Trend | t_ema_stack, t_hh_hl, t_adx, t_slope, t_persistence | L142-149; helpers `_ema` (L65), `_adx` (L73), `_swing_structure` (L99) |
| Smart Money | s_delivery, s_obv, s_cmf, s_volflow | L151-157; helpers `_obv` (L85), `_cmf` (L88) |
| Sector RS | r_rrg, r_sector_pct | L159-170 (RRG); `r_sector_pct` cross-sectional in `score_universe` L258 |
| Earnings | e_growth, e_accel, e_margin, e_surprise | L172-192; fed by `earnings_adapter.build_earnings` (`earnings_adapter.py` L130) |
| Risk | v_atr_fit, v_compression, v_gap_safety, v_dd_stability | L194-203; helpers `_atr` (L68), `_max_drawdown` (L94); `ATR_BAND` (L54) |
| Sector-index name resolution (feeds Sector RS) | `sector_map.get_sector_index_name(symbol)` (`sector_map.py` L119); map `SECTOR_TO_NSE_INDEX` (L61) | (see Deviation d) |
| Earnings PIT dict (rev/pat growth, accel, OPM, EPS, days_since_result) | `earnings_adapter.build_earnings` (L130), `build_earnings_batch` (L265) | (see Deviations e, f) |

---

## Section 4 - Normalization

| Spec item | Code location | Notes |
|---|---|---|
| cross-sectional z-score, winsorize, NaN->0 neutral | `engine._winz_z(s)` (L210); applied per sub-feature in `score_universe` (L262) | `Z_CLIP = 3.0` (L53) |
| percentile-rank features (RS rank, sector pct) | `score_universe` L257-258 (`.rank(pct=True)*100`) | |
| earnings freshness decay | `engine._earn_decay(d)` (L217); `EARN_FRESH_DAYS=10` (L51), `EARN_STALE_DAYS=75` (L52); applied `score_universe` L268-269 | decays earnings factor_z to neutral when stale/missing |

---

## Section 5 - Composite

| Spec item | Code location |
|---|---|
| factor_z = weighted sum of normalized sub-features | `score_universe` L265-267 (`factor_z[fct] = sum(z[c]*sw[fct][c])`) |
| earnings decay applied to its factor_z before composite | `score_universe` L268-269 |
| composite_z = weighted sum of factor_z | `score_universe` L270 (`composite = sum(factor_z[f]*fw[f])`) |
| score 0-100 = percentile of composite | `score_universe` L271 (`score = composite.rank(pct=True)*100`) |

---

## Section 6 - Confidence (DISPLAY ONLY)

| Spec item | Code location | Notes |
|---|---|---|
| data_integrity tier | `score_universe` L274-275 (`present`*0.8 + `decay`*0.2 -> `_tier`); `OHLCV_FEATURES` (L60); `_tier` (L234) | DISPLAY ONLY - never ranks/sizes/gates |
| signal_agreement tier | `score_universe` L276-277 (dispersion of factor_z -> `_tier`) | DISPLAY ONLY |

Locked rule honored: tiers are emitted as columns only; nothing downstream reads them for ranking/sizing/gating.

---

## Section 7 - Attribution

| Spec item | Code location |
|---|---|
| per-factor contribution c_<factor> = factor_z*fw | `score_universe` L280 (`contrib = {f: factor_z[f]*fw[f]}`), emitted with `c_` prefix (L285) |
| top-3 drivers / bottom-3 weaknesses | `engine._drivers(row, top, k=3)` (L237); `score_universe` L287-288 |

---

## Section 8 - Ranking

| Spec item | Code location |
|---|---|
| rank 1..N by composite_z desc | `score_universe` L286 (`rank = composite_z.rank(ascending=False, method="first")`); output sorted L289 |
| TOP_N buy band | `engine.TOP_N = 25` (L55) |
| hysteresis (hold to TOP_N*mult) | `engine.apply_hysteresis(ranked, held)` (L293); `HYSTERESIS_MULT = 2.0` (L56) |

---

## Section 9 - Monitoring (factor correlation)

| Spec item | Code location | Notes |
|---|---|---|
| factor correlation monitor (|rho|>0.85 warn) | `engine.factor_correlation_monitor(factor_z_history)` (L303); `CORR_WARN = 0.85` (L57) | MONITORING ONLY - never in score |
| build factor_z history across dates + run monitor + readable report | `monitor.py :: run_correlation_monitor(as_of_dates, mode)` (NEW); `build_factor_z_history` recovers factor_z = `c_<factor>/fw[factor]` via `engine._weights(mode)`; `print_report`; CLI `main` | NEW file. CLI guarded by `bootstrap.require_pg()`. Monthly cadence intended (`--step 21`). |

---

## Section 10 - Validation switch (equal vs tuned walk-forward)

| Spec item | Code location | Notes |
|---|---|---|
| WEIGHT_MODE {equal, tuned} selectable | `engine._weights(mode)` (L224); threaded through `adapter.run_scoring(mode=...)` (L144) and `monitor` | both modes must run |
| PIT forward-return realisation (eval reads dates AFTER entry) | `pit_loader.load_price_df` (L85) - entry features are strictly `date<=as_of`; forward returns read post-entry bars (allowed for realised-outcome eval) | |
| PG environment guard (no SQLite fallback) | `bootstrap.require_pg()` (`bootstrap.py` L77) - loads repo .env, hard-fails if not on live PostgreSQL | mandatory for all entry points |

---

# Deviations

Logged per the build mandate. Each is an implementation fidelity note relative to
the original `marketos_scoring_engine_final.py` / spec; none alters the locked
scoring mechanism, score, or rank.

**(a) engine.py docstring glyphs ASCII-normalised; formulas verbatim.**
`engine.py` was ported verbatim from `marketos_scoring_engine_final.py`; only
comment/docstring glyphs (em-dash, arrows) were normalised to ASCII (`-`, `->`)
for Windows/cp1252 safety. ALL code, formulas, weights, windows, and constants
are unchanged (`engine.py` header L9-11).

**(b) gates turnover unit = LAKHS (`/100`), fixed from `/1e7`.**
`daily_bars.turnover` is ingested from NSE `sec_bhavdata_full` `TURNOVER_LACS` ->
stored in LAKHS of rupees, not rupees. 1 crore = 100 lakh, so
`turnover_cr = stored/100` via `LAKHS_PER_CRORE = 100.0` (`gates.py` L90, used
L412). Verified empirically (close*volume / stored turnover ~ 1e5 uniformly;
RELIANCE ~1677 cr). The earlier `/1e7` (assuming rupees) under-counted turnover
by 10^5 and rejected the entire universe.

**(c) coverage = fill-rate vs REAL trading calendar over listed period; fixed from 252-cal/252.**
Coverage now = distinct bar dates / market trading days the symbol COULD have
traded since its first bar, both within a trailing 365-CALENDAR-day window
(`gates.py` `COVERAGE_LOOKBACK_DAYS=365` L84, `_market_trading_dates` L263,
`_expected_trading_days` L291, applied L426-434). The previous version divided a
trailing-252-CALENDAR-day bar count by a fixed 252 denominator; trading days are
~69% of calendar days, so coverage capped at ~0.71 and EVERY stock failed the
0.90 gate. Makes coverage a data-QUALITY (no-gaps) gate, graceful for 180-365-day
names. Static `COVERAGE_EXPECTED_BARS=252` (L85) kept only as a fallback if the
market-calendar query fails.

**(d) sector_map upgraded + verified vs PG (~91% coverage).**
`sector_map.SECTOR_TO_NSE_INDEX` (`sector_map.py` L61) was rebuilt and verified
against the REAL production PG `index_bars` (160 distinct NSE indices); every
non-None index string exists VERBATIM in `index_bars`. 3 loose fits sharpened
(Industrial->Capital Goods, Power->Nifty Power, Construction->Nifty Construction)
and 11 sectors with a dedicated NSE index added (Chemicals/Defence/Cement/
Insurance/Logistics/Retail/Telecom/Railways + Hotels/Travel/Aviation->India
Tourism). Coverage 73% -> ~91%. Unmapped sectors -> None -> engine treats
sector_rs as NEUTRAL (no fabrication).

**(e) eps_consensus always None -> surprise uses eps_trend.**
No analyst-estimate source exists in the broker-free pipeline, so
`earnings_adapter.build_earnings` returns `eps_consensus = None` always
(`earnings_adapter.py` L259). The engine's `e_surprise` therefore falls back from
the consensus formula to the eps_trend formula (`engine.compute_symbol_features`
L181-187: `(ea-ec)/|ec|` only when consensus present, else `(ea-et)/|et|`).
`eps_trend` is derived from the trailing-4-quarter EPS mean
(`earnings_adapter.py` L201-207).

**(f) days_since_result from NSE corp-actions.**
`days_since_result` is anchored to a genuine NSE result/board-meeting event date
<= as_of (never a quarter-end), via
`earnings_adapter._latest_result_date -> intelligence.corporate_actions.get_corporate_actions`
(`earnings_adapter.py` L96-123, applied L162-167). Future/unknown dates -> None,
which makes `engine._earn_decay` neutralise the earnings block (no look-ahead).

---

# Data window & verification caveat

The PG point-in-time store covers **2025-06-02 .. 2026-06-26** (~1 trading year;
daily_bars ~638k rows / 2727 symbols; index_bars 160 indices). The 126-bar
eligibility floor makes scoring reliable from ~2025-12 onward, and 20-day forward
evaluation needs data up to as_of+20d, so the usable walk-forward / monitoring
window is approximately **2025-12 .. 2026-05**. All `monitor.py` runs and any
validation MUST be executed on the REAL PG store (`bootstrap.require_pg()` first);
the SQLite fallback is empty and was the cause of the Phase-1 verification bugs
(b) and (c) above.
