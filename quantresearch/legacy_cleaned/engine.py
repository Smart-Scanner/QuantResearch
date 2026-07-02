"""
legacy_cleaned — ENGINE (THIRD scoring engine for QuantResearch).

ADDITIVE. Does NOT modify analyzer.py or quantresearch/scoring_v1/engine.py
(both FROZEN). It MIRRORS scoring_v1's cross-sectional method exactly by REUSING
v1's functions read-only:

    from quantresearch.scoring_v1.engine import
        compute_symbol_features, _winz_z, _earn_decay

The ONLY differences vs v1 are:
  1) Factor grouping/weights (v1 momentum+trend merged into 'technical'; v1 'risk'
     dropped; NEW 'fundamental' factor added) — see config.py.
  2) An added Fundamental factor computed here (compute_fundamental_features).
  3) PER-STOCK RENORMALIZATION on missing data: v1 fills a missing sub/factor's
     z-score with 0 (neutral). This engine instead treats it as ABSENT and
     rescales the remaining sub/factor weights to sum to 1 for THAT stock.

Zero network. Pure compute on passed-in data. numpy/pandas only.

Run `python engine.py` for a tiny synthetic smoke test.
"""

from __future__ import annotations

import json
import numpy as np
import pandas as pd

# --- v1 math, reused READ-ONLY (do not copy/modify) ---
from quantresearch.scoring_v1.engine import (
    compute_symbol_features,
    _winz_z,
    _earn_decay,
)

from quantresearch.legacy_cleaned.config import (
    FACTOR_WEIGHTS,
    SUBFACTOR_WEIGHTS,
    MIN_HISTORY_DAYS,
)
# legacy_cleaned's OWN technical block (A+D) — replaces v1's m_*/t_* technicals.
from quantresearch.legacy_cleaned.technical_ad import compute_ad_technical

# Sub-features that come from OHLCV (always-expected) — used for data-integrity tier.
# These are exactly v1's technical + smart_money families (no fundamentals/earnings).
OHLCV_FEATURES = (
    list(SUBFACTOR_WEIGHTS["technical"]) + list(SUBFACTOR_WEIGHTS["smart_money"])
)


# ============================== FUNDAMENTAL FEATURES ==============================

_FUND_KEYS = ("f_roce", "f_debt2eq_inv", "f_promoter", "f_roe")


def _clean_num(x):
    """Return float(x) if x is a real, non-zero number; else np.nan.

    CRITICAL missing/0-as-absent rule: universe_catalog has no fragility guard, so a
    spurious 0 must NOT become a real score. Any None/empty/NaN/0 -> ABSENT (np.nan).
    """
    if x is None:
        return np.nan
    if isinstance(x, str):
        x = x.strip()
        if x == "":
            return np.nan
        try:
            x = float(x)
        except (TypeError, ValueError):
            return np.nan
    try:
        v = float(x)
    except (TypeError, ValueError):
        return np.nan
    if np.isnan(v) or v == 0:
        return np.nan
    return v


def compute_fundamental_features(fund):
    """Quality sub-features for one symbol.

    fund : dict | None with keys (any may be None/missing/0):
             roce, debt_to_equity, promoter_pct, roe

    Returns {"f_roce","f_debt2eq_inv","f_promoter","f_roe"} where higher = better:
      f_roce         = roce
      f_promoter     = promoter_pct
      f_roe          = roe
      f_debt2eq_inv  = -debt_to_equity   (lower debt -> higher score)

    Each input is passed through the 0-as-absent rule (None/empty/NaN/0 -> np.nan).
    """
    fund = fund or {}
    roce = _clean_num(fund.get("roce"))
    d2e = _clean_num(fund.get("debt_to_equity"))
    prom = _clean_num(fund.get("promoter_pct"))
    roe = _clean_num(fund.get("roe"))
    return {
        "f_roce": roce,
        "f_debt2eq_inv": (np.nan if np.isnan(d2e) else -d2e),
        "f_promoter": prom,
        "f_roe": roe,
    }


# ============================== SCORING HELPERS ==============================

def _tier(v, hi, lo):
    return "High" if v >= hi else ("Low" if v < lo else "Medium")


def _drivers(row, top=True, k=3):
    s = row.sort_values(ascending=False)
    picks = s.head(k) if top else s.tail(k)[::-1]
    return ", ".join(f"{n} {v:+.2f}" for n, v in picks.items())


def _factor_from_subs(zrow, present_row, subs_weights):
    """Weighted sum of a factor's sub-z, weights RENORMALIZED over PRESENT subs.

    zrow          : dict sub -> z-score (may be NaN; ignored when absent)
    present_row   : dict sub -> bool (raw feature was not NaN)
    subs_weights  : dict sub -> configured weight

    Returns (factor_value, factor_present):
      factor_present = any sub present; if none present -> (np.nan, False).
    """
    present_subs = [s for s in subs_weights if present_row.get(s, False)]
    if not present_subs:
        return np.nan, False
    wsum = sum(subs_weights[s] for s in present_subs)
    if wsum <= 0:
        return np.nan, False
    val = 0.0
    for s in present_subs:
        z = zrow.get(s, np.nan)
        if pd.isna(z):
            z = 0.0  # present-but-neutral (raw existed; z collapsed to 0 by _winz_z)
        val += z * (subs_weights[s] / wsum)
    return val, True


