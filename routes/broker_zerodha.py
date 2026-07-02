"""
Phase 5.2.0: Zerodha Kite Connect Backend Proxy
Routes: /api/broker/zerodha/*

Architecture:
    Browser → MarketOS Flask Backend → kiteconnect SDK → Kite API

Security:
    - api_secret never leaves server
    - access_token stored in server-side session
    - All endpoints require authenticated user session
"""

import logging
import os
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, session, redirect

log = logging.getLogger("broker_zerodha")

zerodha_bp = Blueprint("broker_zerodha", __name__, url_prefix="/api/broker/zerodha")

# ── Configuration ─────────────────────────────────────────────────
# Set these in .env or environment:
#   KITE_API_KEY=your_api_key
#   KITE_API_SECRET=your_api_secret
#   KITE_REDIRECT_URL=http://localhost:5000/api/broker/zerodha/callback

KITE_API_KEY = os.getenv("KITE_API_KEY", "")
KITE_API_SECRET = os.getenv("KITE_API_SECRET", "")
KITE_REDIRECT_URL = os.getenv("KITE_REDIRECT_URL", "http://127.0.0.1:5000/api/broker/zerodha/callback")


def _get_kite_session():
    """Get or create a KiteConnect instance for the current session."""
    try:
        from kiteconnect import KiteConnect
    except ImportError:
        log.error("kiteconnect package not installed. Run: pip install kiteconnect")
        return None, "kiteconnect package not installed"

    if not KITE_API_KEY:
        return None, "KITE_API_KEY not configured"

    access_token = session.get("kite_access_token")
    kite = KiteConnect(api_key=KITE_API_KEY)

    if access_token:
        kite.set_access_token(access_token)

    return kite, None


