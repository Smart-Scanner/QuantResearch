"""
Fundamentals Engine — Phase 3 (Dhan.co + DB Cache, yfinance removed)
-------------------------------------------------------------
Scoring: P/E, P/B, ROE, ROCE, EPS, NPM, FCF, DivYield
Strategy: low PE + high ROE + growing EPS + positive FCF = bullish

Data Source: Dhan.co → universe_catalog → fundamentals cache
  Level 1 — In-memory dict (_fund_cache) — TTL 6 hours
  Level 2 — Disk JSON  (cache/fundamentals/<SYM>.json) — TTL 6 hours
  Level 3 — Database   (fundamentals table)             — TTL 6 hours
  Level 4 — universe_catalog (Dhan data)                — always available

Public API (unchanged for backward compatibility):
  get_fundamentals_yf(symbol, cache_only=False) -> dict
  _empty_fundamentals() -> dict
  extract_detailed_financials(symbol, upcoming_events, recent_news_titles) -> dict
"""

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from metrics.timer import timed
# yf_guard kept for backward compatibility — functions are no-ops now
try:
    from intelligence.yf_guard import yf_is_available, yf_record_failure, yf_record_success, get_yf_ticker
except ImportError:
    def yf_is_available(): return False
    def yf_record_failure(**kw): pass
    def yf_record_success(): pass
    def get_yf_ticker(s, **kw): return None

log = logging.getLogger("screener")

# ─── Cache config ────────────────────────────────────────────────────────────

_FUND_TTL      = 6 * 3600          # 6 hours in seconds
_FUND_DISK_DIR = Path(__file__).parent.parent / "cache" / "fundamentals"
_fund_cache: dict = {}             # symbol → {data, ts}
_fund_lock  = threading.Lock()

# ─── Dhan fundamentals read-path restoration flag (default OFF; rollback = unset) ──
# When ON: scans read live Dhan fundamentals from universe_catalog (Level 4 — a local DB
# read) even in cache_only mode, AND treat empty/poisoned Level-3 cache rows as a miss so
# they cannot mask valid Dhan data. No scoring / HC / schema / Recommendation-Engine change.
# Read at import; toggled via env var (restart applies it). Rollback = set to 0 / unset.
FUND_DHAN_FALLBACK = os.environ.get("FUND_DHAN_FALLBACK", "0") == "1"

# ─── Helpers ─────────────────────────────────────────────────────────────────

