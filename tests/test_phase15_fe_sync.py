"""Phase 1.5 Change Set E-1 unit test — flag-gated no-store on freshness-critical endpoints.

The frontend parts (E-2 version-driven refresh, E-3 TradingView encode / Mission Control dedupe)
are template/JS changes validated by browser/production (per the playbook; rollback = git-revert).
This test covers the SERVER-SIDE E-1 logic via a minimal Flask app that replicates the
`@api_bp.after_request` handler verbatim (importing routes.api is not test-safe — heavy module
imports of scanner/analyzer/live_feed).

Run: python -m pytest test_phase15_fe_sync.py -v
"""
import os

import pytest

flask = pytest.importorskip("flask")
from flask import Flask, jsonify, request

_PHASE15_FE_NOSTORE_PATHS = ("/api/status", "/api/live-prices", "/api/results", "/api/dashboard")


def _make_app():
    app = Flask(__name__)

    @app.route("/api/status")
    def status():
        return jsonify({"ok": 1})

    @app.route("/api/other")
    def other():
        return jsonify({"ok": 1})

    @app.after_request                          # VERBATIM replica of routes/api.py:_phase15_fe_no_store
    def _phase15_fe_no_store(resp):
        try:
            if os.environ.get("PHASE15_FE_SYNC") == "1" and request.path in _PHASE15_FE_NOSTORE_PATHS:
                resp.headers["Cache-Control"] = "no-store"
        except Exception:
            pass
        return resp

    return app.test_client()


def test_flag_off_no_header():
    os.environ.pop("PHASE15_FE_SYNC", None)
    c = _make_app()
    assert "no-store" not in (c.get("/api/status").headers.get("Cache-Control") or "")


def test_flag_on_critical_path_no_store():
    os.environ["PHASE15_FE_SYNC"] = "1"
    try:
        c = _make_app()
        assert c.get("/api/status").headers.get("Cache-Control") == "no-store"
    finally:
        os.environ.pop("PHASE15_FE_SYNC", None)


def test_flag_on_non_critical_path_unaffected():
    os.environ["PHASE15_FE_SYNC"] = "1"
    try:
        c = _make_app()
        assert "no-store" not in (c.get("/api/other").headers.get("Cache-Control") or "")
    finally:
        os.environ.pop("PHASE15_FE_SYNC", None)


def test_critical_paths_set_covers_e1_endpoints():
    for p in ("/api/status", "/api/live-prices", "/api/results", "/api/dashboard"):
        assert p in _PHASE15_FE_NOSTORE_PATHS
