"""
universe_builder.py — Phase 5.6B/C: Eligible Universe Builder

Replaces direct hardcoded universe usage with a filtered, versioned
eligible universe derived from universe_catalog (Stock Master Registry).

Phase 5.6B/C Changes:
  - Skip ETF, INDEX, MF, NAV, INAV instrument types
  - Apply name heuristics only if last_synced_at IS NULL (Metadata > Heuristics)
  - Price >= 20 filter moved to Stage-3 (Build) instead of Stage-1
  - Candidate freeze integrity verification before build
  - Atomic version activation with validation gates
  - Universe snapshot audit trail

Filters (Stage-3 Build):
  - Market Cap > UNIVERSE_MIN_MCAP_CR (1500 Cr)
  - 20 Day Avg Turnover > UNIVERSE_MIN_AVG_TURNOVER_CR (5 Cr) — PRIMARY
  - 20 Day Avg Volume > UNIVERSE_MIN_AVG_VOLUME (100,000) — secondary
  - Price > UNIVERSE_MIN_PRICE (20) — applied here in Stage-3
  - Not ETF, INDEX, MF, NAV, INAV
  - Not SME
  - Not Suspended (is_active = True)

Always Include:
  - Open portfolio positions
  - User watchlist symbols

Schedule: Called by liquidity_enrichment.py when coverage >= 80%

Output:
  - Persisted to `eligible_universe` table + `universe_snapshot` audit trail
  - Versioned: UNIVERSE_v001, UNIVERSE_v002, ...
"""

import logging
import re
from datetime import datetime

log = logging.getLogger("screener")

# ETF/NAV heuristic patterns — only applied to unsynced symbols
_HEURISTIC_ETF_PATTERNS = [
    "BEES", "ETF", "LIQUID", "GOLDBEES", "SILVERBEES",
    "NIFTYBEES", "BANKBEES", "JUNIORBEES", "SETFNIF50",
]
_HEURISTIC_NAV_PATTERNS = ["NAV", "INAV"]


def build_eligible_universe() -> tuple[list[str], str]:
    """Legacy entry point — builds universe using old flow.
    Still used by boot-prep and daily scheduler as fallback.
    """
    import db
    db.set_meta("universe_build_status", "BUILDING")
    try:
        symbols, version = _build_eligible_universe_impl()
        db.set_meta("universe_build_status", "READY")
        return symbols, version
    except Exception:
        db.set_meta("universe_build_status", "FAILED")
        raise


