"""
harness.py - Point-in-time WALK-FORWARD backtest: WEIGHT_MODE equal vs tuned.
=============================================================================
ADDITIVE / READ-ONLY. Drives the LOCKED scoring engine (via the foundation
loaders + engine.score_universe) across a rolling set of rebalance dates,
forms a long-only swing portfolio per WEIGHT_MODE, realises close-to-close
forward returns NET of a realistic NSE-delivery cost model, and prints the
equal-vs-tuned performance side by side.

WHAT IT IS (and is NOT)
-----------------------
This is a PLUMBING / SANITY validation, NOT a deployment decision. The PG
point-in-time store covers ~1 trading year (2025-06-02 .. 2026-06-26). With
the engine's 126-bar eligibility floor and a 20-trading-day forward-eval
need, the usable walk-forward window is roughly 2025-12 .. 2026-05. That is
far too little data for statistical confidence - treat every number as a
plumbing check, not evidence. The harness PRINTS the actual window used,
#rebalances, cross-section size per date, and this caveat.

LOCKED-SPEC COMPLIANCE
----------------------
- The engine is consumed UNMODIFIED. Both WEIGHT_MODE {equal, tuned} are run.
- Regime is NOT applied here (regime is an exposure throttle only and must
  never alter score/rank; this harness scores+ranks straight from the engine
  and sizes equal-weight, so regime is simply out of scope - documented).
- Confidence tiers (data_integrity / signal_agreement) are DISPLAY ONLY and
  are never used to rank / size / gate.
- News / Macro / Catalyst are not in the score and are not used here.
- Buy/hold band uses the engine's own hysteresis (engine.apply_hysteresis):
  BUY on entering top-25, HOLD until rank exits top-50; exits.should_exit
  layers ATR stop / EMA-chandelier trail / 20d time-stop / momentum-fade.

POINT-IN-TIME / NO LOOK-AHEAD
-----------------------------
Entry signals (scoring) for date D use ONLY bars with date <= D (the loaders
enforce this). Forward returns for an open position are realised from bars
AFTER the entry date - that is the realised outcome, which is legitimate for
backtest evaluation (we never use future bars to MAKE a decision).

EARNINGS (tractability caveat - read this)
------------------------------------------
earnings_adapter.build_earnings_batch hits live network feeds (screener.in
scrape + NSE corporate-actions) at ~4 s/symbol. Over a 560-name eligible
cross-section that is ~35 min PER rebalance date -> a full walk-forward would
take many hours and depend on flaky network calls. Earnings is 12% of the
score and DECAYS to neutral when days_since_result is missing/stale, so by
default this harness scores with earnings NEUTRALISED (earnings={}), exactly
as the engine does for any name with no fresh result. BOTH modes are scored
identically this way, so the equal-vs-tuned comparison is apples-to-apples.
Pass --earnings to use the real (slow, network-bound) earnings adapter; that
path is intended for a single-date sanity check, not a full walk-forward.
The harness LOGS which earnings mode it used.

CLI
---
    python -m quantresearch.scoring_v1.validation.harness
        [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--step N]
        [--capital RUPEES] [--earnings] [--turnover-window N]
        [--max-rebalances N] [--quiet]

bootstrap.require_pg() runs FIRST (hard-fails on SQLite).
"""

from __future__ import annotations

# ── PG GUARD (must be first; loads .env so db hits PostgreSQL, not SQLite) ────
import sys as _sys
import os as _os
_sys.path.insert(0, r"d:\Gulshan\QuantResearch")
from quantresearch.scoring_v1 import bootstrap  # noqa: E402  (FIRST import)

import argparse  # noqa: E402
import logging  # noqa: E402
import math  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from typing import Dict, List, Optional, Tuple  # noqa: E402

log = logging.getLogger("screener")

# ── foundation imports (relative-first, absolute fallback) ───────────────────
try:  # pragma: no cover - import plumbing
    from .. import pit_loader, sector_map, earnings_adapter, gates
    from .. import engine as _engine
    from . import costs, exits
