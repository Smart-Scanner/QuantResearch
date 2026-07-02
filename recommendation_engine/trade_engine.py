"""RE-3 Trade Engine — the SINGLE trade-construction engine (RE-1 §15-20).

Produces ONE canonical trade block (entry / stop_loss / TG1-3 / one RR / trailing /
holding / exit_rules) from structural inputs, and enforces the hard geometric invariants
(RE-1 §0.5, fail-closed):

    stop_loss < entry_low ≤ entry_ref ≤ entry_high < TG1 < TG2 < TG3
    risk_per_share = entry_ref − stop_loss > 0
    RR_i = (TG_i − entry_ref) / risk_per_share          (the ONLY RR formula)
    risk_reward = RR_1 ;  RR_1 ≥ MIN_RR

This replaces the dual System-A/System-B computation proven defective in the audit. It is
deterministic (no wall-clock/random) for reproducibility.
"""
from . import (MIN_RR, ATR_SL_MULTIPLIER, TARGET_MIN_R, DEFAULT_TARGET_R,
               TARGET_ALLOCATION)


def _f(v):
    try:
        x = float(v)
        if x != x or x in (float("inf"), float("-inf")):
            return None
        return x
    except (TypeError, ValueError):
        return None


def build_trade(result: dict) -> dict:
    """Construct the canonical trade block. Returns a dict with `valid` + `reasons`.

    When `valid` is False the recommendation must be marked eligible=False (fail-closed);
    the partial geometry is still returned for audit.
    """
    reasons = []
    price = _f(result.get("price"))
    trade_in = result.get("trade") or {}
    atr_pct = _f(result.get("atr_pct"))
    atr = (price * atr_pct / 100.0) if (price and atr_pct) else (price * 0.02 if price else None)
    resistances = sorted({r for r in (_f(x) for x in (result.get("resistances") or [])) if r})

    if not price:
        return {"valid": False, "reasons": ["no_price"]}

    # ── Entry band (RE-1 §15): structural band → single entry_ref anchor ──
    el = _f(trade_in.get("entry_low"))
    eh = _f(trade_in.get("entry_high"))
    entry_basis = "structural"
    if not (el and eh and el < eh):
        el, eh, entry_basis = round(price * 0.99, 2), round(price * 1.01, 2), "neutral_band"
    entry_ref = round((el + eh) / 2.0, 2)

    # ── Stop loss (RE-1 §16): structural SL strictly below entry_low ──
    sl = _f(trade_in.get("stop_loss"))
    sl_basis = "structural"
    if not (sl and sl < el):
        sl = round(entry_ref - ATR_SL_MULTIPLIER * atr, 2) if atr else None
        sl_basis = "atr"
    if not (sl and sl < el):
        return {"valid": False, "reasons": ["invalid_stop_loss"],
                "entry": {"ref": entry_ref, "low": round(el, 2), "high": round(eh, 2)}}

    risk = round(entry_ref - sl, 2)
    if risk <= 0:
        return {"valid": False, "reasons": ["nonpositive_risk"]}

    # ── Targets TG1/2/3 (RE-1 §17): monotonic ladder, structural-first, min-R enforced ──
    cand = [r for r in resistances if r > eh]
    targets = []
    prev = eh
    for i in range(3):
        floor_price = entry_ref + TARGET_MIN_R[i] * risk
        pick = next((r for r in cand if r > prev and r >= floor_price), None)
        tg = pick if pick else round(entry_ref + DEFAULT_TARGET_R[i] * risk, 2)
        # enforce strictly-increasing ladder with a minimum step
        tg = round(max(tg, prev + max(0.05, 0.005 * entry_ref)), 2)
        rr = round((tg - entry_ref) / risk, 2)
        targets.append({
            "idx": i + 1, "price": tg, "rr": rr,
            "basis": "structural" if pick else "r_multiple",
            "allocation_pct": TARGET_ALLOCATION[i],
        })
        prev = tg
        cand = [r for r in cand if r > tg]

    rr1 = targets[0]["rr"]

    # ── Invariant validation (RE-1 §0.5, fail-closed) ──
    chain_ok = sl < el <= entry_ref <= eh < targets[0]["price"] < targets[1]["price"] < targets[2]["price"]
    if not chain_ok:
        reasons.append("geometry")
    if rr1 < MIN_RR:
        reasons.append("min_rr")

    return {
        "valid": not reasons,
        "reasons": reasons,
        "direction": "long",
        "entry": {"ref": entry_ref, "low": round(el, 2), "high": round(eh, 2), "basis": entry_basis},
        "stop_loss": {"price": sl, "pct": round((sl - entry_ref) / entry_ref * 100, 2), "basis": sl_basis},
        "targets": targets,
        "risk_reward": rr1,                 # the ONE RR (RE-1 §17)
        "risk_per_share": risk,
        "trailing": {"mode": "breakeven_after_tg1", "trigger": "TG1", "step": None, "current": None},
        "holding": {"min": 5, "max": 30, "unit": "sessions"},
        "exit_rules": [
            {"type": "target", "condition": "cmp>=TG_i", "action": "scale_out"},
            {"type": "stop", "condition": "cmp<=trailing_sl", "action": "exit"},
            {"type": "time", "condition": "sessions>=holding.max", "action": "exit"},
        ],
    }