# ============================== SCORING ==============================

def score_universe(price_data, benchmark=None, sector_idx=None,
                   earnings=None, fundamentals=None, mode="tuned"):
    """Score the eligible universe (legacy_cleaned).

    Args mirror v1 plus `fundamentals` (dict[symbol -> dict]).

    Cross-sectional method is v1-identical (z-score + winsorize +-3 via _winz_z,
    earnings decay via _earn_decay), EXCEPT missing factors are dropped and the
    remaining factor weights are renormalized PER STOCK (v1 fills NaN -> 0).

    Returns a DataFrame sorted by composite_z desc.
    """
    fundamentals = fundamentals or {}

    # 1) Per-symbol raw features (v1 math) + fundamentals merged in.
    rows = {}
    for sym, df in price_data.items():
        if len(df) < MIN_HISTORY_DAYS:  # eligibility floor (v1-identical)
            continue
        raw = compute_symbol_features(
            df,
            sec_idx=(sector_idx or {}).get(sym),
            bench=benchmark,
            earn=(earnings or {}).get(sym),
        )
        raw.update(compute_fundamental_features(fundamentals.get(sym)))
        # legacy_cleaned's OWN A+D technical sub-features (t_ad_*) — the technical factor now
        # scores THESE (config SUBFACTOR_WEIGHTS['technical']), not v1's m_*/t_* (still computed
        # by compute_symbol_features above for the smart_money/sector_rs/earnings factors).
        raw.update(compute_ad_technical(df, benchmark))
        rows[sym] = raw
    if not rows:
        return pd.DataFrame()
    raw = pd.DataFrame(rows).T

    # 2) Cross-sectional sub-features (v1-identical).
    raw["m_rs_rank"] = raw["_ret_63"].rank(pct=True) * 100
    raw["r_sector_pct"] = raw["_sec_ret_21"].rank(pct=True) * 100

    # 3) z-score + winsorize every sub via _winz_z. KEEP NaN (do NOT fillna(0)) so
    #    we can detect absent factors per stock. _winz_z leaves NaN inputs as NaN.
    all_subs = [c for d in SUBFACTOR_WEIGHTS.values() for c in d]
    z = pd.DataFrame({c: _winz_z(raw[c]) for c in all_subs}, index=raw.index)

    # 3b) "present" = the RAW feature was not NaN (a real observation existed).
    present = pd.DataFrame(
        {c: raw[c].notna() for c in all_subs}, index=raw.index
    )

    # 4) factor_z[fct] = renormalized weighted sum of PRESENT sub-z per stock.
    #    factor_present[fct] = any sub present.
    factor_z = pd.DataFrame(index=raw.index, columns=list(FACTOR_WEIGHTS), dtype=float)
    factor_present = pd.DataFrame(index=raw.index, columns=list(FACTOR_WEIGHTS), dtype=bool)
    for sym in raw.index:
        zrow = {c: z.at[sym, c] for c in all_subs}
        prow = {c: bool(present.at[sym, c]) for c in all_subs}
        for fct in FACTOR_WEIGHTS:
            val, is_present = _factor_from_subs(zrow, prow, SUBFACTOR_WEIGHTS[fct])
            factor_z.at[sym, fct] = val
            factor_present.at[sym, fct] = is_present

    # 5) Earnings decay (v1-identical), applied only where earnings factor is present.
    decay = raw["_days_since_result"].apply(_earn_decay)
    factor_z["earnings"] = factor_z["earnings"] * decay

    # 6) PER-STOCK RENORMALIZED COMPOSITE.
    #    Drop absent factors; rescale remaining FACTOR_WEIGHTS to sum 1 for THAT stock.
    composite = pd.Series(index=raw.index, dtype=float)
    renorm_weights = {}
    for sym in raw.index:
        present_factors = [f for f in FACTOR_WEIGHTS if bool(factor_present.at[sym, f])]
        wsum = sum(FACTOR_WEIGHTS[f] for f in present_factors)
        if not present_factors or wsum <= 0:
            composite.at[sym] = np.nan
            renorm_weights[sym] = {}
            continue
        eff = {f: FACTOR_WEIGHTS[f] / wsum for f in present_factors}  # sum to 1
        comp = 0.0
        for f in present_factors:
            fv = factor_z.at[sym, f]
            if pd.isna(fv):
                fv = 0.0
            comp += fv * eff[f]
        composite.at[sym] = comp
        # store effective weights re-summed to 100 for readability
        renorm_weights[sym] = {f: round(eff[f] * 100.0, 4) for f in present_factors}

    score = composite.rank(pct=True) * 100

    # 7) Confidence tiers (v1 logic).
    present_ohlcv = raw[OHLCV_FEATURES].notna().mean(axis=1)
    integrity = (0.8 * present_ohlcv + 0.2 * decay).apply(lambda v: _tier(v, 0.85, 0.60))
    disp = factor_z[list(FACTOR_WEIGHTS)].std(axis=1, ddof=0)
    agreement = (1 - disp.rank(pct=True)).apply(lambda v: _tier(v, 0.66, 0.33))

    # 8) Attribution: contribution of each factor to the (renormalized) composite.
    contrib = pd.DataFrame(index=raw.index, columns=list(FACTOR_WEIGHTS), dtype=float)
    for sym in raw.index:
        eff = renorm_weights.get(sym, {})
        for f in FACTOR_WEIGHTS:
            if f in eff and bool(factor_present.at[sym, f]):
                fv = factor_z.at[sym, f]
                contrib.at[sym, f] = (0.0 if pd.isna(fv) else fv) * (eff[f] / 100.0)
            else:
                contrib.at[sym, f] = 0.0

    # 9) Per-factor percentile (factor_z rank pct * 100 per factor).
    pctl = pd.DataFrame(index=raw.index)
    for f in FACTOR_WEIGHTS:
        pctl[f] = factor_z[f].rank(pct=True) * 100

    # 10) Assemble output — MIRRORS v1 columns + additions.
    out = pd.DataFrame({
        "composite_z": composite,
        "score": score.round(1),
        "data_integrity": integrity,
        "signal_agreement": agreement,
    })
    out = out.join(contrib.add_prefix("c_"))
    out = out.join(pctl.add_prefix("pctl_"))
    out["rank"] = out["composite_z"].rank(ascending=False, method="first")
    # rank may contain NaN if a stock had no present factors; keep as-is (Int-safe).
    out["drivers"] = [_drivers(contrib.loc[s], True) for s in out.index]
    out["weaknesses"] = [_drivers(contrib.loc[s], False) for s in out.index]
    out["renorm_weights"] = [json.dumps(renorm_weights.get(s, {})) for s in out.index]

    return out.sort_values("composite_z", ascending=False)


