"""
quantresearch/legacy_cleaned/gates.py — UPSTREAM hard gates (legacy_cleaned engine).
================================================================================
ADDITIVE module. Applies the UNIVERSE + QUALITY hard gates that run BEFORE the
legacy_cleaned scoring engine (quantresearch/legacy_cleaned/engine.py). MIRRORS
quantresearch/scoring_v1/gates.py in structure and semantics, but carries its OWN
config (this engine's turnover floor + >=126-bar listing floor) and REUSES v1's
surveillance module (quantresearch.scoring_v1.asm_gsm) read-only.

This file touches nothing outside itself. It does NOT modify scoring_v1/gates.py.

WHAT DIFFERS FROM scoring_v1/gates.py
-------------------------------------
  * turnover floor sourced from quantresearch.legacy_cleaned.config
    (UNIVERSE_MIN_AVG_TURNOVER_CR = 10 cr) with a defensive fallback of 10.
  * listing floor is expressed as a >=126-BAR floor (config.MIN_HISTORY_DAYS,
    ~6 months of trading days), not a calendar-day floor — i.e. the symbol must
    have at least MIN_HISTORY_BARS distinct daily_bars on/<= as_of_date.
Everything else (EQ/market-cap/price structural gates, MEDIAN turnover, coverage
vs the store's real trading calendar, ASM/GSM quality gate, the _BULK windowed
bulk-warm pattern, missing-field policy) mirrors v1 exactly.

POINT-IN-TIME MANDATE
---------------------
Every query filters `date <= as_of_date`. No look-ahead. Any field that is
unavailable in the pipeline is mapped to None and treated as MISSING.

GATE SEMANTICS
--------------
UNIVERSE gates (ALL hard; a stock must satisfy every one to be eligible):
  * NSE EQ instrument (universe_catalog.instrument_type = 'EQ', is_active).
  * market_cap >= UNIVERSE_MIN_MCAP_CR (structural; absent -> reject).
  * 20-day MEDIAN turnover >= UNIVERSE_MIN_AVG_TURNOVER_CR (10 cr, legacy config).
    MEDIAN of daily_bars.turnover over the last 20 bars <= as_of_date. Turnover is
    stored in LAKHS (NSE TURNOVER_LACS) -> convert to crore (/100).
  * price >= UNIVERSE_MIN_PRICE. Last close on/<= as_of_date.
  * data coverage >= UNIVERSE_MIN_DATA_COVERAGE over the trailing window, measured
    against the store's ACTUAL trading calendar (as v1 does).
  * listed >= MIN_HISTORY_BARS (>=126 bars): distinct daily_bars on/<= as_of.

  IMPORTANT for UNIVERSE: a HARD numeric threshold rejects when the value is
  PRESENT and fails. A field that is genuinely MISSING (e.g. no market_cap row at
  all) is recorded as the reason and rejects — structural requirements, not the
  quality "missing -> pass" rule.

QUALITY gates (exclude if ANY trips; a MISSING field does NOT exclude):
  * ASM / GSM surveillance  (quantresearch.scoring_v1.asm_gsm.should_reject).
  * suspended / restricted   (STUB: not in pipeline -> missing -> pass; TODO).
  * promoter pledge > 30%    (STUB: not in pipeline -> missing -> pass; TODO).
  * extreme distress: interest-coverage<1 / negative net worth /
    auditor-qualification (STUB: not in pipeline -> missing -> pass; TODO).

PUBLIC API
----------
  apply_universe_gates(candidates, as_of_date)
      -> (eligible_list, rejected_dict)
         rejected_dict: {symbol: "reason string"}
  GATE_THRESHOLDS  (dict snapshot of the thresholds used, for reporting)

CONSTRAINTS: reads ONLY the stored layer (daily_bars etc.) via db.execute_db.
No live fetch (asm_gsm may warm from its own local cache; it never excludes on
fetch failure).
"""

from __future__ import annotations

import logging
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

log = logging.getLogger("screener")

