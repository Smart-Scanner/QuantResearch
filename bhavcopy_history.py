"""
bhavcopy_history.py — Broker-free EOD daily-bar history store
=============================================================
A FREE, broker-independent store of End-Of-Day (EOD) daily OHLCV bars +
delivery % for equities, and OHLC for indices, sourced directly from the
NSE archives (sec_bhavdata_full + ind_close_all). No Angel / broker
dependency, no per-symbol rate limit — one CSV file per trading day.

ROLLBACK-SAFETY
---------------
This module is ADDITIVE. Nothing here changes the existing Angel/jugaad
candle path, scoring math, throttle, or FinBERT prewarm. The new behavior
is gated behind the module-level USE_BHAVCOPY_HISTORY flag (default OFF).
When OFF, callers must keep using the existing path; this store is only a
source of candle/index/delivery data when the flag is ON and the store hits.

Reuses the NSE session/cookie + header pattern from nse_bhavcopy.py
(_get_nse_headers, homepage hit to seed cookies, then file download).

MODULE CONTRACT (all integration must use exactly these):
  - USE_BHAVCOPY_HISTORY: bool
  - get_history(symbol, days=365)        -> pandas.DataFrame | None
        columns: DATE, OPEN, HIGH, LOW, CLOSE, VOLUME, "DELIVERY %"  (newest-last)
  - get_index_history(index, days=365)   -> pandas.DataFrame | None
        columns: DATE, OPEN, HIGH, LOW, CLOSE  (newest-last)
  - has_history(symbol, min_rows=50)     -> bool
  - backfill(start_date, end_date, workers=6) -> dict {days, eq_rows, idx_rows}
  - append_latest()                      -> dict

DB tables (created lazily by init_history_tables()):
  daily_bars(symbol, date, open, high, low, close, volume, turnover,
             delivery_pct, PK(symbol, date))
  index_bars(index_name, date, open, high, low, close, PK(index_name, date))
"""

import io
import csv
import os
import time
import logging
import threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger("screener")

# ─── Feature flag (DEFAULT OFF — byte-identical to today when OFF) ────────────
USE_BHAVCOPY_HISTORY: bool = os.getenv("USE_BHAVCOPY_HISTORY", "0") == "1"

# NSE archive URLs (broker-free)
_SEC_BHAVDATA_URL = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv"
_IND_CLOSE_URL = "https://nsearchives.nseindia.com/content/indices/ind_close_all_{ddmmyyyy}.csv"


# ─── NSE session/cookie + header pattern (reused from nse_bhavcopy.py) ────────

def _get_nse_headers() -> dict:
    """NSE requires browser-like headers (mirror of nse_bhavcopy._get_nse_headers)."""
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.nseindia.com/",
    }


def _make_nse_session():
    """Create a requests.Session pre-seeded with NSE cookies (homepage hit)."""
    import requests
    session = requests.Session()
    try:
        session.get("https://www.nseindia.com/", headers=_get_nse_headers(), timeout=10)
        time.sleep(1)  # NSE needs a small delay after cookie fetch
    except Exception:
        pass
    return session


def _safe_float(val):
    """Convert NSE CSV value to float; '-'/''/None -> None."""
    if val is None:
        return None
    s = str(val).strip()
    if s == "" or s == "-":
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _safe_int(val):
    """Convert NSE CSV value to int; '-'/''/None -> 0."""
    f = _safe_float(val)
    if f is None:
        return 0
    try:
        return int(f)
    except (ValueError, TypeError):
        return 0


# ─── (1) Security-wise full bhavcopy ──────────────────────────────────────────