except Exception:  # pragma: no cover - fallback when scoring_v1 is on sys.path
    import pit_loader        # type: ignore
    import sector_map        # type: ignore
    import earnings_adapter  # type: ignore
    import gates             # type: ignore
    import engine as _engine  # type: ignore
    from quantresearch.scoring_v1.validation import costs, exits  # type: ignore


# ─────────────────────────── tunables / defaults ────────────────────────────
DEFAULT_STEP = 10                 # trading-day step between rebalances
DEFAULT_CAPITAL = 10_000_000.0    # Rs 1 crore notional book (sizing reference)
DEFAULT_TURNOVER_WINDOW = 60      # bars for median daily turnover (slippage)
TRADING_DAYS_PER_YEAR = 252

TOP_N = _engine.TOP_N                              # 25
HOLD_CUT = int(_engine.TOP_N * _engine.HYSTERESIS_MULT)  # 50


# ─────────────────────────── trading calendar ───────────────────────────────
def trading_dates(start: Optional[str] = None, end: Optional[str] = None) -> List[str]:
    """All distinct trading dates in daily_bars within [start, end] (ISO asc)."""
    import db
    sql = "SELECT DISTINCT date FROM daily_bars"
    clauses, params = [], []
    if start:
        clauses.append("date >= ?"); params.append(start)
    if end:
        clauses.append("date <= ?"); params.append(end)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY date ASC"
    rows = db.execute_db(sql, tuple(params) if params else None, fetch="all")
    return [r["date"] for r in rows]


def median_turnover_cr(symbol: str, as_of: str, window: int = DEFAULT_TURNOVER_WINDOW) -> Optional[float]:
    """
    Median daily traded VALUE (close*volume) in Rs crore over the last `window`
    bars up to as_of. Point-in-time (date <= as_of). Drives the slippage term.
    Returns None if no usable rows.
    """
    import db, statistics
    rows = db.execute_db(
        "SELECT close, volume FROM daily_bars WHERE symbol=? AND date<=? "
        "ORDER BY date DESC LIMIT ?",
        (symbol, as_of, int(window)),
        fetch="all",
    )
    vals = []
    for r in rows or []:
        c, v = r.get("close"), r.get("volume")
        if c and v:
            try:
                vals.append((float(c) * float(v)) / 1e7)  # rupees -> crore
            except (TypeError, ValueError):
                continue
    return statistics.median(vals) if vals else None


# ─────────────────────────── scoring (full cross-section) ────────────────────
def score_universe_pit(as_of: str, mode: str, use_earnings: bool):
    """
    Score the FULL eligible cross-section as of `as_of` for one WEIGHT_MODE,
    point-in-time. Returns the engine's ranked DataFrame (composite desc).

    The whole eligible universe is scored (z-scores are cross-sectional - we
    never subset the cross-section). Earnings is neutralised by default for
    tractability (see module docstring); --earnings uses the real adapter.

    Reuses the EXACT input-assembly logic the adapter uses, so scores match
    adapter.run_scoring when use_earnings=True.
    """
    cands = pit_loader.list_symbols_with_history(as_of)
    try:
        eligible, _ = gates.apply_universe_gates(cands, as_of)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("[harness] gates failed on %s: %s - using candidates", as_of, exc)
        eligible = list(cands)

    price_data: Dict[str, object] = {}
    for sym in eligible:
        df = pit_loader.load_price_df(sym, as_of)
        if df is not None and len(df):
            price_data[sym] = df

    benchmark = pit_loader.load_benchmark(as_of)

    sector_idx: Dict[str, object] = {}
    idx_cache: Dict[str, object] = {}
    for sym in eligible:
        nm = sector_map.get_sector_index_name(sym)
        if not nm:
            continue
        if nm not in idx_cache:
            idx_cache[nm] = pit_loader.load_index_series(nm, as_of)
        series = idx_cache[nm]
        if series is not None and len(series):
            sector_idx[sym] = series

    if use_earnings:
        earnings = earnings_adapter.build_earnings_batch(list(price_data.keys()), as_of)
    else:
        earnings = {}  # engine neutralises earnings (decay=0); identical for both modes

    ranked = _engine.score_universe(
        price_data, benchmark=benchmark, sector_idx=sector_idx,
        earnings=earnings, mode=mode,
    )
    return ranked, price_data


