"""Portfolio management API routes."""

import logging
import urllib.parse
from datetime import datetime
from flask import Blueprint, jsonify, request

import live_feed
import db
from metrics import counters
from target_utils import resolve_targets
from symbol_utils import check_symbol_exists

log = logging.getLogger("portfolio")

portfolio_bp = Blueprint("portfolio", __name__)


def _fresh_indicators(symbol):
    """Fetch fresh RSI/MACD/ADX for a symbol NOT in scan_results.

    Reuses the same computation path as /api/stock (live_feed.fetch_historical
    -> ta indicators). Returns a dict shaped like a scan_results row subset so
    compute_position_advice can treat universe and non-universe symbols
    identically. Returns {} on any failure (advice degrades gracefully).
    """
    try:
        import pandas as pd
        from ta.momentum import RSIIndicator
        from ta.trend import MACD, ADXIndicator

        clean = symbol.upper().replace(".NS", "").replace("NSE:", "")
        df = live_feed.fetch_historical(clean, days=120)
        if df is None or df.empty or len(df) < 40:
            return {}
        df = df.sort_values("DATE").reset_index(drop=True)
        close = df["CLOSE"].astype(float)
        high = df["HIGH"].astype(float)
        low = df["LOW"].astype(float)

        rsi_s = RSIIndicator(close, window=14).rsi()
        macd_ind = MACD(close)
        macd_line = macd_ind.macd()
        macd_sig = macd_ind.macd_signal()
        adx_s = ADXIndicator(high, low, close, window=14).adx()

        def _last(series, default=None):
            try:
                v = float(series.iloc[-1])
                return v if v == v else default  # NaN guard
            except Exception:
                return default

        def _prev(series, default=None):
            try:
                v = float(series.iloc[-2])
                return v if v == v else default
            except Exception:
                return default

        rsi = _last(rsi_s)
        rsi_prev = _prev(rsi_s)
        macd_l = _last(macd_line)
        macd_l_prev = _prev(macd_line)
        macd_s = _last(macd_sig)
        adx = _last(adx_s)

        macd_signal_label = None
        if macd_l is not None and macd_s is not None:
            macd_signal_label = "Bullish" if macd_l >= macd_s else "Bearish"

        return {
            "rsi": round(rsi, 2) if rsi is not None else None,
            "rsi_prev": round(rsi_prev, 2) if rsi_prev is not None else None,
            "adx": round(adx, 2) if adx is not None else None,
            "macd_signal": macd_signal_label,
            "macd_line": macd_l,
            "macd_line_prev": macd_l_prev,
            "_source": "fresh_indicators",
        }
    except Exception as exc:
        log.warning("[ADVICE] fresh indicator fetch failed for %s: %s", symbol, exc)
        return {}


