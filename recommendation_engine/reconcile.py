"""RE-3 P1 shadow build + supersession + reconciliation (RE-2A §3 / O1-O3).

Builds ROs for a set of analyzer results with the SUPERSESSION POLICY owned by the RO
pipeline (O1, Option A): same-day DEEP supersedes FAST, and no duplicate ROs for the same
logical recommendation. Persists via the batched upsert (O2). Emits full build
instrumentation (O3): built / rejected / duplicate / missing_symbol / superseded /
persisted (+ eligible / invariant_violations / legacy_divergence).
"""
import logging

from . import store
from .builder import build_recommendation_object
from .projection import project_legacy
from .trade_engine import _f

log = logging.getLogger("recommendation_engine.reconcile")

_GEN_AT = "1970-01-01 00:00:00"


def _invariants_hold(ro) -> bool:
    t = ro.get("trade") or {}
    if not t.get("valid"):
        return False
    e = t["entry"]; sl = t["stop_loss"]["price"]; tg = [x["price"] for x in t["targets"]]
    rr = t["risk_reward"]
    return (sl < e["low"] <= e["ref"] <= e["high"] < tg[0] < tg[1] < tg[2]
            and rr is not None and rr >= 1.5)


def _dedup_prefer_deep(results):
    """Collapse duplicate symbols, DEEP winning over FAST (in-batch supersession, O1).

    Returns (by_symbol, missing_symbol_count, duplicate_count).
    """
    by_symbol = {}
    missing = 0
    duplicate = 0
    for r in results or []:
        sym = r.get("symbol")
        if not sym:
            missing += 1
            continue
        sym = sym.upper()
        if sym in by_symbol:
            duplicate += 1
            prev_deep = (by_symbol[sym].get("scan_mode") == "deep")
            new_deep = (r.get("scan_mode") == "deep")
            if new_deep and not prev_deep:        # DEEP supersedes FAST in-batch
                by_symbol[sym] = r
        else:
            by_symbol[sym] = r
    return by_symbol, missing, duplicate


def shadow_build(results, scan_id, persist=True, generated_at_utc=_GEN_AT):
    if persist:
        try:
            store.init_recommendation_store()
        except Exception as exc:
            log.warning("[RE3-P1] store init failed (continuing in-memory): %s", exc)
            persist = False

    m = {"scan_id": scan_id, "total": 0, "built": 0, "eligible": 0, "rejected": 0,
         "duplicate": 0, "missing_symbol": 0, "superseded": 0, "persisted": 0,
         "invariant_violations": 0,
         "persist_attempted": False, "persist_ok": None, "persist_error": None,  # RC3-B recovery
         "legacy_divergence": {"target": 0, "stop_loss": 0, "rr": 0, "compared": 0},
         "violation_examples": []}

    by_symbol, m["missing_symbol"], m["duplicate"] = _dedup_prefer_deep(results)
    m["total"] = len(by_symbol)

    # Cross-scan supersession authority: symbols with a same-day DEEP analysis (O1).
    # Read-only + fail-open (empty set ⇒ in-batch supersession still applies); independent
    # of persist so the policy is consistent in shadow and in-memory replay alike.
    today = (generated_at_utc or _GEN_AT)[:10]
    deep_today = store.get_deep_symbols_today(today)

    built_ros = []
    for sym, result in by_symbol.items():
        mode = result.get("scan_mode", "fast")
        if mode != "deep" and sym in deep_today:      # FAST superseded by same-day DEEP RO
            m["superseded"] += 1
            continue
        ro = build_recommendation_object(result, scan_id=scan_id, generated_at_utc=generated_at_utc)
        m["built"] += 1
        if ro["eligibility"]["eligible"]:
            m["eligible"] += 1
            if not _invariants_hold(ro):
                m["invariant_violations"] += 1
                if len(m["violation_examples"]) < 5:
                    m["violation_examples"].append(ro["meta"]["symbol"])
        else:
            m["rejected"] += 1

        leg = project_legacy(ro)            # reconciliation vs original analyzer values
        ot = _f((result.get("trade") or {}).get("target1"))
        osl = _f((result.get("trade") or {}).get("stop_loss"))
        orr = _f(result.get("risk_reward"))
        d = m["legacy_divergence"]; d["compared"] += 1
        if ot is not None and leg["target_price"] is not None and abs(ot - leg["target_price"]) > 0.01:
            d["target"] += 1
        if osl is not None and leg["stop_loss"] is not None and abs(osl - leg["stop_loss"]) > 0.01:
            d["stop_loss"] += 1
        if orr is not None and leg["risk_reward"] is not None and abs(orr - leg["risk_reward"]) > 0.05:
            d["rr"] += 1

        built_ros.append(ro)

    if persist:
        # RC3-B recovery: a persist failure is ISOLATED here (never propagated to the scan) and
        # self-heals on the next scan — re-running the same built set is a no-op on already-
        # persisted recommendation_ids (RC3-A append-only) and the batch is atomic (no partial
        # write: RC3-B), so the store is always either the prior state or fully updated.
        m["persist_attempted"] = True
        try:
            m["persisted"] = store.save_recommendations_batch(built_ros)
            m["persist_ok"] = True
        except Exception as exc:
            m["persist_ok"] = False
            m["persist_error"] = str(exc)[:200]
            log.warning("[RE3-P1] batch persist failed (recoverable on next scan): %s", exc)

    log.info("[RE3-P1] shadow build scan=%s total=%d built=%d eligible=%d rejected=%d "
             "duplicate=%d missing_symbol=%d superseded=%d persisted=%d invariant_violations=%d "
             "persist_ok=%s",
             scan_id, m["total"], m["built"], m["eligible"], m["rejected"], m["duplicate"],
             m["missing_symbol"], m["superseded"], m["persisted"], m["invariant_violations"],
             m["persist_ok"])
    return m