def _require_kite_auth(f):
    """Decorator: ensure user has a valid Kite access token in session."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"success": False, "error": "Not authenticated"}), 401
        if not session.get("kite_access_token"):
            return jsonify({"success": False, "error": "Zerodha not connected. Please login first."}), 403
        return f(*args, **kwargs)
    return decorated


# ── Authentication ────────────────────────────────────────────────

@zerodha_bp.route("/login")
def login():
    """Redirect user to Zerodha Kite login page."""
    if not KITE_API_KEY:
        return jsonify({"success": False, "error": "KITE_API_KEY not configured"}), 500

    try:
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=KITE_API_KEY)
        login_url = kite.login_url()
        return jsonify({"success": True, "login_url": login_url})
    except ImportError:
        return jsonify({"success": False, "error": "kiteconnect package not installed"}), 500
    except Exception as e:
        log.error("Kite login URL error: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@zerodha_bp.route("/callback")
def callback():
    """
    Handle Kite OAuth callback.
    Exchanges request_token for access_token (server-side).
    The api_secret NEVER reaches the browser.
    """
    request_token = request.args.get("request_token")
    if not request_token:
        return jsonify({"success": False, "error": "No request_token received"}), 400

    kite, err = _get_kite_session()
    if err:
        return jsonify({"success": False, "error": err}), 500

    try:
        data = kite.generate_session(request_token, api_secret=KITE_API_SECRET)
        session["kite_access_token"] = data["access_token"]
        session["kite_user_id"] = data.get("user_id", "")
        session["kite_login_time"] = datetime.now(timezone.utc).isoformat()

        log.info("Kite session created for user %s (broker user: %s)",
                 session.get("user_id"), data.get("user_id"))

        # Redirect to Mission Control after successful login
        return redirect("/mission-control?broker=zerodha&status=connected")
    except Exception as e:
        log.error("Kite session generation error: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@zerodha_bp.route("/disconnect", methods=["POST"])
def disconnect():
    """Clear Kite session tokens."""
    session.pop("kite_access_token", None)
    session.pop("kite_user_id", None)
    session.pop("kite_login_time", None)
    return jsonify({"success": True})


@zerodha_bp.route("/status")
def connection_status():
    """Check if Kite session is active."""
    has_token = bool(session.get("kite_access_token"))
    return jsonify({
        "success": True,
        "connected": has_token,
        "broker_user_id": session.get("kite_user_id", ""),
        "login_time": session.get("kite_login_time", ""),
    })


# ── Read-Only Data Endpoints ─────────────────────────────────────

@zerodha_bp.route("/profile")
@_require_kite_auth
def profile():
    """Fetch user profile from Kite."""
    kite, err = _get_kite_session()
    if err:
        return jsonify({"success": False, "error": err}), 500
    try:
        data = kite.profile()
        return jsonify({"success": True, "profile": data})
    except Exception as e:
        log.error("Kite profile error: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@zerodha_bp.route("/funds")
@_require_kite_auth
def funds():
    """Fetch margins/funds from Kite."""
    kite, err = _get_kite_session()
    if err:
        return jsonify({"success": False, "error": err}), 500
    try:
        data = kite.margins()
        return jsonify({"success": True, "margins": data})
    except Exception as e:
        log.error("Kite margins error: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@zerodha_bp.route("/positions")
@_require_kite_auth
def positions():
    """Fetch positions from Kite."""
    kite, err = _get_kite_session()
    if err:
        return jsonify({"success": False, "error": err}), 500
    try:
        data = kite.positions()
        # Map to MarketOS schema
        mapped = []
        for pos_type in ['net', 'day']:
            for p in data.get(pos_type, []):
                mapped.append({
                    "broker_position_id": f"kite_{p.get('tradingsymbol')}_{p.get('exchange')}",
                    "symbol": p.get("tradingsymbol", ""),
                    "exchange": p.get("exchange", ""),
                    "quantity": p.get("quantity", 0),
                    "average_price": p.get("average_price", 0),
                    "last_price": p.get("last_price", 0),
                    "pnl": p.get("pnl", 0),
                    "product": p.get("product", ""),
                    "position_type": pos_type,
                })
        return jsonify({"success": True, "positions": mapped})
    except Exception as e:
        log.error("Kite positions error: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@zerodha_bp.route("/orders")
@_require_kite_auth
def orders():
    """Fetch today's orders from Kite."""
    kite, err = _get_kite_session()
    if err:
        return jsonify({"success": False, "error": err}), 500
    try:
        data = kite.orders()
        mapped = []
        for o in (data or []):
            mapped.append({
                "broker_order_id": o.get("order_id", ""),
                "symbol": o.get("tradingsymbol", ""),
                "exchange": o.get("exchange", ""),
                "side": o.get("transaction_type", "").lower(),
                "order_type": o.get("order_type", "").lower(),
                "product": o.get("product", ""),
                "quantity": o.get("quantity", 0),
                "filled_quantity": o.get("filled_quantity", 0),
                "price": o.get("price", 0),
                "average_price": o.get("average_price", 0),
                "status": o.get("status", "").lower(),
                "placed_at": o.get("order_timestamp", ""),
                "exchange_timestamp": o.get("exchange_timestamp", ""),
            })
        return jsonify({"success": True, "orders": mapped})
    except Exception as e:
        log.error("Kite orders error: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@zerodha_bp.route("/holdings")
@_require_kite_auth
def holdings():
    """Fetch holdings from Kite."""
    kite, err = _get_kite_session()
    if err:
        return jsonify({"success": False, "error": err}), 500
    try:
        data = kite.holdings()
        mapped = []
        for h in (data or []):
            mapped.append({
                "symbol": h.get("tradingsymbol", ""),
                "exchange": h.get("exchange", ""),
                "isin": h.get("isin", ""),
                "quantity": h.get("quantity", 0),
                "average_price": h.get("average_price", 0),
                "last_price": h.get("last_price", 0),
                "pnl": h.get("pnl", 0),
            })
        return jsonify({"success": True, "holdings": mapped})
    except Exception as e:
        log.error("Kite holdings error: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


# ── Phase 5.2.2: Trading Proxy Endpoints ─────────────────────────
# All governance (6 safety gates) is enforced in the JS command handler
# BEFORE these routes are called. These routes are thin proxies only.
# api_key + access_token never leave the server.

@zerodha_bp.route("/orders/<variety>", methods=["POST"])
@_require_kite_auth
def place_order(variety):
    """
    Proxy: POST /orders/{variety} → Kite place_order.
    variety: regular | amo
    Body: Kite-compatible order params (tradingsymbol, exchange, etc.)
    """
    kite, err = _get_kite_session()
    if err:
        return jsonify({"success": False, "error": err}), 500

    data = request.get_json(silent=True) or {}
    log.info("place_order [%s] symbol=%s qty=%s tag=%s",
             variety, data.get("tradingsymbol"), data.get("quantity"), data.get("tag"))

    try:
        order_id = kite.place_order(
            variety=variety,
            exchange=data.get("exchange", "NSE"),
            tradingsymbol=data.get("tradingsymbol"),
            transaction_type=data.get("transaction_type"),
            quantity=int(data.get("quantity", 0)),
            order_type=data.get("order_type", "MARKET"),
            product=data.get("product", "CNC"),
            price=float(data.get("price", 0)) or None,
            trigger_price=float(data.get("trigger_price", 0)) or None,
            validity=data.get("validity", "DAY"),
            disclosed_quantity=int(data.get("disclosed_quantity", 0)),
            tag=data.get("tag", ""),  # intent_id for traceability — DO NOT REMOVE
        )
        log.info("Kite order placed: order_id=%s", order_id)
        return jsonify({"success": True, "order_id": order_id})
    except Exception as e:
        log.error("Kite place_order error: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@zerodha_bp.route("/orders/<variety>/<order_id>", methods=["DELETE"])
@_require_kite_auth
def cancel_order(variety, order_id):
    """
    Proxy: DELETE /orders/{variety}/{order_id} → Kite cancel_order.
    """
    kite, err = _get_kite_session()
    if err:
        return jsonify({"success": False, "error": err}), 500

    log.info("cancel_order [%s] order_id=%s", variety, order_id)
    try:
        result = kite.cancel_order(variety=variety, order_id=order_id)
        return jsonify({"success": True, "order_id": result})
    except Exception as e:
        log.error("Kite cancel_order error: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@zerodha_bp.route("/orders/<variety>/<order_id>", methods=["PUT"])
@_require_kite_auth
def modify_order(variety, order_id):
    """
    Proxy: PUT /orders/{variety}/{order_id} → Kite modify_order.
    """
    kite, err = _get_kite_session()
    if err:
        return jsonify({"success": False, "error": err}), 500

    data = request.get_json(silent=True) or {}
    log.info("modify_order [%s] order_id=%s", variety, order_id)
    try:
        result = kite.modify_order(
            variety=variety,
            order_id=order_id,
            quantity=data.get("quantity"),
            price=data.get("price"),
            order_type=data.get("order_type"),
            trigger_price=data.get("trigger_price"),
            validity=data.get("validity"),
        )
        return jsonify({"success": True, "order_id": result})
    except Exception as e:
        log.error("Kite modify_order error: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@zerodha_bp.route("/orders/<order_id>", methods=["GET"])
@_require_kite_auth
def get_order_status(order_id):
    """
    Proxy: GET /orders/{order_id} → Kite order history.
    Returns latest state of a specific order.
    """
    kite, err = _get_kite_session()
    if err:
        return jsonify({"success": False, "error": err}), 500

    try:
        history = kite.order_history(order_id=order_id)
        if not history:
            return jsonify({"success": False, "error": "Order not found"}), 404
        # Return latest state (last entry in history)
        latest = history[-1]
        return jsonify({
            "success": True,
            "order": {
                "order_id": latest.get("order_id"),
                "status": latest.get("status", ""),
                "filled_quantity": latest.get("filled_quantity", 0),
                "pending_quantity": latest.get("pending_quantity", 0),
                "average_price": latest.get("average_price", 0),
                "fills": history,  # full history for partial fill tracking
            }
        })
    except Exception as e:
        log.error("Kite order_history error: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


# ── Phase 5.2.3: Webhook Endpoint ────────────────────────────────
# Kite sends order postbacks here.
# Flow: Kite → Flask webhook → store to immutable log → forward to client
# The client calls PROCESS_BROKER_EVENT to drive the State Machine.

import hashlib
import hmac
import json

# In-memory buffer of pending webhook events for client polling.
# In production, replace with Redis/DB queue.
_pending_webhook_events = []

KITE_WEBHOOK_SECRET = os.getenv("KITE_WEBHOOK_SECRET", "")


@zerodha_bp.route("/webhook", methods=["POST"])
def webhook():
    """
    Receive Kite order postback.
    Validates HMAC checksum if KITE_WEBHOOK_SECRET is configured.
    Stores raw event to immutable audit log.
    Buffers event for client polling.

    Kite postback payload example:
    {
        "order_id": "220303000000001",
        "status": "COMPLETE",
        "filled_quantity": 100,
        "pending_quantity": 0,
        "average_price": 2451.5,
        "tradingsymbol": "RELIANCE",
        "exchange": "NSE",
        "transaction_type": "BUY",
        "tag": "intent_abc123",
        "checksum": "sha256hex..."
    }
    """
    raw_body = request.get_data(as_text=True)
    data = request.get_json(silent=True) or {}

    # 1. Validate checksum (if configured)
    if KITE_WEBHOOK_SECRET:
        incoming_checksum = data.get("checksum", "")
        # Kite checksum = SHA-256(order_id + order_timestamp + KITE_WEBHOOK_SECRET)
        order_id = data.get("order_id", "")
        order_ts = data.get("exchange_timestamp") or data.get("order_timestamp", "")
        expected = hashlib.sha256(
            f"{order_id}{order_ts}{KITE_WEBHOOK_SECRET}".encode()
        ).hexdigest()

        if not hmac.compare_digest(incoming_checksum, expected):
            log.warning("Webhook checksum mismatch for order %s", order_id)
            return jsonify({"success": False, "error": "CHECKSUM_MISMATCH"}), 403

    # 2. Store to immutable audit log
    webhook_id = f"wh_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{data.get('order_id', 'unknown')}"
    log_entry = {
        "webhook_id": webhook_id,
        "broker": "zerodha",
        "received_at": datetime.now(timezone.utc).isoformat(),
        "raw_payload": data,
        "payload_hash": hashlib.sha256(raw_body.encode()).hexdigest(),
        "processed": False,
        "order_id": data.get("tag", ""),  # tag = intent_id for traceability
        "kite_order_id": data.get("order_id", ""),
    }

    # Persist to file-based audit log (append-only, never overwrite)
    try:
        import pathlib
        log_dir = pathlib.Path("data/webhook_audit_log")
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "zerodha_webhooks.jsonl"
        with open(log_file, "a") as f:
            f.write(json.dumps(log_entry) + "\n")
    except Exception as e:
        log.error("Failed to persist webhook audit log: %s", e)
        # Continue processing — audit failure should not block order processing

    log.info("Webhook received: order_id=%s status=%s filled=%s",
             data.get("order_id"), data.get("status"), data.get("filled_quantity"))

    # 3. Buffer for client polling
    client_event = {
        "webhook_id": webhook_id,
        "kite_order_id": data.get("order_id", ""),
        "status": data.get("status", ""),
        "filled_quantity": data.get("filled_quantity", 0),
        "pending_quantity": data.get("pending_quantity", 0),
        "average_price": data.get("average_price", 0),
        "exchange_timestamp": data.get("exchange_timestamp", ""),
        "tradingsymbol": data.get("tradingsymbol", ""),
        "tag": data.get("tag", ""),  # intent_id
        "received_at": log_entry["received_at"],
    }
    _pending_webhook_events.append(client_event)

    return jsonify({"success": True, "webhook_id": webhook_id})


@zerodha_bp.route("/pending-events", methods=["GET"])
@_require_kite_auth
def pending_events():
    """
    Client polls this endpoint to get buffered webhook events.
    Each call drains the buffer (events are returned once).
    Client then calls PROCESS_BROKER_EVENT for each event.
    """
    global _pending_webhook_events
    events = list(_pending_webhook_events)
    _pending_webhook_events = []  # drain buffer
    return jsonify({"success": True, "events": events, "count": len(events)})