# ─── legacy_cleaned's OWN thresholds ─────────────────────────────────────────
# turnover floor (10 cr) + listing-bar floor (126) come from THIS engine's config;
# imported defensively with spec fallbacks so the gate stays importable even if
# config lands later.
try:
    from quantresearch.legacy_cleaned.config import (
        UNIVERSE_MIN_AVG_TURNOVER_CR,
        MIN_HISTORY_DAYS as MIN_HISTORY_BARS,
    )
except Exception as exc:  # config may not exist yet — stay defensive.
    log.warning(
        "[lc.gates] legacy_cleaned.config import failed (%s) — using defaults", exc
    )
    UNIVERSE_MIN_AVG_TURNOVER_CR = 10       # cr (legacy_cleaned floor)
    MIN_HISTORY_BARS = 126                  # ~6 months of trading days

# ─── Structural thresholds (mirror v1; sourced from top-level config.py) ──────
# These are NOT legacy_cleaned-specific (EQ / market-cap / price / coverage are the
# same universe definition v1 uses); source them from config.py with spec defaults
# so behaviour matches v1 exactly.
try:
    from config import (
        UNIVERSE_MIN_MCAP_CR,
        UNIVERSE_MIN_PRICE,
        UNIVERSE_MIN_DATA_COVERAGE,
    )
except Exception as exc:  # config import should never fail, but stay defensive
    log.warning("[lc.gates] config import failed (%s) — using spec defaults", exc)
    UNIVERSE_MIN_MCAP_CR = 1000.0
    UNIVERSE_MIN_PRICE = 50.0
    UNIVERSE_MIN_DATA_COVERAGE = 0.90

# Constants used by the gates (not configurable; defined by spec).
TURNOVER_WINDOW_BARS = 20            # 20-day MEDIAN turnover window
# Coverage is measured over a trailing ~1-year window. We span 365 CALENDAR days
# (which contains ~252 NSE TRADING days) and measure the symbol's fill-rate against
# the store's ACTUAL trading calendar (distinct dates in daily_bars) over the period
# the symbol has been listed within that window. This makes coverage a DATA-QUALITY
# gate (no big gaps) rather than a history-LENGTH gate, so young names are NOT
# rejected for coverage; the >=126-bar listing floor handles history length.
COVERAGE_LOOKBACK_DAYS = 365         # trailing CALENDAR days spanning ~252 trading days
COVERAGE_EXPECTED_BARS = 252         # fallback denominator only if the calendar query fails
# daily_bars.turnover is ingested from NSE sec_bhavdata_full's TURNOVER_LACS
# column -> stored in LAKHS of rupees (NOT rupees). 1 crore = 100 lakh, so
# turnover_cr = stored_turnover / 100.
LAKHS_PER_CRORE = 100.0

GATE_THRESHOLDS = {
    "engine": "legacy_cleaned",
    "gate_mode": "same_as_legacy",  # relaxed from the earlier turnover>=10 gate on request
    "instrument_type": "EQ",
    "exchange": "NSE",
    "min_market_cap_cr": UNIVERSE_MIN_MCAP_CR,
    "min_price": UNIVERSE_MIN_PRICE,
    "min_listing_bars": MIN_HISTORY_BARS,
    "turnover_gate": "DISABLED (legacy has no turnover floor; was 10cr MEDIAN)",
    "coverage_gate": "DISABLED (legacy has no coverage floor; was 0.90)",
    "quality_gates": "DISABLED (legacy applies no ASM/GSM surveillance exclusion)",
    "missing_field_policy": {
        "universe": "structural thresholds (EQ/active/mcap/price/bars) reject when value absent",
    },
}


# ─── Date helpers ────────────────────────────────────────────────────────────

def _to_date_str(as_of_date) -> str:
    """Normalize as_of_date (datetime/date/'YYYY-MM-DD') to 'YYYY-MM-DD'."""
    if isinstance(as_of_date, str):
        return as_of_date[:10]
    if isinstance(as_of_date, (datetime,)):
        return as_of_date.strftime("%Y-%m-%d")
    # date object (datetime.date)
    try:
        return as_of_date.strftime("%Y-%m-%d")
    except Exception:
        return str(as_of_date)[:10]


