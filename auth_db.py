"""
Auth database (SQLite) — Google OAuth users + device fingerprint bindings.

Isolated from screener.db so auth data has its own lifecycle and can be
backed up / migrated independently.
"""

import sqlite3
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("auth")

DB_PATH = Path(__file__).parent / "cache" / "auth.db"
_local = threading.local()

DEFAULT_TRIAL_DAYS = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _migrate_devices_to_v2(conn: sqlite3.Connection) -> None:
    """If devices table is at v1 (no 'pending' in CHECK), rebuild it to v2.

    Detection: the table's CREATE TABLE text must contain 'pending' literal.
    Migration: rename old, create new, copy compatible columns, drop old.
    Idempotent: noop on fresh DBs (no devices table yet) and on already-v2 DBs.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='devices'"
    ).fetchone()
    if not row:
        return  # No devices table yet — init_db's CREATE IF NOT EXISTS will create v2
    if "'pending'" in (row[0] or ""):
        return  # Already v2

    conn.executescript("""
        DROP INDEX IF EXISTS uniq_devices_visitor_active;
        DROP INDEX IF EXISTS uniq_devices_user_active;
        DROP INDEX IF EXISTS idx_devices_user;
        ALTER TABLE devices RENAME TO devices_v1;
        CREATE TABLE devices (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            visitor_id          TEXT NOT NULL,
            confidence_score    REAL,
            user_agent          TEXT,
            ip                  TEXT,
            status              TEXT NOT NULL CHECK (status IN ('pending','active','revoked')),
            first_seen_at       TEXT NOT NULL,
            last_seen_at        TEXT NOT NULL,
            approved_at         TEXT,
            approved_by         TEXT,
            revoked_at          TEXT,
            revoked_by          TEXT,
            revoked_reason      TEXT
        );
        INSERT INTO devices
            (id, user_id, visitor_id, confidence_score, user_agent, ip, status,
             first_seen_at, last_seen_at, revoked_at, revoked_by, revoked_reason)
            SELECT id, user_id, visitor_id, confidence_score, user_agent, ip, status,
                   first_seen_at, last_seen_at, revoked_at, revoked_by, revoked_reason
            FROM devices_v1;
        DROP TABLE devices_v1;
        CREATE UNIQUE INDEX IF NOT EXISTS uniq_devices_visitor_active
            ON devices(visitor_id) WHERE status='active';
        CREATE UNIQUE INDEX IF NOT EXISTS uniq_devices_user_active
            ON devices(user_id) WHERE status='active';
        CREATE UNIQUE INDEX IF NOT EXISTS uniq_devices_user_pending
            ON devices(user_id) WHERE status='pending';
        CREATE INDEX IF NOT EXISTS idx_devices_user ON devices(user_id);
    """)
    log.info("Migrated devices table to v2 (added 'pending' status + approved_at/by + indexes)")


def _get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        DB_PATH.parent.mkdir(exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return _local.conn


def init_db() -> None:
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            google_sub      TEXT UNIQUE,
            email           TEXT NOT NULL UNIQUE,
            name            TEXT,
            picture_url     TEXT,
            phone           TEXT,
            status          TEXT NOT NULL CHECK (status IN ('pending','approved','rejected','suspended')),
            created_at      TEXT NOT NULL,
            approved_at     TEXT,
            approved_by     TEXT,
            last_login_at   TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);
        CREATE INDEX IF NOT EXISTS idx_users_email  ON users(email);

        CREATE TABLE IF NOT EXISTS devices (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            visitor_id          TEXT NOT NULL,
            confidence_score    REAL,
            user_agent          TEXT,
            ip                  TEXT,
            status              TEXT NOT NULL CHECK (status IN ('pending','active','revoked')),
            first_seen_at       TEXT NOT NULL,
            last_seen_at        TEXT NOT NULL,
            approved_at         TEXT,
            approved_by         TEXT,
            revoked_at          TEXT,
            revoked_by          TEXT,
            revoked_reason      TEXT
        );

        -- A given fingerprint can be claimed by at most ONE active binding (cross-user uniqueness)
        CREATE UNIQUE INDEX IF NOT EXISTS uniq_devices_visitor_active
            ON devices(visitor_id) WHERE status='active';

        -- Each user has at most ONE active device
        CREATE UNIQUE INDEX IF NOT EXISTS uniq_devices_user_active
            ON devices(user_id) WHERE status='active';

        -- Each user has at most ONE pending device awaiting admin approval
        CREATE UNIQUE INDEX IF NOT EXISTS uniq_devices_user_pending
            ON devices(user_id) WHERE status='pending';

        CREATE INDEX IF NOT EXISTS idx_devices_user ON devices(user_id);

        CREATE TABLE IF NOT EXISTS subscription_plans (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            duration_days   INTEGER NOT NULL CHECK (duration_days > 0),
            price_inr       INTEGER NOT NULL CHECK (price_inr >= 0),
            is_active       INTEGER NOT NULL DEFAULT 1,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_plans_active ON subscription_plans(is_active);

        CREATE TABLE IF NOT EXISTS settings (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS payment_submissions (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            plan_id             INTEGER,  -- nullable: if plan is deleted, history is preserved
            utr                 TEXT NOT NULL,
            screenshot_path     TEXT,
            note                TEXT,
            status              TEXT NOT NULL CHECK (status IN ('pending','approved','rejected')),
            submitted_at        TEXT NOT NULL,
            reviewed_at         TEXT,
            reviewed_by         TEXT,
            review_note         TEXT
        );

        -- One pending submission per user at a time (resubmit replaces)
        CREATE UNIQUE INDEX IF NOT EXISTS uniq_payments_user_pending
            ON payment_submissions(user_id) WHERE status='pending';
        CREATE INDEX IF NOT EXISTS idx_payments_status ON payment_submissions(status);
        CREATE INDEX IF NOT EXISTS idx_payments_user ON payment_submissions(user_id);
    """)

    # Migrate v1 devices table to v2 (add 'pending' to CHECK; add approved_at/by; pending uniqueness)
    _migrate_devices_to_v2(conn)

    # subscription_plans column additions (idempotent)
    for col, ddl in [
        ("qr_image_path", "TEXT"),
        ("upi_id",        "TEXT"),
    ]:
        if not _column_exists(conn, "subscription_plans", col):
            conn.execute(f"ALTER TABLE subscription_plans ADD COLUMN {col} {ddl}")
            log.info("Migrated: added subscription_plans.%s", col)

    # users column additions (idempotent migration from v1 schema)
    for col, ddl in [
        ("is_admin",         "INTEGER NOT NULL DEFAULT 0"),
        ("trial_started_at", "TEXT"),
        ("sub_plan_id",      "INTEGER"),
        ("sub_started_at",   "TEXT"),
        ("sub_expires_at",   "TEXT"),
    ]:
        if not _column_exists(conn, "users", col):
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
            log.info("Migrated: added users.%s", col)

    conn.execute(
        "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?,?,?)",
        ("trial_duration_days", str(DEFAULT_TRIAL_DAYS), _now()),
    )

    conn.commit()
    log.info("Auth DB initialized: %s", DB_PATH)


