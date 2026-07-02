"""
quantresearch/scoring_v1/gates.py — UPSTREAM hard gates (spec section 1).
================================================================================
ADDITIVE module. Applies the UNIVERSE + QUALITY hard gates that run BEFORE the
locked scoring engine (quantresearch/scoring_v1/engine.py). The engine itself
enforces only the >=126-bar eligibility floor; everything below is the universe
and quality layer.

POINT-IN-TIME MANDATE
---------------------
Every query filters `date <= as_of_date`. No look-ahead. Any field that is
unavailable in the pipeline is mapped to None and treated as MISSING.

GATE SEMANTICS
--------------
UNIVERSE gates (ALL hard; a stock must satisfy every one to be eligible):
  * NSE EQ instrument (universe_catalog.instrument_type = 'EQ', is_active).
  * market_cap >= UNIVERSE_MIN_MCAP_CR  (1000 cr; from universe_catalog/Dhan, in cr).
  * 20-day MEDIAN turnover >= UNIVERSE_MIN_AVG_TURNOVER_CR (10 cr). MEDIAN, not
    mean, of daily_bars.turnover over the last 20 bars <= as_of_date. Turnover is
    stored in LAKHS (NSE TURNOVER_LACS) -> convert to crore (/100).
  * price >= UNIVERSE_MIN_PRICE (50). Last close on/<= as_of_date.
  * data coverage >= UNIVERSE_MIN_DATA_COVERAGE (0.90) over trailing 252 calendar
    days (distinct bar dates within (as_of-252, as_of] / 252).
  * listed >= UNIVERSE_MIN_LISTING_DAYS (180): earliest daily_bars date <= as_of-180.

  IMPORTANT for UNIVERSE: a HARD numeric threshold rejects when the value is
  PRESENT and fails. A field that is genuinely MISSING (e.g. no market_cap row at
  all) is recorded as the reason and rejects — these are structural requirements,
  not the quality "missing -> pass" rule. (Turnover/price/coverage/listing are
  always computable from daily_bars; only market_cap can be structurally absent,
  in which case the stock is not an established >=1000cr name and is excluded.)

QUALITY gates (exclude if ANY trips; a MISSING field does NOT exclude):
  * ASM / GSM surveillance  (asm_gsm.is_under_surveillance).
  * suspended / restricted   (STUB: not in pipeline -> missing -> pass; TODO).
  * promoter pledge > 30%    (STUB: not in pipeline -> missing -> pass; TODO).
  * extreme distress: interest-coverage<1 / negative net worth /
    auditor-qualification (STUB: not in pipeline -> missing -> pass; TODO).

PUBLIC API
----------
  apply_universe_gates(symbols, as_of_date)
      -> (eligible_list, rejected_dict)
         rejected_dict: {symbol: "reason string"}
  GATE_THRESHOLDS  (dict snapshot of the thresholds used, for reporting)
"""

from __future__ import annotations

import logging
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

log = logging.getLogger("screener")

# ─── Thresholds (sourced from config.py; defaults match spec) ────────────────
try:
    from config import (
        UNIVERSE_MIN_MCAP_CR,
        UNIVERSE_MIN_AVG_TURNOVER_CR,
        UNIVERSE_MIN_PRICE,
        UNIVERSE_MIN_DATA_COVERAGE,
        UNIVERSE_MIN_LISTING_DAYS,
    )
except Exception as exc:  # config import should never fail, but stay defensive
    log.warning("[gates] config import failed (%s) — using spec defaults", exc)
    UNIVERSE_MIN_MCAP_CR = 1000.0
    UNIVERSE_MIN_AVG_TURNOVER_CR = 10.0
    UNIVERSE_MIN_PRICE = 50.0
    UNIVERSE_MIN_DATA_COVERAGE = 0.90
    UNIVERSE_MIN_LISTING_DAYS = 180