# ============================== SMOKE TEST ==============================

if __name__ == "__main__":
    rng = np.random.default_rng(11)
    dates = pd.bdate_range("2024-01-01", periods=300)

    def make_walk(drift):
        p = 100 * np.exp(np.cumsum(rng.normal(drift, 0.02, len(dates))))
        h = p * (1 + rng.uniform(0, 0.02, len(dates)))
        l = p * (1 - rng.uniform(0, 0.02, len(dates)))
        return pd.DataFrame({
            "open": (h + l) / 2, "high": h, "low": l, "close": p,
            "volume": rng.integers(1e5, 1e6, len(dates)).astype(float),
            "delivery_pct": rng.uniform(20, 80, len(dates)),
        }, index=dates)

    pdata = {f"S{i:02d}": make_walk(rng.normal(0.0003, 0.0004)) for i in range(30)}
    bench = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0002, 0.01, len(dates)))), index=dates)
    sidx = {s: pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0002, 0.012, len(dates)))), index=dates)
            for s in pdata}
    earn = {s: {"rev_growth_yoy": rng.normal(15, 20), "pat_growth_yoy": rng.normal(12, 25),
                "pat_growth_yoy_prev": rng.normal(8, 20), "opm_latest": rng.normal(18, 5),
                "opm_yago": rng.normal(16, 5), "eps_actual": rng.normal(10, 3),
                "eps_consensus": rng.normal(9, 3), "days_since_result": int(rng.integers(0, 120))}
            for s in pdata}

    # Fundamentals present for MOST names; a few deliberately missing / all-zero to
    # exercise the per-stock renormalization path.
    funds = {}
    for i, s in enumerate(pdata):
        if i % 7 == 0:
            funds[s] = None                                   # no fundamentals at all
        elif i % 7 == 1:
            funds[s] = {"roce": 0, "debt_to_equity": 0,       # all zero -> all absent
                        "promoter_pct": 0, "roe": 0}
        else:
            funds[s] = {"roce": rng.uniform(8, 30), "debt_to_equity": rng.uniform(0.1, 2.0),
                        "promoter_pct": rng.uniform(30, 75), "roe": rng.uniform(8, 25)}

    res = score_universe(pdata, bench, sidx, earn, fundamentals=funds, mode="tuned")
    print(res.head(8)[["score", "rank", "data_integrity", "signal_agreement",
                       "c_fundamental", "renorm_weights"]].to_string())

    # Show a name with all-fundamentals-missing: 'fundamental' should be absent and
    # renorm_weights should re-sum the OTHER 4 factors to ~100.
    miss = [s for i, s in enumerate(pdata) if i % 7 in (0, 1)]
    print("\nAll-fundamentals-missing example:", miss[0])
    print("  renorm_weights:", res.loc[miss[0], "renorm_weights"])
    print("  c_fundamental :", res.loc[miss[0], "c_fundamental"])
