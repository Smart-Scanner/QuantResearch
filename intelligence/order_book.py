"""
Order Book Proxy Engine
-----------------------
- Estimates revenue visibility via forward revenue × growth rate vs market cap
- Capex deployment and FCF signals
- Especially meaningful for Defence, Railways, Infra, Capital Goods stocks
  (where public order book disclosures exist in news — GDELT enriches this)
"""

import logging
# yfinance removed — fundamentals dict from universe_catalog has all data

log = logging.getLogger("screener")


def get_order_book_proxy(symbol: str, fundamentals: dict) -> dict:
    """
    Estimates order book visibility.
    Returns {ob_to_mcap, ob_score, signals, capex, free_cash}.

    Uses fundamentals dict already fetched from universe_catalog (Dhan data).
    """
    try:
        total_rev       = fundamentals.get("total_revenue") or 0
        rev_growth_pct  = fundamentals.get("revenue_growth") or 0   # already as %
        rev_growth_rate = rev_growth_pct / 100                       # decimal form
        capex           = abs(fundamentals.get("capex") or 0)
        free_cash       = fundamentals.get("free_cash_flow") or 0
        mcap            = fundamentals.get("market_cap") or 0
        sector          = (fundamentals.get("sector") or "").lower()
        industry        = (fundamentals.get("industry") or "").lower()

        ob_score = 0
        signals = []

        # Order book to market cap ratio
        # Forward revenue estimate = total_rev × (1 + max(rev_growth, 0) × 2)
        ob_to_mcap = None
        if total_rev and mcap:
            # Estimated forward revenue (labelled clearly as proxy)
            fwd_rev = total_rev * (1 + max(rev_growth_rate, 0) * 2)
            ob_to_mcap = round(fwd_rev / mcap, 3)

            if ob_to_mcap > 2.0:
                ob_score += 12
                signals.append(f"OB/MCap: {ob_to_mcap:.2f}x (Excellent Visibility)")
            elif ob_to_mcap > 1.5:
                ob_score += 10
                signals.append(f"OB/MCap: {ob_to_mcap:.2f}x (High Visibility)")
            elif ob_to_mcap > 0.8:
                ob_score += 5
                signals.append(f"OB/MCap: {ob_to_mcap:.2f}x (Moderate)")

        # Revenue growth = strong order execution
        if rev_growth_rate and rev_growth_rate > 0.30:
            ob_score += 8
            signals.append(f"Revenue Growth {rev_growth_rate*100:.1f}% — Order Execution")
        elif rev_growth_rate and rev_growth_rate > 0.15:
            ob_score += 4

        # Capex deployment = capacity build (future revenue locked in)
        if capex and total_rev and total_rev > 0:
            capex_ratio = capex / total_rev
            if capex_ratio > 0.15:
                ob_score += 5
                signals.append(f"High Capex ({capex_ratio*100:.1f}%) — Capacity Expansion")
            elif capex_ratio > 0.08:
                ob_score += 2

        # Positive FCF = cash from confirmed orders
        if free_cash and free_cash > 0:
            ob_score += 3
            signals.append("Positive Free Cash Flow ✅")

        # Sector-specific bonus: only genuine order-book businesses
        high_visibility_sectors = [
            "defence", "railway", "capital goods",
            "industrial", "engineering", "heavy electrical",
        ]
        if any(s in (sector + " " + industry) for s in high_visibility_sectors):
            ob_score += 5
            signals.append("Sector: Backlog-Visible Business")

        ob_score = min(ob_score, 25)

        return {
            "ob_to_mcap": ob_to_mcap,
            "ob_score": ob_score,
            "signals": signals,
            "capex": capex,
            "free_cash": free_cash,
        }

    except Exception as exc:
        log.debug("OrderBook proxy failed for %s: %s", symbol, exc)
        return {"ob_to_mcap": None, "ob_score": 0, "signals": [], "capex": 0, "free_cash": 0}
