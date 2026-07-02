"""
QuantResearch — scoring_v1 (locked swing/momentum scoring engine).

SINGLE SOURCE OF TRUTH = marketos_scoring_final_spec.md + this package's engine.py
(ported verbatim from marketos_scoring_engine_final.py). Do NOT re-tune weights,
"improve" formulas, add factors, or change normalization. Files win.

This package is ADDITIVE and runs in SHADOW by default — it does not modify or
replace analyzer.py's live scoring path. See TRACEABILITY.md for spec->code mapping.
"""

from .engine import (
    FACTOR_WEIGHTS, SUBFACTOR_WEIGHTS, MIN_HISTORY_DAYS,
    compute_symbol_features, score_universe, apply_hysteresis,
    factor_correlation_monitor,
)

__all__ = [
    "FACTOR_WEIGHTS", "SUBFACTOR_WEIGHTS", "MIN_HISTORY_DAYS",
    "compute_symbol_features", "score_universe", "apply_hysteresis",
    "factor_correlation_monitor",
]