def safe_load_json(path: Path) -> dict | None:
    """Load JSON from path. Returns None on any error.
    Phase 9: On JSON decode error, renames to .corrupt (never silently drops data).
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # Phase 9: Preserve corrupt file for debugging
        try:
            corrupt_path = path.with_suffix(path.suffix + ".corrupt")
            path.rename(corrupt_path)
            log.warning("Corrupt JSON renamed: %s -> %s", path, corrupt_path)
        except Exception:
            pass
        return None
    except Exception:
        return None


def _load_fund_disk_cache(symbol: str) -> dict | None:
    """Level-2 cache: load from disk if file exists and is fresh."""
    _FUND_DISK_DIR.mkdir(parents=True, exist_ok=True)
    path = _FUND_DISK_DIR / f"{symbol.upper()}.json"
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > _FUND_TTL:
        return None
    return safe_load_json(path)


def _store_fund_cache(symbol: str, data: dict) -> None:
    """Write to memory (Level 1) and disk (Level 2) atomically."""
    sym = symbol.upper()
    # Level 1
    with _fund_lock:
        _fund_cache[sym] = {"data": data, "ts": time.time()}
    # Level 2
    try:
        _FUND_DISK_DIR.mkdir(parents=True, exist_ok=True)
        path = _FUND_DISK_DIR / f"{sym}.json"
        # Atomic write via temp file
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        log.debug("fund disk cache write failed for %s: %s", sym, exc)


def _empty_fundamentals() -> dict:
    """Return a zeroed-out fundamentals dict (used as fallback)."""
    return {
        "pe": None, "pb": None, "fwd_pe": None,
        "roe": None, "roa": None,
        "revenue_growth": None, "earnings_growth": None,
        "debt_to_equity": None, "promoter_pct": None,
        "market_cap": None, "free_cash_flow": None,
        "total_revenue": None, "capex": None,
        "eps_fwd": None, "eps_trail": None,
        "sector": "Unknown", "industry": "",
        "fund_score": 0,
    }


# ─── Main function ────────────────────────────────────────────────────────────

@timed("fundamentals_yf")
def get_fundamentals_yf(symbol: str, cache_only: bool = False) -> dict:
    """
    Fetch fundamentals for symbol (NSE, without .NS suffix).

    Four-level cache strategy:
      L1 Memory → L2 Disk → L3 Database → L4 yfinance

    Args:
        symbol:     NSE symbol (e.g. "TCS")
        cache_only: If True, skip yfinance (Level 4). Used in Fast Scan mode.

    Returns:
        dict with fund_score (0–32) and all fundamental fields.
    """
    sym = symbol.upper()

    # ── Level 1: Memory ──────────────────────────────────────────────────────
    with _fund_lock:
        entry = _fund_cache.get(sym)
        if entry and (time.time() - entry["ts"]) < _FUND_TTL:
            return entry["data"]

    # ── Level 2: Disk ────────────────────────────────────────────────────────
    disk_data = _load_fund_disk_cache(sym)
    if disk_data:
        with _fund_lock:
            _fund_cache[sym] = {"data": disk_data, "ts": time.time()}
        return disk_data

    # ── Level 3: Database ────────────────────────────────────────────────────
    try:
        import db
        row = db.execute_db(
            "SELECT pe, pb, fwd_pe, roe, roa, revenue_growth, earnings_growth, "
            "debt_to_equity, promoter_pct, market_cap, free_cash_flow, total_revenue, "
            "capex, eps_fwd, eps_trail, fund_score, updated_at "
            "FROM fundamentals WHERE symbol=?",
            (sym,), fetch="one"
        )
        if row and row.get("updated_at"):
            updated = row["updated_at"]
            if isinstance(updated, str):
                upd_time = datetime.strptime(updated[:19], "%Y-%m-%d %H:%M:%S")
            else:
                upd_time = updated
            age = (datetime.now() - upd_time).total_seconds()
            if age < _FUND_TTL:
                db_data = {
                    k: row.get(k) for k in (
                        "pe", "pb", "fwd_pe", "roe", "roa", "revenue_growth",
                        "earnings_growth", "debt_to_equity", "promoter_pct",
                        "market_cap", "free_cash_flow", "total_revenue", "capex",
                        "eps_fwd", "eps_trail", "fund_score",
                    )
                }
                db_data.setdefault("sector", "Unknown")
                db_data.setdefault("industry", "")
                # FUND_DHAN_FALLBACK: don't let an empty/poisoned cache row mask live Dhan data.
                # If the L3 row carries no usable fundamentals, treat it as a MISS and fall
                # through to Level 4 (universe_catalog) instead of returning zeros.
                _fs = db_data.get("fund_score")
                _poisoned = (not _fs) and db_data.get("pe") is None \
                    and db_data.get("roe") is None and db_data.get("market_cap") is None
                if not (FUND_DHAN_FALLBACK and _poisoned):
                    _store_fund_cache(sym, db_data)
                    return db_data
                # poisoned row + flag ON → fall through to Level 4 (universe_catalog)
    except Exception as exc:
        log.debug("fund DB cache miss for %s: %s", sym, exc)

    # ── Level 4: universe_catalog (Dhan fundamentals, replaces yfinance) ────────
    if cache_only and not FUND_DHAN_FALLBACK:
        log.debug("fund cache miss %s — cache_only=True, returning empty", sym)
        return _empty_fundamentals()
    # FUND_DHAN_FALLBACK ON: proceed to Level 4 (universe_catalog) even under cache_only —
    # it is a local DB read of already-ingested Dhan data, not an external/slow call.

    try:
        import db
        uc_row = db.execute_db(
            """SELECT pe, pb, roe, roce, eps, div_yield, industry_pe,
                      revenue, free_cash_flow, net_profit_margin,
                      market_cap, sector, industry, fundamentals_updated_at
               FROM universe_catalog
               WHERE symbol = ? AND is_active = TRUE""",
            (sym,), fetch="one"
        )
        if uc_row and uc_row.get("market_cap"):
            pe      = uc_row.get("pe")
            pb      = uc_row.get("pb")
            roe_val = uc_row.get("roe")
            roce    = uc_row.get("roce")
            eps_val = uc_row.get("eps")
            mcap    = uc_row.get("market_cap")
            fcf     = uc_row.get("free_cash_flow")
            total_rev = uc_row.get("revenue")
            sector  = uc_row.get("sector") or "Unknown"
            industry = uc_row.get("industry") or ""
            npm     = uc_row.get("net_profit_margin")

            # ── Scoring (same logic, adapted for Dhan scale) ──
            # Dhan ROE is in % (e.g. 25.0), not ratio (0.25)
            fund_score = 0

            # PE scoring
            if pe is not None:
                if pe < 0:
                    fund_score -= 2
                elif pe <= 15:
                    fund_score += 5
                elif pe <= 35:
                    fund_score += 4
                elif pe <= 60:
                    fund_score += 2
                elif pe <= 100:
                    fund_score += 0
                else:
                    fund_score -= 2

            # ROE scoring (Dhan gives % directly)
            if roe_val is not None:
                if roe_val > 25:
                    fund_score += 8
                elif roe_val > 15:
                    fund_score += 5
                elif roe_val > 10:
                    fund_score += 3

            # ROCE scoring (bonus from Dhan — not available in yfinance!)
            if roce is not None and roce > 20:
                fund_score += 3

            # Net Profit Margin (Dhan gives % directly)
            if npm is not None:
                if npm > 20:
                    fund_score += 4
                elif npm > 10:
                    fund_score += 2
                elif npm < 0:
                    fund_score -= 3

            # Free Cash Flow
            if fcf is not None and fcf > 0:
                fund_score += 2

            # Dividend Yield bonus
            div_y = uc_row.get("div_yield")
            if div_y is not None and div_y > 2:
                fund_score += 2

            fund_score = min(max(fund_score, 0), 32)

            result = {
                "pe":              pe,
                "pb":              pb,
                "fwd_pe":          None,  # Dhan doesn't provide forward PE
                "roe":             roe_val,
                "roa":             None,  # Dhan doesn't provide ROA
                "revenue_growth":  None,  # Would need historical comparison
                "earnings_growth": None,  # Would need historical comparison
                "debt_to_equity":  None,  # Dhan doesn't provide D/E directly
                "promoter_pct":    None,  # Dhan doesn't provide promoter %
                "market_cap":      mcap,
                "free_cash_flow":  fcf,
                "total_revenue":   total_rev,
                "capex":           None,
                "eps_fwd":         None,
                "eps_trail":       eps_val,
                "sector":          sector,
                "industry":        industry,
                "fund_score":      fund_score,
                # Dhan bonus fields
                "roce":            roce,
                "net_profit_margin": npm,
                "div_yield":       div_y,
                "industry_pe":     uc_row.get("industry_pe"),
            }

            # Persist to cache (L1 + L2) and database (L3 write-through)
            _store_fund_cache(sym, result)
            try:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                db.execute_db("""
                    INSERT INTO fundamentals (
                        symbol, pe, pb, fwd_pe, roe, roa, revenue_growth, earnings_growth,
                        debt_to_equity, promoter_pct, market_cap, free_cash_flow, total_revenue,
                        capex, eps_fwd, eps_trail, fund_score, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        pe=excluded.pe, pb=excluded.pb, fwd_pe=excluded.fwd_pe,
                        roe=excluded.roe, roa=excluded.roa,
                        revenue_growth=excluded.revenue_growth, earnings_growth=excluded.earnings_growth,
                        debt_to_equity=excluded.debt_to_equity, promoter_pct=excluded.promoter_pct,
                        market_cap=excluded.market_cap, free_cash_flow=excluded.free_cash_flow,
                        total_revenue=excluded.total_revenue, capex=excluded.capex,
                        eps_fwd=excluded.eps_fwd, eps_trail=excluded.eps_trail,
                        fund_score=excluded.fund_score, updated_at=excluded.updated_at
                """, (
                    sym, result.get("pe"), result.get("pb"), result.get("fwd_pe"),
                    result.get("roe"), result.get("roa"), result.get("revenue_growth"),
                    result.get("earnings_growth"), result.get("debt_to_equity"),
                    result.get("promoter_pct"), result.get("market_cap"),
                    result.get("free_cash_flow"), result.get("total_revenue"),
                    result.get("capex"), result.get("eps_fwd"), result.get("eps_trail"),
                    result.get("fund_score"), now,
                ))
            except Exception as exc:
                log.debug("fund DB write-through failed for %s: %s", sym, exc)

            return result
    except Exception as exc:
        log.debug("fund universe_catalog read failed for %s: %s", sym, exc)

    return _empty_fundamentals()


# ─── Detailed financials (on-demand, 7-day cache) ────────────────────────────

@timed("detailed_financials")
def extract_detailed_financials(
    symbol: str,
    upcoming_events: list = None,
    recent_news_titles: list = None,
) -> dict:
    """
    On-demand fetch and compute quarterly & yearly financials for the stock.
    Implements 7-day caching with smart invalidation on corporate event / news detection.
    Uses yf_guard — will not call yfinance if circuit is OPEN.
    """
    import os
    import pandas as pd
    import numpy as np
    import db

    clean = symbol.upper().replace(".NS", "")
    cache_dir = Path(__file__).parent.parent / "cache" / "financials"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{clean}.json"

    # NSE corporate actions (no BSE) drive smart cache-invalidation: a dividend/
    # result/board event near the cache date forces a fresh financials pull.
    if upcoming_events is None:
        try:
            from intelligence.corporate_actions import get_upcoming_events
            upcoming_events = get_upcoming_events(clean)
        except Exception:
            upcoming_events = None

    # Cache policy: 168 hours (7 days)
    ttl_seconds = 168 * 3600
    use_cache = False
    cache_data = None

    # 1. Try database fundamentals table first
    db_data = db.get_detailed_fundamentals(clean)
    if db_data:
        row = db.execute_db(
            "SELECT updated_at FROM fundamentals WHERE symbol = ?",
            (clean,), fetch="one"
        )
        if row and row.get("updated_at"):
            try:
                upd_val = row["updated_at"]
                upd_time = datetime.strptime(upd_val[:19], "%Y-%m-%d %H:%M:%S") if isinstance(upd_val, str) else upd_val
                age = (datetime.now() - upd_time).total_seconds()
                if age < ttl_seconds:
                    use_cache = True
                    cache_data = db_data
            except Exception:
                pass

    # 2. Fall back to local JSON cache file
    if not use_cache and cache_file.exists():
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(cache_file))
            age = (datetime.now() - mtime).total_seconds()
            if age < ttl_seconds:
                loaded = safe_load_json(cache_file)
                if loaded:
                    cache_data = loaded
                    use_cache = True
        except Exception:
            pass

    # 3. Smart invalidation checks (Corporate calendar & News)
    if use_cache and cache_data:
        mtime = None
        row = db.execute_db("SELECT updated_at FROM fundamentals WHERE symbol = ?", (clean,), fetch="one")
        if row and row.get("updated_at"):
            try:
                upd_val = row["updated_at"]
                mtime = datetime.strptime(upd_val[:19], "%Y-%m-%d %H:%M:%S") if isinstance(upd_val, str) else upd_val
            except Exception:
                pass
        if not mtime and cache_file.exists():
            mtime = datetime.fromtimestamp(os.path.getmtime(cache_file))

        if mtime:
            if upcoming_events:
                for ev in upcoming_events:
                    ev_name = (ev.get("event") or "").lower()
                    ev_date_str = ev.get("date")
                    if ev_date_str and ("earnings" in ev_name or "result" in ev_name or "board" in ev_name):
                        try:
                            ev_date = datetime.strptime(ev_date_str, "%Y-%m-%d")
                            cache_date = mtime.date()
                            event_date = ev_date.date()
                            today_date = datetime.now().date()
                            if cache_date <= event_date <= today_date:
                                use_cache = False
                                break
                        except Exception:
                            pass

            if use_cache and recent_news_titles:
                keywords = [
                    "result", "earnings", "net profit", "quarterly results",
                    "q1", "q2", "q3", "q4", "board meeting", "dividend",
                ]
                for title in recent_news_titles:
                    if any(kw in title.lower() for kw in keywords):
                        use_cache = False
                        break

    if use_cache and cache_data:
        return cache_data

    # 4. Fallback: screener.in scrape (detailed financials source while the Upstox
    #    Company-Fundamentals API is unavailable). Persists to the 7-day cache file.
    try:
        from intelligence.screener_financials import fetch_screener_financials
        sf = fetch_screener_financials(clean)
        if sf and (sf.get("quarterly") or sf.get("yearly")):
            try:
                import json as _json
                with open(cache_file, "w", encoding="utf-8") as _f:
                    _json.dump(sf, _f)
            except Exception:
                pass
            log.debug("extract_detailed_financials: screener.in hit for %s (%dQ/%dY)",
                      clean, len(sf.get("quarterly", [])), len(sf.get("yearly", [])))
            return sf
    except Exception as exc:
        log.debug("extract_detailed_financials: screener fallback failed for %s: %s", clean, exc)

    # 5. Data unavailable — return empty (system still runs; earnings momentum scores 0)
    log.debug("extract_detailed_financials: no live data source for %s — returning empty", clean)
    return {
        "yearly": [], "quarterly": [],
        "fin_health_score": 0,
        "fin_health_verdict": "Stressed",
        "fin_alerts": ["Detailed financial data unavailable (Upstox/screener both unreachable)"],
    }


# Dead code removed — extract_detailed_financials previously used yfinance
# to fetch quarterly/yearly financials. Now returns cached data or empty.
# TODO: Implement via Dhan/Angel API if detailed financials are needed.


# ─── Bulk prefill + invalidate utilities ─────────────────────────────────────

def prefill_fundamentals_from_db(symbols: list) -> int:
    """
    Bulk-load fundamentals from the DB into the memory cache at scan start.
    Reduces cold yfinance calls to zero for symbols with fresh DB data.
    Returns count of symbols loaded into memory cache.
    """
    loaded = 0
    try:
        import db
        for sym in symbols:
            sym = sym.upper()
            with _fund_lock:
                if sym in _fund_cache:
                    continue  # already warm
            try:
                row = db.execute_db(
                    "SELECT pe, pb, fwd_pe, roe, roa, revenue_growth, earnings_growth, "
                    "debt_to_equity, promoter_pct, market_cap, free_cash_flow, total_revenue, "
                    "capex, eps_fwd, eps_trail, fund_score, updated_at "
                    "FROM fundamentals WHERE symbol=?",
                    (sym,), fetch="one"
                )
                if row and row.get("updated_at"):
                    upd = row["updated_at"]
                    if isinstance(upd, str):
                        upd_time = datetime.strptime(upd[:19], "%Y-%m-%d %H:%M:%S")
                    else:
                        upd_time = upd
                    if (datetime.now() - upd_time).total_seconds() < _FUND_TTL:
                        db_data = {k: row.get(k) for k in (
                            "pe", "pb", "fwd_pe", "roe", "roa", "revenue_growth",
                            "earnings_growth", "debt_to_equity", "promoter_pct",
                            "market_cap", "free_cash_flow", "total_revenue", "capex",
                            "eps_fwd", "eps_trail", "fund_score",
                        )}
                        db_data.setdefault("sector", "Unknown")
                        db_data.setdefault("industry", "")
                        with _fund_lock:
                            _fund_cache[sym] = {"data": db_data, "ts": time.time()}
                        loaded += 1
            except Exception:
                pass
    except Exception as exc:
        log.debug("prefill_fundamentals_from_db failed: %s", exc)
    log.info("fund prefill: %d/%d symbols loaded from DB into memory cache", loaded, len(symbols))
    return loaded


def invalidate_fundamentals_cache(symbol: str) -> None:
    """
    Clear memory cache + delete disk file for a symbol.
    Call after receiving earnings result, corporate action, or manual refresh.
    """
    sym = symbol.upper()
    with _fund_lock:
        _fund_cache.pop(sym, None)
    disk_path = _FUND_DISK_DIR / f"{sym}.json"
    disk_path.unlink(missing_ok=True)
    log.debug("fund cache invalidated for %s", sym)


# ═══════════════════════════════════════════════════════════════════════════════
# RELEASE 2 — EARNINGS MOMENTUM ENGINE
# ═══════════════════════════════════════════════════════════════════════════════
# Answers: "Is this business getting better faster than the market expects?"
# Separate from fundamental quality (which answers: "Is this a good business?")
#
# 7 Components:
#   1. Revenue Momentum   (20 pts)
#   2. PAT Momentum       (25 pts)
#   3. EPS Momentum       (15 pts)
#   4. Margin Expansion   (15 pts)
#   5. Cash Flow Momentum (10 pts)
#   6. Guidance           (10 pts) — disabled, architecture only
#   7. PEG Ratio          ( 5 pts)
# Total max: 100 pts → normalized 0-100
# ═══════════════════════════════════════════════════════════════════════════════

_EARNINGS_TTL = 24 * 3600   # 24 hours — quarterly data changes slowly
_EARNINGS_DISK_DIR = Path(__file__).parent.parent / "cache" / "earnings_momentum"
_earnings_cache: dict = {}  # symbol → {data, ts}
_earnings_lock = threading.Lock()


def _load_earnings_disk(symbol: str) -> dict | None:
    """Level-2 cache: disk JSON with 24h TTL."""
    _EARNINGS_DISK_DIR.mkdir(parents=True, exist_ok=True)
    path = _EARNINGS_DISK_DIR / f"{symbol.upper()}.json"
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > _EARNINGS_TTL:
        return None
    return safe_load_json(path)


def _store_earnings_cache(symbol: str, data: dict) -> None:
    """Write to memory (L1) and disk (L2)."""
    sym = symbol.upper()
    with _earnings_lock:
        _earnings_cache[sym] = {"data": data, "ts": time.time()}
    try:
        _EARNINGS_DISK_DIR.mkdir(parents=True, exist_ok=True)
        path = _EARNINGS_DISK_DIR / f"{sym}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def _empty_earnings() -> dict:
    """Return zeroed earnings momentum result."""
    return {
        "earnings_momentum_score": 0,
        "earnings_momentum_raw": 0,
        "earnings_grade": "D",
        "earnings_signals": [],
        "confidence": 0,
        "data_available": False,
    }


def _classify_earnings_grade(score: int) -> str:
    """Map score to institutional grade."""
    if score >= 90: return "A+"
    if score >= 80: return "A"
    if score >= 70: return "B+"
    if score >= 60: return "B"
    if score >= 50: return "C"
    return "D"


def get_earnings_momentum(symbol: str, fundamentals: dict = None,
                           cache_only: bool = False) -> dict:
    """
    Compute earnings momentum score for a symbol.

    Uses quarterly data from extract_detailed_financials() with its own
    24h cache layer. During fast scans (cache_only=True), returns cached
    data only — never triggers yfinance.

    Args:
        symbol:       NSE symbol (e.g. "TCS")
        fundamentals: dict from get_fundamentals_yf() — provides PE, earnings_growth
        cache_only:   If True, skip yfinance (Fast Scan mode)

    Returns:
        dict with earnings_momentum_score (0-100), earnings_grade, earnings_signals
    """
    sym = symbol.upper()

    # ── Level 1: Memory cache ────────────────────────────────────────────────
    with _earnings_lock:
        entry = _earnings_cache.get(sym)
        if entry and (time.time() - entry["ts"]) < _EARNINGS_TTL:
            return entry["data"]

    # ── Level 2: Disk cache ──────────────────────────────────────────────────
    disk_data = _load_earnings_disk(sym)
    if disk_data:
        with _earnings_lock:
            _earnings_cache[sym] = {"data": disk_data, "ts": time.time()}
        return disk_data

    # ── Level 3: Compute from detailed financials ────────────────────────────
    # extract_detailed_financials has its own 7-day cache
    try:
        detailed = extract_detailed_financials(sym)
    except Exception as exc:
        log.debug("earnings momentum: detailed financials failed for %s: %s", sym, exc)
        if cache_only:
            return _empty_earnings()
        detailed = None

    if not detailed or (not detailed.get("quarterly") and not detailed.get("yearly")):
        result = _empty_earnings()
        _store_earnings_cache(sym, result)
        return result

    quarterly = detailed.get("quarterly", [])  # latest first
    yearly = detailed.get("yearly", [])        # latest first

    if fundamentals is None:
        fundamentals = {}

    score = 0
    signals = []

    # ═══════════════════════════════════════════════════════════════════════
    # COMPONENT 1: REVENUE MOMENTUM (max 20 pts)
    # ═══════════════════════════════════════════════════════════════════════
    rev_score = 0

    # Revenue YoY growth (from yearly data, latest)
    rev_yoy = None
    if yearly and yearly[0].get("rev_growth_yoy") is not None:
        rev_yoy = yearly[0]["rev_growth_yoy"]
    elif len(quarterly) >= 4:
        # Compute from quarterly: sum(last 4Q) / sum(prev 4Q)
        # quarterly is latest-first
        recent_rev = [q.get("revenue") for q in quarterly[:4] if q.get("revenue")]
        if len(recent_rev) == 4 and len(quarterly) >= 8:
            older_rev = [q.get("revenue") for q in quarterly[4:8] if q.get("revenue")]
            if len(older_rev) == 4 and sum(older_rev) > 0:
                rev_yoy = ((sum(recent_rev) - sum(older_rev)) / sum(older_rev)) * 100

    if rev_yoy is not None:
        if rev_yoy > 50:   rev_score += 10; signals.append(f"Revenue YoY +{rev_yoy:.0f}% 🚀")
        elif rev_yoy > 30: rev_score += 8; signals.append(f"Revenue YoY +{rev_yoy:.0f}%")
        elif rev_yoy > 20: rev_score += 6; signals.append(f"Revenue YoY +{rev_yoy:.0f}%")
        elif rev_yoy > 10: rev_score += 4
        elif rev_yoy > 0:  rev_score += 2

    # Revenue QoQ growth (from quarterly, latest)
    rev_qoq = quarterly[0].get("rev_growth_qoq") if quarterly else None
    if rev_qoq is not None:
        if rev_qoq > 25:   rev_score += 5; signals.append(f"Revenue QoQ +{rev_qoq:.0f}%")
        elif rev_qoq > 15: rev_score += 4
        elif rev_qoq > 10: rev_score += 3
        elif rev_qoq > 5:  rev_score += 2

    # Revenue acceleration: latest growth vs avg of previous two (handles seasonality)
    if len(quarterly) >= 3:
        qoq_values = [q.get("rev_growth_qoq") for q in quarterly[:3]]
        if all(v is not None for v in qoq_values):
            # quarterly is latest-first
            avg_prev = (qoq_values[1] + qoq_values[2]) / 2
            accel = qoq_values[0] - avg_prev
            if accel > 20:  rev_score += 5; signals.append(f"Revenue Accelerating +{accel:.0f}pp 📈")
            elif accel > 10: rev_score += 4; signals.append("Revenue Accelerating 📈")
            elif accel > 5:  rev_score += 2

    rev_score = min(20, rev_score)
    score += rev_score

    # ═══════════════════════════════════════════════════════════════════════
    # COMPONENT 2: PAT MOMENTUM (max 25 pts)
    # ═══════════════════════════════════════════════════════════════════════
    pat_score = 0

    # PAT (Net Income) growth — from quarterly QoQ
    pat_qoq = quarterly[0].get("net_income_growth_qoq") if quarterly else None
    pat_yoy = None
    if yearly and yearly[0].get("net_income_growth_yoy") is not None:
        pat_yoy = yearly[0]["net_income_growth_yoy"]

    # Use YoY if available, else QoQ
    pat_growth = pat_yoy if pat_yoy is not None else pat_qoq
    if pat_growth is not None:
        # Fix #3: Cap bonus for micro-cap distortions (net_income < 50 Cr)
        latest_ni = quarterly[0].get("net_income") if quarterly else None
        is_micro = latest_ni is not None and abs(latest_ni) < 50  # in Cr (already divided by 1Cr)
        pat_cap = 8 if is_micro else 25  # micro-caps capped at 8 pts

        if pat_growth > 60:   pat_score += min(pat_cap, 12); signals.append(f"PAT Growth +{pat_growth:.0f}% 🔥")
        elif pat_growth > 40: pat_score += min(pat_cap, 10); signals.append(f"PAT Growth +{pat_growth:.0f}%")
        elif pat_growth > 25: pat_score += min(pat_cap, 8); signals.append(f"PAT Growth +{pat_growth:.0f}%")
        elif pat_growth > 15: pat_score += 6
        elif pat_growth > 5:  pat_score += 3

    # PAT acceleration: latest growth vs avg of previous two
    if len(quarterly) >= 3:
        pat_qoq_vals = [q.get("net_income_growth_qoq") for q in quarterly[:3]]
        if all(v is not None for v in pat_qoq_vals):
            avg_prev_pat = (pat_qoq_vals[1] + pat_qoq_vals[2]) / 2
            pat_accel = pat_qoq_vals[0] - avg_prev_pat
            if pat_accel > 20:  pat_score += 5; signals.append(f"PAT Accelerating +{pat_accel:.0f}pp 📈")
            elif pat_accel > 10: pat_score += 4; signals.append("PAT Accelerating 📈")
            elif pat_accel > 5:  pat_score += 2

    # Consecutive positive quarters
    if len(quarterly) >= 4:
        net_incomes = [q.get("net_income") for q in quarterly[:4]]
        if all(ni is not None and ni > 0 for ni in net_incomes):
            # Check if all are growing
            growing = all(
                quarterly[i].get("net_income_growth_qoq") is not None
                and quarterly[i]["net_income_growth_qoq"] > 0
                for i in range(min(3, len(quarterly)))
            )
            if growing:
                pat_score += 8; signals.append("4Q Consecutive Growth ✅")

    pat_score = min(25, pat_score)
    score += pat_score

    # ═══════════════════════════════════════════════════════════════════════
    # COMPONENT 3: EPS MOMENTUM (max 15 pts)
    # ═══════════════════════════════════════════════════════════════════════
    eps_score = 0

    # EPS growth from fundamentals (earningsGrowth from yfinance)
    eps_growth_pct = None
    earn_g = fundamentals.get("earnings_growth")
    if earn_g is not None:
        eps_growth_pct = earn_g  # already in % from fundamentals

    # Fallback: compute from quarterly EPS
    if eps_growth_pct is None and len(quarterly) >= 2:
        eps_curr = quarterly[0].get("eps")
        eps_prev = quarterly[1].get("eps")
        if eps_curr and eps_prev and eps_prev > 0:
            eps_growth_pct = ((eps_curr - eps_prev) / abs(eps_prev)) * 100

    if eps_growth_pct is not None:
        if eps_growth_pct > 50:   eps_score += 8; signals.append(f"EPS Growth +{eps_growth_pct:.0f}%")
        elif eps_growth_pct > 30: eps_score += 6
        elif eps_growth_pct > 20: eps_score += 4
        elif eps_growth_pct > 10: eps_score += 2

    # EPS acceleration: latest growth vs avg of previous two (aligned with rev/PAT)
    if len(quarterly) >= 3:
        eps_vals = [q.get("eps") for q in quarterly[:3]]
        if all(e is not None and e > 0 for e in eps_vals):
            growth_recent = (eps_vals[0] - eps_vals[1]) / abs(eps_vals[1]) * 100
            growth_prev = (eps_vals[1] - eps_vals[2]) / abs(eps_vals[2]) * 100
            eps_accel = growth_recent - growth_prev
            if eps_accel > 20:  eps_score += 4; signals.append(f"EPS Accelerating +{eps_accel:.0f}pp 📈")
            elif eps_accel > 10: eps_score += 3; signals.append("EPS Accelerating 📈")
            elif eps_accel > 5:  eps_score += 2

    # Earnings surprise placeholder (+3) — future enhancement
    # eps_score += 3 if surprise > 0

    eps_score = min(15, eps_score)
    score += eps_score

    # ═══════════════════════════════════════════════════════════════════════
    # COMPONENT 4: MARGIN EXPANSION (max 15 pts)
    # ═══════════════════════════════════════════════════════════════════════
    margin_score = 0

    if len(quarterly) >= 2:
        # Fix #2: Compare to avg of previous 3Q (or available) instead of just 1Q
        ebitda_curr = quarterly[0].get("ebitda_margin")
        ebitda_prevs = [q.get("ebitda_margin") for q in quarterly[1:4]
                        if q.get("ebitda_margin") is not None]
        if ebitda_curr is not None and ebitda_prevs:
            ebitda_avg = sum(ebitda_prevs) / len(ebitda_prevs)
            ebitda_change = ebitda_curr - ebitda_avg
            if ebitda_change > 5:   margin_score += 8; signals.append(f"EBITDA Margin +{ebitda_change:.1f}pp vs avg")
            elif ebitda_change > 3: margin_score += 5; signals.append("EBITDA Margin Expanding")
            elif ebitda_change > 1: margin_score += 3

        # Net margin expansion vs avg of previous quarters
        net_curr = quarterly[0].get("net_margin")
        net_prevs = [q.get("net_margin") for q in quarterly[1:4]
                     if q.get("net_margin") is not None]
        if net_curr is not None and net_prevs:
            net_avg = sum(net_prevs) / len(net_prevs)
            net_change = net_curr - net_avg
            if net_change > 4:   margin_score += 7; signals.append(f"Net Margin +{net_change:.1f}pp vs avg")
            elif net_change > 2: margin_score += 5
            elif net_change > 1: margin_score += 3

    margin_score = min(15, margin_score)
    score += margin_score

    # ═══════════════════════════════════════════════════════════════════════
    # COMPONENT 5: CASH FLOW MOMENTUM (max 10 pts)
    # ═══════════════════════════════════════════════════════════════════════
    cf_score = 0

    if len(yearly) >= 2:
        # Operating cash flow growth
        fcf_curr = yearly[0].get("fcf")
        fcf_prev = yearly[1].get("fcf")

        if fcf_curr is not None and fcf_curr > 0:
            cf_score += 5
            if fcf_prev is not None and fcf_prev > 0 and fcf_curr > fcf_prev:
                signals.append("FCF Growing ✅")

        # FCF conversion quality (from detailed financials)
        fcf_conv = yearly[0].get("fcf_conversion")
        if fcf_conv is not None and fcf_conv > 80:
            cf_score += 5; signals.append(f"FCF Conversion {fcf_conv:.0f}%")
        elif fcf_conv is not None and fcf_conv > 50:
            cf_score += 3

    cf_score = min(10, cf_score)
    score += cf_score

    # ═══════════════════════════════════════════════════════════════════════
    # COMPONENT 6: GUIDANCE (max 10 pts) — DISABLED, architecture only
    # ═══════════════════════════════════════════════════════════════════════
    # Phase 2B: Earnings Call NLP, Management Sentiment, Estimate Revisions
    guidance_score = 0
    # score += guidance_score  # disabled

    # ═══════════════════════════════════════════════════════════════════════
    # COMPONENT 7: PEG RATIO (max 5 pts)
    # ═══════════════════════════════════════════════════════════════════════
    peg_score = 0
    pe = fundamentals.get("pe")

    # Use eps_growth_pct computed above, or earningsGrowth from fundamentals
    peg_growth = eps_growth_pct
    if peg_growth is None and earn_g is not None:
        peg_growth = earn_g

    if pe is not None and peg_growth is not None and peg_growth > 0:
        peg = pe / peg_growth
        if peg < 1.0:   peg_score += 5; signals.append(f"PEG {peg:.1f} — Undervalued 🎯")
        elif peg < 1.5: peg_score += 4; signals.append(f"PEG {peg:.1f} — Fair")
        elif peg < 2.0: peg_score += 2; signals.append(f"PEG {peg:.1f}")
        elif peg < 3.0: peg_score += 1

    peg_score = min(5, peg_score)
    score += peg_score

    # ═══════════════════════════════════════════════════════════════════════
    # FINAL SCORE
    # ═══════════════════════════════════════════════════════════════════════
    # Max possible = 20 + 25 + 15 + 15 + 10 + 0 + 5 = 90
    # Normalize to 0-100 (guidance reserved at 10 pts = max would be 100)
    score_raw_100 = min(100, max(0, round((score / 90.0) * 100)))

    # Confidence based on data availability
    n_quarters = len(quarterly)
    confidence = min(100, n_quarters * 25)  # 4+ quarters = 100% confidence

    # Fix #4: Confidence affects final score — low data = lower score
    # 100% confidence → 1.0x, 50% → 0.85x, 25% → 0.775x
    conf_multiplier = 0.7 + (confidence / 333.0)
    score_100 = min(100, max(0, round(score_raw_100 * conf_multiplier)))
    grade = _classify_earnings_grade(score_100)

    result = {
        "earnings_momentum_score": score_100,
        "earnings_momentum_raw": score,
        "earnings_grade": grade,
        "earnings_signals": signals,
        "confidence": confidence,
        "data_available": n_quarters >= 2,
        "quarters_available": n_quarters,
        # Component breakdown for attribution
        "components": {
            "revenue": rev_score,
            "pat": pat_score,
            "eps": eps_score,
            "margin": margin_score,
            "cash_flow": cf_score,
            "guidance": guidance_score,
            "peg": peg_score,
        },
    }

    _store_earnings_cache(sym, result)
    log.debug("earnings momentum for %s: score=%d grade=%s signals=%d",
              sym, score_100, grade, len(signals))
    return result
