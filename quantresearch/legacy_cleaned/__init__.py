"""
QuantResearch — legacy_cleaned (THIRD scoring engine).

ADDITIVE and independent of scoring_v1. It REUSES scoring_v1's cross-sectional
math verbatim (compute_symbol_features, _winz_z, _earn_decay imported read-only),
and differs ONLY in: factor grouping/weights, an added Fundamental quality factor,
and per-stock RENORMALIZATION of factor weights over the factors present for that
stock (v1 fills missing factors -> 0; this engine drops + rescales instead).

Does NOT modify analyzer.py or scoring_v1 (both FROZEN).
"""

from .engine import (
    compute_fundamental_features,
    score_universe,
)
from .config import (
    FACTOR_WEIGHTS, SUBFACTOR_WEIGHTS, MIN_HISTORY_DAYS,
    MODEL, ENGINE_VERSION, WEIGHT_VERSION, SPEC_VERSION,
)

__all__ = [
    "compute_fundamental_features", "score_universe",
    "FACTOR_WEIGHTS", "SUBFACTOR_WEIGHTS", "MIN_HISTORY_DAYS",
    "MODEL", "ENGINE_VERSION", "WEIGHT_VERSION", "SPEC_VERSION",
]
