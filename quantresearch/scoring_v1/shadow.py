"""
shadow.py - Dual-scorer shadow logger (ADDITIVE, no live decision changes).
============================================================================
Logs BOTH scorers' output per as_of_date to a dedicated PostgreSQL table
(`scoring_v1_shadow`) so the v1 engine can be attributed against realised
forward returns WITHOUT touching any live decision / trade behaviour.

WHAT THIS DOES
--------------
1) Ensures the idempotent shadow table exists on PG.
2) run_shadow(as_of_date, modes) — runs the LOCKED v1 engine (via
   adapter.run_scoring) for each WEIGHT_MODE and UPSERTs one row per
   (as_of_date, symbol, engine='v1', mode). It then BEST-EFFORT pulls any
   existing LEGACY analyzer score for that date from the live results tables
   (read-only) and UPSERTs those as engine='legacy'. It NEVER runs analyzer.py
   and NEVER mutates any live table.
3) backfill_forward_returns(as_of_date) — fills fwd_5d/10d/20d for rows whose
   forward window has fully realised in the store (close[as_of+N]/close[as_of]-1
   on TRADING days), point-in-time-safe for the *entry* (we only read prices on
   or after the entry to realise the outcome — that is the backtest outcome,
   which is allowed).
4) CLI: `python -m quantresearch.scoring_v1.shadow --date YYYY-MM-DD`.

ROLLBACK-SAFETY
---------------
Pure additive analytics. Writes ONLY to the new `scoring_v1_shadow` table.
Reads from daily_bars (forward returns) and the legacy results tables
(read-only). It imports the LOCKED engine via the adapter and never modifies
engine.py / adapter.py / gates.py / analyzer.py. The config flag
SCORING_V1_SHADOW_ENABLED gates whether a caller chooses to invoke it; this
module itself takes no action on import.

PG MANDATE
----------
Import quantresearch.scoring_v1.bootstrap FIRST so db.py sees DATABASE_URL and
binds to the live PostgreSQL point-in-time store; require_pg() HARD-FAILS on the
SQLite fallback. All analytics here are meaningless on an empty SQLite DB.
"""

from __future__ import annotations

import logging
from datetime import datetime, date

log = logging.getLogger("screener")

# ─────────────────────── defensive foundation imports ───────────────────────
try:  # pragma: no cover - import plumbing
    from . import adapter, pit_loader
    from . import engine as _engine
except Exception:  # pragma: no cover - fallback path
    import adapter        # type: ignore
    import pit_loader     # type: ignore
    import engine as _engine  # type: ignore


SHADOW_TABLE = "scoring_v1_shadow"

# Factor-contribution columns produced by the engine (engine contract).
_FACTOR_COLS = (
    "c_momentum",
    "c_trend",
    "c_smart_money",
    "c_sector_rs",
    "c_earnings",
    "c_risk",
)

# Forward-return horizons (TRADING days).
_FWD_HORIZONS = (5, 10, 20)


# ───────────────────────── helpers ─────────────────────────

def _import_db():
    """Lazy import of db (after bootstrap has loaded .env)."""
    import db
    return db


def _as_iso(as_of_date) -> str:
    """Normalize date/datetime/'YYYY-MM-DD' -> ISO 'YYYY-MM-DD' string."""
    if as_of_date is None:
        raise ValueError("as_of_date is required (point-in-time mandate)")
    if isinstance(as_of_date, datetime):
        return as_of_date.date().strftime("%Y-%m-%d")
    if isinstance(as_of_date, date):
        return as_of_date.strftime("%Y-%m-%d")
    s = str(as_of_date).strip()
    if "T" in s:
        s = s.split("T", 1)[0]
    if " " in s:
        s = s.split(" ", 1)[0]
    datetime.strptime(s, "%Y-%m-%d")  # validate shape
    return s


def _to_float(v):
    """Coerce a value to float or None (never NaN/inf into the store)."""
    try:
        import math
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _to_int(v):
    """Coerce to int or None."""
    try:
        if v is None:
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_text(v):
    """Coerce to a plain string or None (data_integrity / signal_agreement)."""
    if v is None:
        return None
    return str(v)


# ───────────────────────── table creation ─────────────────────────

