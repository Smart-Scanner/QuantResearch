"""
bootstrap.py - PG environment guard for scoring_v1 entry points.
================================================================
IMPORT THIS FIRST (before `db`, `pit_loader`, `adapter`, ...) in every
runnable scoring_v1 entry point (shadow runner, validation harness, any
standalone test/driver script).

WHY THIS EXISTS
---------------
db.py reads DATABASE_URL from os.environ ONCE, at import time
(db.py: `DATABASE_URL = os.environ.get("DATABASE_URL") ...`). A standalone
script that imports `db` WITHOUT first loading the project's .env gets an
empty DATABASE_URL -> is_postgresql() is False -> db silently falls back to
the local SQLite parachute. That is exactly what happened to the build
subagents: they verified against an EMPTY SQLite DB instead of the real
point-in-time PostgreSQL store, so their data-coverage numbers were
meaningless.

Shadow-logging and the walk-forward / equal-vs-tuned validation MUST run on
the real PG point-in-time store or the numbers are worthless. So this module:
  1. Loads the repo-root .env BEFORE db is imported (so DATABASE_URL is set).
  2. Provides require_pg() which HARD-FAILS (raises) if we are not actually on
     a live PostgreSQL connection - it refuses to let analytics run on SQLite.

USAGE
-----
    from quantresearch.scoring_v1 import bootstrap   # must be the FIRST import
    bootstrap.require_pg()                            # raises if not on live PG
    import db                                         # now sees DATABASE_URL
"""

from __future__ import annotations

import os
import logging
from pathlib import Path

log = logging.getLogger("screener")

# Repo root = .../quantresearch/scoring_v1/bootstrap.py -> parents[2]
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_PATH = _REPO_ROOT / ".env"


def _load_env() -> None:
    """Load the repo .env so db.py sees DATABASE_URL (-> PostgreSQL)."""
    try:
        from dotenv import load_dotenv
    except Exception:  # pragma: no cover - dotenv always present in this env
        log.warning("[bootstrap] python-dotenv unavailable; relying on ambient env")
        return
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH)
    else:  # pragma: no cover - defensive
        load_dotenv()  # fall back to default search
    # If db was already imported (without env), refresh its module-level URL so
    # is_postgresql() re-evaluates against the now-loaded DATABASE_URL.
    import sys
    if "db" in sys.modules:
        try:
            sys.modules["db"].DATABASE_URL = (
                os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("[bootstrap] could not refresh db.DATABASE_URL: %s", exc)


# Load env at import time so the mere act of importing bootstrap first fixes the
# environment for any subsequent `import db`.
_load_env()


class NotOnPostgres(RuntimeError):
    """Raised when a scoring_v1 analytics entry point is not on a live PG store."""


def require_pg(probe: bool = True):
    """
    Assert we are on a live PostgreSQL point-in-time store; raise otherwise.

    Call this at the top of every shadow / validation entry point. It refuses
    to let analytics silently run on the SQLite parachute (which is empty and
    would make every number meaningless).

    Args:
        probe: if True, also issue a real `SELECT 1` round-trip (require_pg) so
               we fail when PG is configured but unreachable, not just when the
               URL is missing.

    Returns:
        The DATABASE_URL host:port string (for logging) on success.

    Raises:
        NotOnPostgres: if DATABASE_URL is not a Postgres URL, or (probe) PG is
                       unreachable.
    """
    import db

    if not db.is_postgresql():
        raise NotOnPostgres(
            "scoring_v1 analytics require the real PostgreSQL point-in-time store, "
            "but DATABASE_URL is not a Postgres URL (db is on the SQLite fallback). "
            f"Expected .env at {_ENV_PATH}. Import quantresearch.scoring_v1.bootstrap "
            "FIRST (before db), then run again."
        )

    if probe:
        try:
            row = db.execute_db("SELECT 1 AS ok", fetch="one", require_pg=True)
            if not row or row.get("ok") != 1:
                raise NotOnPostgres(
                    "PostgreSQL probe (SELECT 1) returned no/unexpected result; "
                    "the PG store is not answering. Refusing to run on SQLite."
                )
        except NotOnPostgres:
            raise
        except Exception as exc:
            raise NotOnPostgres(
                f"PostgreSQL is configured but unreachable ({exc}). "
                "Start the quantresearch-pg container / check DATABASE_URL. "
                "Refusing to run analytics on the SQLite fallback."
            ) from exc

    # Mask credentials for the log line.
    url = os.environ.get("DATABASE_URL", "")
    safe = url.split("@")[-1] if "@" in url else url
    log.info("[bootstrap] on live PostgreSQL: %s", safe)
    return safe


if __name__ == "__main__":  # pragma: no cover - manual check
    print("repo root:", _REPO_ROOT)
    print(".env present:", _ENV_PATH.exists())
    try:
        print("require_pg ->", require_pg())
        print("RESULT: OK (live PostgreSQL)")
    except NotOnPostgres as e:
        print("RESULT: FAIL\n", e)