def build_eligible_universe_v2(version: str, health_metrics: dict) -> tuple[list[str], str]:
    """Phase 5.6B/C entry point — called by liquidity worker when coverage >= 80%.

    Performs:
    1. Verify candidate freeze integrity
    2. Build eligible universe from frozen candidates + catalog metrics
    3. Apply all Stage-3 filters (price, turnover, volume, hygiene)
    4. Validate production gates
    5. Atomic version activation if all gates pass
    """
    import db

    db.set_meta("universe_build_status", "BUILDING")
    db.set_meta("building_universe_version", version)

    try:
        # 1. Verify candidate integrity
        is_valid, current_count, checksum = db.verify_candidate_integrity(version)
        if not is_valid:
            log.error("[UniverseBuilder] CANDIDATE INTEGRITY MISMATCH — aborting build for %s", version)
            db.set_meta("universe_state", "CORRUPTED")
            db.set_meta("universe_build_status", "INTEGRITY_FAILED")
            db.set_meta("building_universe_version", "")
            return [], ""

        # 2. Build eligible universe
        symbols, eligible_data, rejected = _build_from_frozen_candidates(version)

        if not symbols:
            log.error("[UniverseBuilder] Build produced 0 eligible symbols — aborting")
            db.set_meta("universe_state", "DEGRADED")
            db.set_meta("universe_build_status", "EMPTY_BUILD")
            db.set_meta("building_universe_version", "")
            return [], ""

        # 3. Save eligible universe + snapshot
        db.save_eligible_universe_with_snapshot(eligible_data, version)

        # 4. Record rebuild history
        try:
            db.save_universe_rebuild_history(
                version=version,
                input_count=current_count,
                eligible_count=len(symbols),
                rejected=rejected,
                force_included=rejected.get("force_included", 0),
                fallback_used=False,
            )
        except Exception as exc:
            log.debug("[UniverseBuilder] Failed to save rebuild history (non-fatal): %s", exc)

        # 5. Check exclusion guard (Risk #5: too many permanently excluded)
        excl_safe, excl_pct, excl_count, excl_total = db.check_exclusion_guard(version)
        if not excl_safe:
            log.error("[UniverseBuilder] EXCLUSION GUARD FAILED: %.1f%% excluded (%d/%d) — aborting activation",
                      excl_pct, excl_count, excl_total)
            db.set_meta("universe_state", "DEGRADED")
            db.set_meta("universe_build_status", "EXCLUSION_GUARD_FAILED")
            db.set_meta("building_universe_version", "")
            return symbols, version

        # 6. Validate production gates
        gate_result = _validate_production_gates(version, current_count, len(symbols),
                                                  health_metrics)

        if gate_result["all_passed"]:
            # Save validation snapshot
            db.save_validation_snapshot(
                version=version,
                candidate_count=current_count,
                eligible_count=len(symbols),
                marketcap_coverage_pct=health_metrics.get("marketcap_coverage_pct", 0),
                liquidity_coverage_pct=health_metrics.get("liquidity_coverage_pct", 0),
            )

            # Verify activation checkpoints (Section 13)
            checkpoints_ok = _verify_activation_checkpoints(version, len(symbols))

            if checkpoints_ok:
                # Atomic version activation
                activated = db.activate_universe_version_transaction(version)
                if activated:
                    log.info("[UniverseBuilder] ✅ Universe %s ACTIVATED: %d eligible stocks",
                             version, len(symbols))
                    db.set_meta("universe_build_status", "READY")

                    # Snapshot retention (Risk #8: keep 90 days, protect active)
                    try:
                        db.cleanup_old_snapshots(keep_days=90)
                    except Exception as exc:
                        log.debug("[UniverseBuilder] Snapshot cleanup failed (non-fatal): %s", exc)
                else:
                    log.error("[UniverseBuilder] Activation transaction failed for %s", version)
                    db.set_meta("universe_state", "DEGRADED")
                    db.set_meta("building_universe_version", "")
            else:
                log.error("[UniverseBuilder] Activation checkpoints failed for %s", version)
                db.set_meta("universe_state", "DEGRADED")
                db.set_meta("building_universe_version", "")
        else:
            log.warning("[UniverseBuilder] Production gates FAILED for %s: %s",
                        version, gate_result.get("failures", []))
            db.set_meta("universe_state", "DEGRADED")
            db.set_meta("universe_build_status", "GATES_FAILED")
            db.set_meta("building_universe_version", "")

        return symbols, version

    except Exception as exc:
        log.error("[UniverseBuilder] Build FAILED for %s: %s", version, exc, exc_info=True)
        db.set_meta("universe_build_status", "FAILED")
        db.set_meta("universe_state", "DEGRADED")
        db.set_meta("building_universe_version", "")
        raise