def ensure_shadow_table() -> None:
    """
    Idempotently create the `scoring_v1_shadow` table on PG.

    PRIMARY KEY (as_of_date, symbol, engine, mode) so UPSERTs are stable across
    re-runs of the same date. created_at defaults to now().
    """
    db = _import_db()
    db.execute_db(
        f"""
        CREATE TABLE IF NOT EXISTS {SHADOW_TABLE} (
            as_of_date       DATE     NOT NULL,
            symbol           TEXT     NOT NULL,
            engine           TEXT     NOT NULL,
            mode             TEXT     NOT NULL DEFAULT '',
            score            REAL,
            rank             INT,
            composite_z      REAL,
            c_momentum       REAL,
            c_trend          REAL,
            c_smart_money    REAL,
            c_sector_rs      REAL,
            c_earnings       REAL,
            c_risk           REAL,
            data_integrity   TEXT,
            signal_agreement TEXT,
            fwd_5d           REAL,
            fwd_10d          REAL,
            fwd_20d          REAL,
            created_at       TIMESTAMP DEFAULT now(),
            PRIMARY KEY (as_of_date, symbol, engine, mode)
        )
        """,
        require_pg=True,
    )
    db.execute_db(
        f"CREATE INDEX IF NOT EXISTS idx_{SHADOW_TABLE}_date "
        f"ON {SHADOW_TABLE}(as_of_date)",
        require_pg=True,
    )


# ───────────────────────── v1 upsert ─────────────────────────

def _upsert_row(db, as_of_iso: str, symbol: str, engine: str, mode: str, vals: dict) -> None:
    """UPSERT a single shadow row (preserves any already-realised fwd returns)."""
    db.execute_db(
        f"""
        INSERT INTO {SHADOW_TABLE}
            (as_of_date, symbol, engine, mode, score, rank, composite_z,
             c_momentum, c_trend, c_smart_money, c_sector_rs, c_earnings, c_risk,
             data_integrity, signal_agreement)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (as_of_date, symbol, engine, mode) DO UPDATE SET
            score            = EXCLUDED.score,
            rank             = EXCLUDED.rank,
            composite_z      = EXCLUDED.composite_z,
            c_momentum       = EXCLUDED.c_momentum,
            c_trend          = EXCLUDED.c_trend,
            c_smart_money    = EXCLUDED.c_smart_money,
            c_sector_rs      = EXCLUDED.c_sector_rs,
            c_earnings       = EXCLUDED.c_earnings,
            c_risk           = EXCLUDED.c_risk,
            data_integrity   = EXCLUDED.data_integrity,
            signal_agreement = EXCLUDED.signal_agreement
        """,
        (
            as_of_iso, symbol, engine, mode,
            vals.get("score"), vals.get("rank"), vals.get("composite_z"),
            vals.get("c_momentum"), vals.get("c_trend"), vals.get("c_smart_money"),
            vals.get("c_sector_rs"), vals.get("c_earnings"), vals.get("c_risk"),
            vals.get("data_integrity"), vals.get("signal_agreement"),
        ),
        require_pg=True,
    )


def _log_v1_mode(db, as_of_iso: str, mode: str, symbols=None, prebuilt=None) -> int:
    """
    Score the v1 engine for one WEIGHT_MODE and UPSERT all rows. Returns row count.

    If `prebuilt` = (price_data, benchmark, sector_idx, earnings) is supplied, the
    engine is scored directly from those SHARED inputs, so run_shadow can build the
    expensive point-in-time inputs ONCE and score every mode from them. Otherwise it
    falls back to adapter.run_scoring (which rebuilds inputs per call).
    """
    if prebuilt is not None:
        price_data, benchmark, sector_idx, earnings = prebuilt
        ranked = _engine.score_universe(
            price_data, benchmark=benchmark, sector_idx=sector_idx,
            earnings=earnings, mode=mode,
        )
    else:
        ranked = adapter.run_scoring(as_of_iso, mode=mode, symbols=symbols)
    if ranked is None or len(ranked) == 0:
        log.warning("[shadow] v1 mode=%s as_of=%s produced 0 rows", mode, as_of_iso)
        return 0

    n = 0
    for symbol, row in ranked.iterrows():
        vals = {
            "score": _to_float(row.get("score")),
            "rank": _to_int(row.get("rank")),
            "composite_z": _to_float(row.get("composite_z")),
            "data_integrity": _to_text(row.get("data_integrity")),
            "signal_agreement": _to_text(row.get("signal_agreement")),
        }
        for col in _FACTOR_COLS:
            vals[col] = _to_float(row.get(col))
        _upsert_row(db, as_of_iso, str(symbol), "v1", mode, vals)
        n += 1
    log.info("[shadow] v1 mode=%s as_of=%s -> %d rows", mode, as_of_iso, n)
    return n


# ───────────────────────── legacy (best-effort, read-only) ─────────────────────────

