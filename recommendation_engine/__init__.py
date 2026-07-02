"""MarketOS Recommendation Engine (RE-3) — canonical Recommendation Object pipeline.

Implements RE-1 / RE-1G / RE-2A. P0 = FOUNDATION: build the Recommendation Object (RO)
in SHADOW mode and persist it to dedicated tables, WITHOUT changing any existing consumer.

Design invariants (RE-1 §0):
  * The RO is the single source of truth. Built ONCE by the builder; legacy shapes are
    derived via projection (P1+). In P0 nothing consumes the RO yet.
  * Compute-once, server-side. Deterministic (no wall-clock/random in the core) so the
    RO is reproducible from (inputs_snapshot, formula_versions).
  * Fail-closed: invalid geometry/gates ⇒ eligible=False with reasons; never shipped broken.

P0 is feature-flagged OFF by default (RE2_RO_BUILD=0). When enabled, a single guarded,
exception-isolated hook shadow-builds ROs after a scan — it can never affect the scan.
"""
import os

# ── Versioning (RE-1G §2) ────────────────────────────────────────────────────
SCHEMA_VERSION = "1.0.0"          # RO shape
MODEL_VERSION = "re3-p0-1.0.0"    # composite pipeline build
FORMULA_VERSIONS = {
    "technical": "1.0.0", "fundamental": "1.0.0", "smart_money": "1.0.0",
    "corporate_action": "1.0.0", "news": "1.0.0", "sector_regime": "1.0.0",
    "liquidity": "1.0.0", "risk": "1.0.0", "trade": "1.0.0", "scoring": "1.0.0",
    "sizing": "1.0.0",
}

# ── Canonical constants (RE-1 §3-22) ─────────────────────────────────────────
ENGINE_WEIGHTS = {            # RE-1 §3-10 — sum = 100
    "technical": 30, "fundamental": 20, "smart_money": 15, "news": 10,
    "sector_regime": 10, "corporate_action": 5, "liquidity": 5, "risk": 5,
}
MIN_RR = 1.5                  # RE-1G eligibility gate (RR_1 floor)
ATR_SL_MULTIPLIER = 2.0       # matches existing config.py:69
TARGET_MIN_R = (1.0, 2.0, 3.0)      # min R-multiple per target tier
DEFAULT_TARGET_R = (1.5, 2.5, 4.0)  # fallback ladder when structural resistance insufficient
TARGET_ALLOCATION = (50, 30, 20)    # scale-out % per RE-1 §17
DEFAULT_EQUITY = 1_000_000.0  # paper account equity for sizing (RE-1 §21)
RISK_PER_TRADE_PCT = 1.0
LIQ_PRICE_FLOOR = 20.0        # liquidity gate: min price
RISK_ATR_CEILING_PCT = 12.0   # risk gate: max ATR% of price

# ── Feature flags (RE-2A §6) ─────────────────────────────────────────────────
def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default) == "1"

RO_BUILD_ENABLED = _flag("RE2_RO_BUILD")     # P0 shadow build (default OFF)
RO_PROJECT_ENABLED = _flag("RE2_RO_PROJECT")  # P2 (approach A): project RO trade levels into
                                              # scan_results_v2 so consumers show/act on RO
                                              # values. Default OFF — flip ON for the cutover.
RO_EXEC_ENABLED = _flag("RE2_RO_EXEC")        # W4 (ADR-001/002/003): execution engine consumes
                                              # the projected RO trade levels + honors RO
                                              # eligibility (fail-closed). Default OFF — the
                                              # dedicated execution cutover flag, independent
                                              # of the display flag RE2_RO_PROJECT.

# Public API
from .trade_engine import build_trade          # noqa: E402
from .builder import build_recommendation_object  # noqa: E402
from . import store                            # noqa: E402
from .projection import project_legacy         # noqa: E402


def shadow_build_results(results, scan_id, persist=True):
    """Shadow-build ROs for a list of analyzer result dicts (RE-2A P0).

    Pure/idempotent. Returns a reconciliation summary. Persistence is best-effort and
    isolated — a failure here MUST NOT propagate to the caller (the scan).
    """
    from datetime import datetime, timezone
    from .reconcile import shadow_build as _sb
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return _sb(results, scan_id, persist=persist, generated_at_utc=gen)