def _build_from_frozen_candidates(version: str) -> tuple[list[str], list[dict], dict]:
    """Build eligible universe from frozen candidates + current catalog metrics.
    Returns (symbols_list, eligible_data, rejected_counts).
    """
    import db
    from config import (
        UNIVERSE_MIN_AVG_TURNOVER_CR,
        UNIVERSE_MIN_AVG_VOLUME,
        UNIVERSE_MIN_PRICE,
    )

    # Get frozen candidates
    frozen_symbols = db.get_frozen_candidates(version)
    if not frozen_symbols:
        return [], [], {}

    # Get full catalog data for these symbols
    catalog_map = {}
    catalog = db.get_universe_catalog_eligible()
    for row in catalog:
        catalog_map[row.get("symbol", "")] = row

    eligible_data = []
    rejected = {"mcap": 0, "turnover": 0, "volume": 0, "price": 0,
                "etf": 0, "sme": 0, "suspended": 0, "ipo_age": 0,
                "force_included": 0}

    for sym in frozen_symbols:
        stock = catalog_map.get(sym)
        if not stock:
            continue

        instrument = (stock.get("instrument_type") or "EQ").upper()
        is_active = stock.get("is_active", True)
        name = (stock.get("company_name") or sym).upper()
        last_synced = stock.get("last_synced_at")

        # Skip suspended
        if not is_active:
            rejected["suspended"] += 1
            continue

        # Skip non-equity instruments (Metadata classification)
        if instrument in ("ETF", "INDEX", "MF", "NAV", "INAV"):
            rejected["etf"] += 1
            continue

        # Apply name heuristics ONLY if unsynced (Metadata > Heuristics)
        if not last_synced:
            sym_upper = sym.upper()

            # ETF heuristics
            is_heuristic_etf = False
            for pattern in _HEURISTIC_ETF_PATTERNS:
                if pattern in sym_upper or pattern in name:
                    is_heuristic_etf = True
                    break

            if is_heuristic_etf:
                rejected["etf"] += 1
                continue

            # NAV/INAV heuristics — careful to avoid GOLDIAM, SILVERTOUCH
            is_heuristic_nav = False
            for pattern in _HEURISTIC_NAV_PATTERNS:
                if (sym_upper.endswith(pattern) or
                    f" {pattern}" in name or
                    name.startswith(f"{pattern} ")):
                    is_heuristic_nav = True
                    break

            if is_heuristic_nav:
                rejected["etf"] += 1
                continue

        # Skip SME
        if instrument == "SME" or _is_sme_symbol(sym):
            rejected["sme"] += 1
            continue

        mcap = stock.get("market_cap") or 0
        avg_turnover = stock.get("avg_turnover_20d") or 0
        avg_volume = stock.get("avg_volume_20d") or 0
        price = stock.get("price") or 0

        # Normalize market_cap
        mcap_cr = mcap / 1e7 if mcap > 10000 else mcap

        # PRIMARY: Turnover filter (Stage-3)
        turnover_cr = avg_turnover / 1e7 if avg_turnover > 10000 else avg_turnover
        if turnover_cr > 0 and turnover_cr < UNIVERSE_MIN_AVG_TURNOVER_CR:
            rejected["turnover"] += 1
            continue

        # SECONDARY: Volume filter (Stage-3)
        if avg_volume > 0 and avg_volume < UNIVERSE_MIN_AVG_VOLUME:
            rejected["volume"] += 1
            continue

        # Price filter (Stage-3 — moved from Stage-1)
        if price > 0 and price < UNIVERSE_MIN_PRICE:
            rejected["price"] += 1
            continue

        eligible_data.append({
            "symbol": sym,
            "market_cap_cr": mcap_cr,
            "avg_volume_20d": avg_volume,
            "avg_turnover_20d": turnover_cr,
            "price": price,
            "eligibility_reason": "FILTER_PASS",
        })

    # Force-include portfolio + watchlist
    eligible_symbols = {s["symbol"] for s in eligible_data}
    try:
        positions = db.execute_db(
            "SELECT DISTINCT symbol FROM positions WHERE status='OPEN'",
            fetch="all"
        )
        if positions:
            for p in positions:
                sym = p.get("symbol")
                if sym and sym not in eligible_symbols:
                    eligible_data.append({
                        "symbol": sym,
                        "market_cap_cr": 0,
                        "avg_volume_20d": 0,
                        "avg_turnover_20d": 0,
                        "price": 0,
                        "eligibility_reason": "PORTFOLIO_FORCE_INCLUDE",
                    })
                    eligible_symbols.add(sym)
                    rejected["force_included"] += 1
    except Exception:
        pass

    try:
        customs = db.get_custom_stocks()
        if customs:
            for c in customs:
                sym = c.get("symbol")
                if sym and sym not in eligible_symbols:
                    eligible_data.append({
                        "symbol": sym,
                        "market_cap_cr": 0,
                        "avg_volume_20d": 0,
                        "avg_turnover_20d": 0,
                        "price": 0,
                        "eligibility_reason": "WATCHLIST_FORCE_INCLUDE",
                    })
                    eligible_symbols.add(sym)
                    rejected["force_included"] += 1
    except Exception:
        pass

    symbols = sorted(eligible_symbols)

    log.info("[UniverseBuilder] Stage-3 Build: eligible=%d | rejected: mcap=%d turnover=%d "
             "volume=%d price=%d etf=%d sme=%d suspended=%d force_included=%d",
             len(symbols), rejected["mcap"], rejected["turnover"],
             rejected["volume"], rejected["price"], rejected["etf"],
             rejected["sme"], rejected["suspended"], rejected["force_included"])

    return symbols, eligible_data, rejected


