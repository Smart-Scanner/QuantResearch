"""RE-3 P0 (Foundation) unit tests — canonical Recommendation Object pipeline.

Fully ISOLATED against a throwaway SQLite DB (DATABASE_URL cleared, db.DB_PATH redirected)
BEFORE importing db / recommendation_engine, so production is never touched.

Validates RE-1/RE-1G/RE-2A P0:
  * trade engine enforces SL<entry<TG1<TG2<TG3, single RR, RR_1>=MIN_RR (fail-closed)
  * builder emits the FULL RO (all RE-1 sections), deterministic id/input_hash
  * store round-trips the RO; projection reproduces legacy levels from the RO
  * shadow build yields 0 invariant violations among eligible ROs

Run: python -m pytest test_re3_p0.py -v
"""
import os
import tempfile
import pathlib

import pytest

os.environ.pop("DATABASE_URL", None)
os.environ.pop("SUPABASE_DB_URL", None)

import db  # noqa: E402
_RE3_DB = pathlib.Path(tempfile.mkdtemp(prefix="re3_")) / "re3.db"
db.DB_PATH = _RE3_DB
assert not db.is_postgresql()
db._init_sqlite()

import recommendation_engine as re_eng  # noqa: E402
from recommendation_engine import build_trade, build_recommendation_object, store, project_legacy  # noqa: E402
from recommendation_engine.reconcile import shadow_build  # noqa: E402

store.init_recommendation_store()


@pytest.fixture(autouse=True)
def _pin_re3_db():
    """Re-pin the shared global db.DB_PATH to this module's SQLite before every test,
    so a sibling test module (which also mutates db.DB_PATH at import) cannot redirect
    our store reads/writes. Test-isolation only; not a product concern."""
    db.DB_PATH = _RE3_DB
    store.init_recommendation_store()
    yield


def _clean(sym="TCS", price=100.0):
    return {"symbol": sym, "price": price, "atr_pct": 2.0,
            "trade": {"entry_low": 99.0, "entry_high": 101.0, "stop_loss": 95.0,
                      "target1": 108.0, "target2": 115.0, "target3": 125.0},
            "resistances": [108.0, 115.0, 125.0], "supports": [95.0, 90.0],
            "technical_score": 70, "fundamental_score": 60, "smart_money_score": 55,
            "risk_score": 50, "score": 65, "grade": "B"}


# ── Trade engine: geometry + single RR (fail-closed) ─────────────────────────
def test_trade_engine_valid_geometry_and_single_rr():
    t = build_trade(_clean())
    assert t["valid"] is True and not t["reasons"]
    e = t["entry"]["ref"]; sl = t["stop_loss"]["price"]; tg = [x["price"] for x in t["targets"]]
    assert sl < t["entry"]["low"] <= e <= t["entry"]["high"] < tg[0] < tg[1] < tg[2]
    risk = e - sl
    assert abs(t["risk_reward"] - round((tg[0] - e) / risk, 2)) < 0.01   # the ONE RR
    assert t["risk_reward"] >= re_eng.MIN_RR


def test_trade_engine_rejects_when_no_valid_stop():
    # tiny ATR + no real SL ⇒ derived SL not below entry_low ⇒ invalid (fail-closed)
    bad = _clean(); bad["atr_pct"] = 0.3; bad["trade"].pop("stop_loss")
    t = build_trade(bad)
    assert t["valid"] is False and "invalid_stop_loss" in t["reasons"]


def test_trade_engine_targets_strictly_increasing():
    t = build_trade(_clean())
    tg = [x["price"] for x in t["targets"]]
    assert tg == sorted(tg) and len(set(tg)) == 3


# ── Builder: full RO + determinism + eligibility ─────────────────────────────
def test_builder_emits_all_re1_sections():
    ro = build_recommendation_object(_clean(), scan_id="s1", generated_at_utc="2026-06-25 11:00:00")
    for sec in ("meta", "inputs_snapshot", "engines", "scoring", "eligibility",
                "trade", "sizing", "allocation", "presentation", "payloads", "audit"):
        assert sec in ro, f"missing RO section {sec}"
    assert set(ro["engines"]) == {"technical", "fundamental", "smart_money", "news",
                                  "sector_regime", "corporate_action", "liquidity", "risk"}
    assert abs(sum(e["weight"] for e in ro["engines"].values()) - 100) < 0.01
    assert ro["eligibility"]["eligible"] is True
    assert ro["meta"]["status"] == "ELIGIBLE"
    assert ro["audit"]["input_hash"] and ro["audit"]["formula_versions"]


def test_builder_is_deterministic():
    a = build_recommendation_object(_clean(), scan_id="s1", generated_at_utc="t")
    b = build_recommendation_object(_clean(), scan_id="s1", generated_at_utc="t")
    assert a["meta"]["recommendation_id"] == b["meta"]["recommendation_id"]
    assert a["inputs_snapshot"]["input_hash"] == b["inputs_snapshot"]["input_hash"]


def test_builder_rejects_below_liquidity_floor_no_payloads():
    ro = build_recommendation_object(_clean(price=10.0), scan_id="s1", generated_at_utc="t")
    assert ro["eligibility"]["eligible"] is False
    assert ro["meta"]["status"] == "REJECTED"
    assert ro["payloads"]["execution"] is None       # fail-closed: no actionable payload
    assert "liquidity_price_floor" in ro["eligibility"]["reject_reason"]


# ── Store round-trip ─────────────────────────────────────────────────────────
def test_store_roundtrip():
    ro = build_recommendation_object(_clean("INFY"), scan_id="s2", generated_at_utc="t")
    store.save_recommendation(ro)
    got = store.get_recommendation("INFY")
    assert got is not None
    assert got["trade"]["risk_reward"] == ro["trade"]["risk_reward"]
    assert got["status"] == "ELIGIBLE"
    assert len(store.get_recommendations("s2")) >= 1


# ── Projection reproduces legacy levels from the RO (M1/M2) ───────────────────
def test_projection_reproduces_levels():
    ro = build_recommendation_object(_clean(), scan_id="s1", generated_at_utc="t")
    leg = project_legacy(ro)
    assert leg["target_price"] == ro["trade"]["targets"][0]["price"]
    assert leg["stop_loss"] == ro["trade"]["stop_loss"]["price"]
    assert leg["risk_reward"] == ro["trade"]["risk_reward"]
    assert leg["trade"]["entry_low"] == ro["trade"]["entry"]["low"]


# ── Shadow build gate: 0 invariant violations among eligible ─────────────────
def test_shadow_build_zero_invariant_violations():
    results = [_clean(s) for s in ("AAA", "BBB", "CCC")] + [_clean("PENNY", price=5.0)]
    summary = shadow_build(results, scan_id="s3", persist=True)
    assert summary["total"] == 4
    assert summary["invariant_violations"] == 0          # the P0 gate
    assert summary["eligible"] == 3 and summary["rejected"] == 1
    assert summary["persisted"] == 4