def _shift_days(date_str: str, days: int) -> str:
    """Return date_str shifted back by `days` calendar days, 'YYYY-MM-DD'."""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return (d - timedelta(days=days)).strftime("%Y-%m-%d")


# ─── Catalog (market cap / instrument type) — point-in-time-agnostic ─────────
# universe_catalog stores the CURRENT classification + market cap; it is not a
# time series. We use it for the structural EQ / market-cap requirement (read-only).

def _load_catalog(symbols: list) -> dict:
    """
    Return {symbol_upper: {market_cap, instrument_type, is_active, price}} for the
    requested symbols. Missing symbols are simply absent from the dict.
    """
    import db

    out: dict = {}
    if not symbols:
        return out

    # Chunk IN-lists to keep SQL parameter counts reasonable.
    CHUNK = 500
    for i in range(0, len(symbols), CHUNK):
        chunk = [s.upper() for s in symbols[i:i + CHUNK]]
        placeholders = ",".join(["?"] * len(chunk))
        try:
            rows = db.execute_db(
                f"""SELECT symbol, market_cap, instrument_type, is_active, price
                    FROM universe_catalog
                    WHERE UPPER(symbol) IN ({placeholders})""",
                tuple(chunk),
                fetch="all",
            )
        except Exception as exc:
            log.warning("[lc.gates] catalog query failed: %s", exc)
            rows = []
        for r in rows or []:
            out[(r["symbol"] or "").upper()] = {
                "market_cap": r.get("market_cap"),
                "instrument_type": r.get("instrument_type"),
                "is_active": r.get("is_active"),
                "price": r.get("price"),
            }
    return out


# ─── bulk warm-cache (batches the per-symbol reads into a few queries) ───────
# When warm, the per-symbol helpers below read from these maps instead of issuing
# one query each. The bulk queries mirror the per-symbol SQL EXACTLY (same filters,
# same ordering), so the eligible set is byte-identical.
_BULK: dict | None = None


def _warm_bulk(symbols, as_of: str) -> None:
    global _BULK
    import db
    syms = [s.upper() for s in symbols if s]
    start = _shift_days(as_of, COVERAGE_LOOKBACK_DAYS)
    turn: dict = defaultdict(list)
    close: dict = {}
    cov: dict = {}
    first: dict = {}
    barcount: dict = {}
    CHUNK = 700
    for i in range(0, len(syms), CHUNK):
        ch = syms[i:i + CHUNK]
        ph = ",".join(["?"] * len(ch))
        try:
            # last-N turnovers, newest first (== _last_n_turnovers)
            for r in (db.execute_db(
                f"SELECT symbol, turnover FROM (SELECT symbol, turnover, "
                f"ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) rn "
                f"FROM daily_bars WHERE symbol IN ({ph}) AND date <= ? AND turnover IS NOT NULL) t "
                f"WHERE rn <= ? ORDER BY symbol, rn",
                tuple(ch) + (as_of, TURNOVER_WINDOW_BARS), fetch="all", require_pg=True) or []):
                turn[r["symbol"]].append(float(r["turnover"]))
            # most recent close (== _last_close)
            for r in (db.execute_db(
                f"SELECT symbol, close FROM (SELECT symbol, close, "
                f"ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) rn "
                f"FROM daily_bars WHERE symbol IN ({ph}) AND date <= ? AND close IS NOT NULL) t "
                f"WHERE rn = 1",
                tuple(ch) + (as_of,), fetch="all", require_pg=True) or []):
                try:
                    close[r["symbol"]] = float(r["close"])
                except (TypeError, ValueError):
                    pass
            # coverage distinct-bar count (== _coverage_bar_count)
            for r in (db.execute_db(
                f"SELECT symbol, COUNT(DISTINCT date) c FROM daily_bars "
                f"WHERE symbol IN ({ph}) AND date > ? AND date <= ? GROUP BY symbol",
                tuple(ch) + (start, as_of), fetch="all", require_pg=True) or []):
                cov[r["symbol"]] = int(r["c"] or 0)
            # earliest bar date (== _first_bar_date)
            for r in (db.execute_db(
                f"SELECT symbol, MIN(date) f FROM daily_bars "
                f"WHERE symbol IN ({ph}) AND date <= ? GROUP BY symbol",
                tuple(ch) + (as_of,), fetch="all", require_pg=True) or []):
                if r.get("f"):
                    first[r["symbol"]] = str(r["f"])[:10]
            # TOTAL distinct bar count on/<= as_of (== _total_bar_count; the >=126-bar
            # listing floor is a HISTORY-LENGTH count, distinct from the coverage window)
            for r in (db.execute_db(
                f"SELECT symbol, COUNT(DISTINCT date) c FROM daily_bars "
                f"WHERE symbol IN ({ph}) AND date <= ? GROUP BY symbol",
                tuple(ch) + (as_of,), fetch="all", require_pg=True) or []):
                barcount[r["symbol"]] = int(r["c"] or 0)
        except Exception as exc:
            log.warning("[lc.gates] bulk warm failed (chunk %d) — falling back to per-symbol: %s", i, exc)
            _BULK = None
            return
    _BULK = {
        "turn": dict(turn), "close": close, "cov": cov,
        "first": first, "barcount": barcount,
    }