# ───────────────────────────── users ─────────────────────────────

def get_user_by_email(email: str) -> Optional[sqlite3.Row]:
    conn = _get_conn()
    return conn.execute(
        "SELECT * FROM users WHERE email = ?", (email.lower(),)
    ).fetchone()


def get_user_by_google_sub(sub: str) -> Optional[sqlite3.Row]:
    conn = _get_conn()
    return conn.execute(
        "SELECT * FROM users WHERE google_sub = ?", (sub,)
    ).fetchone()


def get_user_by_id(user_id: int) -> Optional[sqlite3.Row]:
    conn = _get_conn()
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def upsert_oauth_user(google_sub: str, email: str, name: str, picture_url: str) -> sqlite3.Row:
    """Find user by google_sub or email; auto-approve + start trial if absent.

    Behavior:
      - Existing row (admin-seeded or returning user): link google_sub if needed,
        refresh profile fields, leave status / trial / sub unchanged. Backfills
        trial_started_at if the row is approved but never had a trial recorded
        (covers users migrated from the older 'pending' flow).
      - New row: status='approved', trial_started_at=now (3-day trial starts
        immediately at signup; no admin approval required).
    """
    email = email.lower()
    conn = _get_conn()
    now = _now()

    row = (
        conn.execute("SELECT * FROM users WHERE google_sub = ?", (google_sub,)).fetchone()
        or conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    )

    if row:
        new_google_sub = google_sub if row["google_sub"] != google_sub else row["google_sub"]
        # Backfill trial for approved-but-no-trial users (legacy migration path).
        trial_started_at = row["trial_started_at"]
        if row["status"] == "approved" and not trial_started_at and not row["is_admin"]:
            trial_started_at = now
        conn.execute(
            """UPDATE users
                  SET google_sub=?, name=?, picture_url=?, last_login_at=?, trial_started_at=?
                WHERE id=?""",
            (new_google_sub, name, picture_url, now, trial_started_at, row["id"]),
        )
        conn.commit()
        return conn.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()

    cur = conn.execute(
        """INSERT INTO users
           (google_sub, email, name, picture_url, status,
            created_at, approved_at, approved_by, last_login_at, trial_started_at)
           VALUES (?,?,?,?, 'approved', ?,?, 'system:auto-approve', ?, ?)""",
        (google_sub, email, name, picture_url, now, now, now, now),
    )
    conn.commit()
    log.info("Created user %s (id=%s) status=approved trial_started=%s", email, cur.lastrowid, now)
    return conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()


