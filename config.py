"""Centralized configuration constants."""

import os
from pathlib import Path

# Phase 0: Scan version — increment on ANY scoring formula change
SCAN_VERSION = "v3.0.0"

# Section 5, 35: Fine-grained version tracking for reproducibility
# Bump SCORING_VERSION on any weight/math change in analyzer.py
SCORING_VERSION = "v3.0.0"
# Bump RECOMMENDATION_VERSION on any grade/recommendation logic change
RECOMMENDATION_VERSION = "v1.0.0"
# Bump UNIVERSE_SELECTION_VERSION on any change to universe.py selection logic
UNIVERSE_SELECTION_VERSION = "v1.0.0"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Auth — Session Secret Key
# ---------------------------------------------------------------------------
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "")

# ---------------------------------------------------------------------------
# Single-user / local-first mode
# ---------------------------------------------------------------------------
# QuantResearch is a personal, single-user, local-first research platform.
# When enabled (default), the app transparently signs in the local admin so
# there is no login wall. Set SINGLE_USER_MODE=0 to restore multi-user auth.
SINGLE_USER_MODE = os.getenv("SINGLE_USER_MODE", "1") == "1"

# Paths
CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)
CACHE_FILE = CACHE_DIR / "scan_results.json"

# Scan settings
CACHE_TTL_HOURS = 6
DATA_LOOKBACK_DAYS = 365
BENCHMARK_LOOKBACK_DAYS = 60
_ON_RAILWAY = bool(os.environ.get("RAILWAY_ENVIRONMENT"))

# Determine number of data-provider accounts to scale workers.
# Canonical naming is PROVIDER_<n>_API_KEY (used by data_provider.py / live_feed.py);
# ANGEL_API_KEY[_n] is kept only as a legacy fallback.
_env_path = Path(__file__).parent / ".env"


def _count_provider_accounts() -> int:
    import re
    names = set(os.environ.keys())
    if _env_path.exists():
        for line in _env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            names.add(line.split("=", 1)[0].strip())
    provider = {n for n in names if re.fullmatch(r"PROVIDER_\d+_API_KEY", n)}
    if provider:
        return len(provider)
    angel = {n for n in names if re.fullmatch(r"ANGEL_API_KEY_\d+", n)}
    if angel:
        return len(angel)
    if "PROVIDER_1_API_KEY" in names or "ANGEL_API_KEY" in names:
        return 1
    return 0


_account_count = _count_provider_accounts()

# Scale workers: 3 workers per account
_multiplier = max(1, _account_count)
MAX_WORKERS = 3 * _multiplier if _ON_RAILWAY else 4 * _multiplier

MAX_RAW_SCORE = 380   # 25 technical indicators (~220 pts) + 12 intelligence layers (~160 pts)
TOP_N_RESULTS = 3000
# Phase D: Dashboard endpoints use a smaller limit for faster queries.
# TOP_N_RESULTS remains unchanged at 3000 for export/admin endpoints.
DASHBOARD_MAX_RESULTS = int(os.getenv("DASHBOARD_MAX_RESULTS", "500"))

# Batch scan settings — more conservative on Railway (shared IP gets rate-limited faster)
BATCH_SIZE = 30 if _ON_RAILWAY else 80
BATCH_DELAY = 20 if _ON_RAILWAY else 10  # seconds between batches

# Phase 5: Batch processing + deep scan config
WRITE_BATCH_SIZE = int(os.getenv("WRITE_BATCH_SIZE", "50"))
FAST_SCAN_WORKERS = int(os.getenv("FAST_SCAN_WORKERS", "1"))
DEEP_SCAN_WORKERS = int(os.getenv("DEEP_SCAN_WORKERS", "3"))
DEEP_SCAN_MAX_CANDIDATES = int(os.getenv("DEEP_SCAN_MAX_CANDIDATES", "100"))

# Auto scan interval (minutes)
AUTO_SCAN_INTERVAL = int(os.getenv("AUTO_SCAN_INTERVAL", "60"))

# ATR-based risk management
ATR_SL_MULTIPLIER = 2.0
TARGET_USES_RESISTANCE = True

