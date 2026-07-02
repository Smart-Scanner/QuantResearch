#!/usr/bin/env python3
"""
Smart Screener — Entry Point
NSE Stock Screener + Portfolio Manager with Angel One Live Feed
"""

import os
import time
import signal
import logging
import threading
import warnings

# Phase A: Suppress pandas/pytz timezone UserWarning (log noise, non-actionable)
warnings.filterwarnings(
    "ignore",
    message=".*no explicit representation of timezones.*",
    category=UserWarning,
)

from dotenv import load_dotenv
load_dotenv()  # must run before any config import that reads env vars

# Set Windows Process Priority to BELOW_NORMAL to prevent CPU starvation/laptop freezes
import sys
if sys.platform == "win32":
    try:
        import ctypes
        # 0x00004000 = BELOW_NORMAL_PRIORITY_CLASS
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)
        logging.basicConfig(level=logging.INFO)
        logging.getLogger("screener").info("System: Windows process priority set to BELOW_NORMAL to optimize responsiveness.")
    except Exception:
        pass

from flask import Flask, session, request, jsonify
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_compress import Compress

# Pre-create jugaad_data cache dirs to avoid race condition
for d in [os.path.expanduser("~/.cache/nsehistory-stock"),
          os.path.expanduser("~/.cache/nsehistory-index")]:
    os.makedirs(d, exist_ok=True)

# Ensure the local logs/ dir exists (runtime + error handler write here).
os.makedirs("logs", exist_ok=True)

import db
import auth_db
import live_feed
live_feed.load_token_map()  # Ensure angel_tokens.json exists before universe_sync runs
import cache_layer
from config import AUTO_SCAN_INTERVAL, FLASK_SECRET_KEY, DATA_LOOKBACK_DAYS
from scanner import scan_state, has_valid_cache, run_full_scan, _shutdown_event
from scan_context import ScanContext
from analyzer import fetch_and_analyze
from routes.pages import pages_bp
from routes.api import api_bp
from routes.portfolio import portfolio_bp
from routes.auth import auth_bp
from routes.admin import admin_bp
from routes.broker_zerodha import zerodha_bp

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("screener")

# ---------------------------------------------------------------------------
# Flask App
# ---------------------------------------------------------------------------
app = Flask(__name__)
# Honor X-Forwarded-* headers so OAuth callbacks built with url_for(_external=True)
# use the public HTTPS scheme/host (ngrok or any reverse proxy) instead of the
# local HTTP origin. Without this, Google rejects with redirect_uri_mismatch.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.secret_key = FLASK_SECRET_KEY or "nse-screener-dev-key-change-me"
if not FLASK_SECRET_KEY:
    log = logging.getLogger("screener")
    log.warning("FLASK_SECRET_KEY not set — using insecure dev key. Set it in .env before deploy.")

# P3: Gzip/Brotli compression — shrinks API payloads ~80%
Compress(app)

# P4: Browser caches static assets (CSS/JS/fonts) for 24 hours
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 86400
app.config['TEMPLATES_AUTO_RELOAD'] = True

