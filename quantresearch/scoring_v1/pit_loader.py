"""
pit_loader.py - Point-in-time data loaders (broker-free)
========================================================
The foundation the scoring adapter sits on. Reads exclusively from the
broker-free bhavcopy store (daily_bars / index_bars, populated by
bhavcopy_history.py) and shapes the data EXACTLY as the locked scoring
engine (quantresearch/scoring_v1/engine.py) expects.

POINT-IN-TIME MANDATE
---------------------
Every query filters `date <= as_of_date`. There is NO look-ahead: a loader
called with an as_of_date in the past returns only the bars that existed up
to and including that date. Any field that is missing/unavailable in the
store is surfaced as None (never fabricated).

ENGINE CONTRACT (what these loaders produce)
--------------------------------------------
- load_price_df(symbol, as_of_date) -> pandas.DataFrame
    lowercase cols: open, high, low, close, volume, delivery_pct
    DatetimeIndex, ASCENDING by date. This IS engine price_data[symbol].
- load_index_series(index_name, as_of_date) -> pandas.Series
    of close, DatetimeIndex ASCENDING.
- load_benchmark(as_of_date) -> load_index_series("Nifty 500", ...).
- list_symbols_with_history(as_of_date, min_bars=126) -> list[str].

ROLLBACK-SAFETY
---------------
ADDITIVE module. It only READS from db.execute_db. It never writes, and it
imports no live scoring/analyzer code. Source tables (daily_bars,
index_bars) are owned by bhavcopy_history.py.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

log = logging.getLogger("screener")

# Approved broad benchmark for the engine (NSE).
BENCHMARK_INDEX = "Nifty 500"

# Default eligibility floor mirrors engine.MIN_HISTORY_DAYS (126 bars).
DEFAULT_MIN_BARS = 126

# How far back to pull so EMA200 / 252-bar windows are well-fed. The engine
# uses min(252, len) windows, but EMA(200) wants a long warm-up; 400 calendar
# days (~270 trading days) gives a comfortable margin.
DEFAULT_LOOKBACK_DAYS = 400


# ───────────────────────── helpers ─────────────────────────

def _as_iso(as_of_date) -> str:
    """
    Normalize a date / datetime / 'YYYY-MM-DD' str to an ISO 'YYYY-MM-DD'
    string. The store keeps `date` as TEXT in ISO form, so string comparison
    (date <= as_of_date) is correct and index-friendly.
    """
    if as_of_date is None:
        raise ValueError("as_of_date is required (point-in-time mandate)")
    if isinstance(as_of_date, datetime):
        return as_of_date.date().strftime("%Y-%m-%d")
    if isinstance(as_of_date, date):
        return as_of_date.strftime("%Y-%m-%d")
    s = str(as_of_date).strip()
    # Accept already-ISO strings; tolerate a trailing time component.
    if "T" in s:
        s = s.split("T", 1)[0]
    if " " in s:
        s = s.split(" ", 1)[0]
    # Validate shape (raises if malformed) but keep the original ISO text.
    datetime.strptime(s, "%Y-%m-%d")
    return s


def _import_db():
    """Lazy import of db so the module loads even if db is unavailable."""
    import db
    return db


# ───────────────────────── price (equity) loader ─────────────────────────

def load_price_df(symbol: str, as_of_date, lookback_days: int = DEFAULT_LOOKBACK_DAYS):
    """
    Point-in-time daily OHLCV + delivery_pct for `symbol`, as of `as_of_date`.

    Returns a pandas.DataFrame shaped EXACTLY as engine price_data[symbol]:
        columns : open, high, low, close, volume, delivery_pct  (lowercase)
        index   : DatetimeIndex, ASCENDING by date
    Missing delivery_pct (or any OHLCV) is surfaced as NaN/None per-row.

    Returns None if pandas/db are unavailable or there is no data for the
    symbol up to as_of_date.
    """
    try:
        import pandas as pd
        db = _import_db()
    except Exception as exc:  # pragma: no cover - defensive
        log.error("[pit_loader] load_price_df import failed: %s", exc)
        return None

    try:
        end_iso = _as_iso(as_of_date)
    except Exception as exc:
        log.warning("[pit_loader] load_price_df bad as_of_date %r: %s", as_of_date, exc)
        return None

    # Lower bound on the lookback window (calendar days) to bound the scan.
    start_iso = _window_start(end_iso, lookback_days)

    try:
        rows = db.execute_db(
            """SELECT date, open, high, low, close, volume, delivery_pct
                 FROM daily_bars
                WHERE symbol = ? AND date <= ? AND date >= ?
                ORDER BY date ASC""",
            (symbol, end_iso, start_iso),
            fetch="all",
        )
    except Exception as exc:
        log.warning("[pit_loader] load_price_df query failed for %s: %s", symbol, exc)
        return None

    if not rows:
        return None

    df = pd.DataFrame(
        [
            {
                "open": r["open"],
                "high": r["high"],
                "low": r["low"],
                "close": r["close"],
                "volume": r["volume"],
                "delivery_pct": r["delivery_pct"],
            }
            for r in rows
        ],
        index=pd.to_datetime([r["date"] for r in rows]),
    )
    if df.empty:
        return None

    # Engine math is numeric; coerce so a stray text value -> NaN (neutral),
    # never a fabricated number.
    for col in ("open", "high", "low", "close", "volume", "delivery_pct"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.sort_index()  # guarantee ASCENDING even if DB ordering surprises us
    df.index.name = "date"
    return df


def load_price_df_bulk(symbols, as_of_date, lookback_days: int = DEFAULT_LOOKBACK_DAYS):
    """Bulk equivalent of load_price_df: ONE query for many symbols -> {symbol: DataFrame}.

    Each DataFrame is shaped IDENTICALLY to load_price_df (same columns, same numeric
    coercion, same ASCENDING DatetimeIndex), so engine inputs are byte-identical to the
    per-symbol path — only the I/O is batched (897 round-trips -> a few).
    """
    try:
        import pandas as pd
        db = _import_db()
    except Exception as exc:  # pragma: no cover
        log.error("[pit_loader] load_price_df_bulk import failed: %s", exc)
        return {}
    try:
        end_iso = _as_iso(as_of_date)
    except Exception as exc:
        log.warning("[pit_loader] load_price_df_bulk bad as_of_date %r: %s", as_of_date, exc)
        return {}
    start_iso = _window_start(end_iso, lookback_days)
    syms = [s for s in (symbols or []) if s]
    if not syms:
        return {}

    from collections import defaultdict
    by_sym: dict = defaultdict(list)
    CHUNK = 800  # keep the IN-list parameter count sane
    for i in range(0, len(syms), CHUNK):
        chunk = syms[i:i + CHUNK]
        ph = ",".join(["?"] * len(chunk))
        try:
            rows = db.execute_db(
                f"""SELECT symbol, date, open, high, low, close, volume, delivery_pct
                      FROM daily_bars
                     WHERE symbol IN ({ph}) AND date <= ? AND date >= ?
                  ORDER BY symbol ASC, date ASC""",
                tuple(chunk) + (end_iso, start_iso), fetch="all",
            ) or []
        except Exception as exc:
            log.warning("[pit_loader] load_price_df_bulk query failed: %s", exc)
            rows = []
        for r in rows:
            by_sym[r["symbol"]].append(r)

    out: dict = {}
    for sym, rs in by_sym.items():
        df = pd.DataFrame(
            [{"open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
              "volume": r["volume"], "delivery_pct": r["delivery_pct"]} for r in rs],
            index=pd.to_datetime([r["date"] for r in rs]),
        )
        if df.empty:
            continue
        for col in ("open", "high", "low", "close", "volume", "delivery_pct"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.sort_index()
        df.index.name = "date"
        out[sym] = df
    return out


# ───────────────────────── index loader ─────────────────────────

def load_index_series(index_name: str, as_of_date, lookback_days: int = DEFAULT_LOOKBACK_DAYS):
    """
    Point-in-time close series for an index, as of `as_of_date`.

    Returns a pandas.Series of close (float), DatetimeIndex ASCENDING.
    Returns None if pandas/db unavailable or no data up to as_of_date.
    """
    try:
        import pandas as pd
        db = _import_db()
    except Exception as exc:  # pragma: no cover - defensive
        log.error("[pit_loader] load_index_series import failed: %s", exc)
        return None

    try:
        end_iso = _as_iso(as_of_date)
    except Exception as exc:
        log.warning("[pit_loader] load_index_series bad as_of_date %r: %s", as_of_date, exc)
        return None

    start_iso = _window_start(end_iso, lookback_days)

    try:
        rows = db.execute_db(
            """SELECT date, close
                 FROM index_bars
                WHERE index_name = ? AND date <= ? AND date >= ?
                ORDER BY date ASC""",
            (index_name, end_iso, start_iso),
            fetch="all",
        )
    except Exception as exc:
        log.warning("[pit_loader] load_index_series query failed for %s: %s", index_name, exc)
        return None

    if not rows:
        return None

    s = pd.Series(
        pd.to_numeric([r["close"] for r in rows], errors="coerce"),
        index=pd.to_datetime([r["date"] for r in rows]),
        name=index_name,
    )
    if s.empty:
        return None

    s = s.sort_index()
    s.index.name = "date"
    return s


def load_benchmark(as_of_date, lookback_days: int = DEFAULT_LOOKBACK_DAYS):
    """
    Point-in-time broad-benchmark close series (engine `benchmark` argument).
    APPROVED benchmark = NSE "Nifty 500".
    """
    return load_index_series(BENCHMARK_INDEX, as_of_date, lookback_days=lookback_days)


# ───────────────────────── universe enumeration ─────────────────────────

def list_symbols_with_history(as_of_date, min_bars: int = DEFAULT_MIN_BARS):
    """
    Symbols having at least `min_bars` daily bars up to `as_of_date`.

    Point-in-time: counts only bars with date <= as_of_date, so a symbol that
    only later accrued enough history is correctly excluded for past dates.

    Returns a sorted list[str]; empty list on error / no data.
    """
    try:
        db = _import_db()
    except Exception as exc:  # pragma: no cover - defensive
        log.error("[pit_loader] list_symbols_with_history import failed: %s", exc)
        return []

    try:
        end_iso = _as_iso(as_of_date)
    except Exception as exc:
        log.warning("[pit_loader] list_symbols_with_history bad as_of_date %r: %s", as_of_date, exc)
        return []

    try:
        rows = db.execute_db(
            """SELECT symbol, COUNT(*) AS bars
                 FROM daily_bars
                WHERE date <= ?
             GROUP BY symbol
               HAVING COUNT(*) >= ?
             ORDER BY symbol ASC""",
            (end_iso, int(min_bars)),
            fetch="all",
        )
    except Exception as exc:
        log.warning("[pit_loader] list_symbols_with_history query failed: %s", exc)
        return []

    if not rows:
        return []

    return [r["symbol"] for r in rows if r.get("symbol")]


# ───────────────────────── internal: window start ─────────────────────────

def _window_start(end_iso: str, lookback_days: int) -> str:
    """
    ISO date `lookback_days` calendar days before end_iso. Used as a lower
    bound on the scan window so the query stays indexed and bounded. We pass
    a generous lookback (default 400 days) so the >=126 trading-bar engine
    floor and EMA200 warm-up are always satisfied.
    """
    from datetime import timedelta
    end_dt = datetime.strptime(end_iso, "%Y-%m-%d").date()
    start_dt = end_dt - timedelta(days=max(1, int(lookback_days)))
    return start_dt.strftime("%Y-%m-%d")