# High Conviction thresholds — Release 1 Calibration (P0.8 Audit-Derived)
# HC_MIN_SCORE = 55 is a Release 1 observation value subject to P1 Truth Audit.
HC_MIN_SCORE = 55            # was 50 — calibrated from P0.8 counterfactual ladder
HC_MIN_SIGNALS_BULLISH = 5
HC_RSI_RANGE = (40, 70)      # was (28, 70) — avoid deep oversold entries
HC_DELIVERY_MIN = 40         # was 45 — relaxed per audit funnel analysis
HC_ATR_RANGE = (1.5, 5.5)
HC_RISK_MAX = 40             # was 45 — tighter risk for quality
HC_REQUIRE_MACD_BULLISH = False  # was True — disabled to break Intersection of Death
HC_REQUIRE_VOLUME = 1.0       # min volume ratio — unchanged
HC_MIN_RISK_REWARD = 2.2     # unchanged

# Bear Play thresholds (oversold bounce in bear market)
BP_RSI_MAX = 40
BP_VOLUME_MIN = 1.2
BP_DELIVERY_MIN = 45
BP_WEEK1_MAX_LOSS = -2.0      # 1W return not worse than -2%
BP_MACD_BULLISH = True         # MACD must be bullish
BP_TARGET_PCT = 10.0           # realistic target in bear market


# ═══════════════════════════════════════════════════════════════
# Phase 5.5: Universe Engine + Parallel Scan Configuration
# ═══════════════════════════════════════════════════════════════
USE_UNIVERSE_ENGINE = os.getenv("USE_UNIVERSE_ENGINE", "0") == "1"

# Scan Engine
AUTO_SCAN_ENABLED_DEFAULT = os.getenv("AUTO_SCAN_ENABLED_DEFAULT", "0") == "1"
SCAN_BATCH_SIZE = int(os.getenv("SCAN_BATCH_SIZE", "50"))
MAX_SCAN_WORKERS = int(os.getenv("MAX_SCAN_WORKERS", "6"))  # 6 parallel workers
PROGRESSIVE_PUBLISH_INTERVAL = int(os.getenv("PROGRESSIVE_PUBLISH_INTERVAL", "25"))

# Universe Eligibility Filters (Turnover = primary, Volume = secondary)
UNIVERSE_MIN_MCAP_CR = float(os.getenv("UNIVERSE_MIN_MCAP_CR") or "1000")
UNIVERSE_MIN_AVG_TURNOVER_CR = float(os.getenv("UNIVERSE_MIN_AVG_TURNOVER_CR") or "10")
UNIVERSE_MIN_AVG_VOLUME = int(os.getenv("UNIVERSE_MIN_AVG_VOLUME") or "10000")
UNIVERSE_MIN_PRICE = float(os.getenv("UNIVERSE_MIN_PRICE") or "50")
UNIVERSE_MIN_DATA_COVERAGE = float(os.getenv("UNIVERSE_MIN_DATA_COVERAGE") or "0.90")
UNIVERSE_MIN_LISTING_DAYS = int(os.getenv("UNIVERSE_MIN_LISTING_DAYS") or "180")  # IPO age filter

# ═══════════════════════════════════════════════════════════════
# Phase 5.6B/C: Liquidity Enrichment & Universe Governance
# ═══════════════════════════════════════════════════════════════
LIQUIDITY_ENRICHMENT_BATCH_SIZE = int(os.getenv("LIQUIDITY_ENRICHMENT_BATCH_SIZE", "50"))
LIQUIDITY_ENRICHMENT_WORKERS = int(os.getenv("LIQUIDITY_ENRICHMENT_WORKERS", "4"))
LIQUIDITY_MIN_COVERAGE_PCT = float(os.getenv("LIQUIDITY_MIN_COVERAGE_PCT", "80"))

# API Governance — protect Angel Historical API stability
LIQUIDITY_API_RPS = float(os.getenv("LIQUIDITY_API_RPS", "2"))
LIQUIDITY_WORKER_SLEEP_MS = int(os.getenv("LIQUIDITY_WORKER_SLEEP_MS", "500"))
LIQUIDITY_MAX_RETRIES = int(os.getenv("LIQUIDITY_MAX_RETRIES", "3"))

# Master Sync & Universe Rebuild
MASTER_SYNC_INTERVAL_DAYS = int(os.getenv("MASTER_SYNC_INTERVAL_DAYS", "14"))
MASTER_SYNC_DAILY_BATCH_SIZE = int(os.getenv("MASTER_SYNC_DAILY_BATCH_SIZE", "500"))  # incremental sync per run
UNIVERSE_REBUILD_HOUR = 8   # 8:30 AM IST daily
UNIVERSE_REBUILD_MINUTE = 30

# Performance Alerting
SCAN_DURATION_ALERT_MINUTES = int(os.getenv("SCAN_DURATION_ALERT_MINUTES", "20"))