def _clear_bulk() -> None:
    global _BULK
    _BULK = None


# ─── daily_bars point-in-time reads ──────────────────────────────────────────

def _last_n_turnovers(symbol: str, as_of: str, n: int) -> list:
    """Last `n` non-null turnover values (in LAKHS) with date <= as_of, newest first."""
    if _BULK is not None:
        return list(_BULK["turn"].get(symbol, []))[:n]
    import db
    try:
        rows = db.execute_db(
            """SELECT turnover FROM daily_bars
               WHERE symbol = ? AND date <= ? AND turnover IS NOT NULL
               ORDER BY date DESC
               LIMIT ?""",
            (symbol, as_of, n),
            fetch="all",
        )
    except Exception as exc:
        log.debug("[lc.gates] turnover query failed for %s: %s", symbol, exc)
        return []
    return [float(r["turnover"]) for r in (rows or []) if r.get("turnover") is not None]


def _last_close(symbol: str, as_of: str):
    """Most recent close with date <= as_of, or None."""
    if _BULK is not None:
        return _BULK["close"].get(symbol)
    import db
    try:
        rows = db.execute_db(
            """SELECT close FROM daily_bars
               WHERE symbol = ? AND date <= ? AND close IS NOT NULL
               ORDER BY date DESC
               LIMIT 1""",
            (symbol, as_of),
            fetch="all",
        )
    except Exception as exc:
        log.debug("[lc.gates] close query failed for %s: %s", symbol, exc)
        return None
    if rows:
        try:
            return float(rows[0]["close"])
        except (TypeError, ValueError):
            return None
    return None


