"""RE-3 legacy projection (RE-2A §0.1 / M1-M2).

`project_legacy(ro)` derives the LEGACY-shaped trade fields from the canonical RO so that,
from P1 onward, every legacy consumer reads values that are byte-identical to the RO
(legacy == projection(RO)). In P0 it is used only for reconciliation (RO-derived vs the
original analyzer values), proving the RO can reproduce the legacy surface.
"""


def project_result_copy(result: dict, scan_id: str, generated_at_utc: str) -> dict:
    """Return a COPY of an analyzer result with the RO TRADE LEVELS overlaid (approach A, P2).

    ONLY trade levels are projected (entry/SL/TG1-3/RR, top-level + the `trade` sub-dict);
    scoring/grade/engines/conviction stay legacy (P2 scope = trade levels only). The original
    dict is never mutated — the shadow build and live_feed.subscribe read originals. Rejected/
    invalid ROs yield None levels (no valid trade), surfaced via `_ro_status`.
    """
    from .builder import build_recommendation_object
    ro = build_recommendation_object(result, scan_id=scan_id, generated_at_utc=generated_at_utc)
    leg = project_legacy(ro)
    r2 = dict(result)
    r2["trade"] = {**(result.get("trade") or {}), **leg["trade"]}
    r2["target_price"] = leg["target_price"]
    r2["stop_loss"] = leg["stop_loss"]
    r2["risk_reward"] = leg["risk_reward"]
    r2["_ro_status"] = ro["meta"]["status"]
    r2["_ro_projected"] = True
    return r2


def project_legacy(ro: dict) -> dict:
    """Return legacy top-level + trade-dict fields derived purely from the RO."""
    trade = ro.get("trade") or {}
    entry = trade.get("entry") or {}
    sl = (trade.get("stop_loss") or {}).get("price")
    tgs = trade.get("targets") or []
    t = [x.get("price") for x in tgs]
    while len(t) < 3:
        t.append(None)
    entry_ref = entry.get("ref")
    return {
        # System-A top-level shape
        "price": (ro.get("inputs_snapshot") or {}).get("cmp"),
        "target_price": t[0],
        "stop_loss": sl,
        "risk_reward": trade.get("risk_reward"),
        # System-B trade-dict shape
        "trade": {
            "entry_low": entry.get("low"), "entry_high": entry.get("high"),
            "stop_loss": sl, "target1": t[0], "target2": t[1], "target3": t[2],
            "risk_reward": trade.get("risk_reward"),
        },
        "_entry_ref": entry_ref,
    }