# Register blueprints
app.register_blueprint(auth_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(pages_bp)
app.register_blueprint(api_bp)

@app.errorhandler(404)
def handle_not_found(e):
    """Clean 404 — JSON for /api/* paths, minimal HTML otherwise."""
    if request.path.startswith("/api/"):
        return jsonify({"error": "not found", "path": request.path}), 404
    return "<h1>404 Not Found</h1>", 404


@app.errorhandler(Exception)
def handle_exception(e):
    # Let werkzeug HTTP errors (404, 405, etc.) pass through with their real
    # status instead of being masked as 500 by the catch-all below.
    if isinstance(e, HTTPException):
        return e
    import traceback
    tb = traceback.format_exc()
    try:
        os.makedirs('logs', exist_ok=True)
        with open('logs/flask_err.txt', 'w') as errf:
            errf.write(tb)
    except Exception:
        pass
    log.error("Unhandled exception: %s", tb)
    return str(e), 500

app.register_blueprint(portfolio_bp)
app.register_blueprint(zerodha_bp)


@app.context_processor
def inject_template_globals():
    """Variables auto-available in every Jinja template."""
    from datetime import datetime
    return {"current_year": datetime.now().year}


# ---------------------------------------------------------------------------
# Single-user mode — transparent local-admin auto-login (no login wall).
# Gated by SINGLE_USER_MODE (config). Reuses the existing auth user + decorators,
# so multi-user behaviour is fully preserved when SINGLE_USER_MODE=0.
# ---------------------------------------------------------------------------
from config import SINGLE_USER_MODE

_LOCAL_ADMIN_EMAIL = "admin@local.dev"


@app.before_request
def _single_user_autologin():
    if not SINGLE_USER_MODE or session.get("user_id"):
        return
    try:
        _u = auth_db.get_or_create_local_admin(_LOCAL_ADMIN_EMAIL, name="Local Admin")
        session["user_id"] = _u["id"]
        session["email"] = _u["email"]
    except Exception as exc:
        log.warning("[single-user] auto-login failed: %s", exc)


@app.route("/healthz")
def _healthz():
    """Lightweight liveness probe (no DB/broker dependency)."""
    return {"status": "ok", "service": "quantresearch"}, 200

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
log.info("QuantResearch | Personal Quant Research Platform (single-user, local-first)")

# Init DBs
db.init_db()
auth_db.init_db()

# Phase A: Non-blocking status cache warm-up
# Runs in daemon thread so DB coldness/slowness never delays startup
def _warmup_compute():
    """Minimal compute function used only for startup warm-up."""
    state = scan_state.status()
    use_pg = db.is_postgresql() and not db.pg_cooldown_active()
    try:
        scan_id = db.get_latest_completed_scan_id()
        if use_pg:
            agg = db.execute_db("""
                SELECT
                    COALESCE(SUM(high_conviction), 0) as hc_count,
                    COALESCE(SUM(CASE WHEN (data->>'is_golden')::text IN ('true','1') THEN 1 ELSE 0 END), 0) as golden_count,
                    COALESCE(SUM(CASE WHEN COALESCE(NULLIF(data->>'change_pct',''),'0')::numeric > 0 THEN 1 ELSE 0 END), 0) as adv_count,
                    COALESCE(SUM(CASE WHEN COALESCE(NULLIF(data->>'change_pct',''),'0')::numeric < 0 THEN 1 ELSE 0 END), 0) as dec_count
                FROM scan_results_v2
                WHERE scan_id = %s
            """, (scan_id,), fetch="one")
        else:
            raise Exception("use sqlite")
    except Exception:
        agg = db.execute_db("""
            SELECT
                COALESCE(SUM(high_conviction), 0) as hc_count,
                COALESCE(SUM(CASE WHEN json_extract(data, '$.is_golden') IN (1, 'true') THEN 1 ELSE 0 END), 0) as golden_count,
                COALESCE(SUM(CASE WHEN CAST(json_extract(data, '$.change_pct') AS REAL) > 0 THEN 1 ELSE 0 END), 0) as adv_count,
                COALESCE(SUM(CASE WHEN CAST(json_extract(data, '$.change_pct') AS REAL) < 0 THEN 1 ELSE 0 END), 0) as dec_count
            FROM scan_results_v2
            WHERE scan_id = ?
        """, (db.get_latest_completed_scan_id(),), fetch="one")
    hc = agg.get("hc_count", 0) if isinstance(agg, dict) else 0
    golden = agg.get("golden_count", 0) if isinstance(agg, dict) else 0
    adv = agg.get("adv_count", 0) if isinstance(agg, dict) else 0
    dec = agg.get("dec_count", 0) if isinstance(agg, dict) else 0
    return {
        "scanning": state.get("scanning", False),
        "progress": state.get("progress", 0),
        "total": state.get("total", 0),
        "last_scan": db.get_meta("last_scan"),
        "market_regime": db.get_meta("market_regime", "unknown"),
        "login_status": db.get_meta("angel_login_status", {}),
        "hc_count": hc,
        "golden_count": golden,
        "adv_count": adv,
        "dec_count": dec,
    }

threading.Thread(
    target=cache_layer.warm_status_cache,
    args=(_warmup_compute,),
    daemon=True,
    name="cache-warmup",
).start()

# Phase 5.5: Startup resume + universe rebuild check
from config import USE_UNIVERSE_ENGINE, AUTO_SCAN_ENABLED_DEFAULT
if USE_UNIVERSE_ENGINE:
    log.info("[Phase 5.5] Universe Engine ACTIVE")
    
    # Background Boot Sequence (New Pipeline: Universe Sync → Bhavcopy → Eligible)
    def _boot_universe_prep():
        # Reset stale locks from previous container (Railway deploy = new container)
        try:
            db.set_meta("master_sync_status", "idle")
            db.set_meta("liquidity_worker_status", "idle")
            log.info("[BootPrep] Reset stale locks")
        except Exception:
            pass

        log.info("[BootPrep] Running NEW Universe Pipeline (Sync → Bhavcopy → Eligible)...")
        try:
            from nse_bhavcopy import run_bhavcopy_pipeline
            result = run_bhavcopy_pipeline()
            eligible = result.get("universe", {}).get("eligible_count", 0)
            log.info("[BootPrep] ✅ Universe Pipeline Complete — %d eligible stocks", eligible)
        except Exception as e:
            log.error("[BootPrep] New Pipeline error: %s — falling back to legacy", e)
            # Fallback to old pipeline if new one fails
            try:
                from master_sync import run_master_sync
                run_master_sync()
            except Exception as e2:
                log.error("[BootPrep] Legacy Master Sync also failed: %s", e2)
            try:
                from universe_builder import build_eligible_universe
                build_eligible_universe()
            except Exception as e3:
                log.error("[BootPrep] Legacy Universe Build also failed: %s", e3)

        log.info("[BootPrep] Universe Prep Complete.")
        
    threading.Thread(target=_boot_universe_prep, daemon=True, name="boot-prep").start()

    # Check for incomplete scan from Railway restart
    _resume = db.get_pending_resume()
    if _resume and _resume.get("status") == "running":
        log.info("[Phase 5.5] Found incomplete scan %s — scheduling resume",
                 _resume.get("scan_id", "unknown"))
        _resume_ctx = ScanContext.create(trigger_source="resume", user_id="system", mode="auto")
        threading.Thread(target=run_full_scan, args=(_resume_ctx, _resume.get("scan_id")), daemon=True,
                         name="scan-resume").start()
    else:
        log.info("[Phase 5.5] No pending resume state")

# Start Angel One WebSocket for live prices
def _start_websocket_with_retry():
    """Start WebSocket with retry — handles Railway network timeout on first boot."""
    import time as _time
    for attempt, delay in enumerate([0, 30, 90], start=1):
        if delay > 0:
            log.info("WebSocket retry #%d in %ds...", attempt, delay)
            _time.sleep(delay)
        try:
            live_feed.start_websocket()
            log.info("Angel One WebSocket started (attempt #%d)", attempt)
            return
        except Exception as exc:
            log.warning("WebSocket attempt #%d failed: %s", attempt, exc)
    log.error("WebSocket failed after 3 attempts — using REST fallback for live prices")

import threading
threading.Thread(target=_start_websocket_with_retry, name="ws-startup", daemon=True).start()


# ── STAGE-1 daily data ingestion (06:00 IST) ──────────────────────────────────
# Fetch + store EVERYTHING once each morning (price -> daily_bars, fundamentals ->
# universe_catalog, earnings -> earnings_store) so the research scans run from the
# stored layer with ZERO external fetch. Idempotent; runs at most once per day.
def _daily_ingestion_loop():
    import time as _t
    from datetime import datetime as _dt
    try:
        from zoneinfo import ZoneInfo
        _IST = ZoneInfo("Asia/Kolkata")
    except Exception:
        _IST = None
    _last = None
    while True:
        try:
            now = _dt.now(_IST) if _IST else _dt.now()
            if now.hour == 6 and _last != now.date():
                _last = now.date()
                log.info("[ingest] 06:00 IST — Stage-1 daily ingestion starting")
                try:
                    from quantresearch.scoring_v1 import data_ingest
                    res = data_ingest.run_ingestion(refresh_price=True, refresh_fundamentals=True)
                    log.info("[ingest] Stage-1 done: %s", res)
                except Exception as exc:
                    log.error("[ingest] Stage-1 daily ingestion failed: %s", exc)
        except Exception:
            pass
        _t.sleep(300)  # re-check every 5 min

threading.Thread(target=_daily_ingestion_loop, name="daily-ingest", daemon=True).start()

# Load cached data or start fresh scan
if has_valid_cache():
    log.info("DB has valid cache (%d stocks). Subscribing to live feed...", db.get_result_count())
    cached_syms = db.get_all_symbols()
    if cached_syms:
        live_feed.subscribe(cached_syms)
        log.info("Subscribed %d cached stocks to live feed", len(cached_syms))
    
    # Warm up global intelligence snapshots (RRG, macro, FRED, GDELT) on startup in background
    from intelligence import warmup_all
    threading.Thread(target=lambda: warmup_all(set(cached_syms) if cached_syms else None), daemon=True, name="startup-warmup").start()
else:
    log.info("No valid cache. Checking auto_scan_enabled toggle before starting first scan...")
    _enabled = db.get_meta("auto_scan_enabled")
    _is_enabled = (_enabled == "1") if _enabled else AUTO_SCAN_ENABLED_DEFAULT
    
    if _is_enabled:
        # Phase 1: Create ScanContext for initial scan
        _startup_ctx = ScanContext.create(trigger_source="auto", user_id="system", mode="auto")
        threading.Thread(target=run_full_scan, args=(_startup_ctx,), daemon=True).start()
    else:
        log.info("AUTO_SCAN_ENABLED is disabled. Waiting for manual scan start or toggle via Mission Control.")


# ---------------------------------------------------------------------------
# Auto-scan scheduler (Phase 4: Event-driven)
# ---------------------------------------------------------------------------

# Interval constants (seconds)
_NEWS_INTERVAL   = 15 * 60     # News refresh every 15 min
_FAST_INTERVAL   = AUTO_SCAN_INTERVAL * 60  # Fast scan (from config, default 60 min)
_DEEP_INTERVAL   = 120 * 60    # Deep scan every 2 hours
_MACRO_INTERVAL  = 60 * 60     # Macro refresh every 1 hour
_GRACE_PERIOD    = 5 * 60      # Skip if manual scan ran < 5 min ago

def _auto_scan_loop():
    """
    Event-driven auto-scan loop (Phase 4).
    Order: News refresh -> Fast scan -> Macro refresh -> Deep scan (if needed).
    Phase 5.5: Also handles master sync + daily universe rebuild.
    """
    from scanner import refresh_news_pipeline, _shortlist_for_deep_scan
    from intelligence import warmup_all

    time.sleep(60)  # startup grace

    # Load timestamps from DB
    def _get_ts(key, default=0.0):
        v = db.get_meta(key)
        try:
            return float(v) if v else default
        except (ValueError, TypeError):
            return default

    last_news  = _get_ts("last_news_refresh_ts")
    last_fast  = _get_ts("last_fast_scan_ts")
    last_deep  = _get_ts("last_deep_scan_ts")
    last_macro = _get_ts("last_macro_refresh_ts")
    last_universe_rebuild = _get_ts("last_universe_rebuild_ts")

    while True:
        try:
            now = time.time()

            # Phase 1.5 (Change Set D): scheduler liveness heartbeat. Flag-gated —
            # PHASE15_OPS_ENDPOINT OFF => no write (production-identical). Epoch seconds.
            if os.environ.get("PHASE15_OPS_ENDPOINT") == "1":
                try:
                    db.set_meta("scheduler_heartbeat_ts", str(now))
                except Exception:
                    pass

            # Grace period: skip if manual scan ran < 5 min ago
            last_any = _get_ts("last_scan_ts")
            if last_any and (now - last_any) < _GRACE_PERIOD:
                log.debug("[AutoScan] Grace period active, sleeping")
                time.sleep(30)
                continue

            # Phase 5.5: Master Sync (every 14 days)
            if USE_UNIVERSE_ENGINE:
                try:
                    from master_sync import is_master_sync_due, run_master_sync
                    if is_master_sync_due():
                        log.info("[Phase 5.5] Master sync due — starting")
                        run_master_sync()
                except Exception as exc:
                    log.warning("[Phase 5.5] Master sync failed: %s", exc)

                # Daily universe refresh (bhavcopy enrichment at configured hour)
                try:
                    from datetime import datetime, timezone, timedelta as _td
                    _IST = timezone(_td(hours=5, minutes=30))
                    _now_ist = datetime.now(_IST)
                    from config import UNIVERSE_REBUILD_HOUR, UNIVERSE_REBUILD_MINUTE
                    if (_now_ist.hour == UNIVERSE_REBUILD_HOUR and
                        _now_ist.minute >= UNIVERSE_REBUILD_MINUTE and
                        _now_ist.minute < UNIVERSE_REBUILD_MINUTE + 5 and
                        (now - last_universe_rebuild) > 3600):
                        log.info("[DailyRefresh] Daily bhavcopy pipeline triggered")
                        from nse_bhavcopy import run_bhavcopy_pipeline
                        threading.Thread(
                            target=run_bhavcopy_pipeline,
                            daemon=True,
                            name="daily-bhavcopy"
                        ).start()
                        last_universe_rebuild = time.time()
                        db.set_meta("last_universe_rebuild_ts", str(last_universe_rebuild))
                except Exception as exc:
                    log.warning("[DailyRefresh] Daily universe refresh failed: %s", exc)

            market_open = live_feed.is_market_open()
            # TEMP validation hook (RC3-B closeout): SCHEDULER_FORCE_MARKET_OPEN=1 forces ONLY the
            # market-open check to True so one full scheduler cycle can run off-hours for deployment
            # validation. Default "0" ⇒ behaviour IDENTICAL to production. Every other gate (interval,
            # is_scanning, auto_scan_enabled, run_full_scan) still executes through the normal path.
            # Disable (set to 0) immediately after validation to restore NSE-hours behaviour.
            if os.environ.get("SCHEDULER_FORCE_MARKET_OPEN", "0") == "1":
                market_open = True

            # 1. NEWS REFRESH — first in market hours
            if market_open and (now - last_news >= _NEWS_INTERVAL):
                log.info("[AutoScan] News refresh starting...")
                try:
                    universe = set(db.get_all_symbols() or [])
                    event_signals = refresh_news_pipeline(universe)
                    last_news = time.time()
                    db.set_meta("last_news_refresh_ts", str(last_news))
                    log.info("[AutoScan] News refresh done")
                except Exception as exc:
                    log.warning("[AutoScan] News refresh error: %s", exc)
                    event_signals = {"spikes": set(), "announcements": set()}
            else:
                event_signals = {"spikes": set(), "announcements": set()}

            # 2. FAST SCAN — second in market hours
            needs_deep = False
            _enabled = db.get_meta("auto_scan_enabled")
            _is_enabled = (_enabled == "1") if _enabled else AUTO_SCAN_ENABLED_DEFAULT

            if market_open and (now - last_fast >= _FAST_INTERVAL) and not scan_state.is_scanning:
                if _is_enabled:
                    log.info("[AutoScan] Market open -- starting fast scan")
                    # Phase 1: Create ScanContext for auto-scan
                    _auto_ctx = ScanContext.create(
                        trigger_source="auto", user_id="system", mode="auto",
                    )
                    run_full_scan(_auto_ctx)
                    last_fast = time.time()
                    db.set_meta("last_fast_scan_ts", str(last_fast))
                    db.set_meta("last_scan_ts", str(last_fast))
                    # Change Set D fix: refresh the scheduler heartbeat right after the blocking
                    # scan so the post-scan window isn't read as stalled (flag-gated).
                    if os.environ.get("PHASE15_OPS_ENDPOINT") == "1":
                        try:
                            db.set_meta("scheduler_heartbeat_ts", str(last_fast))
                        except Exception:
                            pass
                    needs_deep = True
                else:
                    log.debug("[AutoScan] Fast scan scheduled but AUTO_SCAN_ENABLED is 0. Skipping.")
            elif not market_open:
                last = db.get_meta("last_scan")
                if not last:
                    if _is_enabled:
                        log.info("[AutoScan] No data yet -- starting scan")
                        _auto_ctx = ScanContext.create(
                            trigger_source="auto", user_id="system", mode="auto",
                        )
                        run_full_scan(_auto_ctx)
                        last_fast = time.time()
                        db.set_meta("last_fast_scan_ts", str(last_fast))
                        db.set_meta("last_scan_ts", str(last_fast))
                        if os.environ.get("PHASE15_OPS_ENDPOINT") == "1":   # Change Set D fix: post-scan heartbeat
                            try:
                                db.set_meta("scheduler_heartbeat_ts", str(last_fast))
                            except Exception:
                                pass
                    else:
                        log.debug("[AutoScan] Initial scan pending but AUTO_SCAN_ENABLED is 0. Skipping.")

            # 3. MACRO REFRESH — any time
            if now - last_macro >= _MACRO_INTERVAL:
                log.info("[AutoScan] Macro refresh...")
                try:
                    from intelligence.macro import scan_world_markets
                    from intelligence.macro_events import scan_macro_events
                    scan_world_markets()
                    scan_macro_events()
                    last_macro = time.time()
                    db.set_meta("last_macro_refresh_ts", str(last_macro))
                except Exception as exc:
                    log.warning("[AutoScan] Macro refresh error: %s", exc)

            # 4. DEEP SCAN — if event signals or interval exceeded
            has_events = bool(event_signals.get("spikes") or event_signals.get("announcements"))
            if (needs_deep or has_events or (now - last_deep >= _DEEP_INTERVAL)) and not scan_state.is_scanning:
                # Deep scan is only for shortlisted candidates, not a full re-scan
                try:
                    all_results = db.get_all_results()  # current fast scan results from DB
                    if all_results:
                        shortlist = _shortlist_for_deep_scan(all_results, event_signals)
                        if shortlist:
                            log.info("[AutoScan] Deep scan for %d shortlisted candidates", len(shortlist))
                            nifty_1m = db.get_meta("nifty50_1m", 0)
                            regime = db.get_meta("market_regime", "unknown")
                            deep_results = []
                            for sym in shortlist:
                                try:
                                    df = live_feed.fetch_historical(sym, days=DATA_LOOKBACK_DAYS)
                                    if df is not None and not df.empty:
                                        r = fetch_and_analyze(sym, nifty_1m, regime, ext_df=df, scan_mode="deep")
                                        if r:
                                            deep_results.append(r)
                                except Exception:
                                    pass
                            if deep_results:
                                db.save_results(deep_results)
                                log.info("[AutoScan] Deep scan complete: %d stocks enriched", len(deep_results))
                        last_deep = time.time()
                        db.set_meta("last_deep_scan_ts", str(last_deep))
                except Exception as exc:
                    log.warning("[AutoScan] Deep scan error: %s", exc)

        except Exception as exc:
            log.warning("[AutoScan] Error: %s", exc)

        time.sleep(30)  # check every 30 seconds


# ---------------------------------------------------------------------------
# Institutional scan scheduler (additive; gated by SCAN_SCHEDULE_MODE)
# ---------------------------------------------------------------------------
# SCAN_SCHEDULE_MODE selects which auto-scan scheduler runs:
#   'institutional' (DEFAULT) -> _institutional_scan_loop below: PRE-OPEN (~08:45 IST)
#                                + EOD (~18:30 IST, after bhavcopy_history.append_latest()).
#                                NO hourly re-scan; intraday is live-price only (unchanged).
#   'legacy'                  -> the original every-60-min _auto_scan_loop (kept intact).
# ROLLBACK: set env SCAN_SCHEDULE_MODE=legacy to restore the old 60-min churn exactly.
SCAN_SCHEDULE_MODE = os.getenv("SCAN_SCHEDULE_MODE", "institutional")

# Institutional window definitions (IST hour, minute).
_PREOPEN_HOUR, _PREOPEN_MIN = 8, 45     # ~08:45 IST pre-open scan
_EOD_HOUR, _EOD_MIN         = 18, 30    # ~18:30 IST end-of-day scan
_WINDOW_SPAN_MIN            = 15        # how long each window stays "active" for catch-up

# ── scoring_v1 EOD pipeline (ADDITIVE, flag-gated, OFF by default) ──
# When SCORING_V1_LIVE=1, the EOD window — AFTER the legacy scan and the bhavcopy
# append — runs the scoring_v1 LIVE pipeline: scores the universe (tuned), tags
# scan_results_v2/recommendation_snapshots model_version='scoring_v1' (the UI reads
# these via the engine toggle), and auto paper-trades the top picks tagged
# 'scoring_v1' (rank<=25 after the over-extension entry filter). Legacy keeps
# running backend-only tagged 'legacy'. Additionally logs the equal-vs-tuned shadow
# backtest table (scoring_v1_shadow) + realised 5/10/20d forward returns.
# Best-effort, never raises into the scheduler. ROLLBACK: leave unset / set to 0.
SCORING_V1_LIVE = os.getenv("SCORING_V1_LIVE", "0") == "1"


def _run_shadow_logging():
    """EOD scoring_v1 pipeline (flag-gated by SCORING_V1_LIVE). Runs the live
    pipeline (UI tagging + auto paper-trades) then the shadow analytics. Anchors on
    the freshest bar in daily_bars (the EOD bar just appended). Idempotent-friendly;
    never raises into the scheduler."""
    if not SCORING_V1_LIVE:
        return
    # 1) LIVE pipeline — scoring_v1 drives the UI + auto paper-trades (model_version tagged)
    try:
        from quantresearch.scoring_v1 import live_pipeline as _lp
        out = _lp.run_daily()  # as_of = latest store date; real earnings; submits top picks
        log.info("[scoring_v1] EOD live pipeline: %s", {k: out.get(k) for k in
                 ("scan_id", "as_of", "eligible", "scored", "submitted", "skipped_thin_universe")})
    except Exception as exc:
        log.warning("[scoring_v1] EOD live pipeline failed (non-fatal): %s", exc)
    # 2) Shadow analytics — equal-vs-tuned + realised 5/10/20d forward returns
    try:
        from quantresearch.scoring_v1 import shadow as _shadow
        row = db.execute_db("SELECT MAX(date) AS d FROM daily_bars", fetch="one", require_pg=True)
        as_of_iso = str(row["d"])[:10] if row and row.get("d") else None
        if as_of_iso:
            _shadow.run_shadow(as_of_iso)
            _shadow.backfill_forward_returns()
    except Exception as exc:
        log.warning("[scoring_v1] EOD shadow analytics failed (non-fatal): %s", exc)


# ── legacy_cleaned EOD pipeline (ADDITIVE, flag-gated, DEFAULT ON) ──
# Third engine. Runs at EOD AFTER the legacy scan + bhavcopy append + the scoring_v1
# pipeline, reading ONLY the stored layer (zero external fetch). Tags scan_results_v2 /
# recommendation_snapshots / paper_trades model_version='legacy_cleaned'. Does NOT touch
# legacy or scoring_v1 (their scores/levels/clock are untouched). ROLLBACK: LEGACY_CLEANED_LIVE=0.
LEGACY_CLEANED_LIVE = os.getenv("LEGACY_CLEANED_LIVE", "1") == "1"


def _run_legacy_cleaned_eod():
    """EOD legacy_cleaned pipeline (flag-gated by LEGACY_CLEANED_LIVE, default ON).
    Additive third engine; store-only (zero network); never raises into the scheduler."""
    if not LEGACY_CLEANED_LIVE:
        return
    try:
        from quantresearch.legacy_cleaned import live_pipeline as _lcp
        out = _lcp.run_daily()  # store-only; scores + tags legacy_cleaned + auto paper-trades
        log.info("[legacy_cleaned] EOD live pipeline: %s", {k: out.get(k) for k in
                 ("scan_id", "as_of", "eligible", "scored", "submitted")})
    except Exception as exc:
        log.warning("[legacy_cleaned] EOD live pipeline failed (non-fatal): %s", exc)


def _institutional_scan_loop():
    """
    Institutional auto-scan schedule (Phase 4 successor, flag-gated).

    Runs TWO scans per weekday, IST-aware (Asia/Kolkata, +5:30):
      1. PRE-OPEN  ~08:45 IST  -> full scan ahead of the session.
      2. EOD       ~18:30 IST  -> bhavcopy_history.append_latest() (so today's bar
                                  is in the store) THEN a full scan.

    NO hourly re-scan (intraday is live-price only, unchanged). Skips weekends,
    skips if a scan is already active, and uses a run-once-per-window guard
    (persisted in db.get_meta) so a restart inside a window won't double-fire.

    Manual /api/scan and /api/force-scan remain fully independent of this loop.
    """
    from datetime import datetime, timezone, timedelta as _td

    _IST = timezone(_td(hours=5, minutes=30))

    time.sleep(60)  # startup grace (mirror legacy loop)

    def _in_window(now_ist, hour, minute):
        start = now_ist.replace(hour=hour, minute=minute, second=0, microsecond=0)
        end = start + _td(minutes=_WINDOW_SPAN_MIN)
        return start <= now_ist < end

    def _run_window_scan(window_name, do_bhavcopy_append):
        """Run a single institutional scan for the named window with guards."""
        _enabled = db.get_meta("auto_scan_enabled")
        _is_enabled = (_enabled == "1") if _enabled else AUTO_SCAN_ENABLED_DEFAULT
        if not _is_enabled:
            log.debug("[Institutional] %s window: AUTO_SCAN_ENABLED is 0. Skipping.", window_name)
            return
        if scan_state.is_scanning:
            log.info("[Institutional] %s window: a scan is already active. Skipping.", window_name)
            return

        # EOD: make sure today's bar is in the bhavcopy history store BEFORE scanning.
        if do_bhavcopy_append:
            try:
                import bhavcopy_history  # lazy import — optional dependency
                bhavcopy_history.append_latest()
                log.info("[Institutional] EOD: bhavcopy_history.append_latest() done")
            except Exception as exc:
                log.warning("[Institutional] EOD: bhavcopy_history.append_latest() failed: %s", exc)

        log.info("[Institutional] %s window -- starting full scan", window_name)
        _ctx = ScanContext.create(trigger_source="auto", user_id="system", mode="auto")
        run_full_scan(_ctx)
        _now = time.time()
        db.set_meta("last_scan_ts", str(_now))
        if os.environ.get("PHASE15_OPS_ENDPOINT") == "1":
            try:
                db.set_meta("scheduler_heartbeat_ts", str(_now))
            except Exception:
                pass
        log.info("[Institutional] %s window scan complete", window_name)

        # scoring_v1 EOD shadow logging (flag-gated OFF by default). Runs ONLY at
        # EOD, AFTER the bhavcopy append (today's bar in store) and AFTER the legacy
        # scan (so legacy scores exist for the date). Best-effort, never affects the
        # live scan. No-op unless SCORING_V1_SHADOW_ENABLED=1.
        if window_name == "EOD":
            _run_shadow_logging()
            _run_legacy_cleaned_eod()  # additive third engine (LEGACY_CLEANED_LIVE, default ON)

    while True:
        try:
            now_ist = datetime.now(_IST)

            # Phase 1.5 scheduler liveness heartbeat (flag-gated, production-identical when OFF).
            if os.environ.get("PHASE15_OPS_ENDPOINT") == "1":
                try:
                    db.set_meta("scheduler_heartbeat_ts", str(time.time()))
                except Exception:
                    pass

            # Weekdays only (Mon=0 .. Fri=4).
            if now_ist.weekday() < 5:
                today_str = now_ist.date().isoformat()

                # --- PRE-OPEN window (~08:45 IST) ---
                if _in_window(now_ist, _PREOPEN_HOUR, _PREOPEN_MIN):
                    guard_key = "last_institutional_preopen_date"
                    if db.get_meta(guard_key) != today_str:
                        db.set_meta(guard_key, today_str)  # set guard BEFORE scan to avoid re-fire
                        try:
                            _run_window_scan("PRE-OPEN", do_bhavcopy_append=False)
                        except Exception as exc:
                            log.warning("[Institutional] PRE-OPEN scan error: %s", exc)

                # --- EOD window (~18:30 IST) ---
                elif _in_window(now_ist, _EOD_HOUR, _EOD_MIN):
                    guard_key = "last_institutional_eod_date"
                    if db.get_meta(guard_key) != today_str:
                        db.set_meta(guard_key, today_str)  # set guard BEFORE scan to avoid re-fire
                        try:
                            _run_window_scan("EOD", do_bhavcopy_append=True)
                        except Exception as exc:
                            log.warning("[Institutional] EOD scan error: %s", exc)

        except Exception as exc:
            log.warning("[Institutional] Scheduler error: %s", exc)

        time.sleep(60)  # check every minute (windows are minute-granular)


def _portfolio_scan_loop():
    time.sleep(120)  # wait 2 mins for startup
    while True:
        try:
            log.info("[PortfolioScan] Running 30-min portfolio check...")
            positions = db.execute_db("SELECT id, symbol, buy_price, stop_loss, target FROM positions WHERE status = 'OPEN'", fetch="all")
            if positions:
                symbols = list(set(p["symbol"] for p in positions))
                # Use WebSocket cache instead of rate-limited REST bulk fetch
                prices = {}
                for s in symbols:
                    p_data = live_feed.get_live_price(s)
                    if p_data:
                        prices[s] = p_data
                scan_lookup = db.get_stocks_map(symbols)
                from datetime import datetime
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                for pos in positions:
                    sym = pos["symbol"]
                    buy_price = pos["buy_price"]
                    sl = pos["stop_loss"]
                    tgt = pos["target"]
                    pos_id = pos["id"]

                    # Fallback to scanner values if position-specific values are not set
                    scan = scan_lookup.get(sym, {})
                    if (sl is None or sl == 0) and scan:
                        sl = scan.get("stop_loss")
                    if (tgt is None or tgt == 0) and scan:
                        tgt = scan.get("target_price")

                    price_data = prices.get(sym)
                    if not price_data:
                        price_data = live_feed.get_live_price(sym)

                    if price_data:
                        ltp = price_data.get("ltp") or price_data.get("price", 0.0)
                        if not ltp:
                            continue

                        # Core hold, sell, book scenarios
                        if tgt is not None and ltp >= tgt:
                            rec = "Book Profit (Target Reached)"
                        elif sl is not None and ltp <= sl:
                            rec = "Exit / Stop Loss Triggered"
                        elif ltp > buy_price * 1.05:
                            rec = f"Hold (Trail SL to Cost: ₹{buy_price})"
                        else:
                            rec = "Hold (Position Active)"

                        db.execute_db("UPDATE positions SET scan_analysis = ?, last_scan_at = ? WHERE id = ?", (rec, now_str, pos_id))
                        log.info("[PortfolioScan] Checked %s: LTP=%s, Rec=%s", sym, ltp, rec)
            else:
                log.info("[PortfolioScan] No open positions to scan")
        except Exception as exc:
            log.warning("[PortfolioScan] Error in portfolio scan: %s", exc)
        time.sleep(1800)  # every 30 mins


# ---------------------------------------------------------------------------
# Release 4: Daily Recommendation Snapshot (keeps top-20 record, no trade creation)
# Trade creation is now handled by execution_engine.py via scanner signals.
# ---------------------------------------------------------------------------
_SNAPSHOT_HOUR = 11  # 11:00 AM IST

def _recommendation_snapshot_loop():
    """
    Daily at 11:00 AM IST:
    1. Save top 20 recommendation snapshot (for historical record)
    2. Save daily equity curve
    NOTE: Paper trade CREATION is now handled by execution_engine.py in real-time.
    """
    time.sleep(180)  # 3 min startup grace

    while True:
        try:
            from datetime import datetime as _dt
            now = _dt.now()

            if now.hour == _SNAPSHOT_HOUR and now.minute < 10:
                today = now.strftime("%Y-%m-%d")

                existing = db.execute_db(
                    "SELECT COUNT(*) as cnt FROM recommendation_snapshots WHERE snapshot_date = ?",
                    (today,), fetch="one"
                )
                if existing and existing.get("cnt", 0) > 0:
                    time.sleep(600)
                    continue

                all_results = db.get_all_results()
                if not all_results:
                    time.sleep(600)
                    continue

                all_results.sort(key=lambda x: x.get("score", 0), reverse=True)
                regime = db.get_meta("market_regime", "unknown")
                nifty_price = None
                try:
                    nifty_meta = db.get_meta("nifty50_price")
                    if nifty_meta:
                        nifty_price = float(nifty_meta)
                except Exception:
                    pass

                db.save_recommendation_snapshot(today, all_results, regime)
                db.save_portfolio_daily(nifty_price)
                log.info("[PaperTrade] Daily recommendation snapshot saved (top 20)")
                time.sleep(600)
            else:
                time.sleep(60)

        except Exception as exc:
            log.warning("[PaperTrade] Snapshot error: %s", exc)
            time.sleep(300)


# ---------------------------------------------------------------------------
# Release 4: Research Lifecycle Updater (every 30 min during market hours)
# NOTE: SL/Target execution is now handled by execution_engine.py in real-time.
# This loop ONLY handles the Research Lifecycle Engine (Phase 6) updates.
# ---------------------------------------------------------------------------

def _research_lifecycle_loop():
    """
    Every 30 minutes during market hours:
    1. Update Research Lifecycle Engine (Phase 6) outcomes
    2. Save daily equity curve
    NOTE: SL/Target/Time exits are NOW handled by execution_engine.py via WebSocket ticks.
    """
    time.sleep(300)  # 5 min startup grace

    while True:
        try:
            if not live_feed.is_market_open():
                time.sleep(300)
                continue

            # Fetch prices for Research Lifecycle Engine
            open_trades = db.get_open_paper_trades()
            prices_for_lifecycle = {}
            for trade in open_trades:
                sym = trade["symbol"]
                p_data = live_feed.get_live_price(sym)
                if p_data:
                    ltp = p_data.get("ltp") or p_data.get("price", 0)
                    if ltp and ltp > 0:
                        prices_for_lifecycle[sym] = ltp

            # Execute Phase 6 Lifecycle engine (research_snapshots_v2)
            if prices_for_lifecycle:
                db.update_research_lifecycle_outcomes(prices_for_lifecycle)

            # Save daily equity curve
            nifty_price = None
            try:
                nifty_meta = db.get_meta("nifty50_price")
                if nifty_meta:
                    nifty_price = float(nifty_meta)
            except Exception:
                pass
            db.save_portfolio_daily(nifty_price)

            log.info("[PaperTrade] Research lifecycle update: %d prices checked", len(prices_for_lifecycle))

        except Exception as exc:
            log.warning("[PaperTrade] Research lifecycle error: %s", exc)

        time.sleep(1800)  # every 30 mins


# ---------------------------------------------------------------------------
# Release 4: Order Expiry Loop (once daily)
# ---------------------------------------------------------------------------
def _order_expiry_loop():
    """Expire stale PENDING paper orders once per day."""
    time.sleep(600)  # 10 min startup grace
    while True:
        try:
            from execution_engine import expire_stale_orders
            expire_stale_orders()
        except Exception as exc:
            log.warning("[PaperTrade] Order expiry error: %s", exc)
        time.sleep(86400)  # once per day

# ---------------------------------------------------------------------------
# P0.1C: Production Stability Audit Loop (11:55 PM IST daily)
# ---------------------------------------------------------------------------
def _stability_audit_loop():
    """
    Daily at 11:55 PM IST:
    Generates the stability scorecard for the day.
    """
    time.sleep(60)  # startup grace
    
    while True:
        try:
            from datetime import datetime as _dt, timezone, timedelta as _td
            _IST = timezone(_td(hours=5, minutes=30))
            now = _dt.now(_IST)
            
            # Run at 11:55 PM
            if now.hour == 23 and now.minute >= 55:
                # Check if we already ran it for today
                last_run = db.get_meta("last_stability_audit_date")
                today_str = now.date().isoformat()
                
                if last_run != today_str:
                    from stability_audit import generate_daily_scorecard
                    generate_daily_scorecard(now.date())
                    db.set_meta("last_stability_audit_date", today_str)
                    
                time.sleep(3600)  # Sleep 1 hr so we don't re-run in the same window
            else:
                time.sleep(60)  # Check every minute
        except Exception as exc:
            log.warning("[StabilityAudit] Audit loop error: %s", exc)
            time.sleep(300)



# SCAN_SCHEDULE_MODE dispatch (additive). Default 'institutional' = PRE-OPEN + EOD only.
# Set SCAN_SCHEDULE_MODE=legacy to restore the original every-60-min loop exactly.
if SCAN_SCHEDULE_MODE == "legacy":
    threading.Thread(target=_auto_scan_loop, daemon=True, name="auto-scan").start()
    log.info("Auto-scan enabled (legacy mode): every %d minutes", AUTO_SCAN_INTERVAL)
else:
    threading.Thread(target=_institutional_scan_loop, daemon=True, name="auto-scan").start()
    log.info("Auto-scan enabled (institutional mode): PRE-OPEN ~08:45 IST + EOD ~18:30 IST, "
             "no hourly re-scan")

# Phase 1.5 (Change Set D / P2-1): cache-generation freshness correctness (Change Sets A/B)
# assumes a SINGLE web worker (per-worker in-memory cache). Warn if scaled out.
_wc = os.environ.get("WEB_CONCURRENCY")
if _wc and _wc.isdigit() and int(_wc) > 1:
    log.warning("[Phase1.5] WEB_CONCURRENCY=%s (>1) -- Phase 1.5 cache-generation correctness "
                "assumes --workers 1; per-worker caches may diverge.", _wc)

threading.Thread(target=_portfolio_scan_loop, daemon=True, name="portfolio-scan").start()
log.info("Portfolio-scan enabled: every 30 minutes")

# Release 4: Execution Engine + Paper trading threads
from execution_engine import initialize_engine
initialize_engine()
log.info("Execution Engine started: real-time paper trading active")

threading.Thread(target=_recommendation_snapshot_loop, daemon=True, name="rec-snapshot").start()
log.info("Recommendation snapshot enabled: daily at 11:00 AM IST")

threading.Thread(target=_research_lifecycle_loop, daemon=True, name="research-lifecycle").start()
log.info("Research lifecycle updater enabled: every 30 minutes during market hours")

threading.Thread(target=_order_expiry_loop, daemon=True, name="order-expiry").start()
log.info("Order expiry loop enabled: once daily")

threading.Thread(target=_stability_audit_loop, daemon=True, name="stability-audit").start()
log.info("P0.1C Stability Audit enabled: daily at 11:55 PM IST")

# Phase 8: Start MarketAux background worker
from scanner import start_marketaux_worker
start_marketaux_worker()

# Phase 2: Start Active Watchdog (Section 3, 8)
from watchdog import start_watchdog
_watchdog_thread = start_watchdog(_shutdown_event)
log.info("Watchdog started (Section 3: active stale scan recovery)")


# ─── Phase 0B: Graceful Shutdown Handler (Section 12) ─────────────────
def _graceful_shutdown(signum, frame):
    """Handle SIGTERM/SIGINT for graceful process termination.
    Section 12: On shutdown:
    1. Set shutdown event for all daemon threads
    2. If a scan is active, transition it to FAILED
    3. Stop the watchdog
    """
    sig_name = signal.Signals(signum).name if hasattr(signal, 'Signals') else str(signum)
    log.warning("[SHUTDOWN] Signal %s received — initiating graceful shutdown", sig_name)

    # 1. Signal all threads to stop
    _shutdown_event.set()

    # 2. Flush any active scan to FAILED
    try:
        active, active_scan_id = db.is_scan_active()
        if active:
            log.warning("[SHUTDOWN] Active scan %s — transitioning to FAILED", active_scan_id)
            db.transition_scan_state(
                scan_id=active_scan_id,
                from_status="running",
                to_status="failed",
                reason="graceful_shutdown",
                actor="system",
                error_message=f"Process terminated by {sig_name}",
            )
    except Exception as exc:
        log.error("[SHUTDOWN] Failed to flush active scan: %s", exc)

    # 3. Stop watchdog
    try:
        from watchdog import stop_watchdog
        stop_watchdog()
    except Exception:
        pass

    log.warning("[SHUTDOWN] Graceful shutdown complete")


# Register signal handlers
signal.signal(signal.SIGTERM, _graceful_shutdown)
signal.signal(signal.SIGINT, _graceful_shutdown)
# Windows-specific: SIGBREAK (Ctrl+Break)
if hasattr(signal, 'SIGBREAK'):
    signal.signal(signal.SIGBREAK, _graceful_shutdown)
log.info("Signal handlers registered (SIGTERM, SIGINT) for graceful shutdown")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5051))
    app.run(debug=False, host="0.0.0.0", port=port)
