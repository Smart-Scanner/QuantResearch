"""RE-3 P2 unit tests — RO→legacy projection into scan_results_v2 (approach A).

Validates the gated projection (Phase 2): with RE2_RO_PROJECT OFF the persisted result is
unchanged (legacy); with it ON, the persisted result's TRADE LEVELS are the RO's, while
scoring/grade are untouched, and the original dict is never mutated.

Fully ISOLATED SQLite (no prod). Run: python -m pytest test_re3_p2.py -v
"""
import os
import json
import tempfile
import pathlib

import pytest

os.environ.pop("DATABASE_URL", None)
os.environ.pop("RE2_RO_PROJECT", None)

import db  # noqa: E402
_DB = pathlib.Path(tempfile.mkdtemp(prefix="re3p2_")) / "re3p2.db"
db.DB_PATH = _DB
assert not db.is_postgresql()
db._init_sqlite()

import recommendation_engine  # noqa: E402
from recommendation_engine import build_recommendation_object  # noqa: E402
from recommendation_engine.projection import project_result_copy  # noqa: E402


@pytest.fixture(autouse=True)
def _pin():
    db.DB_PATH = _DB
    db.execute_db("DELETE FROM scan_results_v2")
    recommendation_engine.RO_PROJECT_ENABLED = False
    yield
    recommendation_engine.RO_PROJECT_ENABLED = False


def _r(sym):
    # legacy target_price/risk_reward deliberately DIFFER from what the RO will produce
    return {"symbol": sym, "scan_mode": "fast", "price": 100.0, "atr_pct": 2.0,
            "score": 65, "grade": "B", "target_price": 150.0, "risk_reward": 9.9,
            "trade": {"entry_low": 99.0, "entry_high": 101.0, "stop_loss": 95.0,
                      "target1": 108.0, "target2": 115.0, "target3": 125.0},
            "resistances": [108.0, 115.0, 125.0], "supports": [95.0],
            "technical_score": 70, "fundamental_score": 60}


def _saved(sym):
    row = db.execute_db("SELECT data FROM scan_results_v2 WHERE symbol=? ORDER BY updated_at DESC LIMIT 1",
                        (sym,), fetch="one")
    return json.loads(row["data"]) if row else None


def test_projection_off_writes_legacy():
    recommendation_engine.RO_PROJECT_ENABLED = False
    db.save_results([_r("OFF")], scan_id="s1")
    d = _saved("OFF")
    assert d["risk_reward"] == 9.9 and d["target_price"] == 150.0   # legacy preserved
    assert "_ro_projected" not in d


def test_projection_on_overlays_ro_levels():
    recommendation_engine.RO_PROJECT_ENABLED = True
    db.save_results([_r("ON")], scan_id="s1")
    d = _saved("ON")
    assert d.get("_ro_projected") is True
    ro = build_recommendation_object(_r("ON"), scan_id="s1", generated_at_utc="t")
    assert d["risk_reward"] == ro["trade"]["risk_reward"] != 9.9       # RO RR, not legacy
    assert d["target_price"] == ro["trade"]["targets"][0]["price"]
    assert d["trade"]["target1"] == ro["trade"]["targets"][0]["price"]
    assert d["stop_loss"] == ro["trade"]["stop_loss"]["price"]


def test_projection_scope_trade_levels_only():
    recommendation_engine.RO_PROJECT_ENABLED = True
    db.save_results([_r("SCOPE")], scan_id="s1")
    d = _saved("SCOPE")
    assert d["score"] == 65 and d["grade"] == "B"        # scoring/grade UNTOUCHED (P2 scope)


def test_project_result_copy_does_not_mutate_original():
    orig = _r("MUT")
    snap = json.dumps(orig, sort_keys=True)
    project_result_copy(orig, "s1", "t")
    assert json.dumps(orig, sort_keys=True) == snap      # original never mutated