def _validate_production_gates(version: str, candidate_count: int,
                                eligible_count: int, health_metrics: dict) -> dict:
    """Validate production gates before universe activation.

    Gates:
    1. eligible_count >= 500
    2. marketcap_coverage_pct >= 70
    3. liquidity_coverage_pct >= 80
    4. candidate_count >= 500
    """
    import db

    failures = []

    if eligible_count < 500:
        failures.append(f"eligible_count={eligible_count} < 500")

    mcap_cov = health_metrics.get("marketcap_coverage_pct", 0)
    if mcap_cov < 70:
        failures.append(f"marketcap_coverage={mcap_cov}% < 70%")

    liq_cov = health_metrics.get("liquidity_coverage_pct", 0)
    if liq_cov < 80:
        failures.append(f"liquidity_coverage={liq_cov}% < 80%")

    if candidate_count < 500:
        failures.append(f"candidate_count={candidate_count} < 500")

    # Check active worker count
    worker_status = db.get_meta("liquidity_worker_status")
    if worker_status == "running":
        # Worker is still running — this is OK if build is triggered mid-enrichment
        log.info("[UniverseBuilder] Worker still running — proceeding with build activation")

    all_passed = len(failures) == 0

    if not all_passed:
        log.warning("[UniverseBuilder] PRODUCTION GATES FAILED: %s", "; ".join(failures))
    else:
        log.info("[UniverseBuilder] ✅ All production gates PASSED")

    return {"all_passed": all_passed, "failures": failures}


def _verify_activation_checkpoints(version: str, eligible_count: int) -> bool:
    """Verify activation checkpoints before switching active version (Section 13).

    Checks:
    1. active_universe_version != target_version
    2. eligible_universe rows exist for target_version
    3. universe_snapshot rows exist for target_version
    4. validation_snapshot row exists
    5. eligible_count matches validation_snapshot count
    """
    import db

    current_active = db.get_meta("active_universe_version") or ""
    if current_active == version:
        log.warning("[UniverseBuilder] Version %s already active — skipping", version)
        return False

    # Check eligible_universe rows
    eu_rows = db.execute_db(
        "SELECT COUNT(*) as c FROM eligible_universe WHERE universe_version = ?",
        (version,), fetch="one"
    )
    eu_count = int(eu_rows.get("c", 0)) if eu_rows else 0
    if eu_count == 0:
        log.error("[UniverseBuilder] No eligible_universe rows for %s", version)
        return False

    # Check universe_snapshot rows
    us_rows = db.execute_db(
        "SELECT COUNT(*) as c FROM universe_snapshot WHERE universe_version = ?",
        (version,), fetch="one"
    )
    us_count = int(us_rows.get("c", 0)) if us_rows else 0
    if us_count == 0:
        log.error("[UniverseBuilder] No universe_snapshot rows for %s", version)
        return False

    # Check validation_snapshot
    vs_row = db.execute_db(
        "SELECT eligible_count FROM universe_build_validation_snapshot WHERE universe_version = ? ORDER BY build_timestamp DESC LIMIT 1",
        (version,), fetch="one"
    )
    if not vs_row:
        log.error("[UniverseBuilder] No validation_snapshot for %s", version)
        return False

    vs_eligible = int(vs_row.get("eligible_count", 0))
    if vs_eligible != eligible_count:
        log.error("[UniverseBuilder] Eligible count mismatch: snapshot=%d actual=%d", vs_eligible, eligible_count)
        return False

    # Checkpoint 6: Post-build row integrity verification
    # Verify that eligible_universe rows actually have non-zero data
    # (catches partial insert failures / corrupted builds)
    healthy_row = db.execute_db(
        """SELECT COUNT(*) as c FROM eligible_universe
           WHERE universe_version = ?
             AND avg_volume_20d > 0
             AND avg_turnover_20d > 0
             AND price > 0
             AND market_cap_cr > 0""",
        (version,), fetch="one"
    )
    healthy_count = int(healthy_row.get("c", 0)) if healthy_row else 0

    # Force-included rows (portfolio/watchlist) have 0 metrics — exclude from check
    force_row = db.execute_db(
        """SELECT COUNT(*) as c FROM eligible_universe
           WHERE universe_version = ?
             AND eligibility_reason IN ('PORTFOLIO_FORCE_INCLUDE', 'WATCHLIST_FORCE_INCLUDE')""",
        (version,), fetch="one"
    )
    force_count = int(force_row.get("c", 0)) if force_row else 0

    # Expected healthy = eligible - force_included
    expected_healthy = eligible_count - force_count
    if expected_healthy > 0 and healthy_count < (expected_healthy * 0.95):
        log.error("[UniverseBuilder] ROW INTEGRITY FAILED: healthy=%d expected=%d (%.1f%%) "
                  "(eligible=%d force=%d) — possible partial insert failure",
                  healthy_count, expected_healthy,
                  (healthy_count / expected_healthy * 100) if expected_healthy > 0 else 0,
                  eligible_count, force_count)
        return False

    log.info("[UniverseBuilder] ✅ All activation checkpoints passed for %s "
             "(rows=%d healthy=%d force=%d)",
             version, eu_count, healthy_count, force_count)
    return True