def compute_position_advice(symbol, pos, scan):
    """Produce a buy/hold/sell/partial-book advisory for ANY position.

    Works for both universe symbols (scan = scan_results row) and
    out-of-universe holdings (scan empty -> fresh indicators fetched the same
    way /api/stock does).

    Returns {"verdict": BUY|HOLD|SELL|PARTIAL_BOOK, "reasons": [...],
             "in_universe": bool, "indicator_source": str}.
    Additive: does not mutate existing scan-based signal fields.
    """
    in_universe = bool(scan)
    src = scan if scan else _fresh_indicators(symbol)
    indicator_source = "scan_results" if in_universe else (
        src.get("_source", "unavailable") if src else "unavailable")

    cp = pos.get("current_price") or pos.get("buy_price") or 0
    buy_price = pos.get("buy_price") or 0
    pnl_pct = pos.get("pnl_pct")
    if pnl_pct is None and buy_price and cp:
        pnl_pct = ((cp - buy_price) / buy_price) * 100
    pnl_pct = pnl_pct or 0

    rsi = src.get("rsi")
    rsi_prev = src.get("rsi_prev")
    adx = src.get("adx")
    macd_signal = src.get("macd_signal")
    macd_line = src.get("macd_line")
    macd_line_prev = src.get("macd_line_prev")
    stop_loss = scan.get("stop_loss") if in_universe else pos.get("stop_loss")
    target_price = scan.get("target_price") if in_universe else pos.get("target")

    reasons = []
    verdict = "HOLD"

    # ── Hard exits (highest priority) ──
    if stop_loss and cp and cp <= stop_loss:
        verdict = "SELL"
        reasons.append("Stop loss hit")
    elif stop_loss and cp and stop_loss > 0 and cp <= stop_loss * 1.02:
        # Within 2% of stop — flag proximity but do not force exit here
        reasons.append("Near stop loss")

    if verdict != "SELL" and target_price and cp and cp >= target_price:
        verdict = "PARTIAL_BOOK"
        reasons.append("Target reached")

    # ── RSI regime ──
    if rsi is not None:
        if rsi > 75:
            if verdict not in ("SELL",):
                verdict = "SELL" if verdict == "HOLD" else verdict
            reasons.append(f"RSI overbought ({rsi:.0f})")
        elif rsi > 65:
            if verdict == "HOLD":
                verdict = "PARTIAL_BOOK"
            reasons.append(f"RSI elevated ({rsi:.0f}) — trail stop")
        elif rsi < 35:
            if verdict == "HOLD":
                verdict = "BUY"
            reasons.append(f"RSI oversold ({rsi:.0f})")
        if rsi_prev is not None and rsi > rsi_prev and 40 <= rsi <= 65:
            reasons.append("RSI rising")
            if verdict == "HOLD":
                verdict = "BUY"

    # ── MACD momentum ──
    if macd_signal == "Bullish":
        reasons.append("MACD improving")
        if verdict == "HOLD":
            verdict = "BUY"
    elif macd_signal == "Bearish":
        reasons.append("MACD bearish")
        if verdict == "HOLD":
            verdict = "PARTIAL_BOOK"
    if (macd_line is not None and macd_line_prev is not None
            and macd_line > macd_line_prev and macd_signal != "Bearish"):
        reasons.append("MACD improving")

    # ── Trend strength ──
    if adx is not None and adx < 15:
        reasons.append("Weak trend (ADX < 15)")

    # ── P&L overrides ──
    if pnl_pct < -8:
        verdict = "SELL"
        reasons.append(f"Loss exceeds 8% ({pnl_pct:.1f}%)")
    elif pnl_pct > 15 and verdict not in ("SELL",):
        verdict = "PARTIAL_BOOK"
        reasons.append(f"Profit {pnl_pct:.1f}% — book partial")

    if not reasons:
        if indicator_source == "unavailable":
            reasons.append("No fresh data — holding")
        else:
            reasons.append("Indicators neutral")

    # De-duplicate reasons preserving order
    seen = set()
    deduped = []
    for rsn in reasons:
        if rsn not in seen:
            seen.add(rsn)
            deduped.append(rsn)

    return {
        "verdict": verdict,
        "reasons": deduped,
        "in_universe": in_universe,
        "indicator_source": indicator_source,
    }


@portfolio_bp.route("/api/portfolios", methods=["GET"])
def api_get_portfolios():
    portfolios = db.get_portfolios()
    for p in portfolios:
        summary = db.get_portfolio_summary(p["id"])
        # Fetch live prices for unrealized P&L
        open_positions = db.get_positions(p["id"], status="OPEN")
        symbols = [pos["symbol"] for pos in open_positions]
        if symbols:
            live_feed.subscribe(symbols)
            live_prices = live_feed.get_live_prices(symbols)
            missing = [s for s in symbols if s not in live_prices]
            if missing:
                for ms in missing:
                    p_data = live_feed.get_live_price(ms)
                    if p_data:
                        live_prices[ms] = p_data
            total_invested = 0
            total_current = 0
            for pos in open_positions:
                inv = pos["buy_price"] * pos["quantity"]
                total_invested += inv
                ltp = live_prices.get(pos["symbol"], {}).get("ltp", 0)
                total_current += (ltp or pos["buy_price"]) * pos["quantity"]
            summary["current_value"] = round(total_current, 2)
            summary["unrealized_pnl"] = round(total_current - total_invested, 2)
            summary["unrealized_pnl_pct"] = round(((total_current - total_invested) / total_invested * 100), 2) if total_invested else 0
        else:
            summary["current_value"] = 0
            summary["unrealized_pnl"] = 0
            summary["unrealized_pnl_pct"] = 0
        summary["total_pnl"] = round(summary["unrealized_pnl"] + summary["realized_pnl"], 2)
        p["summary"] = summary
    return jsonify({"portfolios": portfolios})