def mark_login(user_id: int) -> None:
    conn = _get_conn()
    conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (_now(), user_id))
    conn.commit()


def list_users_by_status(status: str) -> list:
    conn = _get_conn()
    return conn.execute(
        "SELECT * FROM users WHERE status = ? ORDER BY created_at DESC",
        (status,),
    ).fetchall()


def approve_user(user_id: int, approver_email: str) -> bool:
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE users SET status='approved', approved_at=?, approved_by=? WHERE id=? AND status='pending'",
        (_now(), approver_email, user_id),
    )
    conn.commit()
    return cur.rowcount > 0


# ───────────────────────── admin management ─────────────────────────

def is_admin_user(user_id: int) -> bool:
    conn = _get_conn()
    row = conn.execute("SELECT is_admin FROM users WHERE id=?", (user_id,)).fetchone()
    return bool(row and row["is_admin"])


def list_admins() -> list:
    conn = _get_conn()
    return conn.execute(
        "SELECT * FROM users WHERE is_admin=1 ORDER BY email"
    ).fetchall()


def set_user_admin(user_id: int, is_admin: bool) -> bool:
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE users SET is_admin=? WHERE id=?", (1 if is_admin else 0, user_id)
    )
    conn.commit()
    return cur.rowcount > 0


# ─────────────────────────── plans ───────────────────────────

def list_plans(active_only: bool = False) -> list:
    conn = _get_conn()
    sql = "SELECT * FROM subscription_plans"
    if active_only:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY duration_days ASC, id ASC"
    return conn.execute(sql).fetchall()


def get_plan(plan_id: int) -> Optional[sqlite3.Row]:
    conn = _get_conn()
    return conn.execute("SELECT * FROM subscription_plans WHERE id = ?", (plan_id,)).fetchone()


def create_plan(name: str, duration_days: int, price_inr: int,
                 upi_id: Optional[str] = None, qr_image_path: Optional[str] = None) -> sqlite3.Row:
    conn = _get_conn()
    now = _now()
    cur = conn.execute(
        """INSERT INTO subscription_plans
           (name, duration_days, price_inr, is_active, upi_id, qr_image_path, created_at, updated_at)
           VALUES (?, ?, ?, 1, ?, ?, ?, ?)""",
        (name.strip(), duration_days, price_inr, (upi_id or None), (qr_image_path or None), now, now),
    )
    conn.commit()
    return conn.execute("SELECT * FROM subscription_plans WHERE id = ?", (cur.lastrowid,)).fetchone()