def fetch_sec_bhavdata_full(dt, session=None) -> list:
    """
    Download NSE security-wise full bhavcopy CSV for date `dt`.

    URL: https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv
    NOTE: the CSV has HEADERS WITH LEADING SPACES — every header is stripped.

    Keeps SERIES == "EQ". Returns list of dicts:
      {symbol, date(YYYY-MM-DD), open, high, low, close,
       volume(int TTL_TRD_QNTY), turnover(float TURNOVER_LACS),
       delivery_pct(float DELIV_PER or None)}

    Returns [] on holiday / 404 / parse failure (tolerant).
    """
    if isinstance(dt, datetime):
        dt = dt.date()
    own_session = False
    if session is None:
        session = _make_nse_session()
        own_session = True

    url = _SEC_BHAVDATA_URL.format(ddmmyyyy=dt.strftime("%d%m%Y"))
    date_iso = dt.strftime("%Y-%m-%d")

    try:
        resp = session.get(url, headers=_get_nse_headers(), timeout=30)
        if resp.status_code != 200:
            log.debug("[BhavHist] sec_bhavdata %s -> %d", date_iso, resp.status_code)
            return []

        reader = csv.DictReader(io.StringIO(resp.text))
        records = []
        for raw in reader:
            # Strip leading spaces from EVERY header (NSE quirk)
            row = {(k.strip() if k else k): v for k, v in raw.items()}

            series = (row.get("SERIES") or "").strip()
            if series != "EQ":
                continue

            symbol = (row.get("SYMBOL") or "").strip()
            if not symbol:
                continue

            try:
                records.append({
                    "symbol": symbol,
                    "date": date_iso,
                    "open": _safe_float(row.get("OPEN_PRICE")),
                    "high": _safe_float(row.get("HIGH_PRICE")),
                    "low": _safe_float(row.get("LOW_PRICE")),
                    "close": _safe_float(row.get("CLOSE_PRICE")),
                    "volume": _safe_int(row.get("TTL_TRD_QNTY")),
                    "turnover": _safe_float(row.get("TURNOVER_LACS")),
                    "delivery_pct": _safe_float(row.get("DELIV_PER")),
                })
            except (ValueError, TypeError):
                continue

        if records:
            log.info("[BhavHist] sec_bhavdata %s: %d EQ rows", date_iso, len(records))
        return records

    except Exception as exc:
        log.warning("[BhavHist] sec_bhavdata fetch failed for %s: %s", date_iso, exc)
        return []
    finally:
        if own_session:
            try:
                session.close()
            except Exception:
                pass


# ─── (2) Index close-all ──────────────────────────────────────────────────────

def fetch_ind_close_all(dt, session=None) -> list:
    """
    Download NSE ind_close_all CSV for date `dt`.

    URL: https://nsearchives.nseindia.com/content/indices/ind_close_all_DDMMYYYY.csv
    Columns: "Index Name, Index Date, Open Index Value, High Index Value,
              Low Index Value, Closing Index Value, ..."

    Returns list of dicts: {index, date(YYYY-MM-DD), open, high, low, close}.
    Returns [] on holiday / 404 / parse failure (tolerant).
    """
    if isinstance(dt, datetime):
        dt = dt.date()
    own_session = False
    if session is None:
        session = _make_nse_session()
        own_session = True

    url = _IND_CLOSE_URL.format(ddmmyyyy=dt.strftime("%d%m%Y"))
    date_iso = dt.strftime("%Y-%m-%d")

    try:
        resp = session.get(url, headers=_get_nse_headers(), timeout=30)
        if resp.status_code != 200:
            log.debug("[BhavHist] ind_close_all %s -> %d", date_iso, resp.status_code)
            return []

        reader = csv.DictReader(io.StringIO(resp.text))
        records = []
        for raw in reader:
            row = {(k.strip() if k else k): v for k, v in raw.items()}

            name = (row.get("Index Name") or "").strip()
            if not name:
                continue

            try:
                records.append({
                    "index": name,
                    "date": date_iso,
                    "open": _safe_float(row.get("Open Index Value")),
                    "high": _safe_float(row.get("High Index Value")),
                    "low": _safe_float(row.get("Low Index Value")),
                    "close": _safe_float(row.get("Closing Index Value")),
                })
            except (ValueError, TypeError):
                continue

        if records:
            log.info("[BhavHist] ind_close_all %s: %d indices", date_iso, len(records))
        return records

    except Exception as exc:
        log.warning("[BhavHist] ind_close_all fetch failed for %s: %s", date_iso, exc)
        return []
    finally:
        if own_session:
            try:
                session.close()
            except Exception:
                pass


# ─── (3) DB schema — lazy init ────────────────────────────────────────────────

_tables_initialized = False
_tables_lock = threading.Lock()