@portfolio_bp.route("/api/portfolios", methods=["POST"])
def api_create_portfolio():
    body = request.get_json(silent=True) or {}
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    try:
        pid = db.create_portfolio(name, body.get("description", ""))
        return jsonify({"status": "ok", "id": pid, "name": name})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@portfolio_bp.route("/api/portfolios/<int:pid>", methods=["PUT"])
def api_update_portfolio(pid):
    body = request.get_json(silent=True) or {}
    db.update_portfolio(pid, name=body.get("name"), description=body.get("description"))
    return jsonify({"status": "ok"})


@portfolio_bp.route("/api/portfolios/<int:pid>", methods=["DELETE"])
def api_delete_portfolio(pid):
    db.delete_portfolio(pid)
    return jsonify({"status": "ok"})


@portfolio_bp.route("/api/portfolios/<int:pid>/positions", methods=["GET"])
def api_get_positions(pid):
    status = request.args.get("status")
    positions = db.get_positions(pid, status)
    all_syms = list({p["symbol"] for p in positions})
    scan_lookup = db.get_stocks_map(all_syms)

    symbols = list({p["symbol"] for p in positions if p["status"] == "OPEN"})
    if symbols:
        live_feed.subscribe(symbols)
        live_prices = live_feed.get_live_prices(symbols)
        # REST fallback for missing prices (WebSocket cache may be empty)
        missing = [s for s in symbols if s not in live_prices]
        if missing:
            for ms in missing:
                p_data = live_feed.get_live_price(ms)
                if p_data:
                    live_prices[ms] = p_data
        for pos in positions:
            sym = pos["symbol"]
            scan = scan_lookup.get(sym, {})

            if pos["status"] == "OPEN":
                lp = live_prices.get(sym, {})
                current = lp.get("ltp", 0)
                if current:
                    pos["current_price"] = current
                    pos["pnl"] = round((current - pos["buy_price"]) * pos["quantity"], 2)
                    pos["pnl_pct"] = round(((current - pos["buy_price"]) / pos["buy_price"]) * 100, 2)
                    pos["day_change"] = lp.get("change", 0)
                    pos["day_change_pct"] = lp.get("change_pct", 0)

                # ── On-demand advisory for EVERY open position (additive) ──
                # Works for out-of-universe holdings too: when no scan_results
                # row exists, compute_position_advice fetches fresh indicators
                # the same way /api/stock does.
                try:
                    advice = compute_position_advice(sym, pos, scan)
                    pos["verdict"] = advice["verdict"]
                    pos["advice_reasons"] = advice["reasons"]
                    pos["in_universe"] = advice["in_universe"]
                    pos["advice_source"] = advice["indicator_source"]
                except Exception as exc:
                    log.warning("[ADVICE] compute failed for %s: %s", sym, exc)

                if scan:
                    # D1-A: Resolve targets from single source of truth
                    targets = resolve_targets(scan, symbol=sym)
                    pos["auto_sl"] = targets["sl"]
                    pos["auto_sl_pct"] = scan.get("stop_loss_pct")
                    pos["auto_target"] = targets["t1"]
                    pos["auto_target_pct"] = scan.get("target_pct")
                    pos["rsi"] = scan.get("rsi")
                    pos["adx"] = scan.get("adx")
                    pos["macd_signal"] = scan.get("macd_signal")
                    pos["score"] = scan.get("score")
                    pos["risk_score"] = scan.get("risk_score")
                    pos["risk_reward"] = scan.get("risk_reward")
                    pos["signals"] = scan.get("signals", [])
                    pos["sector"] = scan.get("sector", "")
                    pos["weekly_trend"] = scan.get("weekly_trend", "")
                    pos["volume_ratio"] = scan.get("volume_ratio")
                    pos["delivery_pct"] = scan.get("delivery_pct")
                    pos["high_conviction"] = scan.get("high_conviction", False)

                    # ── Legacy signal (active — no behavior change) ──
                    signal = "HOLD"
                    signal_reasons = []
                    cp = current or pos["buy_price"]

                    if scan.get("stop_loss") and cp <= scan["stop_loss"]:
                        signal = "SELL"
                        signal_reasons.append("Stop loss hit")
                    elif scan.get("target_price") and cp >= scan["target_price"]:
                        signal = "BOOK PROFIT"
                        signal_reasons.append("Target reached")
                    elif scan.get("rsi", 0) > 75:
                        signal = "SELL"
                        signal_reasons.append(f"RSI overbought ({scan['rsi']})")
                    elif scan.get("rsi", 0) > 65:
                        signal = "TRAIL SL"
                        signal_reasons.append(f"RSI high ({scan['rsi']}), trail stop loss")

                    if scan.get("macd_signal") == "Bearish":
                        if signal == "HOLD":
                            signal = "CAUTION"
                        signal_reasons.append("MACD bearish crossover")
                    if scan.get("adx", 0) < 15:
                        signal_reasons.append("Weak trend (ADX < 15)")

                    pnl_pct = pos.get("pnl_pct", 0)
                    if pnl_pct < -8:
                        signal = "SELL"
                        signal_reasons.append(f"Loss exceeds 8% ({pnl_pct:.1f}%)")
                    elif pnl_pct > 15:
                        signal = "BOOK PROFIT"
                        signal_reasons.append(f"Profit {pnl_pct:.1f}% — consider booking")

                    pos["signal"] = signal
                    pos["signal_reasons"] = signal_reasons

                    # ── D1-A: Observation Mode — shadow signal comparison ──
                    new_signal = "HOLD"
                    if targets.get("sl") is not None and cp <= targets["sl"]:
                        new_signal = "SELL"
                    elif targets.get("t1") is not None and cp >= targets["t1"]:
                        new_signal = "BOOK PROFIT"
                    elif scan.get("rsi", 0) > 75:
                        new_signal = "SELL"
                    elif scan.get("rsi", 0) > 65:
                        new_signal = "TRAIL SL"
                    if pnl_pct < -8:
                        new_signal = "SELL"
                    elif pnl_pct > 15:
                        new_signal = "BOOK PROFIT"

                    if signal == new_signal:
                        counters.inc("signal_compare_match")
                    else:
                        counters.inc("signal_compare_mismatch")
                        log.info(
                            "[SIGNAL_COMPARE] symbol=%s legacy=%s new=%s cmp=%.2f legacy_target=%s new_target=%s",
                            sym, signal, new_signal, cp,
                            scan.get("target_price"), targets.get("t1"),
                        )

            elif pos["status"] == "CLOSED" and pos["sell_price"]:
                pos["pnl"] = round((pos["sell_price"] - pos["buy_price"]) * pos["quantity"], 2)
                pos["pnl_pct"] = round(((pos["sell_price"] - pos["buy_price"]) / pos["buy_price"]) * 100, 2)
                if scan:
                    pos["sector"] = scan.get("sector", "")

    summary = db.get_portfolio_summary(pid)
    total_current = 0
    total_invested = 0
    for pos in positions:
        if pos["status"] == "OPEN":
            total_invested += pos["buy_price"] * pos["quantity"]
            total_current += pos.get("current_price", pos["buy_price"]) * pos["quantity"]
    summary["current_value"] = round(total_current, 2)
    summary["unrealized_pnl"] = round(total_current - total_invested, 2)
    summary["unrealized_pnl_pct"] = round(((total_current - total_invested) / total_invested * 100), 2) if total_invested else 0
    summary["total_pnl"] = round(summary["unrealized_pnl"] + summary["realized_pnl"], 2)

    return jsonify({"positions": positions, "summary": summary})