def _fetch_legacy_scores(db, as_of_iso: str):
    """
    BEST-EFFORT read of the existing legacy analyzer score per symbol for the
    given date from the live results tables. READ-ONLY; never runs analyzer.py.

    Tries `score_history(symbol, score, scan_date)` first (point-in-time keyed),
    then falls back to `final_scores(symbol, final_score, scan_date)`. Returns a
    list of {symbol, score} dicts (possibly empty). Any error -> empty list
    (legacy rows are simply skipped, per spec).
    """
    # 1) score_history — the natural per-date legacy score table.
    try:
        rows = db.execute_db(
            "SELECT symbol, score FROM score_history WHERE scan_date = ?",
            (as_of_iso,),
            fetch="all",
            require_pg=True,
        )
        if rows:
            return [{"symbol": r["symbol"], "score": r["score"]} for r in rows if r.get("symbol")]
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("[shadow] legacy score_history read failed for %s: %s", as_of_iso, exc)

    # 2) final_scores — fallback (final_score column).
    try:
        rows = db.execute_db(
            "SELECT symbol, final_score AS score FROM final_scores WHERE scan_date = ?",
            (as_of_iso,),
            fetch="all",
            require_pg=True,
        )
        if rows:
            return [{"symbol": r["symbol"], "score": r["score"]} for r in rows if r.get("symbol")]
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("[shadow] legacy final_scores read failed for %s: %s", as_of_iso, exc)

    return []


def _log_legacy(db, as_of_iso: str) -> int:
    """
    Best-effort UPSERT of legacy scores for the date (engine='legacy', mode='').
    Ranks legacy symbols by score desc so the row carries a comparable rank.
    Returns the number of legacy rows written (0 if none available).
    """
    legacy = _fetch_legacy_scores(db, as_of_iso)
    if not legacy:
        log.info("[shadow] no legacy scores available for %s — skipping legacy rows", as_of_iso)
        return 0

    # Derive a rank by score desc (None scores sort last).
    def _key(d):
        s = _to_float(d.get("score"))
        return (s is None, -(s or 0.0))

    legacy_sorted = sorted(legacy, key=_key)

    n = 0
    for i, d in enumerate(legacy_sorted, start=1):
        vals = {
            "score": _to_float(d.get("score")),
            "rank": i,
            "composite_z": None,
            "data_integrity": None,
            "signal_agreement": None,
        }
        for col in _FACTOR_COLS:
            vals[col] = None
        _upsert_row(db, as_of_iso, str(d["symbol"]), "legacy", "", vals)
        n += 1
    log.info("[shadow] legacy as_of=%s -> %d rows", as_of_iso, n)
    return n


# ───────────────────────── public: run_shadow ─────────────────────────

def run_shadow(as_of_date, modes=("tuned", "equal")) -> dict:
    """
    Log BOTH scorers for `as_of_date` to the shadow table.

    For each mode in `modes`: run adapter.run_scoring and UPSERT engine='v1'
    rows. Then best-effort UPSERT engine='legacy' rows from the live results
    tables (read-only; skipped if unavailable — analyzer.py is NEVER run).

    Returns a dict of row counts: {'v1': {mode: n, ...}, 'legacy': n, 'as_of_date': iso}.
    """
    db = _import_db()
    ensure_shadow_table()
    as_of_iso = _as_iso(as_of_date)

    out = {"as_of_date": as_of_iso, "v1": {}, "legacy": 0}

    # Build the expensive point-in-time engine inputs ONCE (gates + hundreds of
    # price-df loads + earnings) and score every WEIGHT_MODE from the SAME inputs:
    # tuned and equal differ only in weights, so rebuilding per mode is pure waste.
    prebuilt = None
    try:
        prebuilt = adapter.build_engine_inputs(as_of_iso)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("[shadow] build_engine_inputs failed for %s: %s", as_of_iso, exc)

    use_prebuilt = prebuilt is not None and bool(prebuilt[0])  # prebuilt[0] = price_data
    for mode in modes:
        if use_prebuilt:
            out["v1"][mode] = _log_v1_mode(db, as_of_iso, mode, prebuilt=prebuilt)
        else:
            out["v1"][mode] = _log_v1_mode(db, as_of_iso, mode)
    out["legacy"] = _log_legacy(db, as_of_iso)
    return out


# ───────────────────────── forward-return backfill ─────────────────────────

def _latest_store_date(db) -> str | None:
    """Latest available date in daily_bars (ISO str) or None."""
    row = db.execute_db("SELECT MAX(date) AS d FROM daily_bars", fetch="one", require_pg=True)
    return row["d"] if row and row.get("d") else None