def init_history_tables():
    """
    Create daily_bars + index_bars (idempotent). Mirrors db.py's
    is_postgresql() branching. Safe to call repeatedly; runs once per process.
    """
    global _tables_initialized
    if _tables_initialized:
        return
    with _tables_lock:
        if _tables_initialized:
            return
        try:
            import db
        except Exception as exc:
            log.error("[BhavHist] cannot import db for table init: %s", exc)
            return

        try:
            if db.is_postgresql():
                ddl_daily = """
                    CREATE TABLE IF NOT EXISTS daily_bars (
                        symbol TEXT NOT NULL,
                        date TEXT NOT NULL,
                        open REAL,
                        high REAL,
                        low REAL,
                        close REAL,
                        volume BIGINT,
                        turnover REAL,
                        delivery_pct REAL,
                        PRIMARY KEY (symbol, date)
                    );
                """
                ddl_index = """
                    CREATE TABLE IF NOT EXISTS index_bars (
                        index_name TEXT NOT NULL,
                        date TEXT NOT NULL,
                        open REAL,
                        high REAL,
                        low REAL,
                        close REAL,
                        PRIMARY KEY (index_name, date)
                    );
                """
            else:
                ddl_daily = """
                    CREATE TABLE IF NOT EXISTS daily_bars (
                        symbol TEXT NOT NULL,
                        date TEXT NOT NULL,
                        open REAL,
                        high REAL,
                        low REAL,
                        close REAL,
                        volume INTEGER,
                        turnover REAL,
                        delivery_pct REAL,
                        PRIMARY KEY (symbol, date)
                    );
                """
                ddl_index = """
                    CREATE TABLE IF NOT EXISTS index_bars (
                        index_name TEXT NOT NULL,
                        date TEXT NOT NULL,
                        open REAL,
                        high REAL,
                        low REAL,
                        close REAL,
                        PRIMARY KEY (index_name, date)
                    );
                """

            db.execute_db(ddl_daily)
            db.execute_db(ddl_index)
            # Helpful indexes (PK already covers (symbol,date)/(index_name,date))
            db.execute_db("CREATE INDEX IF NOT EXISTS idx_daily_bars_symbol ON daily_bars(symbol);")
            db.execute_db("CREATE INDEX IF NOT EXISTS idx_daily_bars_date ON daily_bars(date);")
            db.execute_db("CREATE INDEX IF NOT EXISTS idx_index_bars_name ON index_bars(index_name);")
            db.execute_db("CREATE INDEX IF NOT EXISTS idx_index_bars_date ON index_bars(date);")

            _tables_initialized = True
            log.info("[BhavHist] history tables ready (daily_bars, index_bars)")
        except Exception as exc:
            log.error("[BhavHist] init_history_tables failed: %s", exc)


# ─── (4) Idempotent bulk upserts ──────────────────────────────────────────────