# ─────────────────────────── portfolio bookkeeping ──────────────────────────
@dataclass
class OpenPos:
    symbol: str
    entry_date: str
    entry_price: float
    entry_atr: float
    weight: float                 # fraction of book at entry (equal-weight)
    median_turnover_cr: Optional[float]


@dataclass
class ClosedTrade:
    symbol: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    days_held: int
    gross_ret: float              # close-to-close, fraction
    cost_frac: float              # round-trip cost as fraction of entry notional
    net_ret: float                # gross - cost
    r_multiple: float             # net return / initial risk (ATR_MULT*ATR/entry)
    reason: str


def _atr_at(df, idx_pos: int) -> Optional[float]:
    """ATR14 (engine-identical) at bar position idx_pos (negative = from end)."""
    try:
        s = _engine._atr(df, 14)
        return float(s.iloc[idx_pos])
    except Exception:
        return None


# ─────────────────────────── the walk-forward ───────────────────────────────
@dataclass
class ModeResult:
    mode: str
    equity_curve: List[Tuple[str, float]] = field(default_factory=list)  # (date, equity)
    trades: List[ClosedTrade] = field(default_factory=list)
    rebalances: int = 0
    cross_section_sizes: List[int] = field(default_factory=list)


def run_mode(rebal_dates: List[str], all_dates: List[str], mode: str,
             use_earnings: bool, capital: float, turnover_window: int,
             quiet: bool) -> ModeResult:
    """
    Walk forward over rebal_dates for one WEIGHT_MODE. Between rebalances the
    portfolio is checked every TRADING DAY for exits (point-in-time). Returns a
    ModeResult with equity curve + closed trades.
    """
    import pandas as pd

    res = ModeResult(mode=mode)
    held: Dict[str, OpenPos] = {}
    # Price df loaded to the END of the store, used ONLY for exit checks. We
    # ALWAYS slice it to `<= day` before evaluating, so there is no look-ahead:
    # should_exit never sees a bar after the evaluation date. Loading once (to
    # window end) and slicing is correct AND fast (vs reloading per day).
    exit_df: Dict[str, object] = {}
    last_iso = all_dates[-1]

    # Map each calendar date to its index so we can step day-by-day between
    # rebalances for exit checks.
    date_pos = {d: i for i, d in enumerate(all_dates)}

    for ri, rdate in enumerate(rebal_dates):
        # ---- 1) score the full cross-section as of rdate ----
        ranked, price_data = score_universe_pit(rdate, mode, use_earnings)
        if ranked is None or len(ranked) == 0:
            if not quiet:
                log.warning("[harness:%s] %s: empty ranking - skipped", mode, rdate)
            continue
        res.rebalances += 1
        res.cross_section_sizes.append(len(ranked))
        rank_of = {sym: int(ranked.loc[sym, "rank"]) for sym in ranked.index}

        # ---- 2) hysteresis: which names to hold / buy / drop ----
        hyst = _engine.apply_hysteresis(ranked, held=list(held.keys()))
        to_buy = hyst["buy"]          # entered top-25, not currently held
        # Engine says exit if rank left top-50; exits.should_exit refines timing.

        # ---- 3) advance day-by-day to the NEXT rebalance, checking exits ----
        next_rdate = rebal_dates[ri + 1] if ri + 1 < len(rebal_dates) else None
        start_i = date_pos.get(rdate)
        end_i = date_pos.get(next_rdate) if next_rdate else len(all_dates) - 1
        if start_i is None:
            continue
        # iterate trading days (rdate+1 .. next_rdate inclusive) for exit checks
        # Exit checks run on days AFTER rdate up to (and including) next_rdate.
        # Buys for THIS rebalance are executed first (below) only for the NEXT
        # window, so on rdate itself we evaluate exits for incumbents only.
        for di in range(start_i + 1, (end_i if next_rdate else len(all_dates) - 1) + 1):
            day = all_dates[di]
            day_ts = pd.Timestamp(day)
            for sym in list(held.keys()):
                pos = held[sym]
                df = exit_df.get(sym)
                if df is None:
                    # load ONCE to window end; we always slice <= day below.
                    df = pit_loader.load_price_df(sym, last_iso)
                    if df is None:
                        continue
                    exit_df[sym] = df
                dfd = df[df.index <= day_ts]   # PIT slice: no look-ahead
                if not len(dfd):
                    continue
                cur_rank = rank_of.get(sym)    # rank from latest score (rdate)
                position = exits.Position(
                    symbol=sym, entry_date=pos.entry_date,
                    entry_price=pos.entry_price, entry_atr=pos.entry_atr,
                )
                do_exit, _reason = exits.should_exit(position, dfd, current_rank=cur_rank)
                if do_exit:
                    _close(res, held, sym, day, dfd, capital, turnover_window)

        # ---- 4) execute buys at rdate close (equal-weight into free slots) ----
        # Equal-weight target: 1/TOP_N of the book per name (long-only, top-25).
        target_w = 1.0 / float(TOP_N)
        for sym in to_buy:
            if sym in held:
                continue
            if len(held) >= TOP_N:
                break  # book full; hysteresis holds incumbents until they fade
            df = price_data.get(sym)         # scored as-of rdate (last bar == rdate)
            if df is None or not len(df):
                continue
            entry_price = float(df["close"].iloc[-1])
            entry_atr = _atr_at(df, -1)
            if entry_price <= 0 or entry_atr is None or entry_atr <= 0:
                continue
            mt = median_turnover_cr(sym, rdate, turnover_window)
            held[sym] = OpenPos(
                symbol=sym, entry_date=rdate, entry_price=entry_price,
                entry_atr=entry_atr, weight=target_w, median_turnover_cr=mt,
            )
            # ensure an exit-side df is available for this name going forward
            if sym not in exit_df:
                edf = pit_loader.load_price_df(sym, last_iso)
                if edf is not None:
                    exit_df[sym] = edf

        # ---- 5) mark equity at this rebalance (realised closed + open MTM) ----
        # equity = 1 + sum(net_ret of CLOSED trades)*w + sum(open MTM)*w.
        # Equal-weight, single-pass round-trip accounting (no compounding of the
        # tiny book) - appropriate for a ~1yr sanity check; documented as such.
        equity = 1.0 + _realised_contrib(res) + _open_contrib(held, exit_df, rdate, pd)
        res.equity_curve.append((rdate, equity))

        if not quiet:
            log.info("[harness:%s] %s rebal#%d xsec=%d held=%d buys=%d equity=%.4f",
                     mode, rdate, res.rebalances, len(ranked), len(held),
                     len(to_buy), equity)

    # ---- close any still-open positions at the last available bar ----
    for sym in list(held.keys()):
        df = exit_df.get(sym)
        if df is None:
            df = pit_loader.load_price_df(sym, last_iso)
        if df is None or not len(df):
            held.pop(sym, None)
            continue
        dfd = df[df.index <= pd.Timestamp(last_iso)]
        if len(dfd):
            _close(res, held, sym, last_iso, dfd, capital, turnover_window,
                   force_reason="end_of_window")
        else:
            held.pop(sym, None)

    # final equity = 1 + sum of all realised round-trip contributions.
    res.equity_curve.append((last_iso, 1.0 + _realised_contrib(res)))
    return res