@portfolio_bp.route("/api/positions/<int:pos_id>/advice", methods=["GET"])
def api_position_advice(pos_id):
    """On-demand advisory for a single position (universe or not).

    Lets the UI fetch a fresh BUY/HOLD/SELL/PARTIAL_BOOK verdict + reasons
    per position without recomputing the whole portfolio. For out-of-universe
    holdings this computes fresh indicators the same way /api/stock does.
    """
    pos = db.get_position(pos_id)
    if not pos:
        return jsonify({"error": "position not found"}), 404

    sym = pos["symbol"]
    scan = db.get_stocks_map([sym]).get(sym, {})

    # Attach a live current price so P&L-aware rules apply.
    if pos.get("status") == "OPEN":
        try:
            live_feed.subscribe([sym])
            lp = live_feed.get_live_prices([sym]).get(sym) or live_feed.get_live_price(sym)
            if lp and lp.get("ltp"):
                pos["current_price"] = lp["ltp"]
        except Exception as exc:
            log.warning("[ADVICE] live price fetch failed for %s: %s", sym, exc)

    advice = compute_position_advice(sym, pos, scan)
    return jsonify({
        "position_id": pos_id,
        "symbol": sym,
        "verdict": advice["verdict"],
        "reasons": advice["reasons"],
        "in_universe": advice["in_universe"],
        "advice_source": advice["indicator_source"],
    })