# ── Legacy Helper Functions ───────────────────────────────────

def _build_eligible_universe_impl() -> tuple[list[str], str]:
    """
    Build the eligible universe from universe_catalog.
    Legacy path — used by boot-prep when liquidity worker hasn't run yet.

    1. Read all active stocks from universe_catalog
    2. Apply eligibility filters (turnover primary, volume secondary)
    3. Force-include: open portfolio positions + custom watchlist
    4. Generate version: UNIVERSE_vNNN (auto-increment)
    5. Save to eligible_universe table
    6. Return (eligible_symbols, universe_version)

    If universe_catalog is empty or too small, falls back to the
    curated universe from universe.py (existing behavior).
    """
    import db
    from config import (
        UNIVERSE_MIN_MCAP_CR, UNIVERSE_MIN_AVG_TURNOVER_CR,
        UNIVERSE_MIN_AVG_VOLUME, UNIVERSE_MIN_PRICE,
        UNIVERSE_MIN_LISTING_DAYS,
    )

    # 1. Read universe_catalog
    catalog = db.get_universe_catalog_eligible()
    if not catalog or len(catalog) < 50:
        log.warning("[UniverseBuilder] Catalog too small (%d), falling back to curated universe",
                    len(catalog) if catalog else 0)
        from universe import get_active_universe
        symbols = get_active_universe()
        version = _next_version()
        _save_fallback_universe(symbols, version)
        return symbols, version

    log.info("[UniverseBuilder] Processing %d catalog stocks", len(catalog))

    # 2. Apply eligibility filters
    eligible_data = []
    rejected = {"mcap": 0, "turnover": 0, "volume": 0, "price": 0,
                "etf": 0, "sme": 0, "suspended": 0, "ipo_age": 0}

    for stock in catalog:
        symbol = stock.get("symbol", "")
        if not symbol:
            continue

        mcap = stock.get("market_cap") or 0
        avg_turnover = stock.get("avg_turnover_20d") or 0
        avg_volume = stock.get("avg_volume_20d") or 0
        price = stock.get("price") or 0
        instrument = (stock.get("instrument_type") or "EQ").upper()
        is_active = stock.get("is_active", True)
        last_synced = stock.get("last_synced_at")

        # Skip suspended
        if not is_active:
            rejected["suspended"] += 1
            continue

        # Skip non-equity instruments (metadata classification)
        if instrument in ("ETF", "INDEX", "MF", "NAV", "INAV"):
            rejected["etf"] += 1
            continue

        # Heuristic fallback only for unsynced symbols (Metadata > Heuristics)
        if not last_synced:
            sym_upper = symbol.upper()
            name = (stock.get("company_name") or symbol).upper()

            is_heuristic_skip = False
            for pattern in _HEURISTIC_ETF_PATTERNS:
                if pattern in sym_upper or pattern in name:
                    is_heuristic_skip = True
                    break

            if not is_heuristic_skip:
                for pattern in _HEURISTIC_NAV_PATTERNS:
                    if (sym_upper.endswith(pattern) or
                        f" {pattern}" in name or
                        name.startswith(f"{pattern} ")):
                        is_heuristic_skip = True
                        break

            if is_heuristic_skip:
                rejected["etf"] += 1
                continue

        # Skip SME
        if instrument == "SME" or _is_sme_symbol(symbol):
            rejected["sme"] += 1
            continue

        # Market Cap filter — reject if no data OR below threshold
        mcap_cr = mcap / 1e7 if mcap > 10000 else mcap  # normalize if in absolute
        if mcap_cr < UNIVERSE_MIN_MCAP_CR:
            rejected["mcap"] += 1
            continue

        # PRIMARY: Turnover filter (₹ Cr per day) — reject if no data OR below threshold
        turnover_cr = avg_turnover / 1e7 if avg_turnover > 10000 else avg_turnover
        if turnover_cr < UNIVERSE_MIN_AVG_TURNOVER_CR:
            rejected["turnover"] += 1
            continue

        # SECONDARY: Volume filter — reject if no data OR below threshold
        if avg_volume < UNIVERSE_MIN_AVG_VOLUME:
            rejected["volume"] += 1
            continue

        # Price filter — reject if no data OR below threshold
        if price < UNIVERSE_MIN_PRICE:
            rejected["price"] += 1
            continue

        # IPO Age filter — DISABLED until real listing_date is available.
        #
        # BUG: first_seen_at = CURRENT_TIMESTAMP at first master sync, so ALL stocks
        # get age=0 days on initial run (Reliance, TCS, HDFC all rejected).
        # first_seen_at tracks "when MarketOS discovered it", NOT actual listing date.
        #
        # TODO: Re-enable when master_sync stores actual listing_date from yfinance
        #       (info.get("ipoDate") or earliest available OHLCV date).
        #       Then use: listing_date-based filter instead of first_seen_at.
        #
        # if listing_date and UNIVERSE_MIN_LISTING_DAYS > 0:
        #     age = (datetime.now() - listing_date).days
        #     if age < UNIVERSE_MIN_LISTING_DAYS:
        #         rejected["ipo_age"] += 1
        #         continue

        reason = "FILTER_PASS"
        eligible_data.append({
            "symbol": symbol,
            "market_cap_cr": mcap_cr,
            "avg_volume_20d": avg_volume,
            "avg_turnover_20d": turnover_cr,
            "price": price,
            "eligibility_reason": reason,
        })

    log.info("[UniverseBuilder] Filter results: passed=%d | rejected: mcap=%d turnover=%d "
             "volume=%d price=%d etf=%d sme=%d suspended=%d ipo_age=%d",
             len(eligible_data), rejected["mcap"], rejected["turnover"],
             rejected["volume"], rejected["price"], rejected["etf"],
             rejected["sme"], rejected["suspended"], rejected["ipo_age"])

    # 3. Force-include portfolio + watchlist
    eligible_symbols = {s["symbol"] for s in eligible_data}
    force_included = 0

    try:
        positions = db.execute_db(
            "SELECT DISTINCT symbol FROM positions WHERE status='OPEN'",
            fetch="all"
        )
        if positions:
            for p in positions:
                sym = p.get("symbol")
                if sym and sym not in eligible_symbols:
                    eligible_data.append({
                        "symbol": sym,
                        "market_cap_cr": 0,
                        "avg_volume_20d": 0,
                        "avg_turnover_20d": 0,
                        "price": 0,
                        "eligibility_reason": "PORTFOLIO_FORCE_INCLUDE",
                    })
                    eligible_symbols.add(sym)
                    force_included += 1
    except Exception:
        pass

    try:
        customs = db.get_custom_stocks()
        if customs:
            for c in customs:
                sym = c.get("symbol")
                if sym and sym not in eligible_symbols:
                    eligible_data.append({
                        "symbol": sym,
                        "market_cap_cr": 0,
                        "avg_volume_20d": 0,
                        "avg_turnover_20d": 0,
                        "price": 0,
                        "eligibility_reason": "WATCHLIST_FORCE_INCLUDE",
                    })
                    eligible_symbols.add(sym)
                    force_included += 1
    except Exception:
        pass

    if force_included:
        log.info("[UniverseBuilder] Force-included %d portfolio/watchlist stocks", force_included)

    symbols = sorted(eligible_symbols)
    
    # 3.5 Fallback if eligible is too small
    if len(symbols) < 100:
        log.warning("[UniverseBuilder] Eligible universe too small (%d). Falling back to curated.", len(symbols))
        from universe import get_active_universe
        fallback_symbols = get_active_universe()
        version = _next_version()
        _save_fallback_universe(fallback_symbols, version)
        import db
        db.set_meta("active_universe_version", version)
        return fallback_symbols, version

    # 4. Generate version
    version = _next_version()

    # 5. Save to DB
    import db
    db.save_eligible_universe(eligible_data, version)
    db.set_meta("active_universe_version", version)

    log.info("[UniverseBuilder] Eligible universe: %d stocks, version=%s", len(symbols), version)

    # 6. Record rebuild history for Mission Control / drift debugging
    try:
        db.save_universe_rebuild_history(
            version=version,
            input_count=len(catalog),
            eligible_count=len(symbols),
            rejected=rejected,
            force_included=force_included,
            fallback_used=False,
        )
    except Exception as exc:
        log.debug("[UniverseBuilder] Failed to save rebuild history (non-fatal): %s", exc)

    return symbols, version


