"""
MarketOS - Scoring Engine (FINAL, locked)
Swing / Momentum, 0-4 week holds, NSE cash.

SINGLE SOURCE OF TRUTH for the scoring mechanism. Every weight, formula, window
and direction below MUST match marketos_scoring_final_spec.md exactly.
DO NOT hand-tune weights - validate them (WEIGHT_MODE) instead.

PORTED VERBATIM from marketos_scoring_engine_final.py. Only comment/docstring
glyphs were normalized to ASCII (em-dash -> -, arrows -> ->) for Windows/cp1252
safety; ALL code, formulas, weights, windows and constants are unchanged.

INPUT (point-in-time as of latest bar):
  price_data : dict[symbol -> DataFrame]   date-ascending, cols:
                                            open, high, low, close, volume, delivery_pct
  benchmark  : Series   broad-benchmark close, date-ascending (e.g. Nifty 500)
  sector_idx : dict[symbol -> Series]       the stock's sector-index close, date-ascending
  earnings   : dict[symbol -> dict]         keys (any may be None/missing):
                 rev_growth_yoy, pat_growth_yoy, pat_growth_yoy_prev,
                 opm_latest, opm_yago, eps_actual, eps_consensus, eps_trend,
                 days_since_result

Universe + quality gates are applied UPSTREAM (spec section 1). This engine
applies ONLY the >=126-bar eligibility floor, then scores what remains.

Run `python engine.py` for a synthetic smoke test.
"""

from __future__ import annotations
import numpy as np
import pandas as pd

# ============================== CONFIG (LOCKED) ==============================

FACTOR_WEIGHTS = {                       # sum 100; Fundamental = gate, not scored
    "momentum": 26, "trend": 20, "smart_money": 18,
    "sector_rs": 14, "earnings": 12, "risk": 10,
}

SUBFACTOR_WEIGHTS = {                     # each block sums 100; all features HIGHER = BETTER
    "momentum":    {"m_rs_rank": 30, "m_mom_1m3m": 25, "m_52w_prox": 20, "m_rvol": 15, "m_fip": 10},
    "trend":       {"t_ema_stack": 25, "t_hh_hl": 25, "t_adx": 20, "t_slope": 15, "t_persistence": 15},
    "smart_money": {"s_delivery": 35, "s_obv": 25, "s_cmf": 20, "s_volflow": 20},
    "sector_rs":   {"r_rrg": 55, "r_sector_pct": 45},
    "earnings":    {"e_growth": 35, "e_accel": 30, "e_margin": 20, "e_surprise": 15},
    "risk":        {"v_atr_fit": 30, "v_compression": 25, "v_gap_safety": 25, "v_dd_stability": 20},
}

MIN_HISTORY_DAYS = 126                    # eligibility floor
WIN_52W          = 252                    # 52w-high window (uses min(252, len) -> graceful)
EARN_FRESH_DAYS  = 10
EARN_STALE_DAYS  = 75
Z_CLIP           = 3.0
ATR_BAND         = (0.02, 0.05)           # ATR% sweet-spot
TOP_N            = 25                      # hysteresis buy band
HYSTERESIS_MULT  = 2.0                     # hold until rank exits top (TOP_N * mult)
CORR_WARN        = 0.85

# Features used for the Data-Integrity tier (the always-expected OHLCV ones)
OHLCV_FEATURES = (list(SUBFACTOR_WEIGHTS["momentum"]) + list(SUBFACTOR_WEIGHTS["trend"])
                  + list(SUBFACTOR_WEIGHTS["smart_money"]) + list(SUBFACTOR_WEIGHTS["risk"]))

# ============================== INDICATORS ==============================

def _ema(s, span):
    return s.ewm(span=span, adjust=False).mean()