def _realised_contrib(res: ModeResult) -> float:
    """Sum of equal-weight net-return contributions from CLOSED trades."""
    w = 1.0 / float(TOP_N)
    return sum(t.net_ret * w for t in res.trades)


def _open_contrib(held: Dict[str, OpenPos], exit_df: Dict[str, object],
                  as_of: str, pd) -> float:
    """Equal-weight open MTM contribution (gross, close on/before as_of)."""
    total = 0.0
    ts = pd.Timestamp(as_of)
    for sym, pos in held.items():
        df = exit_df.get(sym)
        if df is None:
            continue
        sub = df[df.index <= ts]
        if not len(sub):
            continue
        px = float(sub["close"].iloc[-1])
        total += pos.weight * (px / pos.entry_price - 1.0)
    return total


def _close(res: ModeResult, held: Dict[str, OpenPos], sym: str, exit_date: str,
           dfd, capital: float, turnover_window: int,
           force_reason: Optional[str] = None) -> None:
    """Close held[sym] at the last bar of dfd, charge round-trip cost, record trade."""
    import pandas as pd
    pos = held.pop(sym, None)
    if pos is None:
        return
    exit_price = float(dfd["close"].iloc[-1])
    entry_price = pos.entry_price
    gross = exit_price / entry_price - 1.0

    # round-trip cost on the per-name notional (equal-weight slice of the book)
    notional = pos.weight * capital
    sell_notional = notional * (1.0 + gross)
    ck = costs.round_trip_cost_bps(
        notional, pos.median_turnover_cr, sell_notional=sell_notional,
    )
    cost_frac = float(ck["total_fraction"])
    net = gross - cost_frac

    # initial risk in fraction terms = ATR_MULT * entry_atr / entry_price
    risk = (exits.ATR_MULT * pos.entry_atr / entry_price) if (pos.entry_atr and entry_price) else None
    r_mult = (net / risk) if (risk and risk > 0) else float("nan")

    try:
        days = int((pd.Timestamp(exit_date) - pd.Timestamp(pos.entry_date)).days)
    except Exception:
        days = 0

    res.trades.append(ClosedTrade(
        symbol=sym, entry_date=pos.entry_date, exit_date=exit_date,
        entry_price=entry_price, exit_price=exit_price, days_held=days,
        gross_ret=gross, cost_frac=cost_frac, net_ret=net, r_multiple=r_mult,
        reason=force_reason or "exit_rule",
    ))