def _coverage_bar_count(symbol: str, as_of: str, lookback_days: int) -> int:
    """Distinct bar dates in (as_of - lookback_days, as_of]."""
    if _BULK is not None:
        return _BULK["cov"].get(symbol, 0)
    import db
    start = _shift_days(as_of, lookback_days)
    try:
        rows = db.execute_db(
            """SELECT COUNT(DISTINCT date) AS c FROM daily_bars
               WHERE symbol = ? AND date > ? AND date <= ?""",
            (symbol, start, as_of),
            fetch="all",
        )
    except Exception as exc:
        log.debug("[lc.gates] coverage query failed for %s: %s", symbol, exc)
        return 0
    if rows:
        try:
            return int(rows[0]["c"] or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _first_bar_date(symbol: str, as_of: str):
    """Earliest bar date with date <= as_of (listing proxy), or None."""
    if _BULK is not None:
        return _BULK["first"].get(symbol)
    import db
    try:
        rows = db.execute_db(
            """SELECT MIN(date) AS first_date FROM daily_bars
               WHERE symbol = ? AND date <= ?""",
            (symbol, as_of),
            fetch="all",
        )
    except Exception as exc:
        log.debug("[lc.gates] first-date query failed for %s: %s", symbol, exc)
        return None
    if rows and rows[0].get("first_date"):
        return rows[0]["first_date"][:10]
    return None


def _total_bar_count(symbol: str, as_of: str) -> int:
    """Total distinct daily_bars on/<= as_of (history-length -> >=126-bar floor)."""
    if _BULK is not None:
        return _BULK["barcount"].get(symbol, 0)
    import db
    try:
        rows = db.execute_db(
            """SELECT COUNT(DISTINCT date) AS c FROM daily_bars
               WHERE symbol = ? AND date <= ?""",
            (symbol, as_of),
            fetch="all",
        )
    except Exception as exc:
        log.debug("[lc.gates] bar-count query failed for %s: %s", symbol, exc)
        return 0
    if rows:
        try:
            return int(rows[0]["c"] or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _market_trading_dates(as_of: str, lookback_days: int) -> list:
    """
    The store's ACTUAL NSE trading calendar in (as_of - lookback_days, as_of]:
    the sorted distinct dates present across ALL symbols in daily_bars. Used as the
    coverage denominator so a symbol's fill-rate is measured against real trading
    days (not calendar days). Computed once per gate run and shared by all symbols.
    """
    import db
    start = _shift_days(as_of, lookback_days)
    try:
        rows = db.execute_db(
            """SELECT DISTINCT date FROM daily_bars
               WHERE date > ? AND date <= ?
               ORDER BY date""",
            (start, as_of),
            fetch="all",
        )
    except Exception as exc:
        log.debug("[lc.gates] market-calendar query failed: %s", exc)
        return []
    out = []
    for r in (rows or []):
        d = r.get("date")
        if d:
            out.append(str(d)[:10])
    return out


def _expected_trading_days(market_dates: list, first_bar_date) -> int:
    """
    Count of market trading days the symbol COULD have traded within the window:
    all market dates on/after the symbol's first bar (so a young name is measured
    only over its listed life, not penalised for pre-listing absence).
    """
    if not market_dates:
        return 0
    if not first_bar_date:
        return len(market_dates)
    fb = str(first_bar_date)[:10]
    return sum(1 for d in market_dates if d >= fb)


# ─── Quality gate (surveillance + STUBs) ─────────────────────────────────────
# REUSES scoring_v1.asm_gsm as-is (read-only). MISSING fields NEVER exclude.

def _quality_reject_reason(symbol: str) -> str | None:
    """
    Return a rejection reason string if the symbol trips a QUALITY gate, else None.
    MISSING fields NEVER exclude (missing/unknown -> allow).

    Surveillance rule (delegated to scoring_v1.asm_gsm.should_reject):
        ASM stage 3/4 -> REJECT (per that module's active policy)
        GSM (ANY stage) -> REJECT
        Suspended / Delisted -> REJECT
    ASM stage 1/2, stage-unknown, and suspended/delisted-unknown -> ALLOW.
    """
    try:
        from quantresearch.scoring_v1 import asm_gsm
        reject, reason = asm_gsm.should_reject(symbol)  # ASM-stage-aware + GSM
        if reject:
            return reason  # 'quality:gsm' | 'quality:asm_stage3' | 'quality:asm_stage4'
    except Exception as exc:
        # Fetch/import failure => treat as MISSING => allow (do not reject).
        log.debug("[lc.gates] surveillance check unavailable for %s -> allow: %s", symbol, exc)

    # TODO(suspended/delisted): NSE suspended + delisted lists not in pipeline yet ->
    #   unknown -> allow (missing never excludes). Reject here once sourced.
    # TODO(pledge/distress): not in pipeline -> allow.
    return None


# ─── Main entry point ────────────────────────────────────────────────────────

def apply_universe_gates(candidates, as_of_date):
    """
    Apply the UNIVERSE + QUALITY hard gates point-in-time as of `as_of_date`.

    Args:
        candidates: iterable of NSE symbols (case-insensitive).
        as_of_date: datetime | date | 'YYYY-MM-DD'. All data is filtered <= this.

    Returns:
        (eligible_list, rejected_dict)
          eligible_list: list[str] symbols passing ALL gates (original casing kept).
          rejected_dict: {symbol: reason} for every rejected symbol.
    """
    as_of = _to_date_str(as_of_date)
    symbols = list(candidates or [])
    if not symbols:
        return [], {}

    catalog = _load_catalog(symbols)

    # NOTE: surveillance pre-warm + trading-calendar query REMOVED — the ASM/GSM
    # quality gate and the coverage gate are disabled under "same gate as legacy",
    # so neither is needed. Dropping the asm_gsm pre-warm also guarantees the gate
    # stays ZERO-network (asm_gsm.get_surveillance_sets can fetch NSE on a cold cache).
    _warm_bulk(symbols, as_of)  # batch the per-symbol daily_bars reads (close + bar-count)

    eligible: list = []
    rejected: dict = {}

    for sym in symbols:
        key = sym.upper()
        cat = catalog.get(key)

        # ── UNIVERSE: NSE EQ instrument (structural) ──
        if cat is None:
            rejected[sym] = "universe:not_in_catalog"
            continue
        inst = (cat.get("instrument_type") or "").upper()
        if inst and inst != "EQ":
            rejected[sym] = f"universe:instrument_type={inst or 'MISSING'}"
            continue
        # is_active may be 0/1 (sqlite) or bool (pg); treat falsy as inactive.
        active = cat.get("is_active")
        if active is not None and not active:
            rejected[sym] = "universe:inactive"
            continue

        # ── UNIVERSE: market_cap >= MIN_MCAP_CR (structural; absent -> reject) ──
        mcap = cat.get("market_cap")
        if mcap is None:
            rejected[sym] = "universe:market_cap_missing"
            continue
        try:
            mcap = float(mcap)
        except (TypeError, ValueError):
            rejected[sym] = "universe:market_cap_invalid"
            continue
        if mcap < UNIVERSE_MIN_MCAP_CR:
            rejected[sym] = f"universe:market_cap={mcap:.0f}cr<{UNIVERSE_MIN_MCAP_CR:.0f}cr"
            continue

        # ── UNIVERSE: price >= MIN_PRICE (last close <= as_of) ──
        last_close = _last_close(key, as_of)
        if last_close is None:
            rejected[sym] = "universe:no_price_data"
            continue
        if last_close < UNIVERSE_MIN_PRICE:
            rejected[sym] = f"universe:price={last_close:.1f}<{UNIVERSE_MIN_PRICE:.0f}"
            continue

        # ── "SAME GATE AS LEGACY" (user request): legacy's universe uses only EQ/active/
        #    market-cap/price + history. legacy_cleaned's earlier turnover>=10 (cut 1335->714),
        #    coverage>=0.90, and ASM/GSM quality gates are DISABLED so lc's universe matches
        #    legacy's looser ~1250 set. (scoring_v1 keeps its own stricter gates — untouched.) ──

        # ── UNIVERSE: listed >= MIN_HISTORY_BARS (>=126 bars, ~6 months) — the engine floor. ──
        total_bars = _total_bar_count(key, as_of)
        if total_bars < MIN_HISTORY_BARS:
            rejected[sym] = f"universe:bars={total_bars}<{MIN_HISTORY_BARS}"
            continue

        eligible.append(sym)

    _clear_bulk()
    log.info(
        "[lc.gates] as_of=%s | in=%d eligible=%d rejected=%d",
        as_of, len(symbols), len(eligible), len(rejected),
    )
    return eligible, rejected


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("GATE_THRESHOLDS (legacy_cleaned):")
    for k, v in GATE_THRESHOLDS.items():
        print(f"  {k}: {v}")