def _forward_close_pct(symbol: str, as_of_iso: str, n: int, latest_iso: str):
    """
    Realised n-TRADING-day forward return for `symbol` from `as_of_iso`:
        close[as_of + n trading bars] / close[as_of] - 1

    Uses pit_loader.load_price_df with as_of = latest store date so the forward
    bars are visible, then locates the entry bar and the +n bar by position.
    Returns a float, or None if the forward window has not fully realised
    (fewer than n bars exist after the entry) or the entry bar is missing.
    """
    df = pit_loader.load_price_df(symbol, latest_iso)
    if df is None or len(df) == 0:
        return None

    import pandas as pd
    try:
        entry_ts = pd.Timestamp(as_of_iso)
    except Exception:
        return None

    # Entry index position: last bar with date <= as_of (entry trades on as_of's
    # close; if as_of itself is a holiday we anchor on the prior trading bar).
    idx = df.index
    le = idx[idx <= entry_ts]
    if len(le) == 0:
        return None
    entry_pos = idx.get_loc(le[-1])
    fwd_pos = entry_pos + n
    if fwd_pos >= len(df):
        return None  # forward window not fully realised yet

    entry_close = _to_float(df["close"].iloc[entry_pos])
    fwd_close = _to_float(df["close"].iloc[fwd_pos])
    if entry_close is None or fwd_close is None or entry_close == 0:
        return None
    return fwd_close / entry_close - 1.0


def backfill_forward_returns(as_of_date=None) -> dict:
    """
    Fill NULL fwd_5d/10d/20d for shadow rows whose forward window has realised.

    For each shadow row (optionally restricted to `as_of_date`) with a NULL
    fwd_Nd, compute the realised n-trading-day forward return from daily_bars
    (only if at least n bars exist after the entry in the store) and UPDATE it.

    Forward returns are a per-(as_of_date, symbol) property, so we compute once
    per symbol/date and apply the value to ALL engine/mode rows of that key.

    Returns {'updated': {5: x, 10: y, 20: z}, 'rows_scanned': k}.
    """
    db = _import_db()
    ensure_shadow_table()

    latest_iso = _latest_store_date(db)
    if not latest_iso:
        log.warning("[shadow] backfill: daily_bars empty — nothing to do")
        return {"updated": {5: 0, 10: 0, 20: 0}, "rows_scanned": 0}

    # Distinct (as_of_date, symbol) pairs that still have at least one NULL fwd.
    where = ""
    params: tuple = ()
    if as_of_date is not None:
        where = "WHERE as_of_date = ?"
        params = (_as_iso(as_of_date),)

    pairs = db.execute_db(
        f"""
        SELECT DISTINCT CAST(as_of_date AS TEXT) AS as_of_date, symbol
          FROM {SHADOW_TABLE}
          {('  '.join([where, 'AND']) if where else 'WHERE')}
               (fwd_5d IS NULL OR fwd_10d IS NULL OR fwd_20d IS NULL)
        """,
        params,
        fetch="all",
        require_pg=True,
    ) or []

    updated = {5: 0, 10: 0, 20: 0}
    for p in pairs:
        as_of_iso = _as_iso(p["as_of_date"])
        symbol = p["symbol"]
        for n in _FWD_HORIZONS:
            col = f"fwd_{n}d"
            ret = _forward_close_pct(symbol, as_of_iso, n, latest_iso)
            if ret is None:
                continue  # not yet realised — leave NULL for a later backfill
            res = db.execute_db(
                f"""
                UPDATE {SHADOW_TABLE}
                   SET {col} = ?
                 WHERE as_of_date = ? AND symbol = ? AND {col} IS NULL
                """,
                (ret, as_of_iso, symbol),
                fetch="rowcount",
                require_pg=True,
            )
            try:
                updated[n] += int(res or 0)
            except (TypeError, ValueError):
                pass

    log.info(
        "[shadow] backfill done: pairs=%d updated 5d=%d 10d=%d 20d=%d",
        len(pairs), updated[5], updated[10], updated[20],
    )
    return {"updated": updated, "rows_scanned": len(pairs)}


# ───────────────────────── CLI ─────────────────────────

def _main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Run dual-scorer shadow logging + forward-return backfill (PG only)."
    )
    parser.add_argument(
        "--date", required=True,
        help="as_of_date (YYYY-MM-DD) to score & log in shadow.",
    )
    parser.add_argument(
        "--modes", default="tuned,equal",
        help="comma-separated weight modes to log (default: tuned,equal).",
    )
    parser.add_argument(
        "--no-backfill", action="store_true",
        help="skip the forward-return backfill step.",
    )
    args = parser.parse_args(argv)

    modes = tuple(m.strip() for m in args.modes.split(",") if m.strip())

    res = run_shadow(args.date, modes=modes)
    print("run_shadow:", res)

    if not args.no_backfill:
        bf = backfill_forward_returns()  # backfill across ALL eligible rows
        print("backfill_forward_returns:", bf)

    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    import sys, os
    sys.path.insert(0, r"d:\Gulshan\QuantResearch")
    from quantresearch.scoring_v1 import bootstrap  # FIRST import - loads .env
    bootstrap.require_pg()                           # raises if not on live PG
    raise SystemExit(_main(sys.argv[1:]))
