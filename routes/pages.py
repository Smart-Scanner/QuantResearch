"""HTML page routes."""

import logging
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Blueprint, render_template, session, redirect, url_for, send_from_directory, abort, request
from werkzeug.utils import secure_filename

log = logging.getLogger("pages")

import auth_db
from routes.auth import approved_required, subscribed_required, _has_access

pages_bp = Blueprint("pages", __name__)


def _trial_status(user, trial_days: int) -> dict:
    """For the dashboard banner: 'in_trial' (days_left), 'subscribed' (days_left), or None."""
    if user["is_admin"]:
        return None
    now = datetime.now(timezone.utc)
    if user["sub_expires_at"]:
        try:
            exp = datetime.fromisoformat(user["sub_expires_at"])
            if exp > now:
                return {"kind": "subscribed", "days_left": (exp - now).days}
        except ValueError:
            pass
    if user["trial_started_at"]:
        try:
            end = datetime.fromisoformat(user["trial_started_at"]) + timedelta(days=trial_days)
            if end > now:
                return {"kind": "trial", "days_left": (end - now).days, "hours_left": int((end - now).total_seconds() // 3600)}
        except ValueError:
            pass
    return None


@pages_bp.route("/")
def index():
    """Smart route:
       - ?auth_code=XXX → Fyers OAuth callback (exchanges for access_token)
       - logged out → public landing page
       - logged in + needs device verify → /auth/device/verify
       - logged in + trial/sub expired → /subscribe
       - logged in + has access → dashboard
    """
    # ── Fyers OAuth Callback: /?auth_code=XXX&state=YYY ──────────────
    auth_code = request.args.get("auth_code")
    if auth_code:
        return _handle_fyers_callback(auth_code)

    if not session.get("user_id"):
        plans = auth_db.list_plans(active_only=True)
        return render_template("landing.html", plans=plans, active="home")

    user = auth_db.get_user_by_id(session["user_id"])
    if not user:
        session.clear()
        plans = auth_db.list_plans(active_only=True)
        return render_template("landing.html", plans=plans, active="home")
    if user["status"] != "approved":
        session.clear()
        plans = auth_db.list_plans(active_only=True)
        return render_template("landing.html", plans=plans, active="home")

    if not user["is_admin"] and not _has_access(user):
        return redirect(url_for("pages.subscribe"))

    # Authenticated home lives at /dashboard
    return redirect(url_for("pages.dashboard"))


@pages_bp.route("/dashboard")
@subscribed_required
def dashboard():
    """Authenticated home — the research workspace dashboard."""
    user = auth_db.get_user_by_id(session["user_id"])
    trial_days = int(auth_db.get_setting("trial_duration_days", "3"))
    return render_template(
        "v3/dashboard.html",
        user=user,
        is_admin=bool(user["is_admin"]),
        trial_status=_trial_status(user, trial_days),
    )


def _handle_fyers_callback(auth_code: str):
    """Exchange Fyers auth_code for access_token and activate provider."""
    import os
    try:
        from fyers_apiv3 import fyersModel
        from config import FYERS_REDIRECT_URI

        app_id = os.getenv("FYERS_APP_ID", "")
        secret_key = os.getenv("FYERS_SECRET_KEY", "")

        if not app_id or not secret_key:
            log.error("[Fyers OAuth] FYERS_APP_ID or FYERS_SECRET_KEY not set")
            return redirect(url_for("pages.index"))

        # Exchange auth_code for access_token
        sess = fyersModel.SessionModel(
            client_id=app_id,
            secret_key=secret_key,
            redirect_uri=FYERS_REDIRECT_URI,
            response_type="code",
            grant_type="authorization_code",
        )
        sess.set_token(auth_code)
        token_response = sess.generate_token()

        if token_response and token_response.get("s") == "ok":
            access_token = token_response["access_token"]
            # Store in DB for persistence across restarts
            import db
            db.set_meta("fyers_access_token", access_token)
            db.set_meta("fyers_token_updated_at",
                        datetime.now(timezone.utc).isoformat())
            log.info("[Fyers OAuth] ✅ Token generated and stored successfully")

            # Hot-reload: activate provider immediately
            try:
                from data_provider import provider_manager
                from fyers_provider import FyersProvider
                config = {
                    "APP_ID": app_id,
                    "SECRET_KEY": secret_key,
                    "REDIRECT_URI": FYERS_REDIRECT_URI,
                    "ACCESS_TOKEN": access_token,
                    "ROLE": "RESEARCH",
                }
                fyers_prov = FyersProvider("FYERS_1", config)
                if fyers_prov.login():
                    provider_manager.providers["FYERS_1"] = fyers_prov
                    log.info("[Fyers OAuth] ✅ Provider hot-reloaded")
            except Exception as exc:
                log.warning("[Fyers OAuth] Hot-reload failed (non-fatal): %s", exc)
        else:
            log.error("[Fyers OAuth] Token exchange failed: %s", token_response)

    except ImportError:
        log.warning("[Fyers OAuth] fyers-apiv3 not installed")
    except Exception as exc:
        log.error("[Fyers OAuth] Callback error: %s", exc)

    return redirect(url_for("pages.index"))


# ── V3 Page Routes ───────────────────────────────────────────────────
@pages_bp.route("/top-picks")
@subscribed_required
def top_picks():
    user = auth_db.get_user_by_id(session["user_id"])
    return render_template("v3/top_picks.html", user=user, is_admin=bool(user["is_admin"]))


def _scoring_v1_active() -> bool:
    """True when the UI is on the scoring_v1 engine (default). Golden/HC/Breakouts
    are LEGACY concepts scoring_v1 has no equivalent for yet, so they are hidden
    while scoring_v1 is active (no legacy data in the UI). Flip the 'ui_reco_source'
    meta to 'legacy' to restore them for side-by-side review."""
    try:
        import db
        return (db.get_meta("ui_reco_source") or "scoring_v1") == "scoring_v1"
    except Exception:
        return True


@pages_bp.route("/golden")
@subscribed_required
def golden_stocks():
    if _scoring_v1_active():
        return redirect(url_for("pages.top_picks"))  # hidden under scoring_v1 (legacy concept)
    user = auth_db.get_user_by_id(session["user_id"])
    return render_template("v3/golden.html", user=user, is_admin=bool(user["is_admin"]))


@pages_bp.route("/hc")
@subscribed_required
def high_conviction():
    if _scoring_v1_active():
        return redirect(url_for("pages.top_picks"))  # hidden under scoring_v1 (legacy concept)
    user = auth_db.get_user_by_id(session["user_id"])
    return render_template("v3/high_conviction.html", user=user, is_admin=bool(user["is_admin"]))


@pages_bp.route("/breakouts")
@subscribed_required
def breakouts_page():
    if _scoring_v1_active():
        return redirect(url_for("pages.top_picks"))  # hidden under scoring_v1 (legacy concept)
    user = auth_db.get_user_by_id(session["user_id"])
    return render_template("v3/breakouts.html", user=user, is_admin=bool(user["is_admin"]))


@pages_bp.route("/discovery")
@subscribed_required
def discovery_center():
    user = auth_db.get_user_by_id(session["user_id"])
    return render_template("discovery_center.html", user=user, is_admin=bool(user["is_admin"]))


@pages_bp.route("/research")
@subscribed_required
def research_center():
    user = auth_db.get_user_by_id(session["user_id"])
    return render_template("research_center.html", user=user, is_admin=bool(user["is_admin"]))


@pages_bp.route("/mission-control")
@subscribed_required
def mission_control():
    user = auth_db.get_user_by_id(session["user_id"])
    return render_template("mission_control.html", user=user, is_admin=bool(user["is_admin"]))



@pages_bp.route("/market")
@subscribed_required
def market_intel():
    user = auth_db.get_user_by_id(session["user_id"])
    return render_template("v3/market_intel.html", user=user, is_admin=bool(user["is_admin"]))


@pages_bp.route("/paper-trades-view")
@subscribed_required
def paper_trades_view():
    user = auth_db.get_user_by_id(session["user_id"])
    return render_template("v3/paper_trades.html", user=user, is_admin=bool(user["is_admin"]))


@pages_bp.route("/outcome")
@subscribed_required
def outcome_intelligence():
    user = auth_db.get_user_by_id(session["user_id"])
    return render_template("v3/outcome.html", user=user, is_admin=bool(user["is_admin"]))


@pages_bp.route("/watchlist")
@subscribed_required
def watchlist_page():
    user = auth_db.get_user_by_id(session["user_id"])
    return render_template("v3/watchlist.html", user=user, is_admin=bool(user["is_admin"]))


@pages_bp.route("/settings")
@subscribed_required
def settings_page():
    user = auth_db.get_user_by_id(session["user_id"])
    return render_template("v3/settings.html", user=user, is_admin=bool(user["is_admin"]))


@pages_bp.route("/pricing")
def pricing():
    plans = auth_db.list_plans(active_only=True)
    return render_template("pricing.html", plans=plans, active="pricing")


@pages_bp.route("/about")
def about():
    return render_template("about.html", active="about")


@pages_bp.route("/contact")
def contact():
    return render_template("contact.html", active="contact")


UPLOAD_BASE = Path(__file__).resolve().parent.parent / "cache" / "uploads"
PAYMENT_UPLOAD_DIR = UPLOAD_BASE / "payments"
PAYMENT_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
PAYMENT_ALLOWED_EXT = {"png", "jpg", "jpeg", "webp", "gif"}
PAYMENT_MAX_BYTES = 5 * 1024 * 1024  # 5 MB (screenshots can be larger than QR)


@pages_bp.route("/sw.js")
def service_worker():
    """Serve the service worker from the root scope so it can control the whole app."""
    static_dir = Path(__file__).resolve().parent.parent / "static"
    resp = send_from_directory(static_dir, "sw.js", mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


@pages_bp.route("/uploads/qr/<path:filename>")
def serve_qr(filename):
    """Serve admin-uploaded QR images. No auth — QR contains a public UPI handle."""
    qr_dir = UPLOAD_BASE / "qr"
    if not (qr_dir / filename).is_file():
        abort(404)
    return send_from_directory(qr_dir, filename)


@pages_bp.route("/uploads/payments/<path:filename>")
@approved_required
def serve_payment_screenshot(filename):
    """Serve a payment screenshot. Restricted: only admins, or the user who uploaded it."""
    if not (PAYMENT_UPLOAD_DIR / filename).is_file():
        abort(404)
    user = auth_db.get_user_by_id(session["user_id"])
    if not user:
        abort(403)
    # Admins can view all
    if user["is_admin"]:
        return send_from_directory(PAYMENT_UPLOAD_DIR, filename)
    # Non-admins: only the file referenced by their own submission(s)
    import sqlite3
    conn = auth_db._get_conn()
    row = conn.execute(
        "SELECT 1 FROM payment_submissions WHERE user_id=? AND screenshot_path=?",
        (user["id"], f"payments/{filename}"),
    ).fetchone()
    if not row:
        abort(403)
    return send_from_directory(PAYMENT_UPLOAD_DIR, filename)


def _save_payment_screenshot(file_storage, user_id: int) -> tuple[bool, str]:
    """Validate and persist a payment-proof screenshot.

    Returns (ok, value_or_error). On success, value is the path stored in DB
    (e.g. 'payments/<filename>').
    """
    if not file_storage or not file_storage.filename:
        return True, ""  # screenshot is optional
    filename = secure_filename(file_storage.filename)
    if "." not in filename:
        return False, "screenshot-bad-ext"
    ext = filename.rsplit(".", 1)[1].lower()
    if ext not in PAYMENT_ALLOWED_EXT:
        return False, "screenshot-bad-ext"
    file_storage.stream.seek(0, 2)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size > PAYMENT_MAX_BYTES:
        return False, "screenshot-too-large"
    if size == 0:
        return True, ""  # treat empty as no upload
    new_name = f"pay_{user_id}_{int(time.time())}_{uuid.uuid4().hex[:8]}.{ext}"
    file_storage.save(str(PAYMENT_UPLOAD_DIR / new_name))
    return True, f"payments/{new_name}"


def _delete_payment_screenshot(rel_path: str) -> None:
    if not rel_path:
        return
    base = rel_path.split("/", 1)[-1]
    target = PAYMENT_UPLOAD_DIR / base
    try:
        if target.is_file():
            target.unlink()
    except OSError as exc:
        log.warning("Could not delete payment screenshot %s: %s", target, exc)


@pages_bp.route("/subscribe/submit-payment", methods=["POST"])
@approved_required
def submit_payment():
    """User submits UTR + screenshot after paying offline."""
    user = auth_db.get_user_by_id(session["user_id"])
    if not user or user["is_admin"]:
        # Admins don't subscribe; nothing to submit
        return redirect(url_for("pages.subscribe"))

    try:
        plan_id = int(request.form["plan_id"])
    except (KeyError, ValueError):
        return redirect(url_for("pages.subscribe", msg="bad-input"))
    utr = (request.form.get("utr") or "").strip()
    note = (request.form.get("note") or "").strip()[:500] or None

    if len(utr) < 4:
        return redirect(url_for("pages.subscribe", msg="utr-too-short"))
    plan = auth_db.get_plan(plan_id)
    if not plan or not plan["is_active"]:
        return redirect(url_for("pages.subscribe", msg="plan-not-found"))

    ok, val = _save_payment_screenshot(request.files.get("screenshot"), user["id"])
    if not ok:
        return redirect(url_for("pages.subscribe", msg=val))
    screenshot_path = val or None

    _, prior_screenshot = auth_db.submit_payment(
        user_id=user["id"],
        plan_id=plan_id,
        utr=utr,
        screenshot_path=screenshot_path,
        note=note,
    )
    if prior_screenshot:
        _delete_payment_screenshot(prior_screenshot)
    return redirect(url_for("pages.subscribe", msg="payment-submitted"))


@pages_bp.route("/symbol/<symbol>")
@subscribed_required
def symbol_workspace(symbol):
    """MarketOS Symbol Workspace — the canonical route for symbol analysis."""
    import re
    import urllib.parse
    from symbol_utils import check_symbol_exists

    # P0-1: Decode URL-encoded symbols (M%26M → M&M, L%26T → L&T)
    normalized_symbol = urllib.parse.unquote(symbol).strip().upper()
    log.info("[SYMBOL_ROUTE] requested=%s normalized=%s", symbol, normalized_symbol)

    # P0-3: Hard malformed validation — redirect to /top-picks
    is_malformed = not re.match(r"^[A-Za-z0-9\-\&\.]+$", normalized_symbol)
    if not normalized_symbol or len(normalized_symbol) > 20 or is_malformed:
        from flask import flash
        flash("Invalid symbol format")
        return redirect(url_for("pages.top_picks"))

    # P0-3: Soft existence validation — show warning, don't block
    data_unavailable = not check_symbol_exists(normalized_symbol)
    if data_unavailable:
        log.info("[SYMBOL_ROUTE] Symbol %s not found in scan results or active universe", normalized_symbol)

    user = auth_db.get_user_by_id(session["user_id"])
    return render_template(
        "symbol_workspace.html",
        symbol=normalized_symbol,
        user=user,
        is_admin=bool(user["is_admin"]),
        active_page="symbol_workspace",
        data_unavailable=data_unavailable,
    )


@pages_bp.route("/stock/<symbol>")
@subscribed_required
def stock_detail(symbol):
    """Backward-compatible redirect to the canonical Symbol Workspace route."""
    import urllib.parse
    return redirect(url_for("pages.symbol_workspace", symbol=urllib.parse.unquote(symbol).strip().upper()), code=301)


@pages_bp.route("/portfolio")
@pages_bp.route("/portfolio/<int:pid>")
@subscribed_required
def portfolio_page(pid=None):
    return render_template("portfolio_center.html", portfolio_id=pid)


@pages_bp.route("/subscribe")
@approved_required
def subscribe():
    """Shown to users whose trial expired and have no active subscription."""
    user = auth_db.get_user_by_id(session["user_id"])
    # If they actually still have access, bounce them back to the dashboard.
    if user["is_admin"] or _has_access(user):
        return redirect(url_for("pages.index"))
    plans = auth_db.list_plans(active_only=True)
    pending = auth_db.get_pending_payment_for_user(user["id"])
    return render_template(
        "subscribe.html",
        user=user,
        plans=plans,
        pending=pending,
        msg=request.args.get("msg", ""),
    )