def store_bars(records: list) -> int:
    """
    Idempotent bulk upsert of equity daily bars into daily_bars.
    `records`: list of dicts (as returned by fetch_sec_bhavdata_full).
    Returns number of rows attempted. Tolerant of failures.
    """
    if not records:
        return 0
    init_history_tables()
    try:
        import db
    except Exception as exc:
        log.error("[BhavHist] store_bars: db import failed: %s", exc)
        return 0

    params = [
        (
            r["symbol"], r["date"], r.get("open"), r.get("high"), r.get("low"),
            r.get("close"), r.get("volume"), r.get("turnover"), r.get("delivery_pct"),
        )
        for r in records
    ]

    try:
        if db.is_postgresql():
            sql = """
                INSERT INTO daily_bars
                    (symbol, date, open, high, low, close, volume, turnover, delivery_pct)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (symbol, date) DO UPDATE SET
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume,
                    turnover = EXCLUDED.turnover,
                    delivery_pct = EXCLUDED.delivery_pct
            """
        else:
            sql = """
                INSERT OR REPLACE INTO daily_bars
                    (symbol, date, open, high, low, close, volume, turnover, delivery_pct)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
        db.execute_many(sql, params)
        return len(params)
    except Exception as exc:
        log.error("[BhavHist] store_bars failed: %s", exc)
        return 0


def store_index_bars(records: list) -> int:
    """
    Idempotent bulk upsert of index bars into index_bars.
    `records`: list of dicts (as returned by fetch_ind_close_all).
    Returns number of rows attempted. Tolerant of failures.
    """
    if not records:
        return 0
    init_history_tables()
    try:
        import db
    except Exception as exc:
        log.error("[BhavHist] store_index_bars: db import failed: %s", exc)
        return 0

    params = [
        (
            r["index"], r["date"], r.get("open"), r.get("high"),
            r.get("low"), r.get("close"),
        )
        for r in records
    ]

    try:
        if db.is_postgresql():
            sql = """
                INSERT INTO index_bars
                    (index_name, date, open, high, low, close)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (index_name, date) DO UPDATE SET
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close
            """
        else:
            sql = """
                INSERT OR REPLACE INTO index_bars
                    (index_name, date, open, high, low, close)
                VALUES (?, ?, ?, ?, ?, ?)
            """
        db.execute_many(sql, params)
        return len(params)
    except Exception as exc:
        log.error("[BhavHist] store_index_bars failed: %s", exc)
        return 0


# ─── (5) Backfill over a date range ───────────────────────────────────────────

def _iter_trading_days(start_date, end_date):
    """Yield date objects from start..end inclusive, skipping Sat/Sun."""
    if isinstance(start_date, datetime):
        start_date = start_date.date()
    if isinstance(end_date, datetime):
        end_date = end_date.date()
    cur = start_date
    one = timedelta(days=1)
    while cur <= end_date:
        if cur.weekday() < 5:  # Mon-Fri
            yield cur
        cur += one


def backfill(start_date, end_date, workers: int = 6) -> dict:
    """
    Backfill daily_bars + index_bars over [start_date, end_date].

    Iterates trading days (skips Sat/Sun), downloads sec_bhavdata_full +
    ind_close_all per day with a ThreadPoolExecutor(workers). Reuses one
    cookie'd session, is polite (small sleeps), tolerates missing days
    (holidays / 404) gracefully, and logs progress.

    Returns {days, eq_rows, idx_rows}.
    """
    init_history_tables()
    session = _make_nse_session()

    days = list(_iter_trading_days(start_date, end_date))
    total = len(days)
    log.info("[BhavHist] backfill start: %s -> %s (%d trading days, workers=%d)",
             days[0] if days else "-", days[-1] if days else "-", total, workers)

    eq_rows = 0
    idx_rows = 0
    days_with_data = 0
    processed = 0
    counter_lock = threading.Lock()

    def _one_day(dt):
        # Be polite: tiny stagger to avoid hammering NSE
        time.sleep(0.2)
        eq = fetch_sec_bhavdata_full(dt, session=session)
        idx = fetch_ind_close_all(dt, session=session)
        n_eq = store_bars(eq) if eq else 0
        n_idx = store_index_bars(idx) if idx else 0
        return dt, n_eq, n_idx

    try:
        with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
            futures = {ex.submit(_one_day, dt): dt for dt in days}
            for fut in as_completed(futures):
                dt = futures[fut]
                try:
                    _dt, n_eq, n_idx = fut.result()
                except Exception as exc:
                    log.warning("[BhavHist] backfill day %s failed: %s", dt, exc)
                    n_eq, n_idx = 0, 0
                with counter_lock:
                    eq_rows += n_eq
                    idx_rows += n_idx
                    if n_eq or n_idx:
                        days_with_data += 1
                    processed += 1
                    if processed % 10 == 0 or processed == total:
                        log.info("[BhavHist] backfill progress %d/%d days | eq_rows=%d idx_rows=%d",
                                 processed, total, eq_rows, idx_rows)
    finally:
        try:
            session.close()
        except Exception:
            pass

    result = {"days": days_with_data, "eq_rows": eq_rows, "idx_rows": idx_rows}
    log.info("[BhavHist] backfill complete: %s", result)
    return result


# ─── (6) Append the most recent available trading day ─────────────────────────

def append_latest() -> dict:
    """
    Fetch + store the most recent available trading day. Tries today and
    walks back up to ~5 days to skip weekends/holidays. Stops at the first
    day that yields data.

    Returns {date, eq_rows, idx_rows} (date None if nothing found).
    """
    init_history_tables()
    session = _make_nse_session()
    today = datetime.now().date()

    try:
        for back in range(0, 6):  # today .. today-5
            dt = today - timedelta(days=back)
            if dt.weekday() >= 5:
                continue
            eq = fetch_sec_bhavdata_full(dt, session=session)
            idx = fetch_ind_close_all(dt, session=session)
            n_eq = store_bars(eq) if eq else 0
            n_idx = store_index_bars(idx) if idx else 0
            if n_eq or n_idx:
                result = {"date": dt.strftime("%Y-%m-%d"), "eq_rows": n_eq, "idx_rows": n_idx}
                log.info("[BhavHist] append_latest: %s", result)
                return result
        log.warning("[BhavHist] append_latest: no data in last 5 days")
        return {"date": None, "eq_rows": 0, "idx_rows": 0}
    finally:
        try:
            session.close()
        except Exception:
            pass


# ─── (7) Read: equity history ─────────────────────────────────────────────────

def get_history(symbol: str, days: int = 365):
    """
    Return a pandas DataFrame of daily bars for `symbol` over the last `days`,
    newest-last, with columns:
        DATE, OPEN, HIGH, LOW, CLOSE, VOLUME, "DELIVERY %"
    Returns None if there is no data (or pandas/db unavailable).
    """
    try:
        import pandas as pd
        import db
    except Exception as exc:
        log.error("[BhavHist] get_history import failed: %s", exc)
        return None

    init_history_tables()
    cutoff = (datetime.now().date() - timedelta(days=int(days))).strftime("%Y-%m-%d")

    try:
        rows = db.execute_db(
            """SELECT date, open, high, low, close, volume, delivery_pct
                 FROM daily_bars
                WHERE symbol = ? AND date >= ?
                ORDER BY date ASC""",
            (symbol, cutoff),
            fetch="all",
        )
    except Exception as exc:
        log.warning("[BhavHist] get_history query failed for %s: %s", symbol, exc)
        return None

    if not rows:
        return None

    df = pd.DataFrame([
        {
            "DATE": r["date"],
            "OPEN": r["open"],
            "HIGH": r["high"],
            "LOW": r["low"],
            "CLOSE": r["close"],
            "VOLUME": r["volume"],
            "DELIVERY %": r["delivery_pct"],
        }
        for r in rows
    ])
    if df.empty:
        return None
    return df


# ─── (8) Read: index history ──────────────────────────────────────────────────

def get_index_history(index: str, days: int = 365):
    """
    Return a pandas DataFrame of index bars for `index` over the last `days`,
    newest-last, with columns: DATE, OPEN, HIGH, LOW, CLOSE.
    Returns None if there is no data.
    """
    try:
        import pandas as pd
        import db
    except Exception as exc:
        log.error("[BhavHist] get_index_history import failed: %s", exc)
        return None

    init_history_tables()
    cutoff = (datetime.now().date() - timedelta(days=int(days))).strftime("%Y-%m-%d")

    try:
        rows = db.execute_db(
            """SELECT date, open, high, low, close
                 FROM index_bars
                WHERE index_name = ? AND date >= ?
                ORDER BY date ASC""",
            (index, cutoff),
            fetch="all",
        )
    except Exception as exc:
        log.warning("[BhavHist] get_index_history query failed for %s: %s", index, exc)
        return None

    if not rows:
        return None

    df = pd.DataFrame([
        {
            "DATE": r["date"],
            "OPEN": r["open"],
            "HIGH": r["high"],
            "LOW": r["low"],
            "CLOSE": r["close"],
        }
        for r in rows
    ])
    if df.empty:
        return None
    return df


def has_history(symbol: str, min_rows: int = 50) -> bool:
    """
    True if daily_bars holds at least `min_rows` rows for `symbol`.
    Tolerant: returns False on any error.
    """
    try:
        import db
    except Exception:
        return False

    init_history_tables()
    try:
        cnt = db.execute_db(
            "SELECT COUNT(*) FROM daily_bars WHERE symbol = ?",
            (symbol,),
            fetch="count",
        )
        return bool(cnt) and int(cnt) >= int(min_rows)
    except Exception as exc:
        log.debug("[BhavHist] has_history check failed for %s: %s", symbol, exc)
        return False
