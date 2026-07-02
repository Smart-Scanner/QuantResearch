"""RE-3 Recommendation Object builder (RE-1 §1-28).

Assembles the FULL canonical Recommendation Object from an analyzer result dict, mapping
existing engine sub-scores into the uniform structure, evaluating eligibility gates
(RE-1G §1), constructing the trade via the single trade engine, and producing sizing /
allocation / presentation / payloads / audit.

Deterministic: recommendation_id and input_hash are content hashes (no uuid/clock), so the
core is reproducible from (inputs_snapshot, formula_versions) per RE-1G §2.
"""
import hashlib
import json
import math

from . import (SCHEMA_VERSION, MODEL_VERSION, FORMULA_VERSIONS, ENGINE_WEIGHTS,
               MIN_RR, DEFAULT_EQUITY, RISK_PER_TRADE_PCT, LIQ_PRICE_FLOOR,
               RISK_ATR_CEILING_PCT)
from .trade_engine import build_trade, _f

# Map RO engine → the existing result key(s) that carry its score (0-100).
_ENGINE_SOURCE = {
    "technical": ("technical_score",),
    "fundamental": ("fundamental_score",),
    "smart_money": ("smart_money_100", "smart_money_score"),
    "news": ("news_sentiment_score", "marketaux_catalyst_score"),
    "sector_regime": ("sector_rotation_score", "macro_score"),
    "corporate_action": ("corporate_action_score",),
    "liquidity": ("liquidity_score",),
    "risk": ("risk_score",),
}


def _first_score(result, keys, default=50.0):
    for k in keys:
        v = _f(result.get(k))
        if v is not None:
            return max(0.0, min(100.0, v))
    return default  # neutral baseline (RE-1G §1) when data missing


