"""
Local credentials login + auth decorators.
"""

import logging
from datetime import datetime, timezone, timedelta
from functools import wraps
from flask import Blueprint, redirect, url_for, session, request, render_template, abort

import auth_db

# ---------------------------------------------------------------------------
# Hardcoded local dev credentials  (username: admin  /  password: admin123)
# ---------------------------------------------------------------------------
_LOCAL_USERNAME = "admin"
_LOCAL_PASSWORD = "admin123"
_LOCAL_EMAIL    = "admin@local.dev"


def _has_access(user) -> bool:
    """True if user has either an active trial OR a non-expired subscription.

    Admins are NOT checked here — caller should treat is_admin as always-true.
    """
    now = datetime.now(timezone.utc)

    sub_expires = user["sub_expires_at"]
    if sub_expires:
        try:
            if datetime.fromisoformat(sub_expires) > now:
                return True
        except ValueError:
            pass

    trial_started = user["trial_started_at"]
    if trial_started:
        try:
            trial_days = int(auth_db.get_setting("trial_duration_days", "3"))
            trial_end = datetime.fromisoformat(trial_started) + timedelta(days=trial_days)
            if trial_end > now:
                return True
        except (ValueError, TypeError):
            pass

    return False

log = logging.getLogger("auth")

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


# ─────────────────────────── routes ───────────────────────────

@auth_bp.route("/local-login", methods=["GET", "POST"])
def local_login():
    """Simple hardcoded username/password login for development.

    Credentials: admin / admin123
    Creates (or reuses) a local admin user so the full app is accessible.
    """
    # One public page: the login form lives on the landing page (pages.index).
    if request.method == "GET":
        return redirect(url_for("pages.index"))

    username = (request.form.get("username") or "").strip()
    password = (request.form.get("password") or "").strip()
    if username == _LOCAL_USERNAME and password == _LOCAL_PASSWORD:
        user = auth_db.get_or_create_local_admin(_LOCAL_EMAIL, name="Local Admin")
        session.clear()
        session["user_id"] = user["id"]
        session["email"]   = user["email"]
        return redirect(request.args.get("next") or url_for("pages.dashboard"))
    elif username == "testuser" and password == "admin123":
        user = auth_db.get_or_create_local_user("testuser@local.dev", name="Test User")
        session.clear()
        session["user_id"] = user["id"]
        session["email"]   = user["email"]
        return redirect(request.args.get("next") or url_for("pages.dashboard"))

    # Failed login: re-render the single public page with an inline error.
    return render_template("landing.html", error="Invalid username or password.")


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("pages.index"))


# ─────────────────────────── decorators ───────────────────────────

def _current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return auth_db.get_user_by_id(uid)


def login_required(f):
    """Any signed-in user."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("auth.local_login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


def approved_required(f):
    """Signed-in + status='approved'."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("auth.local_login", next=request.path))
        user = _current_user()
        if not user:
            session.clear()
            return redirect(url_for("auth.local_login"))
        if user["status"] != "approved":
            session.clear()
            return redirect(url_for("pages.index"))
        return f(*args, **kwargs)
    return wrapper


def subscribed_required(f):
    """Approved + (admin OR has trial/sub access)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("auth.local_login", next=request.path))
        user = _current_user()
        if not user:
            session.clear()
            return redirect(url_for("auth.local_login"))
        if user["status"] != "approved":
            session.clear()
            return redirect(url_for("pages.index"))
        if not user["is_admin"] and not _has_access(user):
            return redirect(url_for("pages.subscribe"))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    """Signed-in AND status='approved' AND users.is_admin=1."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("auth.local_login", next=request.path))
        user = _current_user()
        if not user or user["status"] != "approved" or not user["is_admin"]:
            abort(403)
        return f(*args, **kwargs)
    return wrapper