# ═══════════════════════════════════════════════════════════════
# Telegram Alerts (Paper Trade Notifications)
# ═══════════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

# Fyers OAuth
FYERS_REDIRECT_URI = os.getenv("FYERS_REDIRECT_URI", "http://127.0.0.1:5000/")


# ═══════════════════════════════════════════════════════════════
# Phase 6, Section 39: Configuration Drift Detection
# BASELINE_CONFIG captures the reference values for all
# scanning thresholds, weights, and version identifiers.
# ═══════════════════════════════════════════════════════════════

BASELINE_CONFIG = {
    # Versions
    "SCAN_VERSION": "v3.0.0",
    "SCORING_VERSION": "v3.0.0",
    "RECOMMENDATION_VERSION": "v1.0.0",
    "UNIVERSE_SELECTION_VERSION": "v1.0.0",
    "AUTO_SCAN_ENABLED_DEFAULT": False,
    # Scan settings
    "CACHE_TTL_HOURS": 6,
    "DATA_LOOKBACK_DAYS": 365,
    "BENCHMARK_LOOKBACK_DAYS": 60,
    "MAX_RAW_SCORE": 380,
    "TOP_N_RESULTS": 3000,
    "DASHBOARD_MAX_RESULTS": 500,
    # HC thresholds
    "HC_MIN_SCORE": 55,
    "HC_MIN_SIGNALS_BULLISH": 5,
    "HC_RSI_RANGE": (40, 70),
    "HC_DELIVERY_MIN": 40,
    "HC_ATR_RANGE": (1.5, 5.5),
    "HC_RISK_MAX": 40,
    "HC_REQUIRE_MACD_BULLISH": False,
    "HC_REQUIRE_VOLUME": 1.0,
    "HC_MIN_RISK_REWARD": 2.2,
    # Risk
    "ATR_SL_MULTIPLIER": 2.0,
    # Bear Play
    "BP_RSI_MAX": 40,
    "BP_VOLUME_MIN": 1.2,
    "BP_DELIVERY_MIN": 45,
    "BP_WEEK1_MAX_LOSS": -2.0,
    "BP_MACD_BULLISH": True,
    "BP_TARGET_PCT": 10.0,
    # Phase 5.5: Universe Engine
    "SCAN_BATCH_SIZE": 50,
    "MAX_SCAN_WORKERS": 6,
    "UNIVERSE_MIN_MCAP_CR": 1000,
    "UNIVERSE_MIN_AVG_TURNOVER_CR": 10,
    "UNIVERSE_MIN_AVG_VOLUME": 10000,
    "UNIVERSE_MIN_PRICE": 50,
    "SCAN_DURATION_ALERT_MINUTES": 20,
    "UNIVERSE_MIN_LISTING_DAYS": 180,
    "MASTER_SYNC_DAILY_BATCH_SIZE": 500,
}


def check_config_drift() -> dict:
    """Phase 6, Section 39: Compare currently active config against BASELINE_CONFIG.

    Returns a dictionary of changed variables:
        {variable_name: {"baseline": old_val, "current": new_val}}

    An empty dict means no drift detected.
    """
    import sys
    current_module = sys.modules[__name__]
    drift = {}
    for key, baseline_val in BASELINE_CONFIG.items():
        current_val = getattr(current_module, key, None)
        if current_val is None:
            drift[key] = {"baseline": baseline_val, "current": "MISSING"}
        elif current_val != baseline_val:
            drift[key] = {"baseline": baseline_val, "current": current_val}
    return drift


# === scoring_v1 (additive, shadow) ===
# Additive scoring-engine selector + shadow-logging flag. These are READ-ONLY
# knobs for the new quantresearch/scoring_v1 stack and DO NOT change any
# existing live behaviour. Defaults keep LIVE on the legacy scorer (no cutover).
#   SCORING_ENGINE: which scorer the (future) cutover would use — legacy|v1|shadow.
#                   Default 'legacy' => live path unchanged.
#   SCORING_V1_SHADOW_ENABLED: when '1', a caller MAY invoke the shadow logger
#                   (quantresearch.scoring_v1.shadow); it never auto-runs on import.
SCORING_ENGINE = os.getenv("SCORING_ENGINE", "legacy")  # legacy|v1|shadow
SCORING_V1_SHADOW_ENABLED = os.getenv("SCORING_V1_SHADOW_ENABLED", "0") == "1"
# === end scoring_v1 (additive, shadow) ===
