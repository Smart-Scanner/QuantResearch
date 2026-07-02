"""
costs.py - Realistic Indian NSE CASH (delivery) round-trip cost model.
======================================================================
Pure, 0-DB helper used by the validation harness to charge a realistic
round-trip transaction cost against simulated swing trades. Models the
actual statutory + brokerage + market-impact components of an NSE *delivery*
(CNC / cash-segment) equity trade as of the modelling date.

ROLLBACK-SAFETY: ADDITIVE. Imports nothing from the engine/adapter/live
screener. No DB access, no I/O, no global state mutation.

SCOPE / ASSUMPTIONS
-------------------
- Segment: NSE EQUITY CASH, DELIVERY (positional / swing). NOT intraday,
  NOT F&O - those have different STT and charge structures.
- "Round trip" = one BUY leg + one SELL leg of equal notional (we evaluate
  both legs on their own notional; buy_notional and sell_notional may differ
  if the price moved, and callers can pass both).
- All rates are module-level constants, documented and configurable. The
  defaults follow the public NSE / SEBI / exchange schedules; tweak the
  constants (or pass overrides) if a schedule changes.

COMPONENTS (delivery, per current Indian schedule)
--------------------------------------------------
- Brokerage      : discount-broker delivery is typically Rs 0 -> 0 bps
                   default (configurable via BROKERAGE_BPS).
- STT            : 0.1% on BUY + 0.1% on SELL (delivery).
- Exchange txn   : NSE ~0.00297% on turnover (per leg).
- SEBI charges   : Rs 10 per crore of turnover (per leg) = 0.0001%.
- GST            : 18% on (brokerage + exchange txn charge) - NOT on STT /
                   stamp / SEBI (GST applies only to the service charges).
- Stamp duty     : 0.015% on BUY only.
- Slippage       : a base half-spread plus a size-vs-liquidity impact term,
                   charged on BOTH legs (you pay impact entering and exiting).

SLIPPAGE MODEL
--------------
    slippage_bps(per leg) = SLIP_BASE_BPS
                          + min(SLIP_CAP_BPS,
                                SLIP_K * (order_notional / median_daily_turnover))
where median_daily_turnover is in rupees (median_turnover_cr * 1e7).
    - SLIP_BASE_BPS : a small fixed half-spread you cross on a marketable
                      order even for tiny size (liquid large-cap default).
    - SLIP_K        : converts "fraction of a day's liquidity you consume"
                      into bps of impact. SLIP_K * 1.0 = bps of impact if your
                      order equals one full day's turnover (linear, then
                      capped). Calibrated conservatively for daily swing fills.
    - SLIP_CAP_BPS  : hard ceiling so an illiquid / tiny-turnover name cannot
                      produce an absurd cost; the trade should simply be sized
                      down or skipped by the harness liquidity gate.

ALL public functions return cost expressed as a FRACTION of notional
(e.g. 0.0023 == 23 bps) unless the name ends in `_bps`. round_trip_cost_bps
returns bps (its name says so) and also yields a breakdown dict.
"""

from __future__ import annotations

from typing import Optional, Dict


# ───────────────────────── statutory / broker rates ─────────────────────────
# All "_RATE" constants are fractions of the leg's notional (turnover) unless
# noted. All "_BPS" constants are basis points (1 bp = 0.0001 = 0.01%).

# Brokerage: discount-broker delivery default = 0. Configurable.
BROKERAGE_BPS: float = 0.0                    # bps per leg

# Securities Transaction Tax (delivery): 0.1% each on BUY and SELL.
STT_RATE_BUY: float = 0.001                   # 0.10% of buy notional
STT_RATE_SELL: float = 0.001                  # 0.10% of sell notional

# NSE exchange transaction charge (cash/equity): ~0.00297% of turnover/leg.
EXCHANGE_TXN_RATE: float = 0.0000297          # per leg

# SEBI turnover fee: Rs 10 per crore = 10 / 1e7 of turnover/leg.
SEBI_RATE: float = 10.0 / 1e7                 # = 0.000001 per leg

# GST: 18% on (brokerage + exchange txn charge) only.
GST_RATE: float = 0.18