def _next_version() -> str:
    """Generate next UNIVERSE_vNNN version string."""
    import db
    current = db.get_latest_universe_version()
    # Extract number from UNIVERSE_vNNN
    match = re.search(r"v(\d+)", current)
    if match:
        num = int(match.group(1)) + 1
    else:
        num = 1
    return f"UNIVERSE_v{num:03d}"


def _is_sme_symbol(symbol: str) -> bool:
    """Heuristic: detect SME exchange symbols."""
    # SME stocks typically have 'SME' suffix or are on BSE SME platform
    return symbol.endswith("SME") or "-SM" in symbol


def _save_fallback_universe(symbols: list, version: str):
    """Save a fallback universe when catalog is unavailable."""
    import db
    data = [{"symbol": s, "market_cap_cr": 0, "avg_volume_20d": 0,
             "avg_turnover_20d": 0, "price": 0,
             "eligibility_reason": "FALLBACK_CURATED"} for s in symbols]
    db.save_eligible_universe(data, version)


def refresh_volume_turnover_metrics(symbols: list = None) -> int:
    """
    For each symbol in universe_catalog, compute 20-day avg volume
    and turnover from the last 30 days of historical OHLCV data.

    Updates universe_catalog.avg_volume_20d / avg_turnover_20d / price.
    Returns count of symbols updated.

    This is a heavy operation — called during Master Sync or
    pre-scan warmup, not per-scan.
    """
    import db
    import live_feed

    if symbols is None:
        catalog = db.get_universe_catalog_eligible()
        symbols = [s.get("symbol") for s in catalog if s.get("symbol")]

    updated = 0
    for sym in symbols:
        try:
            df = live_feed.fetch_historical(sym, days=30)
            if df is None or df.empty or len(df) < 10:
                continue

            # Last 20 trading days
            recent = df.tail(20)
            avg_volume = float(recent["VOLUME"].mean())
            # Turnover = Volume × Close price (approximate)
            recent_turnover = recent["VOLUME"] * recent["CLOSE"]
            avg_turnover = float(recent_turnover.mean())
            last_price = float(recent["CLOSE"].iloc[-1])

            db.update_universe_catalog_metrics(sym, avg_volume, avg_turnover, last_price)
            updated += 1
        except Exception as exc:
            log.debug("[UniverseBuilder] Metrics update failed for %s: %s", sym, exc)

    log.info("[UniverseBuilder] Updated volume/turnover metrics for %d symbols", updated)
    return updated