# ─────────────────────────── metrics ────────────────────────────────────────
def _max_drawdown(equity: List[float]) -> float:
    peak, mdd = -1e18, 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    return -mdd  # positive magnitude


def compute_metrics(res: ModeResult, window_years: float) -> dict:
    """
    NET-of-cost performance metrics for one mode. Sharpe is annualised from the
    per-trade net-return distribution scaled by realised trade frequency (a
    crude deflation given the tiny sample - stated honestly as low-power).
    """
    trades = res.trades
    n = len(trades)
    eq_vals = [v for _, v in res.equity_curve] or [1.0]
    final_eq = eq_vals[-1]

    cagr = (final_eq ** (1.0 / window_years) - 1.0) if (final_eq > 0 and window_years > 0) else float("nan")
    mdd = _max_drawdown(eq_vals)

    nets = [t.net_ret for t in trades]
    wins = [r for r in nets if r > 0]
    losses = [r for r in nets if r <= 0]
    hit = (len(wins) / n) if n else float("nan")
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0

    rs = [t.r_multiple for t in trades if t.r_multiple == t.r_multiple]  # drop NaN
    avg_r = (sum(rs) / len(rs)) if rs else float("nan")

    # Sharpe on the per-trade net-return series, annualised by trades/year.
    if n >= 2:
        mu = sum(nets) / n
        var = sum((r - mu) ** 2 for r in nets) / (n - 1)
        sd = math.sqrt(var)
        trades_per_year = n / window_years if window_years > 0 else float("nan")
        sharpe = (mu / sd) * math.sqrt(trades_per_year) if sd > 0 else float("nan")
        # crude deflation for selection over 2 modes + small n (Bailey/Lopez de
        # Prado spirit): shrink by sqrt((n-1)/n) and note the low-power caveat.
        deflated_sharpe = sharpe * math.sqrt((n - 1) / n) if (n and not math.isnan(sharpe)) else float("nan")
    else:
        sharpe = deflated_sharpe = float("nan")

    avg_hold = (sum(t.days_held for t in trades) / n) if n else float("nan")
    # annual turnover ~ (#round trips * per-name weight) annualised
    per_name_w = 1.0 / float(TOP_N)
    ann_turnover = (n * per_name_w / window_years) if window_years > 0 else float("nan")

    return {
        "mode": res.mode,
        "final_equity": final_eq,
        "CAGR": cagr,
        "deflated_sharpe": deflated_sharpe,
        "sharpe_raw": sharpe,
        "hit_rate": hit,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "avg_R": avg_r,
        "max_drawdown": mdd,
        "annual_turnover": ann_turnover,
        "n_trades": n,
        "avg_hold_days": avg_hold,
        "rebalances": res.rebalances,
        "avg_xsec": (sum(res.cross_section_sizes) / len(res.cross_section_sizes))
                    if res.cross_section_sizes else float("nan"),
    }