# Stamp duty (delivery): 0.015% on BUY only.
STAMP_DUTY_RATE_BUY: float = 0.00015          # 0.015% of buy notional

# ───────────────────────── slippage model ─────────────────────────
# Base half-spread crossed on any marketable order (liquid large-cap default).
SLIP_BASE_BPS: float = 3.0                    # bps per leg
# Impact coefficient: bps of impact when order == one full day's turnover.
SLIP_K: float = 50.0                          # bps per (order/turnover) unit
# Hard ceiling on the size-driven impact term (excludes the base half-spread).
SLIP_CAP_BPS: float = 75.0                    # bps per leg

_CR = 1e7  # 1 crore in rupees


# ───────────────────────── per-leg helpers ─────────────────────────

def slippage_bps(order_notional: float, median_turnover_cr: float) -> float:
    """
    Per-leg slippage in bps = base half-spread + capped size/liquidity impact.

    order_notional      : rupee value of THIS leg.
    median_turnover_cr  : symbol's median daily traded VALUE in Rs crore.
                          <= 0 / None -> treat as maximally illiquid (cap).
    """
    if order_notional is None or order_notional <= 0:
        return float(SLIP_BASE_BPS)
    if not median_turnover_cr or median_turnover_cr <= 0:
        # No liquidity info -> assume worst case impact.
        return float(SLIP_BASE_BPS + SLIP_CAP_BPS)
    turnover_rs = median_turnover_cr * _CR
    participation = order_notional / turnover_rs       # fraction of a day's turnover
    impact = min(SLIP_CAP_BPS, SLIP_K * participation)
    return float(SLIP_BASE_BPS + impact)


def buy_leg_cost(notional: float, median_turnover_cr: float) -> Dict[str, float]:
    """
    Statutory + broker + slippage cost of a single BUY leg, in RUPEES.
    Returns a breakdown dict (keys are rupee amounts) plus 'total'.
    """
    notional = float(notional or 0.0)
    brokerage = notional * (BROKERAGE_BPS / 1e4)
    stt = notional * STT_RATE_BUY
    exch = notional * EXCHANGE_TXN_RATE
    sebi = notional * SEBI_RATE
    gst = (brokerage + exch) * GST_RATE
    stamp = notional * STAMP_DUTY_RATE_BUY
    slip = notional * (slippage_bps(notional, median_turnover_cr) / 1e4)
    total = brokerage + stt + exch + sebi + gst + stamp + slip
    return {
        "brokerage": brokerage,
        "stt": stt,
        "exchange_txn": exch,
        "sebi": sebi,
        "gst": gst,
        "stamp_duty": stamp,
        "slippage": slip,
        "total": total,
    }


def sell_leg_cost(notional: float, median_turnover_cr: float) -> Dict[str, float]:
    """
    Statutory + broker + slippage cost of a single SELL leg, in RUPEES.
    SELL has STT but NO stamp duty. Returns a breakdown dict + 'total'.
    """
    notional = float(notional or 0.0)
    brokerage = notional * (BROKERAGE_BPS / 1e4)
    stt = notional * STT_RATE_SELL
    exch = notional * EXCHANGE_TXN_RATE
    sebi = notional * SEBI_RATE
    gst = (brokerage + exch) * GST_RATE
    stamp = 0.0  # stamp duty is buy-side only
    slip = notional * (slippage_bps(notional, median_turnover_cr) / 1e4)
    total = brokerage + stt + exch + sebi + gst + stamp + slip
    return {
        "brokerage": brokerage,
        "stt": stt,
        "exchange_txn": exch,
        "sebi": sebi,
        "gst": gst,
        "stamp_duty": stamp,
        "slippage": slip,
        "total": total,
    }


# ───────────────────────── round-trip API ─────────────────────────