def _atr(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()

def _adx(df, n=14):                       # Wilder ADX
    h, l, c = df["high"], df["low"], df["close"]
    up, dn = h.diff(), -l.diff()
    plus  = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atrn = tr.ewm(alpha=1 / n, adjust=False).mean()
    pdi = 100 * plus.ewm(alpha=1 / n, adjust=False).mean() / atrn
    mdi = 100 * minus.ewm(alpha=1 / n, adjust=False).mean() / atrn
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()

def _obv(df):
    return (np.sign(df["close"].diff()).fillna(0) * df["volume"]).cumsum()

def _cmf(df, n=20):
    h, l, c, v = df["high"], df["low"], df["close"], df["volume"]
    rng = (h - l).replace(0, np.nan)
    mfv = ((c - l) - (h - c)) / rng * v
    return mfv.rolling(n).sum() / v.rolling(n).sum()

def _max_drawdown(close, n=60):
    w = close.iloc[-n:]
    dd = w / w.cummax() - 1.0
    return float(-dd.min())               # positive number

def _swing_structure(df, lookback=60, k=3):
    h = df["high"].iloc[-lookback:].values
    l = df["low"].iloc[-lookback:].values
    n = len(h)
    sh, sl = [], []
    for i in range(k, n - k):
        if h[i] == max(h[i - k:i + k + 1]): sh.append(h[i])
        if l[i] == min(l[i - k:i + k + 1]): sl.append(l[i])
    def frac_rising(x):
        if len(x) < 2: return 0.5
        return sum(b > a for a, b in zip(x, x[1:])) / (len(x) - 1)
    structure = 0.5 * (frac_rising(sh) + frac_rising(sl))
    rng = df["high"] - df["low"]
    bonus = 0.0
    if len(rng) >= 7 and rng.iloc[-1] == rng.iloc[-7:].min():                       # NR7
        bonus += 0.1
    if len(rng) >= 20 and rng.iloc[-10:].mean() < 0.6 * rng.iloc[-20:-10].mean():   # tight base
        bonus += 0.1
    return min(1.0, structure + bonus)

def _ret(close, n):
    return close.iloc[-1] / close.iloc[-1 - n] - 1.0 if len(close) > n else np.nan

# ============================== PER-SYMBOL FEATURES ==============================

def compute_symbol_features(df, sec_idx=None, bench=None, earn=None):
    """Raw features for one symbol. Cross-sectional ones (_ret_63, _sec_ret_21)
    are finalized at universe level in score_universe()."""
    c, v, n = df["close"], df["volume"], len(df)
    f = {}

    # --- Momentum ---
    f["_ret_63"]    = _ret(c, 63)
    r21 = (c.iloc[-6] / c.iloc[-27] - 1.0) if n > 27 else np.nan
    r63 = (c.iloc[-6] / c.iloc[-69] - 1.0) if n > 69 else np.nan
    f["m_mom_1m3m"] = np.nan if (np.isnan(r21) or np.isnan(r63)) else 0.5 * r21 + 0.5 * r63
    W = min(WIN_52W, n)
    f["m_52w_prox"] = min(1.0, c.iloc[-1] / df["high"].iloc[-W:].max())
    f["m_rvol"]     = v.iloc[-5:].mean() / v.iloc[-50:].mean() if n >= 50 else np.nan
    dr = c.pct_change().iloc[-63:].dropna()
    f["m_fip"] = (np.sign(f["_ret_63"]) * ((dr > 0).mean() - (dr < 0).mean())
                  if (len(dr) >= 30 and not np.isnan(f["_ret_63"])) else np.nan)

    # --- Trend ---
    e20, e50, e100, e200 = _ema(c, 20), _ema(c, 50), _ema(c, 100), _ema(c, 200)
    f["t_ema_stack"]   = float(np.mean([c.iloc[-1] > e20.iloc[-1], e20.iloc[-1] > e50.iloc[-1],
                                        e50.iloc[-1] > e100.iloc[-1], e100.iloc[-1] > e200.iloc[-1]]))
    f["t_hh_hl"]       = _swing_structure(df)
    f["t_adx"]         = min(50.0, _adx(df).iloc[-1])
    f["t_slope"]       = (e50.iloc[-1] - e50.iloc[-21]) / c.iloc[-1] if n > 21 else np.nan
    f["t_persistence"] = (c.iloc[-50:] > e50.iloc[-50:]).mean()

    # --- Smart Money ---
    f["s_delivery"] = df["delivery_pct"].iloc[-5:].mean() if "delivery_pct" in df else np.nan
    obv = _obv(df)
    f["s_obv"]      = (obv.iloc[-1] - obv.iloc[-21]) / v.iloc[-20:].mean() if n > 21 else np.nan
    f["s_cmf"]      = _cmf(df).iloc[-1]
    sgn = np.sign(c.diff()).fillna(0).iloc[-20:]
    f["s_volflow"]  = (v.iloc[-20:] * sgn).sum() / v.iloc[-20:].sum()

    # --- Sector RS ---
    f["r_rrg"] = np.nan; f["_sec_ret_21"] = np.nan
    if sec_idx is not None and bench is not None:
        sec = sec_idx.reindex(df.index).ffill()
        bn  = bench.reindex(df.index).ffill()
        rs = (sec / bn).dropna()
        if len(rs) > 73:
            rs_ratio = 100 * rs / rs.rolling(63).mean()
            rs_mom   = 100 * rs_ratio / rs_ratio.rolling(10).mean()
            f["r_rrg"] = (rs_ratio.iloc[-1] - 100) + (rs_mom.iloc[-1] - 100)
            if len(sec.dropna()) > 22:
                f["_sec_ret_21"] = sec.iloc[-1] / sec.iloc[-22] - 1.0

    # --- Earnings (raw; decay applied later) ---
    if earn:
        rg, pg = earn.get("rev_growth_yoy"), earn.get("pat_growth_yoy")
        f["e_growth"] = (np.nan if (rg is None and pg is None)
                         else 0.5 * (rg or 0) + 0.5 * (pg or 0))
        pgp = earn.get("pat_growth_yoy_prev")
        f["e_accel"]  = (pg - pgp) if (pg is not None and pgp is not None) else np.nan
        ol, oy = earn.get("opm_latest"), earn.get("opm_yago")
        f["e_margin"] = (ol - oy) if (ol is not None and oy is not None) else np.nan
        ea, ec, et = earn.get("eps_actual"), earn.get("eps_consensus"), earn.get("eps_trend")
        if ea is not None and ec not in (None, 0):
            f["e_surprise"] = (ea - ec) / abs(ec)
        elif ea is not None and et not in (None, 0):
            f["e_surprise"] = (ea - et) / abs(et)
        else:
            f["e_surprise"] = np.nan
        f["_days_since_result"] = earn.get("days_since_result", np.nan)
    else:
        for kk in ("e_growth", "e_accel", "e_margin", "e_surprise"):
            f[kk] = np.nan
        f["_days_since_result"] = np.nan

    # --- Risk (higher = safer / coiled) ---
    atr = _atr(df)
    atr_pct = atr.iloc[-1] / c.iloc[-1]
    lo, hi = ATR_BAND
    f["v_atr_fit"] = (atr_pct / lo if atr_pct < lo
                      else max(0.0, 1 - (atr_pct - hi) / hi) if atr_pct > hi else 1.0)
    f["v_compression"]  = (atr.iloc[-50:].mean() / atr.iloc[-1] - 1.0) if n >= 50 else np.nan
    gap = (df["open"] - c.shift()).abs() / c.shift()
    f["v_gap_safety"]   = -gap.iloc[-60:].mean()
    f["v_dd_stability"] = -_max_drawdown(c, 60) if n >= 60 else np.nan

    f["_history_days"] = n
    return f

# ============================== SCORING ==============================

def _winz_z(s):
    s = s.astype(float)
    mu, sd = s.mean(skipna=True), s.std(skipna=True, ddof=0)
    if not sd or np.isnan(sd):
        return pd.Series(0.0, index=s.index)
    return ((s - mu) / sd).clip(-Z_CLIP, Z_CLIP)

def _earn_decay(d):
    if d is None or (isinstance(d, float) and np.isnan(d)):
        return 0.0
    if d <= EARN_FRESH_DAYS: return 1.0
    if d >= EARN_STALE_DAYS: return 0.0
    return 1 - (d - EARN_FRESH_DAYS) / (EARN_STALE_DAYS - EARN_FRESH_DAYS)

def _weights(mode):
    if mode == "equal":
        fw = {k: 1 / len(FACTOR_WEIGHTS) for k in FACTOR_WEIGHTS}
        sw = {f: {s: 1 / len(d) for s in d} for f, d in SUBFACTOR_WEIGHTS.items()}
        return fw, sw
    fs = sum(FACTOR_WEIGHTS.values())
    fw = {k: v / fs for k, v in FACTOR_WEIGHTS.items()}
    sw = {f: {s: w / sum(d.values()) for s, w in d.items()} for f, d in SUBFACTOR_WEIGHTS.items()}
    return fw, sw

def _tier(v, hi, lo):
    return "High" if v >= hi else ("Low" if v < lo else "Medium")

def _drivers(row, top=True, k=3):
    s = row.sort_values(ascending=False)
    picks = s.head(k) if top else s.tail(k)[::-1]
    return ", ".join(f"{n} {v:+.2f}" for n, v in picks.items())

def score_universe(price_data, benchmark=None, sector_idx=None, earnings=None, mode="tuned"):
    """Score the eligible universe. mode = 'tuned' | 'equal'. Returns DataFrame, composite desc."""
    fw, sw = _weights(mode)
    rows = {}
    for sym, df in price_data.items():
        if len(df) < MIN_HISTORY_DAYS:                       # eligibility floor
            continue
        rows[sym] = compute_symbol_features(
            df, sec_idx=(sector_idx or {}).get(sym),
            bench=benchmark, earn=(earnings or {}).get(sym))
    if not rows:
        return pd.DataFrame()
    raw = pd.DataFrame(rows).T

    # cross-sectional features
    raw["m_rs_rank"]    = raw["_ret_63"].rank(pct=True) * 100
    raw["r_sector_pct"] = raw["_sec_ret_21"].rank(pct=True) * 100

    # normalize every sub-feature (z, winsorize, NaN -> 0 neutral)
    all_subs = [c for d in SUBFACTOR_WEIGHTS.values() for c in d]
    z = pd.DataFrame({c: _winz_z(raw[c]).fillna(0.0) for c in all_subs}, index=raw.index)

    # factor_z -> earnings decay -> composite
    factor_z = pd.DataFrame(index=raw.index)
    for fct, subs in SUBFACTOR_WEIGHTS.items():
        factor_z[fct] = sum(z[c] * sw[fct][c] for c in subs)
    decay = raw["_days_since_result"].apply(_earn_decay)
    factor_z["earnings"] = factor_z["earnings"] * decay
    composite = sum(factor_z[f] * fw[f] for f in FACTOR_WEIGHTS)
    score = composite.rank(pct=True) * 100

    # confidence (display only)
    present = raw[OHLCV_FEATURES].notna().mean(axis=1)
    integrity = (0.8 * present + 0.2 * decay).apply(lambda v: _tier(v, 0.85, 0.60))
    disp = factor_z[list(fw)].std(axis=1, ddof=0)
    agreement = (1 - disp.rank(pct=True)).apply(lambda v: _tier(v, 0.66, 0.33))

    # attribution
    contrib = pd.DataFrame({f: factor_z[f] * fw[f] for f in FACTOR_WEIGHTS}, index=raw.index)

    out = pd.DataFrame({
        "composite_z": composite, "score": score.round(1),
        "data_integrity": integrity, "signal_agreement": agreement,
    }).join(contrib.add_prefix("c_"))
    out["rank"]       = out["composite_z"].rank(ascending=False, method="first").astype(int)
    out["drivers"]    = [_drivers(contrib.loc[s], True)  for s in out.index]
    out["weaknesses"] = [_drivers(contrib.loc[s], False) for s in out.index]
    return out.sort_values("composite_z", ascending=False)

# ============================== POST-SCORING HELPERS ==============================

def apply_hysteresis(ranked, held):
    """Buy on entering top-N; hold until rank exits top-(N*mult). Returns buy/exit/portfolio."""
    hold_cut = int(TOP_N * HYSTERESIS_MULT)
    buy  = set(ranked.index[ranked["rank"] <= TOP_N])
    keep = set(ranked.index[ranked["rank"] <= hold_cut])
    held = set(held)
    return {"buy": sorted(buy - held),
            "exit": sorted(held - keep),
            "portfolio": sorted((held & keep) | buy)}

def factor_correlation_monitor(factor_z_history):
    """MONITORING ONLY (not in score). factor_z_history: DataFrame [dates x 6 factor_z]."""
    corr = factor_z_history.corr()
    warn = [(a, b, round(corr.loc[a, b], 2))
            for i, a in enumerate(corr.columns) for b in corr.columns[i + 1:]
            if abs(corr.loc[a, b]) > CORR_WARN]
    return corr, warn

# ============================== SMOKE TEST ==============================

if __name__ == "__main__":
    rng = np.random.default_rng(7)
    dates = pd.bdate_range("2024-01-01", periods=300)

    def make_walk(drift):
        p = 100 * np.exp(np.cumsum(rng.normal(drift, 0.02, len(dates))))
        h = p * (1 + rng.uniform(0, 0.02, len(dates)))
        l = p * (1 - rng.uniform(0, 0.02, len(dates)))
        return pd.DataFrame({"open": (h + l) / 2, "high": h, "low": l, "close": p,
                             "volume": rng.integers(1e5, 1e6, len(dates)).astype(float),
                             "delivery_pct": rng.uniform(20, 80, len(dates))}, index=dates)

    pdata = {f"S{i:02d}": make_walk(rng.normal(0.0003, 0.0004)) for i in range(80)}
    bench = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0002, 0.01, len(dates)))), index=dates)
    sidx  = {s: pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0002, 0.012, len(dates)))), index=dates)
             for s in pdata}
    earn  = {s: {"rev_growth_yoy": rng.normal(15, 20), "pat_growth_yoy": rng.normal(12, 25),
                 "pat_growth_yoy_prev": rng.normal(8, 20), "opm_latest": rng.normal(18, 5),
                 "opm_yago": rng.normal(16, 5), "eps_actual": rng.normal(10, 3),
                 "eps_consensus": rng.normal(9, 3), "days_since_result": int(rng.integers(0, 120))}
             for s in pdata}

    res = score_universe(pdata, bench, sidx, earn, mode="tuned")
    print(res.head(10)[["score", "rank", "data_integrity", "signal_agreement", "drivers"]].to_string())
    print("\nHysteresis:", apply_hysteresis(res, held={res.index[40], res.index[3]}))
