"""
stock_store.py — the ONE consolidated "ready data layer" read interface (Stage 2).
================================================================================
ADDITIVE / READ-ONLY. Assembles the full per-symbol record from the 5 stores with
ZERO external network (local DB / file reads only):

    price        <- pit_loader.load_price_df        (daily_bars)
    earnings     <- earnings_adapter.build_earnings  (earnings_store -> dhan_forecast map)
    fundamentals <- data_store.get_fundamentals      (universe_catalog, Dhan-enriched)
    metadata     <- universe_catalog                 (mcap/sector/isin/company_name)

This is the single loader path every engine reads through for the consolidated view.
"""
from __future__ import annotations

import logging

log = logging.getLogger("screener")


def get_consolidated(symbol: str, as_of: str | None = None) -> dict:
    """One consolidated record for a symbol. Local-only; no fetch/scrape."""
    from . import adapter, earnings_adapter, data_store
    import db

    clean = (symbol or "").upper().replace(".NS", "").strip()
    if as_of is None:
        row = db.execute_db("SELECT MAX(date) d FROM daily_bars", fetch="one", require_pg=True)
        as_of = str(row["d"])[:10] if row and row.get("d") else None

    # price (daily_bars)
    try:
        df = adapter.pit_loader.load_price_df(clean, as_of)
        price = {
            "bars": int(len(df)) if df is not None else 0,
            "last_close": float(df["close"].iloc[-1]) if df is not None and len(df) else None,
            "last_date": str(df.index[-1])[:10] if df is not None and len(df) else None,
        }
    except Exception as exc:
        price = {"bars": 0, "error": str(exc)[:80]}

    # earnings (earnings_store via the adapter map)
    try:
        earnings = earnings_adapter.build_earnings(clean, as_of)
    except Exception as exc:
        earnings = {"error": str(exc)[:80]}

    # fundamentals + metadata (universe_catalog, Dhan-enriched)
    fundamentals = data_store.get_fundamentals(clean) or {}

    return {
        "symbol": clean, "as_of": as_of,
        "metadata": {k: fundamentals.get(k) for k in ("company_name", "isin", "market_cap", "sector", "dhan_sid")},
        "price": price,
        "earnings": earnings,
        "fundamentals": {k: fundamentals.get(k) for k in
                         ("pe", "pb", "roe", "roce", "eps", "div_yield", "industry_pe",
                          "revenue", "free_cash_flow", "net_profit_margin",
                          "debt_to_equity", "promoter_pct")},
    }