def _fmt_pct(x):
    return "  n/a " if (x != x) else f"{x*100:7.2f}%"


def _fmt_num(x, w=8, p=2):
    return f"{'n/a':>{w}}" if (x != x) else f"{x:>{w}.{p}f}"


def print_side_by_side(m_eq: dict, m_tu: dict, window: dict) -> str:
    """Build the equal-vs-tuned table + window/caveat block; return as string."""
    L = []
    L.append("=" * 78)
    L.append("WALK-FORWARD VALIDATION  -  WEIGHT_MODE: equal vs tuned  (NET of costs)")
    L.append("=" * 78)
    L.append(f"Data window (PG)      : {window['store_first']} .. {window['store_last']}")
    L.append(f"Rebalance window used : {window['first_rebal']} .. {window['last_rebal']}")
    L.append(f"Rebalances            : {window['n_rebal']}  (step = {window['step']} trading days)")
    L.append(f"Cross-section / date  : ~{window['avg_xsec']:.0f} eligible symbols (FULL universe scored)")
    L.append(f"Window length         : {window['years']:.2f} years")
    L.append(f"Earnings factor       : {window['earnings_mode']}")
    L.append("-" * 78)
    rows = [
        ("Metric", "equal", "tuned"),
        ("final equity (x)", _fmt_num(m_eq["final_equity"], 8, 4), _fmt_num(m_tu["final_equity"], 8, 4)),
        ("CAGR", _fmt_pct(m_eq["CAGR"]), _fmt_pct(m_tu["CAGR"])),
        ("deflated Sharpe", _fmt_num(m_eq["deflated_sharpe"]), _fmt_num(m_tu["deflated_sharpe"])),
        ("  (raw Sharpe)", _fmt_num(m_eq["sharpe_raw"]), _fmt_num(m_tu["sharpe_raw"])),
        ("hit-rate", _fmt_pct(m_eq["hit_rate"]), _fmt_pct(m_tu["hit_rate"])),
        ("avg win", _fmt_pct(m_eq["avg_win"]), _fmt_pct(m_tu["avg_win"])),
        ("avg loss", _fmt_pct(m_eq["avg_loss"]), _fmt_pct(m_tu["avg_loss"])),
        ("avg R (net)", _fmt_num(m_eq["avg_R"]), _fmt_num(m_tu["avg_R"])),
        ("max drawdown", _fmt_pct(m_eq["max_drawdown"]), _fmt_pct(m_tu["max_drawdown"])),
        ("annual turnover", _fmt_num(m_eq["annual_turnover"]), _fmt_num(m_tu["annual_turnover"])),
        ("# trades", _fmt_num(m_eq["n_trades"], 8, 0), _fmt_num(m_tu["n_trades"], 8, 0)),
        ("avg hold (days)", _fmt_num(m_eq["avg_hold_days"]), _fmt_num(m_tu["avg_hold_days"])),
    ]
    for name, a, b in rows:
        L.append(f"{name:<22}| {a:>12} | {b:>12}")
    L.append("-" * 78)
    L.append("CAVEAT: ~1 trading year of data + 126-bar floor + 20d forward eval =>")
    L.append("very few independent trades. This is a PLUMBING / SANITY check, NOT a")
    L.append("deployment decision. Sharpe is annualised from a tiny trade sample and")
    L.append("deflated only crudely; do not read significance into the equal-vs-tuned")
    L.append("gap. No look-ahead in entry signals; no silent date truncation.")
    if window.get("skipped"):
        L.append(f"SKIPPED dates (empty ranking): {window['skipped']}")
    L.append("=" * 78)
    return "\n".join(L)