@portfolio_bp.route("/api/portfolios/<int:pid>/positions", methods=["POST"])
def api_add_position(pid):
    body = request.get_json(silent=True) or {}
    # D1-A: Full normalization & URL decoding (handles M%26M, NSE:M&M, M&M.NS)
    raw_symbol = body.get("symbol", "")
    symbol = urllib.parse.unquote(raw_symbol).upper().strip().replace("NSE:", "").replace(".NS", "")

    # D1-A: Validation — backend is final authority
    if not symbol:
        return jsonify({"error": "symbol is required"}), 400
    # Universe gate relaxed: allow ANY stock (personal tool). We still flag
    # out-of-universe symbols so the UI/response can note research coverage,
    # but we no longer block the add.
    in_universe = check_symbol_exists(symbol)
    try:
        qty = int(body.get("quantity", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "quantity must be a valid integer"}), 400
    if qty <= 0:
        return jsonify({"error": "quantity must be greater than 0"}), 400
    try:
        price = float(body.get("buy_price", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "buy_price must be a valid number"}), 400
    if price <= 0:
        return jsonify({"error": "buy_price must be greater than 0"}), 400

    pos_id = db.add_position(
        portfolio_id=pid, symbol=symbol,
        quantity=qty,
        buy_price=price,
        buy_date=body.get("buy_date", datetime.now().strftime("%Y-%m-%d")),
        stop_loss=float(body["stop_loss"]) if body.get("stop_loss") else None,
        target=float(body["target"]) if body.get("target") else None,
        notes=body.get("notes", ""),
    )
    live_feed.subscribe([symbol])
    resp = {"status": "ok", "id": pos_id, "in_universe": in_universe}
    if not in_universe:
        resp["note"] = (
            f"'{symbol}' is not in the research universe — added anyway. "
            "Advice will use fresh live indicators instead of scan data."
        )
    return jsonify(resp)


@portfolio_bp.route("/api/positions/<int:pos_id>", methods=["PUT"])
def api_update_position(pos_id):
    body = request.get_json(silent=True) or {}
    db.update_position(pos_id, **body)
    return jsonify({"status": "ok"})


@portfolio_bp.route("/api/positions/<int:pos_id>/close", methods=["POST"])
def api_close_position(pos_id):
    body = request.get_json(silent=True) or {}
    sell_price = body.get("sell_price")
    if not sell_price:
        return jsonify({"error": "sell_price required"}), 400
    db.close_position(pos_id, float(sell_price), body.get("sell_date"))
    return jsonify({"status": "ok"})


@portfolio_bp.route("/api/positions/<int:pos_id>", methods=["DELETE"])
def api_delete_position(pos_id):
    db.delete_position(pos_id)
    return jsonify({"status": "ok"})