def update_plan(plan_id: int, name: str, duration_days: int, price_inr: int,
                 upi_id: Optional[str] = None, qr_image_path: Optional[str] = None,
                 clear_qr: bool = False) -> bool:
    conn = _get_conn()
    if qr_image_path is not None:
        # new QR uploaded — overwrite
        cur = conn.execute(
            """UPDATE subscription_plans
                  SET name=?, duration_days=?, price_inr=?, upi_id=?, qr_image_path=?, updated_at=?
                WHERE id=?""",
            (name.strip(), duration_days, price_inr, (upi_id or None), qr_image_path, _now(), plan_id),
        )
    elif clear_qr:
        cur = conn.execute(
            """UPDATE subscription_plans
                  SET name=?, duration_days=?, price_inr=?, upi_id=?, qr_image_path=NULL, updated_at=?
                WHERE id=?""",
            (name.strip(), duration_days, price_inr, (upi_id or None), _now(), plan_id),
        )
    else:
        # leave qr_image_path as-is
        cur = conn.execute(
            """UPDATE subscription_plans
                  SET name=?, duration_days=?, price_inr=?, upi_id=?, updated_at=?
                WHERE id=?""",
            (name.strip(), duration_days, price_inr, (upi_id or None), _now(), plan_id),
        )
    conn.commit()
    return cur.rowcount > 0


def delete_plan(plan_id: int) -> tuple[bool, Optional[str]]:
    """Hard-delete a plan. Returns (success, qr_path_to_remove_from_disk).

    Side-effect: any user with sub_plan_id pointing at this plan is detached
    (sub_plan_id NULL, sub_started_at NULL, sub_expires_at preserved as record).
    """
    conn = _get_conn()
    row = conn.execute("SELECT qr_image_path FROM subscription_plans WHERE id=?", (plan_id,)).fetchone()
    if not row:
        return False, None
    qr_path = row["qr_image_path"]
    conn.execute(
        "UPDATE users SET sub_plan_id=NULL WHERE sub_plan_id=?", (plan_id,),
    )
    conn.execute("DELETE FROM subscription_plans WHERE id=?", (plan_id,))
    conn.commit()
    log.info("Deleted plan %s; detached from users; qr_to_remove=%s", plan_id, qr_path)
    return True, qr_path


# ─────────────────────────── user admin actions ───────────────────────────

def suspend_user(user_id: int, by_email: str) -> bool:
    """Block a user — they're signed out on next request, OAuth callback rejects further sign-ins."""
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE users SET status='suspended' WHERE id=? AND is_admin=0",
        (user_id,),
    )
    conn.commit()
    if cur.rowcount > 0:
        log.info("Suspended user %s by %s", user_id, by_email)
    return cur.rowcount > 0


def unsuspend_user(user_id: int, by_email: str) -> bool:
    """Reinstate a suspended user."""
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE users SET status='approved' WHERE id=? AND status='suspended'",
        (user_id,),
    )
    conn.commit()
    if cur.rowcount > 0:
        log.info("Unsuspended user %s by %s", user_id, by_email)
    return cur.rowcount > 0


# ─────────────────────────── payment submissions ───────────────────────────

def get_pending_payment_for_user(user_id: int) -> Optional[sqlite3.Row]:
    conn = _get_conn()
    return conn.execute(
        """SELECT s.*, p.name AS plan_name, p.duration_days AS plan_duration_days, p.price_inr AS plan_price
             FROM payment_submissions s
             LEFT JOIN subscription_plans p ON p.id = s.plan_id
            WHERE s.user_id = ? AND s.status = 'pending'""",
        (user_id,),
    ).fetchone()


