"""
legacy_cleaned — CONFIG (weights + versions).

Factor grouping differs from scoring_v1: v1's `momentum` (26) and `trend` (20)
are MERGED into a single `technical` factor (40), a NEW `fundamental` quality
factor is added (8), and v1's `risk` factor is dropped from the composite.
The `technical` sub-weights preserve v1's RELATIVE proportions: each sub-feature
keeps its v1 composite-level contribution rescaled by 1/0.46 (since momentum 26 +
trend 20 = 46 points map onto the new technical block of 100).

    e.g. m_rs_rank: v1 momentum sub 30 -> composite contribution = 0.30 * 26 = 7.8
         technical share = 7.8 / 46 * 100 = 16.96 -> rounded to 16.96

All other blocks (smart_money, sector_rs, earnings) are v1 VERBATIM.
"""

# --- Factor weights (sum = 100). Fundamental is scored here (unlike v1's gate). ---
FACTOR_WEIGHTS = {
    "technical": 40, "smart_money": 22, "sector_rs": 16,
    "earnings": 14, "fundamental": 8,
}  # sum 100

# --- Sub-factor weights. Each block is renormalized-per-stock inside the engine. ---
SUBFACTOR_WEIGHTS = {
    # 'technical' = legacy_cleaned's OWN A+D momentum block (technical_ad.py), NOT v1's subs.
    # A) 12-1 momentum + D) relative-strength (RS-line vs benchmark) + RS 52w-high + 21d kicker
    # + RS trend-persistence. Deliberately different from v1's absolute-63d / ADX / volume
    # technicals so the engines diverge while both stay momentum. Sub-weights sum 100.
    "technical": {
        "t_ad_rs_mom_12_1": 30, "t_ad_mom_12_1": 20, "t_ad_rs_52w_prox": 18,
        "t_ad_rs_slope": 14, "t_ad_mom_21": 10, "t_ad_rs_persist": 8,
    },
    "smart_money": {"s_delivery": 35, "s_obv": 25, "s_cmf": 20, "s_volflow": 20},   # v1 verbatim
    "sector_rs":   {"r_rrg": 55, "r_sector_pct": 45},                                # v1 verbatim
    "earnings":    {"e_growth": 35, "e_accel": 30, "e_margin": 20, "e_surprise": 15},# v1 verbatim
    "fundamental": {"f_roce": 35, "f_debt2eq_inv": 25, "f_promoter": 20, "f_roe": 20},  # NEW quality set
}

# --- Eligibility + normalization constants ---
MIN_HISTORY_DAYS = 126
Z_CLIP = 3.0
TOP_N = 25
UNIVERSE_MIN_AVG_TURNOVER_CR = 10

# --- Versioning / provenance ---
MODEL = "legacy_cleaned"
ENGINE_VERSION = "legacy_cleaned-engine-1.0"
WEIGHT_VERSION = "lc-ad-1.0"  # A+D own technical block (12-1 + relative-strength)
SPEC_VERSION = "legacy_cleaned-spec-1.0"
