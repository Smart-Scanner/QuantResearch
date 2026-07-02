"""
ScanContext — Immutable execution context for a single scan run.
Section 5, 7 of the Master Plan.

Created at ingress (API or auto-scan). Passed immutably through the
entire scan lifecycle. Never stored as global/singleton state.

The context captures:
  - Identity: scan_id, correlation_id, request_id
  - Attribution: trigger_source, user_id, session_id
  - Reproducibility: all version strings + config snapshot
  - Linkage: parent_scan_id for retry chains
"""

import uuid
import json
import logging
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional

log = logging.getLogger("scan_context")

IST = timezone(timedelta(hours=5, minutes=30))


def _generate_scan_id(mode: str) -> str:
    """Generate a unique scan_id with mode prefix and timestamp."""
    import time
    ts = int(time.time())
    short_uuid = uuid.uuid4().hex[:6]
    return f"scan_{mode}_{ts}_{short_uuid}"


def _capture_config_snapshot() -> dict:
    """Capture all non-secret config values for reproducibility.
    Section 5, 39: config_snapshot stored per scan for audit.
    """
    from config import (
        SCAN_VERSION, SCORING_VERSION, RECOMMENDATION_VERSION,
        UNIVERSE_SELECTION_VERSION,
        CACHE_TTL_HOURS, DATA_LOOKBACK_DAYS, BENCHMARK_LOOKBACK_DAYS,
        MAX_WORKERS, MAX_RAW_SCORE, TOP_N_RESULTS, DASHBOARD_MAX_RESULTS,
        BATCH_SIZE, BATCH_DELAY, WRITE_BATCH_SIZE,
        FAST_SCAN_WORKERS, DEEP_SCAN_WORKERS, DEEP_SCAN_MAX_CANDIDATES,
        AUTO_SCAN_INTERVAL, ATR_SL_MULTIPLIER, TARGET_USES_RESISTANCE,
        HC_MIN_SCORE, HC_MIN_SIGNALS_BULLISH, HC_RSI_RANGE,
        HC_DELIVERY_MIN, HC_ATR_RANGE, HC_RISK_MAX,
        HC_REQUIRE_MACD_BULLISH, HC_REQUIRE_VOLUME, HC_MIN_RISK_REWARD,
        BP_RSI_MAX, BP_VOLUME_MIN, BP_DELIVERY_MIN,
        BP_WEEK1_MAX_LOSS, BP_MACD_BULLISH, BP_TARGET_PCT,
    )
    return {
        "scan_version": SCAN_VERSION,
        "scoring_version": SCORING_VERSION,
        "recommendation_version": RECOMMENDATION_VERSION,
        "universe_selection_version": UNIVERSE_SELECTION_VERSION,
        "cache_ttl_hours": CACHE_TTL_HOURS,
        "data_lookback_days": DATA_LOOKBACK_DAYS,
        "benchmark_lookback_days": BENCHMARK_LOOKBACK_DAYS,
        "max_workers": MAX_WORKERS,
        "max_raw_score": MAX_RAW_SCORE,
        "top_n_results": TOP_N_RESULTS,
        "dashboard_max_results": DASHBOARD_MAX_RESULTS,
        "batch_size": BATCH_SIZE,
        "batch_delay": BATCH_DELAY,
        "write_batch_size": WRITE_BATCH_SIZE,
        "fast_scan_workers": FAST_SCAN_WORKERS,
        "deep_scan_workers": DEEP_SCAN_WORKERS,
        "deep_scan_max_candidates": DEEP_SCAN_MAX_CANDIDATES,
        "auto_scan_interval": AUTO_SCAN_INTERVAL,
        "atr_sl_multiplier": ATR_SL_MULTIPLIER,
        "target_uses_resistance": TARGET_USES_RESISTANCE,
        "hc_min_score": HC_MIN_SCORE,
        "hc_min_signals_bullish": HC_MIN_SIGNALS_BULLISH,
        "hc_rsi_range": list(HC_RSI_RANGE),
        "hc_delivery_min": HC_DELIVERY_MIN,
        "hc_atr_range": list(HC_ATR_RANGE),
        "hc_risk_max": HC_RISK_MAX,
        "hc_require_macd_bullish": HC_REQUIRE_MACD_BULLISH,
        "hc_require_volume": HC_REQUIRE_VOLUME,
        "hc_min_risk_reward": HC_MIN_RISK_REWARD,
        "bp_rsi_max": BP_RSI_MAX,
        "bp_volume_min": BP_VOLUME_MIN,
        "bp_delivery_min": BP_DELIVERY_MIN,
        "bp_week1_max_loss": BP_WEEK1_MAX_LOSS,
        "bp_macd_bullish": BP_MACD_BULLISH,
        "bp_target_pct": BP_TARGET_PCT,
    }


@dataclass(frozen=True)
class ScanContext:
    """Immutable execution context for a scan run.

    Created once at ingress. Never modified. Passed through the
    entire pipeline. Stored in scan_runs for full reproducibility.
    """
    scan_id: str
    correlation_id: str
    request_id: str
    trigger_source: str        # "manual", "auto", "force", "api"
    user_id: str               # from session or "system"
    session_id: str            # Flask session ID or "system"
    scanner_version: str
    scoring_version: str
    recommendation_version: str
    universe_selection_version: str
    config_snapshot: dict = field(repr=False)
    parent_scan_id: Optional[str] = None  # Section 31: retry linkage
    created_at: str = ""       # ISO format timestamp

    def to_dict(self) -> dict:
        """Serialize to dict for DB storage / logging."""
        d = asdict(self)
        # config_snapshot is already a dict; ensure JSON-safe
        d["config_snapshot"] = self.config_snapshot
        return d

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict(), default=str)

    @staticmethod
    def create(
        trigger_source: str = "manual",
        user_id: str = "system",
        session_id: str = "system",
        mode: str = "manual",
        parent_scan_id: str = None,
    ) -> "ScanContext":
        """Factory: build a fully populated ScanContext.

        This is the ONLY way to create a context. All fields are
        auto-populated from config and runtime state.
        """
        from config import (
            SCAN_VERSION, SCORING_VERSION,
            RECOMMENDATION_VERSION, UNIVERSE_SELECTION_VERSION,
        )
        now = datetime.now(IST)
        return ScanContext(
            scan_id=_generate_scan_id(mode),
            correlation_id=str(uuid.uuid4()),
            request_id=str(uuid.uuid4()),
            trigger_source=trigger_source,
            user_id=user_id,
            session_id=session_id,
            scanner_version=SCAN_VERSION,
            scoring_version=SCORING_VERSION,
            recommendation_version=RECOMMENDATION_VERSION,
            universe_selection_version=UNIVERSE_SELECTION_VERSION,
            config_snapshot=_capture_config_snapshot(),
            parent_scan_id=parent_scan_id,
            created_at=now.isoformat(),
        )