def submit_payment(user_id: int, plan_id: int, utr: str,
                    screenshot_path: Optional[str], note: Optional[str]) -> tuple[sqlite3.Row, Optional[str]]:
    """Insert (or replace) a pending submission for this user.

    Returns (new_row, prior_screenshot_to_delete). The caller should remove the
    prior screenshot file from disk when present.
    """
    conn = _get_conn()
    prior = conn.execute(
        "SELECT id, screenshot_path FROM payment_submissions WHERE user_id=? AND status='pending'",
        (user_id,),
    ).fetchone()
    prior_screenshot = prior["screenshot_path"] if prior else None
    if prior:
        conn.execute("DELETE FROM payment_submissions WHERE id=?", (prior["id"],))
    cur = conn.execute(
        """INSERT INTO payment_submissions
           (user_id, plan_id, utr, screenshot_path, note, status, submitted_at)
           VALUES (?,?,?,?,?,'pending',?)""",
        (user_id, plan_id, utr.strip(), screenshot_path, (note or None), _now()),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM payment_submissions WHERE id=?", (cur.lastrowid,)).fetchone()
    log.info("Payment submission %s by user %s (plan %s utr=%s)", cur.lastrowid, user_id, plan_id, utr[:8])
    return row, prior_screenshot


def list_pending_payments() -> list:
    conn = _get_conn()
    return conn.execute(
        """SELECT s.*,
                  u.email AS user_email, u.name AS user_name,
                  p.name  AS plan_name, p.duration_days AS plan_duration_days, p.price_inr AS plan_price
             FROM payment_submissions s
             JOIN users u ON u.id = s.user_id
             LEFT JOIN subscription_plans p ON p.id = s.plan_id
            WHERE s.status = 'pending'
            ORDER BY s.submitted_at ASC"""
    ).fetchall()


def approve_payment(submission_id: int, by_email: str) -> tuple[bool, Optional[sqlite3.Row]]:
    """Mark a submission approved AND auto-assign the plan to the user.

    Returns (ok, updated_user_row). If plan was deleted (plan_id NULL), still
    approves the submission but skips plan assignment.
    """
    conn = _get_conn()
    sub = conn.execute(
        "SELECT * FROM payment_submissions WHERE id=? AND status='pending'", (submission_id,)
    ).fetchone()
    if not sub:
        return False, None
    user_row = None
    if sub["plan_id"]:
        user_row = assign_subscription(sub["user_id"], sub["plan_id"])
    conn.execute(
        """UPDATE payment_submissions
              SET status='approved', reviewed_at=?, reviewed_by=?
            WHERE id=?""",
        (_now(), by_email, submission_id),
    )
    conn.commit()
    log.info("Payment %s approved by %s for user %s", submission_id, by_email, sub["user_id"])
    return True, user_row


def reject_payment(submission_id: int, by_email: str, review_note: Optional[str] = None) -> bool:
    conn = _get_conn()
    cur = conn.execute(
        """UPDATE payment_submissions
              SET status='rejected', reviewed_at=?, reviewed_by=?, review_note=?
            WHERE id=? AND status='pending'""",
        (_now(), by_email, review_note, submission_id),
    )
    conn.commit()
    return cur.rowcount > 0


def get_payment_submission(submission_id: int) -> Optional[sqlite3.Row]:
    conn = _get_conn()
    return conn.execute(
        "SELECT * FROM payment_submissions WHERE id=?", (submission_id,)
    ).fetchone()


def expire_user_access(user_id: int, by_email: str) -> bool:
    """Force-end a user's trial AND any active subscription, immediately.

    Backdates trial_started_at and zeroes the subscription so _has_access() → False
    on the next page load.
    """
    conn = _get_conn()
    from datetime import datetime, timezone, timedelta
    backdated = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat(timespec="seconds")
    cur = conn.execute(
        """UPDATE users
              SET trial_started_at = ?,
                  sub_plan_id      = NULL,
                  sub_started_at   = NULL,
                  sub_expires_at   = NULL
            WHERE id = ? AND is_admin = 0""",
        (backdated, user_id),
    )
    conn.commit()
    if cur.rowcount > 0:
        log.info("Force-expired access for user %s by %s", user_id, by_email)
    return cur.rowcount > 0


def toggle_plan_active(plan_id: int) -> Optional[bool]:
    """Flip is_active; returns new state or None if plan missing."""
    conn = _get_conn()
    row = conn.execute("SELECT is_active FROM subscription_plans WHERE id = ?", (plan_id,)).fetchone()
    if not row:
        return None
    new_state = 0 if row["is_active"] else 1
    conn.execute(
        "UPDATE subscription_plans SET is_active=?, updated_at=? WHERE id=?",
        (new_state, _now(), plan_id),
    )
    conn.commit()
    return bool(new_state)


# ─────────────────────────── settings ───────────────────────────

def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    conn = _get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    conn = _get_conn()
    conn.execute(
        """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        (key, value, _now()),
    )
    conn.commit()


# ─────────────────────────── users (admin view) ───────────────────────────

def assign_subscription(user_id: int, plan_id: int) -> Optional[sqlite3.Row]:
    """Activate (or extend) a subscription on a user.

    If the user already has a non-expired subscription, new days stack on
    top of the existing expiry instead of resetting to 'now' — so renewing
    early doesn't burn the remaining days. If expired or first-time, the
    new period starts at 'now'.

    Returns the updated user row, or None if user/plan not found.
    """
    conn = _get_conn()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    plan = conn.execute(
        "SELECT * FROM subscription_plans WHERE id = ? AND is_active = 1", (plan_id,)
    ).fetchone()
    if not user or not plan:
        return None

    from datetime import datetime, timezone, timedelta
    now_dt = datetime.now(timezone.utc)
    base = now_dt
    if user["sub_expires_at"]:
        try:
            existing = datetime.fromisoformat(user["sub_expires_at"])
            if existing > now_dt:
                base = existing  # extend from existing expiry
        except ValueError:
            pass

    new_expiry = base + timedelta(days=plan["duration_days"])
    now = _now()
    conn.execute(
        """UPDATE users
              SET sub_plan_id=?, sub_started_at=?, sub_expires_at=?
            WHERE id=?""",
        (plan_id, now, new_expiry.isoformat(timespec="seconds"), user_id),
    )
    conn.commit()
    log.info("Assigned plan %s to user %s; new expiry %s", plan_id, user_id, new_expiry.isoformat())
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def list_all_users_with_plan() -> list:
    """All users, joined with their current plan name (if any)."""
    conn = _get_conn()
    return conn.execute(
        """SELECT u.*, p.name AS plan_name, p.duration_days AS plan_duration_days
             FROM users u
             LEFT JOIN subscription_plans p ON p.id = u.sub_plan_id
            ORDER BY u.is_admin DESC, u.created_at DESC"""
    ).fetchall()


# ─────────────────────────── devices ───────────────────────────

CLAIM_OK_ACTIVE       = "active"           # device verified, session is good
CLAIM_PENDING         = "pending"          # new device, awaits admin approval
CLAIM_CONFLICT_OTHER  = "conflict_other"   # fingerprint already linked to a different account


def get_active_device(user_id: int) -> Optional[sqlite3.Row]:
    conn = _get_conn()
    return conn.execute(
        "SELECT * FROM devices WHERE user_id=? AND status='active'", (user_id,)
    ).fetchone()


def claim_device(user_id: int, visitor_id: str, confidence: Optional[float],
                 user_agent: str, ip: str) -> dict:
    """Reconcile a fingerprint against this user's device binding.

    Returns {state, device}:
      - state='active'         : verified (existing match OR first device for user)
      - state='pending'        : new device for an existing-active user, awaits admin approval
      - state='conflict_other' : fingerprint is already an active device for a DIFFERENT user
    """
    conn = _get_conn()
    now = _now()

    other = conn.execute(
        "SELECT user_id FROM devices WHERE visitor_id=? AND status='active' AND user_id <> ?",
        (visitor_id, user_id),
    ).fetchone()
    if other:
        return {"state": CLAIM_CONFLICT_OTHER, "device": None}

    my_active = conn.execute(
        "SELECT * FROM devices WHERE user_id=? AND status='active'", (user_id,)
    ).fetchone()

    if my_active and my_active["visitor_id"] == visitor_id:
        conn.execute(
            "UPDATE devices SET last_seen_at=?, user_agent=?, ip=? WHERE id=?",
            (now, user_agent, ip, my_active["id"]),
        )
        conn.commit()
        return {"state": CLAIM_OK_ACTIVE,
                "device": conn.execute("SELECT * FROM devices WHERE id=?", (my_active["id"],)).fetchone()}

    if my_active:
        # User has a DIFFERENT active device — this attempt is a new device → pending
        existing_pending = conn.execute(
            "SELECT * FROM devices WHERE user_id=? AND status='pending'", (user_id,)
        ).fetchone()
        if existing_pending:
            if existing_pending["visitor_id"] == visitor_id:
                conn.execute(
                    "UPDATE devices SET last_seen_at=?, user_agent=?, ip=? WHERE id=?",
                    (now, user_agent, ip, existing_pending["id"]),
                )
            else:
                # User flipped between two new devices; replace pending request
                conn.execute("DELETE FROM devices WHERE id=?", (existing_pending["id"],))
                conn.execute(
                    """INSERT INTO devices (user_id, visitor_id, confidence_score, user_agent, ip,
                                             status, first_seen_at, last_seen_at)
                       VALUES (?,?,?,?,?, 'pending', ?, ?)""",
                    (user_id, visitor_id, confidence, user_agent, ip, now, now),
                )
        else:
            conn.execute(
                """INSERT INTO devices (user_id, visitor_id, confidence_score, user_agent, ip,
                                         status, first_seen_at, last_seen_at)
                   VALUES (?,?,?,?,?, 'pending', ?, ?)""",
                (user_id, visitor_id, confidence, user_agent, ip, now, now),
            )
        conn.commit()
        pending = conn.execute(
            "SELECT * FROM devices WHERE user_id=? AND status='pending'", (user_id,)
        ).fetchone()
        return {"state": CLAIM_PENDING, "device": pending}

    # No active device for this user yet — first device claimed automatically.
    cur = conn.execute(
        """INSERT INTO devices (user_id, visitor_id, confidence_score, user_agent, ip,
                                 status, first_seen_at, last_seen_at, approved_at, approved_by)
           VALUES (?,?,?,?,?, 'active', ?,?,?, 'system:first-device')""",
        (user_id, visitor_id, confidence, user_agent, ip, now, now, now),
    )
    conn.commit()
    return {"state": CLAIM_OK_ACTIVE,
            "device": conn.execute("SELECT * FROM devices WHERE id=?", (cur.lastrowid,)).fetchone()}


def list_pending_devices() -> list:
    """Pending device claims awaiting admin approval, joined with the user."""
    conn = _get_conn()
    return conn.execute(
        """SELECT d.*, u.email AS user_email, u.name AS user_name, u.is_admin AS user_is_admin
             FROM devices d
             JOIN users u ON u.id = d.user_id
            WHERE d.status='pending'
            ORDER BY d.first_seen_at DESC"""
    ).fetchall()


def approve_pending_device(device_id: int, approver_email: str) -> bool:
    """Approve a pending device.

    Side-effects: revokes any existing active device for the same user (one
    active device per user — admin re-approval implies replacing the old one).
    """
    conn = _get_conn()
    now = _now()
    pending = conn.execute(
        "SELECT * FROM devices WHERE id=? AND status='pending'", (device_id,)
    ).fetchone()
    if not pending:
        return False
    conn.execute(
        """UPDATE devices
              SET status='revoked', revoked_at=?, revoked_by=?,
                  revoked_reason='replaced by admin-approved new device'
            WHERE user_id=? AND status='active'""",
        (now, approver_email, pending["user_id"]),
    )
    conn.execute(
        "UPDATE devices SET status='active', approved_at=?, approved_by=? WHERE id=?",
        (now, approver_email, device_id),
    )
    conn.commit()
    log.info("Approved device %s for user %s by %s", device_id, pending["user_id"], approver_email)
    return True


def reject_pending_device(device_id: int, rejector_email: str) -> bool:
    conn = _get_conn()
    now = _now()
    cur = conn.execute(
        """UPDATE devices SET status='revoked', revoked_at=?, revoked_by=?,
                                revoked_reason='admin rejected new device'
            WHERE id=? AND status='pending'""",
        (now, rejector_email, device_id),
    )
    conn.commit()
    return cur.rowcount > 0


def add_admin_by_email(email: str) -> sqlite3.Row:
    """Pre-seed (or promote) an admin by email.

    If the email already exists, sets is_admin=1 and status='approved'.
    If not, inserts a stub row (no google_sub yet) so the user will be
    auto-recognized as admin on their first Google login.
    """
    email = email.lower()
    conn = _get_conn()
    now = _now()
    row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if row:
        conn.execute(
            """UPDATE users
                  SET is_admin=1,
                      status='approved',
                      approved_at=COALESCE(approved_at, ?),
                      approved_by=COALESCE(approved_by, 'system:admin-seed')
                WHERE id=?""",
            (now, row["id"]),
        )
        conn.commit()
        log.info("Promoted existing user %s to admin", email)
        return conn.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()

    cur = conn.execute(
        """INSERT INTO users
           (email, name, status, is_admin, created_at, approved_at, approved_by)
           VALUES (?,?,?,?,?,?,?)""",
        (email, email, "approved", 1, now, now, "system:admin-seed"),
    )
    conn.commit()
    log.info("Seeded new admin %s (id=%s) awaiting first login", email, cur.lastrowid)
    return conn.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()


# ─────────────────────────── local dev login ───────────────────────────

def get_or_create_local_admin(email: str, name: str = "Local Admin") -> sqlite3.Row:
    """Get or create a hardcoded local admin user (no Google OAuth required).

    Used by the /auth/local-login route for local development access.
    The user is given admin status, approved status, and device_verified is
    set on the session by the caller — so ALL app features are accessible.
    """
    email = email.lower()
    conn = _get_conn()
    now = _now()
    row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if row:
        # Ensure the local admin always has admin + approved status
        conn.execute(
            """UPDATE users
                  SET is_admin=1, status='approved', name=?,
                      last_login_at=?
                WHERE id=?""",
            (name, now, row["id"]),
        )
        conn.commit()
        return conn.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()

    cur = conn.execute(
        """INSERT INTO users
           (email, name, status, is_admin, created_at, approved_at, approved_by, last_login_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (email, name, "approved", 1, now, now, "system:local-dev", now),
    )
    conn.commit()
    log.info("Created local dev admin user %s (id=%s)", email, cur.lastrowid)
    return conn.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()

def get_or_create_local_user(email: str, name: str = "Test User") -> sqlite3.Row:
    email = email.lower()
    conn = _get_conn()
    now = _now()
    row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    from datetime import datetime, timezone, timedelta
    future = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat(timespec="seconds")
    if row:
        conn.execute(
            """UPDATE users
                  SET is_admin=0, status='approved', name=?,
                      last_login_at=?, sub_expires_at=?
                WHERE id=?""",
            (name, now, future, row["id"]),
        )
        conn.commit()
        return conn.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()

    cur = conn.execute(
        """INSERT INTO users
           (email, name, status, is_admin, created_at, approved_at, approved_by, last_login_at, sub_expires_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (email, name, "approved", 0, now, now, "system:local-dev", now, future),
    )
    conn.commit()
    log.info("Created local dev test user %s (id=%s)", email, cur.lastrowid)
    return conn.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()