# Constants used by the gates (not configurable; defined by spec).
TURNOVER_WINDOW_BARS = 20            # 20-day MEDIAN turnover window
# Coverage is measured over a trailing ~1-year window. We span 365 CALENDAR days
# (which contains ~252 NSE TRADING days) and measure the symbol's fill-rate against
# the store's ACTUAL trading calendar (distinct dates in daily_bars) over the period
# the symbol has been listed within that window. This makes coverage a DATA-QUALITY
# gate (no big gaps) rather than a history-LENGTH gate, so 180-365-day names are NOT
# rejected (per spec: 52W feature uses min(252, available), graceful). The previous
# 252-calendar-day / 252 denominator was unit-mismatched (trading days are ~69% of
# calendar days) so coverage was capped at ~0.71 and EVERY stock failed.
COVERAGE_LOOKBACK_DAYS = 365         # trailing CALENDAR days spanning ~252 trading days
COVERAGE_EXPECTED_BARS = 252         # fallback denominator only if the market-calendar query fails
# daily_bars.turnover is ingested from NSE sec_bhavdata_full's TURNOVER_LACS
# column -> it is stored in LAKHS of rupees (NOT rupees). 1 crore = 100 lakh, so
# turnover_cr = stored_turnover / 100. Verified empirically: close*volume divided
# by stored turnover is ~1e5 uniformly across all symbols (RELIANCE 1677cr, etc.).
LAKHS_PER_CRORE = 100.0

