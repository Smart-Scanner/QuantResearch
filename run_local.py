#!/usr/bin/env python3
"""QuantResearch — local single-user development server.

Runs the Flask app on 127.0.0.1 with:
  * single-user auto-login (no login wall),
  * the bundled local SQLite DB (set DATABASE_URL only if you want Postgres),
  * brokers disabled by default so it boots cleanly offline.

For live data, set PROVIDER_1_* (Angel SmartAPI) and/or FYERS_* in .env and
unset DISABLE_FYERS. See .env.development.example.

Usage:
    python run_local.py            # http://127.0.0.1:5000
    PORT=5050 python run_local.py
"""

import os

# Local-first defaults — only applied if not already set in the environment/.env.
# SINGLE_USER_MODE=0 shows the landing+login page (single-tenant login: admin/admin123).
# Set to 1 to skip the login wall and auto-enter the workspace.
os.environ.setdefault("SINGLE_USER_MODE", "0")
os.environ.setdefault("AUTO_SCAN_ENABLED_DEFAULT", "0")
os.environ.setdefault("DISABLE_FYERS", "1")
os.environ.setdefault("PORT", "5000")

# Import AFTER env defaults so config picks them up at import time.
from app import app  # noqa: E402

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    print(f"\n  QuantResearch (local, single-user)  ->  http://{host}:{port}\n")
    # use_reloader=False: the app starts background threads at import; the
    # reloader would double-import and spawn them twice.
    app.run(debug=False, host=host, port=port, use_reloader=False)
