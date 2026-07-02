"""
Promote (or seed) an admin in auth.db.

Usage:
    python scripts/add_admin.py <email> [<email> ...]

Behavior:
    - If a user with this email already exists: sets is_admin=1, status='approved'.
    - If not: inserts a stub row (no google_sub yet); the user becomes admin
      automatically on their first Google sign-in (upsert_oauth_user matches by email).

This script is the only blessed way to bootstrap the first admin after a fresh
install. Subsequent admin adds/removes happen through the admin UI.
"""

import sys
from pathlib import Path

# Ensure the project root is on sys.path so `import auth_db` resolves when this
# script is run as `python scripts/add_admin.py ...`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

import auth_db


def main(args: list[str]) -> int:
    if not args:
        print(__doc__, file=sys.stderr)
        return 2

    auth_db.init_db()
    for email in args:
        email = email.strip().lower()
        if not email or "@" not in email:
            print(f"  skip: {email!r} (not a valid email)", file=sys.stderr)
            continue
        row = auth_db.add_admin_by_email(email)
        suffix = "(stub — awaits first login)" if not row["google_sub"] else "(linked)"
        print(f"  ok: {email}  id={row['id']}  status={row['status']}  is_admin={row['is_admin']} {suffix}")

    print()
    print("Current admins:")
    for r in auth_db.list_admins():
        linked = "linked" if r["google_sub"] else "not yet signed in"
        print(f"  - {r['email']}  ({linked})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
