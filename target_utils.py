"""MarketOS Target Resolution — Single Source of Truth.

This module is the ONLY place in the codebase allowed to contain
target fallback chain logic. All consumers (API, Portfolio, Workspace,
Charts, Drawer, Forms) must use the resolved output — never read
trade.target1, scan.target1, or scan.target_price directly.

Decoupled from route modules to prevent circular imports.
Uses only stdlib (math, logging) and metrics.counters.
"""

import math
import logging

from metrics import counters

log = logging.getLogger("target_utils")

# ── Resolver Version ────────────────────────────────────────────────
# Increment when the fallback chain or output schema changes.
# Audit scripts and future API consumers can compare against this.
TARGET_RESOLVER_VERSION = 2

# ── Preferred sources (for fallback quality logging) ────────────────
_PREFERRED = {
    "t1": "trade.target1",
    "t2": "trade.target2",
    "t3": "trade.target3",
    "sl": "trade.stop_loss",
    "entry_low": "trade.entry_low",
    "entry_high": "trade.entry_high",
}


def _safe_float(value):
    """Convert value to a rounded float, rejecting NaN/inf.

    Handles: int, float, Decimal, numeric strings.
    Returns None on failure.
    """
    if value is None:
        return None
    try:
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return None
        return round(v, 2)
    except (TypeError, ValueError):
        return None


def _resolve_field(scan, trade, field_name, chain):
    """Walk the fallback chain for a single field.

    Args:
        scan:  The top-level scan dict (from db.get_stock).
        trade: The scan["trade"] sub-dict (or {}).
        field_name: Human label for the field (e.g. "t1").
        chain: List of (dict_ref_name, dict_ref, key) tuples in priority order.

    Returns:
        (value: float | None, source: str | None)
    """
    for source_label, source_dict, key in chain:
        raw = source_dict.get(key)
        val = _safe_float(raw)
        if val is not None:
            return val, source_label
    return None, None


def resolve_targets(scan, symbol=None):
    """Resolve targets from a scan dict using the canonical fallback chain.

    Args:
        scan:   Full scan dict as returned by db.get_stock(symbol).
                May be None (unscanned stock).
        symbol: Optional symbol string for logging context.

    Returns:
        dict with keys: version, critical_fields_complete,
        full_targets_complete, t1-t3, sl, entry_low, entry_high, source.
    """
    if scan is None:
        return {
            "version": TARGET_RESOLVER_VERSION,
            "critical_fields_complete": False,
            "full_targets_complete": False,
            "t1": None, "t2": None, "t3": None,
            "sl": None, "entry_low": None, "entry_high": None,
            "source": {
                "t1": None, "t2": None, "t3": None,
                "sl": None, "entry_low": None, "entry_high": None,
            },
        }

    trade = scan.get("trade") or {}
    sym = symbol or scan.get("symbol", "?")

    # ── Resolution chains ───────────────────────────────────────────
    # Each entry: (source_label, dict_reference, key_in_dict)
    chains = {
        "t1": [
            ("trade.target1", trade, "target1"),
            ("scan.target1",  scan,  "target1"),
            ("scan.target_price", scan, "target_price"),
        ],
        "t2": [
            ("trade.target2", trade, "target2"),
            ("scan.target2",  scan,  "target2"),
            ("scan.target_2", scan,  "target_2"),
        ],
        "t3": [
            ("trade.target3", trade, "target3"),
            ("scan.target3",  scan,  "target3"),
            ("scan.target_3", scan,  "target_3"),
        ],
        "sl": [
            ("trade.stop_loss", trade, "stop_loss"),
            ("scan.stop_loss",  scan,  "stop_loss"),
        ],
        "entry_low": [
            ("trade.entry_low", trade, "entry_low"),
            ("scan.entry_low",  scan,  "entry_low"),
        ],
        "entry_high": [
            ("trade.entry_high", trade, "entry_high"),
            ("scan.entry_high",  scan,  "entry_high"),
        ],
    }

    resolved = {}
    sources = {}

    for field, chain in chains.items():
        val, src = _resolve_field(scan, trade, field, chain)
        resolved[field] = val
        sources[field] = src

        # ── Metrics ─────────────────────────────────────────────────
        if val is None:
            counters.inc("target_missing")
        elif src and src.startswith("trade."):
            counters.inc("target_resolved_trade")
        else:
            counters.inc("target_resolved_scan")

        # ── Fallback quality logging ────────────────────────────────
        preferred = _PREFERRED.get(field)
        if val is not None and src != preferred and preferred is not None:
            log.info(
                "[TARGET_FALLBACK] symbol=%s field=%s resolved_from=%s preferred=%s",
                sym, field, src, preferred,
            )

    # ── Completeness flags ──────────────────────────────────────────
    critical_ok = all(
        resolved[f] is not None for f in ("t1", "sl", "entry_low", "entry_high")
    )
    full_ok = all(
        resolved[f] is not None for f in ("t1", "t2", "t3")
    )

    # ── Entry-based Risk/Reward calculations ────────────────────────
    entry_mid = None
    if resolved["entry_low"] is not None and resolved["entry_high"] is not None:
        entry_mid = (resolved["entry_low"] + resolved["entry_high"]) / 2.0

    risk = None
    if entry_mid is not None and resolved["sl"] is not None:
        risk = entry_mid - resolved["sl"]

    entry_rr = {
        "t1": None,
        "t2": None,
        "t3": None
    }

    if risk is not None and risk > 0:
        if resolved["t1"] is not None:
            entry_rr["t1"] = round((resolved["t1"] - entry_mid) / risk, 1)
        if resolved["t2"] is not None:
            entry_rr["t2"] = round((resolved["t2"] - entry_mid) / risk, 1)
        if resolved["t3"] is not None:
            entry_rr["t3"] = round((resolved["t3"] - entry_mid) / risk, 1)

    return {
        "version": TARGET_RESOLVER_VERSION,
        "critical_fields_complete": critical_ok,
        "full_targets_complete": full_ok,
        "t1": resolved["t1"],
        "t2": resolved["t2"],
        "t3": resolved["t3"],
        "sl": resolved["sl"],
        "entry_low": resolved["entry_low"],
        "entry_high": resolved["entry_high"],
        "entry_rr": entry_rr,
        "source": sources,
    }