def _input_hash(snapshot: dict) -> str:
    blob = json.dumps(snapshot, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _rec_id(symbol, scan_id) -> str:
    return hashlib.sha1(f"{symbol}|{scan_id}|{SCHEMA_VERSION}".encode("utf-8")).hexdigest()[:16]


def build_recommendation_object(result: dict, *, scan_id: str, generated_at_utc: str,
                                equity: float = DEFAULT_EQUITY) -> dict:
    symbol = (result.get("symbol") or "").upper()
    price = _f(result.get("price"))
    atr_pct = _f(result.get("atr_pct")) or 0.0

    # ── inputs_snapshot (reproducibility basis) ──
    inputs_snapshot = {
        "cmp": price, "atr_pct": atr_pct,
        "volume_ratio": _f(result.get("volume_ratio")),
        "delivery_pct": _f(result.get("delivery_pct")),
        "resistances": result.get("resistances") or [],
        "supports": result.get("supports") or [],
        "scan_mode": result.get("scan_mode"),
    }
    input_hash = _input_hash(inputs_snapshot)

    # ── engines (uniform EngineResult + gate signals) ──
    engines = {}
    for name, keys in _ENGINE_SOURCE.items():
        score = _first_score(result, keys)
        engines[name] = {
            "score_0_100": round(score, 2),
            "weight": ENGINE_WEIGHTS.get(name, 0),
            "contributed": any(result.get(k) is not None for k in keys),
            "raw_signals": {k: result.get(k) for k in keys},
            "gate": {"is_gate": name in ("liquidity", "risk", "corporate_action"), "pass": True},
        }

    # ── trade (single engine) ──
    trade = build_trade(result)

    # ── eligibility gates (RE-1G §1) ──
    gates = []

    def gate(gid, ok, value, threshold):
        gates.append({"id": gid, "pass": bool(ok), "value": value, "threshold": threshold})
        return bool(ok)

    g_liq = gate("liquidity_price_floor", (price or 0) >= LIQ_PRICE_FLOOR, price, LIQ_PRICE_FLOOR)
    g_risk = gate("risk_atr_ceiling", atr_pct <= RISK_ATR_CEILING_PCT, atr_pct, RISK_ATR_CEILING_PCT)
    gate("trade_geometry", trade.get("valid", False), trade.get("reasons", []), "valid")
    gate("min_rr", (trade.get("risk_reward") or 0) >= MIN_RR, trade.get("risk_reward"), MIN_RR)
    gate("data_fresh", price is not None, price, "not_null")
    engines["liquidity"]["gate"]["pass"] = g_liq
    engines["risk"]["gate"]["pass"] = g_risk

    hc_reject = result.get("hc_rejection_reasons") or []
    eligible = all(g["pass"] for g in gates)
    reject_reason = None if eligible else ",".join(
        [g["id"] for g in gates if not g["pass"]] + ([str(hc_reject)] if hc_reject else [])
    )

    # ── scoring (RE-1 §11-13) ──
    wsum = sum(e["weight"] for e in engines.values()) or 1
    ai_score = round(sum(e["score_0_100"] * e["weight"] for e in engines.values()) / wsum, 2)
    engine_scores = [e["score_0_100"] for e in engines.values()]
    dispersion = (max(engine_scores) - min(engine_scores)) / 100.0 if engine_scores else 0.0
    contributed = sum(1 for e in engines.values() if e["contributed"]) / max(1, len(engines))
    confidence = round(100.0 * contributed * (1.0 - 0.5 * dispersion), 2)
    agreement = 1.0 - 0.5 * dispersion
    conviction_score = round(ai_score * agreement, 2)
    conviction_tier = ("Very High" if conviction_score >= 75 else "High" if conviction_score >= 60
                       else "Moderate" if conviction_score >= 45 else "Low")
    if not eligible:
        conviction_tier = "Rejected"
    grade = _f(result.get("score"))  # legacy grade label retained for presentation only
    scoring = {"ai_score": ai_score, "conviction": {"tier": conviction_tier, "score": conviction_score},
               "confidence": confidence, "grade": result.get("grade", "")}

    # ── sizing (RE-1 §21) ──
    rps = trade.get("risk_per_share")
    entry_ref = (trade.get("entry") or {}).get("ref")
    sizing = {"risk_per_trade_pct": RISK_PER_TRADE_PCT, "risk_per_share": rps,
              "capital_at_risk": round(equity * RISK_PER_TRADE_PCT / 100.0, 2),
              "quantity": 0, "position_value": 0.0}
    if eligible and rps and rps > 0 and entry_ref:
        qty = math.floor((equity * RISK_PER_TRADE_PCT / 100.0) / rps)
        sizing["quantity"] = qty
        sizing["position_value"] = round(qty * entry_ref, 2)

    # ── allocation (RE-1 §22) ──
    allocation = {"portfolio_weight_pct": round(sizing["position_value"] / equity * 100, 2) if equity else 0.0,
                  "sector": result.get("sector", ""), "correlation_flag": False, "slots_ok": True}

    # ── presentation (RE-1 §23-24) — radar from engine scores, no recompute ──
    radar = [{"dim": n, "value": e["score_0_100"]} for n, e in engines.items()]
    presentation = {
        "radar": radar,
        "explanation": {"headline": result.get("recommendation", ""),
                        "bullets": result.get("signals", []) or result.get("hc_reasons", []),
                        "llm_text": result.get("ai_summary", "")},
        "conviction_label": conviction_tier, "grade_label": result.get("grade", ""),
    }

    # ── payloads (RE-1 §25-27) — pure projections, NO new numbers ──
    payloads = _payloads(symbol, trade, sizing, scoring, presentation) if eligible else \
        {"alert": None, "paper_trade": None, "execution": None}

    # ── audit (RE-1 §28) ──
    provenance = {f"engines.{n}": {"engine": n, "formula_version": FORMULA_VERSIONS.get(n)}
                  for n in engines}
    provenance["trade"] = {"engine": "trade", "formula_version": FORMULA_VERSIONS["trade"]}
    audit = {"provenance": provenance, "formula_versions": FORMULA_VERSIONS,
             "input_hash": input_hash, "computed_by": MODEL_VERSION, "immutable": True}

    return {
        "meta": {"schema_version": SCHEMA_VERSION, "recommendation_id": _rec_id(symbol, scan_id),
                 "symbol": symbol, "exchange": result.get("exchange", "NSE"),
                 "scan_id": scan_id, "generated_at_utc": generated_at_utc,
                 "model_version": MODEL_VERSION, "ttl_sec": 86400, "supersedes_id": None,
                 "status": "ELIGIBLE" if eligible else "REJECTED"},
        "inputs_snapshot": {**inputs_snapshot, "input_hash": input_hash},
        "engines": engines,
        "scoring": scoring,
        "eligibility": {"eligible": eligible, "gates": gates, "reject_reason": reject_reason},
        "trade": trade,
        "sizing": sizing,
        "allocation": allocation,
        "presentation": presentation,
        "payloads": payloads,
        "audit": audit,
    }


def _payloads(symbol, trade, sizing, scoring, presentation):
    entry = trade["entry"]; sl = trade["stop_loss"]; tgs = trade["targets"]
    base = {"symbol": symbol, "entry": entry, "stop_loss": sl, "targets": tgs,
            "risk_reward": trade["risk_reward"]}
    return {
        "alert": {**base, "conviction": scoring["conviction"]["tier"],
                  "headline": presentation["explanation"]["headline"]},
        "paper_trade": {**base, "quantity": sizing["quantity"],
                        "holding": trade["holding"], "exit_rules": trade["exit_rules"]},
        "execution": {"symbol": symbol, "side": "BUY", "quantity": sizing["quantity"],
                      "entry": entry, "stop_loss": sl, "targets": tgs,
                      "trailing": trade["trailing"], "exit_rules": trade["exit_rules"]},
    }