# ─────────────────────────── orchestration ──────────────────────────────────
def run(start: Optional[str], end: Optional[str], step: int, capital: float,
        use_earnings: bool, turnover_window: int, max_rebalances: Optional[int],
        quiet: bool) -> str:
    bootstrap.require_pg()  # hard-fail on SQLite

    all_dates = trading_dates()
    if not all_dates:
        return "ERROR: no trading dates in daily_bars (PG store empty?)."
    store_first, store_last = all_dates[0], all_dates[-1]

    # Usable window: need >=126 bars of history before the first rebalance AND
    # >=20 trading bars of forward data after the last rebalance.
    floor_i = _engine.MIN_HISTORY_DAYS                 # 126
    fwd_pad = exits.MAX_HOLD_DAYS                       # 20
    lo = start or (all_dates[floor_i] if len(all_dates) > floor_i else all_dates[0])
    hi = end or (all_dates[-1 - fwd_pad] if len(all_dates) > fwd_pad else all_dates[-1])

    window_dates = [d for d in all_dates if lo <= d <= hi]
    rebal_dates = window_dates[::max(1, int(step))]
    if max_rebalances:
        rebal_dates = rebal_dates[: int(max_rebalances)]
    if not rebal_dates:
        return f"ERROR: no rebalance dates in [{lo}, {hi}] with step {step}."

    earn_mode = ("REAL adapter (slow, network)" if use_earnings
                 else "NEUTRALISED (excluded for tractability; both modes identical)")
    if not quiet:
        log.info("[harness] window %s..%s  rebalances=%d step=%d earnings=%s",
                 rebal_dates[0], rebal_dates[-1], len(rebal_dates), step,
                 "real" if use_earnings else "neutralised")

    res_eq = run_mode(rebal_dates, all_dates, "equal", use_earnings, capital,
                      turnover_window, quiet)
    res_tu = run_mode(rebal_dates, all_dates, "tuned", use_earnings, capital,
                      turnover_window, quiet)

    # window length in years from first->last rebalance (forward eval extends it)
    try:
        import pandas as pd
        span_days = (pd.Timestamp(rebal_dates[-1]) - pd.Timestamp(rebal_dates[0])).days + fwd_pad
        years = max(span_days / 365.25, 1e-6)
    except Exception:
        years = max(len(rebal_dates) * step / TRADING_DAYS_PER_YEAR, 1e-6)

    m_eq = compute_metrics(res_eq, years)
    m_tu = compute_metrics(res_tu, years)

    avg_xsec = m_tu.get("avg_xsec")
    if avg_xsec != avg_xsec:  # nan
        avg_xsec = m_eq.get("avg_xsec", float("nan"))

    window = {
        "store_first": store_first, "store_last": store_last,
        "first_rebal": rebal_dates[0], "last_rebal": rebal_dates[-1],
        "n_rebal": len(rebal_dates), "step": step,
        "avg_xsec": avg_xsec if avg_xsec == avg_xsec else 0.0,
        "years": years, "earnings_mode": earn_mode,
        "skipped": len(rebal_dates) - res_tu.rebalances if res_tu.rebalances < len(rebal_dates) else 0,
    }
    table = print_side_by_side(m_eq, m_tu, window)
    return table


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(description="Walk-forward equal-vs-tuned validation (net of costs).")
    p.add_argument("--start", default=None, help="ISO start date (default: auto from 126-bar floor)")
    p.add_argument("--end", default=None, help="ISO end date (default: auto, leaves 20d forward pad)")
    p.add_argument("--step", type=int, default=DEFAULT_STEP, help="trading-day step between rebalances")
    p.add_argument("--capital", type=float, default=DEFAULT_CAPITAL, help="book notional (Rs) for cost sizing")
    p.add_argument("--earnings", action="store_true", help="use REAL (slow, network) earnings adapter")
    p.add_argument("--turnover-window", type=int, default=DEFAULT_TURNOVER_WINDOW, help="bars for median turnover")
    p.add_argument("--max-rebalances", type=int, default=None, help="cap #rebalances (debug)")
    p.add_argument("--quiet", action="store_true", help="suppress per-rebalance logs")
    args = p.parse_args(argv)

    out = run(args.start, args.end, args.step, args.capital, args.earnings,
              args.turnover_window, args.max_rebalances, args.quiet)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
