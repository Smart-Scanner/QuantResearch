"""
validation - ADDITIVE evaluation toolkit for the locked scoring engine.
=======================================================================
Pure, side-effect-free building blocks used by the walk-forward / equal-vs-
tuned validation harness. Nothing in this package modifies the engine,
adapter, gates, or any live screener module.

Modules
-------
- costs : realistic Indian NSE CASH (delivery) round-trip transaction-cost
          model (STT, exchange txn, SEBI, GST, stamp duty, slippage).
- exits : point-in-time swing exit rules (hard stop, chandelier/EMA trail,
          time stop, momentum-fade) for backtest position management.

Both modules are 0-DB and import-light so the harness can unit-test them in
isolation. The harness itself (the thing that actually queries PostgreSQL)
MUST import quantresearch.scoring_v1.bootstrap first and call require_pg().
"""

from __future__ import annotations

__all__ = ["costs", "exits"]