def round_trip_cost_bps(
    notional: float,
    median_turnover_cr: float,
    side_qty: Optional[float] = None,
    sell_notional: Optional[float] = None,
) -> Dict[str, object]:
    """
    Realistic NSE delivery round-trip cost (BUY leg + SELL leg).

    Parameters
    ----------
    notional            : BUY-leg rupee notional (qty * entry_price). This is
                          the reference notional the returned bps is expressed
                          against.
    median_turnover_cr  : symbol's median daily traded value in Rs crore,
                          used by the slippage/impact term.
    side_qty            : OPTIONAL share quantity. If given AND sell_notional
                          is None, the sell leg is assumed to be the same
                          quantity at the same price (= notional). Provided for
                          API symmetry / future per-share fee extensions; the
                          current schedule is purely ad-valorem so qty does not
                          change the bps result on its own.
    sell_notional       : OPTIONAL explicit SELL-leg rupee notional (qty *
                          exit_price). Defaults to `notional` (flat round trip).

    Returns
    -------
    dict with:
      'total_bps'      : round-trip cost in basis points of the BUY notional.
      'total_fraction' : same as a fraction (total_bps / 1e4).
      'total_rupees'   : total round-trip cost in rupees.
      'buy'            : per-leg rupee breakdown dict (see buy_leg_cost).
      'sell'           : per-leg rupee breakdown dict (see sell_leg_cost).
      'reference_notional' : the notional the bps is expressed against.
    """
    buy_notional = float(notional or 0.0)
    if sell_notional is None:
        sell_notional = buy_notional
    sell_notional = float(sell_notional)

    buy = buy_leg_cost(buy_notional, median_turnover_cr)
    sell = sell_leg_cost(sell_notional, median_turnover_cr)

    total_rupees = buy["total"] + sell["total"]
    ref = buy_notional if buy_notional > 0 else 1.0
    total_fraction = total_rupees / ref
    return {
        "total_bps": total_fraction * 1e4,
        "total_fraction": total_fraction,
        "total_rupees": total_rupees,
        "buy": buy,
        "sell": sell,
        "reference_notional": buy_notional,
        "side_qty": side_qty,
    }


def round_trip_cost_fraction(
    notional: float,
    median_turnover_cr: float,
    side_qty: Optional[float] = None,
    sell_notional: Optional[float] = None,
) -> float:
    """Convenience: round-trip cost as a FRACTION of the buy notional."""
    return float(
        round_trip_cost_bps(
            notional, median_turnover_cr, side_qty=side_qty, sell_notional=sell_notional
        )["total_fraction"]
    )


# ───────────────────────── self-check ─────────────────────────
# 0-DB module: no bootstrap/require_pg needed (no PostgreSQL access here).
if __name__ == "__main__":
    # Liquid large-cap: Rs 1,00,000 order in a name doing Rs 500 cr/day.
    liquid = round_trip_cost_bps(100_000, median_turnover_cr=500.0)
    # Thin small-cap: same order in a name doing Rs 2 cr/day (impact dominates).
    thin = round_trip_cost_bps(100_000, median_turnover_cr=2.0)
    # No-liquidity sentinel -> worst-case slippage.
    unknown = round_trip_cost_bps(100_000, median_turnover_cr=0.0)

    print("=== NSE delivery round-trip cost self-check ===")
    print(f"Liquid (500cr): {liquid['total_bps']:.2f} bps  "
          f"(Rs {liquid['total_rupees']:.2f} on Rs 1,00,000)")
    print(f"Thin   (2cr)  : {thin['total_bps']:.2f} bps  "
          f"(Rs {thin['total_rupees']:.2f})")
    print(f"Unknown liq.  : {unknown['total_bps']:.2f} bps  (worst-case slip)")

    # Sanity assertions: STT alone is 20 bps round-trip (0.1% x 2), so total
    # must exceed 20 bps; thin must cost more than liquid; cap must bind.
    assert liquid["total_bps"] > 20.0, "round-trip must exceed pure STT (20 bps)"
    assert thin["total_bps"] > liquid["total_bps"], "thin must cost more than liquid"
    # Per-leg slippage cap = base + cap = 78 bps -> round-trip slip <= 156 bps.
    assert unknown["total_bps"] <= 20.0 + 2 * (SLIP_BASE_BPS + SLIP_CAP_BPS) + 5.0, \
        "slippage cap must bound worst case"
    print("self-check OK")