GATE_THRESHOLDS = {
    "instrument_type": "EQ",
    "exchange": "NSE",
    "min_market_cap_cr": UNIVERSE_MIN_MCAP_CR,
    "min_20d_median_turnover_cr": UNIVERSE_MIN_AVG_TURNOVER_CR,
    "turnover_window_bars": TURNOVER_WINDOW_BARS,
    "turnover_aggregation": "MEDIAN",
    "min_price": UNIVERSE_MIN_PRICE,
    "min_data_coverage": UNIVERSE_MIN_DATA_COVERAGE,
    "coverage_lookback_days": COVERAGE_LOOKBACK_DAYS,
    "min_listing_days": UNIVERSE_MIN_LISTING_DAYS,
    "quality_gates": [
        "asm_gsm_surveillance",
        "suspended_restricted (STUB: missing->pass)",
        "promoter_pledge>30% (STUB: missing->pass)",
        "extreme_distress: int_cov<1 / neg_networth / auditor_qual (STUB: missing->pass)",
    ],
    "missing_field_policy": {
        "universe": "structural thresholds reject when value absent",
        "quality": "MISSING -> PASS (never excludes)",
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
# time series. We use it for the structural EQ / market-cap requirement. (There
# is no historical market-cap store in the broker-free pipeline; this is the
# best available source and is read read-only.)

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
            log.warning("[gates] catalog query failed: %s", exc)
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
# When warm, the four per-symbol helpers below read from these maps instead of
# issuing one query each. The bulk queries mirror the per-symbol SQL EXACTLY
# (same filters, same ordering), so the eligible set is byte-identical.
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
        except Exception as exc:
            log.warning("[gates] bulk warm failed (chunk %d) — falling back to per-symbol: %s", i, exc)
            _BULK = None
            return
    _BULK = {"turn": dict(turn), "close": close, "cov": cov, "first": first}


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
        log.debug("[gates] turnover query failed for %s: %s", symbol, exc)
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
        log.debug("[gates] close query failed for %s: %s", symbol, exc)
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
        log.debug("[gates] coverage query failed for %s: %s", symbol, exc)
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
        log.debug("[gates] first-date query failed for %s: %s", symbol, exc)
        return None
    if rows and rows[0].get("first_date"):
        return rows[0]["first_date"][:10]
    return None


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
        log.debug("[gates] market-calendar query failed: %s", exc)
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

def _quality_reject_reason(symbol: str) -> str | None:
    """
    Return a rejection reason string if the symbol trips a QUALITY gate, else None.
    MISSING fields NEVER exclude (missing/unknown -> allow).

    Revised surveillance rule (gates layer only; supersedes the prior "exclude if
    ASM/GSM"):
        ASM Stage 1 -> ALLOW   ASM Stage 2 -> ALLOW
        ASM Stage 3 -> REJECT  ASM Stage 4 -> REJECT
        GSM (ANY stage) -> REJECT
        Suspended -> REJECT    Delisted -> REJECT
    ASM stage-unknown, and suspended/delisted-unknown (not in our pipeline yet),
    are treated as not-tripped -> ALLOW.
    """
    try:
        from . import asm_gsm
        reject, reason = asm_gsm.should_reject(symbol)  # ASM-stage-aware + GSM
        if reject:
            return reason  # 'quality:gsm' | 'quality:asm_stage3' | 'quality:asm_stage4'
    except Exception as exc:
        # Fetch/import failure => treat as MISSING => allow (do not reject).
        log.debug("[gates] surveillance check unavailable for %s -> allow: %s", symbol, exc)

    # TODO(suspended/delisted): NSE suspended + delisted lists not in pipeline yet ->
    #   unknown -> allow (missing never excludes). Reject here once sourced.
    # TODO(pledge/distress): not in pipeline -> allow.
    return None


# ─── Main entry point ────────────────────────────────────────────────────────

def apply_universe_gates(symbols, as_of_date):
    """
    Apply the UNIVERSE + QUALITY hard gates point-in-time as of `as_of_date`.

    Args:
        symbols: iterable of NSE symbols (case-insensitive).
        as_of_date: datetime | date | 'YYYY-MM-DD'. All data is filtered <= this.

    Returns:
        (eligible_list, rejected_dict)
          eligible_list: list[str] symbols passing ALL gates (original casing kept).
          rejected_dict: {symbol: reason} for every rejected symbol.
    """
    as_of = _to_date_str(as_of_date)
    symbols = list(symbols or [])
    if not symbols:
        return [], {}

    catalog = _load_catalog(symbols)

    # Warm the surveillance lists once (cache-or-fetch) so per-symbol checks are
    # cheap and we get a single fetch-status log line.
    try:
        from . import asm_gsm
        asm_gsm.get_surveillance_sets()
    except Exception as exc:
        log.debug("[gates] could not pre-warm surveillance lists: %s", exc)

    listing_cutoff = _shift_days(as_of, UNIVERSE_MIN_LISTING_DAYS)
    # The store's real NSE trading calendar for the coverage window (computed once,
    # shared by every symbol as the coverage denominator).
    market_dates = _market_trading_dates(as_of, COVERAGE_LOOKBACK_DAYS)
    _warm_bulk(symbols, as_of)  # batch the per-symbol daily_bars reads (byte-identical)

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

        # ── UNIVERSE: 20-day MEDIAN turnover >= MIN_TURNOVER_CR ──
        turnovers = _last_n_turnovers(key, as_of, TURNOVER_WINDOW_BARS)
        if not turnovers:
            rejected[sym] = "universe:no_turnover_data"
            continue
        median_turnover_cr = statistics.median(turnovers) / LAKHS_PER_CRORE
        if median_turnover_cr < UNIVERSE_MIN_AVG_TURNOVER_CR:
            rejected[sym] = (
                f"universe:median_turnover={median_turnover_cr:.2f}cr"
                f"<{UNIVERSE_MIN_AVG_TURNOVER_CR:.0f}cr"
            )
            continue

        # ── UNIVERSE: listing date (used by BOTH coverage and the listing gate) ──
        first_date = _first_bar_date(key, as_of)
        if first_date is None:
            rejected[sym] = "universe:no_listing_data"
            continue

        # ── UNIVERSE: data coverage >= MIN_COVERAGE ──
        # Fill-rate vs the real trading calendar over the period the symbol has been
        # listed within the window (data-quality gate, graceful for young names).
        bar_count = _coverage_bar_count(key, as_of, COVERAGE_LOOKBACK_DAYS)
        expected = _expected_trading_days(market_dates, first_date)
        if expected:
            coverage = min(1.0, bar_count / float(expected))
        else:  # market-calendar query failed -> fall back to the static denominator
            coverage = min(1.0, bar_count / float(COVERAGE_EXPECTED_BARS))
        if coverage < UNIVERSE_MIN_DATA_COVERAGE:
            rejected[sym] = (
                f"universe:coverage={coverage:.2f}<{UNIVERSE_MIN_DATA_COVERAGE:.2f}"
            )
            continue

        # ── UNIVERSE: listed >= MIN_LISTING_DAYS ──
        if first_date > listing_cutoff:
            rejected[sym] = f"universe:listed_after={first_date}(<{UNIVERSE_MIN_LISTING_DAYS}d)"
            continue

        # ── QUALITY: surveillance + STUBs (missing -> pass) ──
        q_reason = _quality_reject_reason(sym)
        if q_reason:
            rejected[sym] = q_reason
            continue

        eligible.append(sym)

    _clear_bulk()
    log.info(
        "[gates] as_of=%s | in=%d eligible=%d rejected=%d",
        as_of, len(symbols), len(eligible), len(rejected),
    )
    return eligible, rejected


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("GATE_THRESHOLDS:")
    for k, v in GATE_THRESHOLDS.items():
        print(f"  {k}: {v}")
