"""
MarketOS Execution Engine — Real-Time Paper Trading
====================================================
Event-driven execution engine that processes WebSocket ticks for:
  - Pending order fill detection (entry range trigger)
  - Active position SL / Target evaluation (sub-second)
  - Max drawdown / runup tracking per tick

Architecture:
  WebSocket _on_data → on_tick(symbol, ltp, tick_time)
                         ↓
              In-Memory Order Book + Position Book
                         ↓
              Async DB Writer Queue (non-blocking)

Design constraints:
  - on_tick() MUST complete in < 1ms (WebSocket thread)
  - All DB writes dispatched to background writer
  - State survives restarts via load_state()
"""

import logging
import threading
import queue
import time
from datetime import datetime, timezone, timedelta, date as _date

log = logging.getLogger("execution_engine")

_IST = timezone(timedelta(hours=5, minutes=30))

# ─── In-Memory State ────────────────────────────────────────────────────────
# pending_orders:  { "RELIANCE": [ {order_dict}, ... ] }
# active_positions: { "RELIANCE": [ {position_dict}, ... ] }
_pending_orders = {}
_active_positions = {}
_state_lock = threading.Lock()

# ─── Async DB Writer ────────────────────────────────────────────────────────
_write_queue = queue.Queue(maxsize=5000)
_writer_thread = None
_engine_running = False

# ─── Configuration ──────────────────────────────────────────────────────────
VIRTUAL_CAPITAL = 25000      # ₹25,000 per pick
MAX_HOLD_DAYS = 20           # 20 trading days max
COOLDOWN_DAYS = 5            # Don't re-pick within 5 days
MAX_PENDING_ORDERS = 50      # Cap to prevent memory bloat
ORDER_EXPIRY_DAYS = 5        # Pending orders expire after 5 trading days

# ─── Telemetry ──────────────────────────────────────────────────────────────
_stats = {
    "ticks_processed": 0,
    "ticks_processed_today": 0,
    "orders_filled": 0,
    "positions_closed": 0,
    "sl_hits": 0,
    "target_hits": 0,
    "target_gap_hits": 0,
    "sl_gap_hits": 0,
    "time_exits": 0,
    "signals_received": 0,
    "orders_created": 0,
    "orders_rejected_capacity": 0,
    "orders_rejected_cooldown": 0,
    "orders_rejected_duplicate": 0,
    "orders_rejected_invalid": 0,
    "last_tick_time": None,
    "max_tick_processing_us": 0,
    "_today_date": None,
}
_stats_lock = threading.Lock()


def get_engine_stats() -> dict:
    """Return engine telemetry for observability."""
    with _stats_lock:
        s = {k: v for k, v in _stats.items() if not k.startswith("_")}
    s["queue_depth"] = _write_queue.qsize()
    s["pending_orders"] = sum(len(v) for v in _pending_orders.values())
    s["active_positions"] = sum(len(v) for v in _active_positions.values())
    return s


# ═══════════════════════════════════════════════════════════════════════════════
# CORE TICK PROCESSOR — Called from WebSocket _on_data
# ═══════════════════════════════════════════════════════════════════════════════

def on_tick(symbol: str, ltp: float, tick_time: datetime):
    """Process a single tick from the WebSocket.
    
    MUST be < 1ms. No DB calls here — only in-memory checks.
    DB writes dispatched to background queue.
    """
    if not _engine_running or ltp <= 0:
        return

    # Market hours gate (09:15 - 15:30 IST, weekdays only)
    if not _is_market_hours(tick_time):
        return

    _t0 = time.perf_counter_ns()
    clean = symbol.upper().replace(".NS", "")

    with _stats_lock:
        _stats["ticks_processed"] += 1
        _stats["last_tick_time"] = tick_time.isoformat() if tick_time else None
        # Reset daily counter at midnight
        today_str = _date.today().isoformat()
        if _stats["_today_date"] != today_str:
            _stats["ticks_processed_today"] = 0
            _stats["_today_date"] = today_str
        _stats["ticks_processed_today"] += 1

    with _state_lock:
        # 1. Check pending orders for this symbol
        pending = _pending_orders.get(clean)
        if pending:
            _check_pending_orders(clean, ltp, tick_time, pending)

        # 2. Check active positions for SL / Target
        positions = _active_positions.get(clean)
        if positions:
            _check_active_positions(clean, ltp, tick_time, positions)

    # Track max tick processing time (microseconds)
    _elapsed_us = (time.perf_counter_ns() - _t0) / 1000
    with _stats_lock:
        if _elapsed_us > _stats["max_tick_processing_us"]:
            _stats["max_tick_processing_us"] = round(_elapsed_us, 1)


def _check_pending_orders(symbol: str, ltp: float, tick_time: datetime, orders: list):
    """Check if LTP has entered entry range for any pending order."""
    filled = []
    for order in orders:
        entry_low = order.get("entry_low", 0)
        entry_high = order.get("entry_high", 0)

        # Trigger condition: LTP is within the entry range
        if entry_low and entry_high and entry_low <= ltp <= entry_high:
            filled.append(order)
        elif entry_low and not entry_high and ltp >= entry_low:
            # Fallback: if only entry_low set (market order behavior)
            filled.append(order)

    for order in filled:
        orders.remove(order)
        _fill_order(symbol, order, ltp, tick_time)

    # Clean up empty lists
    if not orders:
        _pending_orders.pop(symbol, None)


def _check_active_positions(symbol: str, ltp: float, tick_time: datetime, positions: list):
    """Check SL/Target for all active positions of this symbol."""
    closed = []
    for pos in positions:
        stop_loss = pos.get("stop_loss")
        target = pos.get("target_price")
        entry_date_str = pos.get("entry_date") or pos.get("entry_time_str", "")

        # Check time exit
        days_held = 0
        try:
            if entry_date_str:
                entry_dt = _date.fromisoformat(entry_date_str[:10])
                days_held = (_date.today() - entry_dt).days
        except Exception:
            pass

        exit_reason = None
        if stop_loss and ltp <= stop_loss:
            # Distinguish gap SL from normal SL
            if ltp < stop_loss:
                exit_reason = "STOPLOSS_GAP"
                with _stats_lock:
                    _stats["sl_gap_hits"] += 1
            else:
                exit_reason = "STOP_HIT"
            with _stats_lock:
                _stats["sl_hits"] += 1
        elif target and ltp >= target:
            # Distinguish gap target from normal target
            if ltp > target:
                exit_reason = "TARGET_GAP"
                with _stats_lock:
                    _stats["target_gap_hits"] += 1
            else:
                exit_reason = "TARGET_HIT"
            with _stats_lock:
                _stats["target_hits"] += 1
        elif days_held >= MAX_HOLD_DAYS:
            exit_reason = "TIME_EXIT"
            with _stats_lock:
                _stats["time_exits"] += 1

        if exit_reason:
            closed.append((pos, exit_reason))
        else:
            # Update drawdown / runup in memory
            entry_price = pos.get("entry_price", 0)
            if entry_price > 0:
                current_pct = ((ltp - entry_price) / entry_price) * 100
                pos["max_drawdown_pct"] = min(pos.get("max_drawdown_pct", 0), current_pct)
                pos["max_runup_pct"] = max(pos.get("max_runup_pct", 0), current_pct)
                # Dispatch extreme update (low priority, batched)
                _enqueue_write("update_extremes", {
                    "trade_id": pos["id"],
                    "max_drawdown_pct": round(pos["max_drawdown_pct"], 2),
                    "max_runup_pct": round(pos["max_runup_pct"], 2),
                })

    for pos, reason in closed:
        positions.remove(pos)
        _close_position(symbol, pos, ltp, tick_time, reason)

    if not positions:
        _active_positions.pop(symbol, None)


# ═══════════════════════════════════════════════════════════════════════════════
# ORDER FILL & POSITION CLOSE
# ═══════════════════════════════════════════════════════════════════════════════

def _fill_order(symbol: str, order: dict, fill_price: float, fill_time: datetime):
    """Fill a pending order → create an active position."""
    log.info("[EXEC] ORDER FILLED: %s @ ₹%.2f | order_id=%s", symbol, fill_price, order.get("order_id"))

    with _stats_lock:
        _stats["orders_filled"] += 1

    quantity = max(1, int(VIRTUAL_CAPITAL / fill_price))
    entry_time_str = fill_time.strftime("%Y-%m-%d") if fill_time else _date.today().isoformat()

    position = {
        "id": None,  # Will be set by DB writer
        "symbol": symbol,
        "model_version": order.get("model_version", "legacy"),  # model-aware dedup/exits
        "order_id": order.get("order_id"),
        "entry_price": fill_price,
        "entry_date": entry_time_str,
        "entry_time": fill_time.isoformat() if fill_time else None,
        "target_price": order.get("target_price"),
        "stop_loss": order.get("stop_loss"),
        "quantity": quantity,
        "max_drawdown_pct": 0,
        "max_runup_pct": 0,
    }

    # Add to active positions
    if symbol not in _active_positions:
        _active_positions[symbol] = []
    _active_positions[symbol].append(position)

    # Dispatch DB writes
    _enqueue_write("fill_order", {
        "order_id": order.get("order_id"),
        "fill_price": fill_price,
        "fill_time": fill_time.isoformat() if fill_time else None,
        "signal_time": order.get("signal_time"),
        "order_data": order,
        "quantity": quantity,
    })

    # Telegram notification (non-blocking)
    try:
        from telegram_alerts import send_entry_alert
        stock_data = order.get("stock_data", {})
        send_entry_alert({
            "symbol": symbol,
            "entry_price": fill_price,
            "target_price": order.get("target_price"),
            "stop_loss": order.get("stop_loss"),
            "quantity": quantity,
            "score_at_entry": order.get("score_at_signal", stock_data.get("score", 0)),
            "grade_at_entry": order.get("grade_at_signal", stock_data.get("grade", "")),
            "confidence_score": stock_data.get("confidence_score", 0),
            "risk_reward": stock_data.get("risk_reward", 0),
            "sector": stock_data.get("sector", ""),
            "high_conviction": stock_data.get("high_conviction", False),
            "is_golden": stock_data.get("is_golden", False),
        })
    except Exception:
        pass  # Telegram is optional — never block execution


def _close_position(symbol: str, position: dict, exit_price: float, exit_time: datetime, reason: str):
    """Close an active position."""
    entry_price = position.get("entry_price", 0)
    return_pct = ((exit_price - entry_price) / entry_price) * 100 if entry_price > 0 else 0

    log.info("[EXEC] POSITION CLOSED: %s @ ₹%.2f | reason=%s | return=%.2f%%",
             symbol, exit_price, reason, return_pct)

    with _stats_lock:
        _stats["positions_closed"] += 1

    _enqueue_write("close_position", {
        "trade_id": position.get("id"),
        "symbol": symbol,
        "exit_price": exit_price,
        "exit_time": exit_time.isoformat() if exit_time else None,
        "exit_reason": reason,
        "entry_price": entry_price,
        "entry_date": position.get("entry_date"),
    })

    # Telegram notification (non-blocking)
    try:
        from telegram_alerts import send_exit_alert
        days_held = 0
        try:
            entry_date_str = position.get("entry_date", "")
            if entry_date_str:
                days_held = (_date.today() - _date.fromisoformat(entry_date_str[:10])).days
        except Exception:
            pass
        send_exit_alert({
            "symbol": symbol,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "exit_reason": reason,
            "return_pct": round(return_pct, 1),
            "quantity": position.get("quantity", 0),
            "days_held": days_held,
        })
    except Exception:
        pass  # Telegram is optional — never block execution


# ═══════════════════════════════════════════════════════════════════════════════
# ORDER SUBMISSION — Called from Scanner
# ═══════════════════════════════════════════════════════════════════════════════

def submit_order(stock_data: dict, scan_context: dict = None) -> bool:
    """Submit a new paper trading order from scanner signal.
    
    Called by scanner after scoring. Creates PENDING order immediately.
    Returns True if order was accepted.
    """
    with _stats_lock:
        _stats["signals_received"] += 1

    if not _engine_running:
        return False

    sym = stock_data.get("symbol", "").upper().replace(".NS", "")
    if not sym:
        with _stats_lock:
            _stats["orders_rejected_invalid"] += 1
        return False

    price = stock_data.get("price", 0)
    if price <= 0:
        with _stats_lock:
            _stats["orders_rejected_invalid"] += 1
        return False

    # Must have valid target and stop loss
    target = stock_data.get("target_price")
    stop_loss = stock_data.get("stop_loss")
    if not target or not stop_loss:
        with _stats_lock:
            _stats["orders_rejected_invalid"] += 1
        return False

    # Canonical engine tag — dedup / cooldown / capacity are scoped per
    # (symbol, model_version) so one 'legacy' and one 'scoring_v1' position can
    # coexist on the same symbol (else the engine that submits first blocks the other).
    import db
    mv = db._canon_model_version(stock_data.get("model_version", ""))

    # Eligibility:
    #   legacy     -> HC or score >= 65 (unchanged).
    #   scoring_v1 -> rank<=25 enforced UPSTREAM (live_pipeline); the legacy score gate
    #                 is bypassed. score is a cross-sectional PERCENTILE here (~96 for a
    #                 top-25 pick); only a sub-~72 eligible universe could push it under
    #                 65 — guarded upstream by MIN_ELIGIBLE_UNIVERSE. Never fail silently.
    score = stock_data.get("score", 0)
    hc = stock_data.get("high_conviction", False)
    if mv != "scoring_v1" and not hc and score < 65:
        with _stats_lock:
            _stats["orders_rejected_invalid"] += 1
        return False
    if mv == "scoring_v1" and not hc and score < 65:
        log.warning("[EXEC] scoring_v1 %s accepted via rank gate despite percentile %s<65 "
                    "(thin universe? check MIN_ELIGIBLE_UNIVERSE)", sym, score)

    # Cap pending orders PER MODEL (each engine gets its own capacity).
    with _state_lock:
        model_pending = sum(1 for v in _pending_orders.values()
                            for o in v if o.get("model_version") == mv)
        if model_pending >= MAX_PENDING_ORDERS:
            log.warning("[EXEC] Order rejected: MAX_PENDING_ORDERS (%d) reached for model=%s",
                        MAX_PENDING_ORDERS, mv)
            with _stats_lock:
                _stats["orders_rejected_capacity"] += 1
            return False

    # Duplicate prevention: model-aware cooldown + existing positions/orders
    if not _check_cooldown(sym, mv):
        return False

    # Build entry range from research data
    entry_low = stock_data.get("entry_low") or stop_loss
    entry_high = stock_data.get("entry_high") or price
    # If no explicit range, use price ± 1% as default range
    if entry_low == stop_loss:
        entry_low = round(price * 0.99, 2)
        entry_high = round(price * 1.01, 2)

    signal_time = datetime.now(_IST)
    expires_at = signal_time + timedelta(days=ORDER_EXPIRY_DAYS)

    order = {
        "order_id": None,  # Set by DB writer
        "symbol": sym,
        "model_version": mv,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "target_price": target,
        "stop_loss": stop_loss,
        "score_at_signal": score,
        "grade_at_signal": stock_data.get("grade", ""),
        "signal_time": signal_time.isoformat(),
        "expires_at": expires_at.isoformat(),
        "signal_source": "scanner",
        "stock_data": stock_data,
        "scan_context": scan_context or {},
    }

    with _state_lock:
        if sym not in _pending_orders:
            _pending_orders[sym] = []
        _pending_orders[sym].append(order)

    with _stats_lock:
        _stats["orders_created"] += 1

    log.info("[EXEC] ORDER SUBMITTED: %s | entry=[%.2f-%.2f] | SL=%.2f | TGT=%.2f | score=%d",
             sym, entry_low, entry_high, stop_loss, target, score)

    # Dispatch DB write
    _enqueue_write("create_order", {
        "order": order,
        "stock_data": stock_data,
        "scan_context": scan_context or {},
    })

    # Subscribe to live feed for this symbol
    try:
        import live_feed
        live_feed.subscribe([sym])
    except Exception:
        pass

    return True


def _check_cooldown(symbol: str, model_version: str = "legacy") -> bool:
    """Check duplicate prevention rules, scoped to (symbol, model_version).

    One 'legacy' AND one 'scoring_v1' position may coexist on the same symbol;
    each engine only blocks ITS OWN duplicate. (Engine-blind dedup previously let
    whichever engine submitted first — legacy intraday — block the other's
    strongest overlapping picks.)
    """
    # Check if THIS engine already has an open position / pending order on the symbol
    with _state_lock:
        if any(p.get("model_version") == model_version for p in _active_positions.get(symbol, [])):
            log.debug("[EXEC] Cooldown: %s already has open %s position", symbol, model_version)
            with _stats_lock:
                _stats["orders_rejected_duplicate"] += 1
            return False
        if any(o.get("model_version") == model_version for o in _pending_orders.get(symbol, [])):
            log.debug("[EXEC] Cooldown: %s already has pending %s order", symbol, model_version)
            with _stats_lock:
                _stats["orders_rejected_duplicate"] += 1
            return False

    # Check DB cooldown (5-day window), scoped to this engine's trades only
    try:
        import db
        recent = db.execute_db(
            "SELECT entry_date FROM paper_trades WHERE symbol = ? AND model_version = ? ORDER BY entry_date DESC LIMIT 1",
            (symbol, model_version), fetch="one"
        )
        if recent:
            last_dt = _date.fromisoformat(recent["entry_date"][:10])
            if (_date.today() - last_dt).days < COOLDOWN_DAYS:
                log.debug("[EXEC] Cooldown: %s traded %d days ago", symbol, (_date.today() - last_dt).days)
                with _stats_lock:
                    _stats["orders_rejected_cooldown"] += 1
                return False
    except Exception as exc:
        log.debug("[EXEC] Cooldown check error for %s: %s", symbol, exc)

    return True


# ═══════════════════════════════════════════════════════════════════════════════
# ASYNC DB WRITER — Background thread for non-blocking persistence
# ═══════════════════════════════════════════════════════════════════════════════

def _enqueue_write(action: str, data: dict):
    """Non-blocking enqueue of a DB write."""
    try:
        _write_queue.put_nowait((action, data))
    except queue.Full:
        log.warning("[EXEC] Write queue full, dropping: %s", action)


def _db_writer_loop():
    """Background thread: processes DB write queue."""
    import db

    log.info("[EXEC Writer] DB writer thread started")

    # Batch extreme updates to reduce DB load
    _extreme_buffer = {}
    _last_extreme_flush = time.time()

    while _engine_running or not _write_queue.empty():
        try:
            try:
                action, data = _write_queue.get(timeout=1.0)
            except queue.Empty:
                # Flush buffered extremes every 5 seconds
                if _extreme_buffer and (time.time() - _last_extreme_flush) > 5:
                    _flush_extremes(db, _extreme_buffer)
                    _extreme_buffer.clear()
                    _last_extreme_flush = time.time()
                continue

            if action == "create_order":
                _db_create_order(db, data)
            elif action == "fill_order":
                _db_fill_order(db, data)
            elif action == "close_position":
                _db_close_position(db, data)
            elif action == "update_extremes":
                # Buffer extremes instead of writing each tick
                tid = data.get("trade_id")
                if tid:
                    _extreme_buffer[tid] = data
                if (time.time() - _last_extreme_flush) > 5:
                    _flush_extremes(db, _extreme_buffer)
                    _extreme_buffer.clear()
                    _last_extreme_flush = time.time()

            _write_queue.task_done()

        except Exception as exc:
            log.error("[EXEC Writer] Error: %s", exc, exc_info=True)

    # Final flush
    if _extreme_buffer:
        try:
            _flush_extremes(db, _extreme_buffer)
        except Exception:
            pass

    log.info("[EXEC Writer] DB writer thread stopped")


def _flush_extremes(db, buffer: dict):
    """Batch flush extreme updates."""
    for tid, data in buffer.items():
        try:
            db.execute_db(
                "UPDATE paper_trades SET max_drawdown_pct=?, max_runup_pct=? WHERE id=?",
                (data["max_drawdown_pct"], data["max_runup_pct"], tid)
            )
        except Exception as exc:
            log.debug("[EXEC Writer] Extreme update failed for %s: %s", tid, exc)


def _derive_recommendation_id(symbol, scan_id):
    """ADR-001 (W4): RO content-address join key = sha1(symbol|scan_id|SCHEMA_VERSION)[:16].

    Deterministic, dependency-light; mirrors recommendation_engine.builder._rec_id so
    a paper order/trade can be joined back to its Recommendation Object. Returns None when
    inputs are absent (legacy rows). Never raises into the writer.
    """
    if not symbol or not scan_id:
        return None
    try:
        from recommendation_engine import SCHEMA_VERSION as _sv
    except Exception:
        _sv = "1.0.0"
    import hashlib
    return hashlib.sha1(f"{symbol}|{scan_id}|{_sv}".encode("utf-8")).hexdigest()[:16]


def _db_create_order(db, data: dict):
    """Persist a new pending order to paper_orders table."""
    order = data["order"]
    stock_data = data["stock_data"]
    scan_context = data.get("scan_context", {})
    import json

    try:
        _order_query = """
            INSERT INTO paper_orders (
                symbol, order_type, side, status,
                entry_low, entry_high, target_price, stop_loss,
                virtual_capital,
                score_at_signal, grade_at_signal,
                scan_id, signal_source,
                signal_time, order_created_at, expires_at,
                correlation_id, recommendation_id, model_version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """
        _order_params = (
            order["symbol"],
            "LIMIT",
            "BUY",
            "PENDING",
            order.get("entry_low"),
            order.get("entry_high"),
            order.get("target_price"),
            order.get("stop_loss"),
            VIRTUAL_CAPITAL,
            order.get("score_at_signal", 0),
            order.get("grade_at_signal", ""),
            scan_context.get("scan_id", ""),
            order.get("signal_source", "scanner"),
            order.get("signal_time"),
            datetime.now(_IST).isoformat(),
            order.get("expires_at"),
            scan_context.get("correlation_id", ""),
            _derive_recommendation_id(order.get("symbol"), scan_context.get("scan_id", "")),
            db._canon_model_version(order.get("model_version", "")),  # carry engine tag onto the pending order
        )
        db.execute_db(_order_query, _order_params)
        
        # P0.1E: Queue to governance DLQ for PG replay if currently on SQLite fallback
        if not db.is_postgresql() or db.pg_cooldown_active():
            db.queue_governance_write(_order_query, _order_params, artifact_type="paper_orders")

        # Get the order ID back
        row = db.execute_db(
            "SELECT MAX(id) as id FROM paper_orders WHERE symbol = ? AND status = 'PENDING'",
            (order["symbol"],), fetch="one"
        )
        if row and row.get("id"):
            # Update in-memory order with DB id
            with _state_lock:
                pending = _pending_orders.get(order["symbol"], [])
                for o in pending:
                    if o.get("signal_time") == order.get("signal_time"):
                        o["order_id"] = row["id"]
                        break

        log.info("[EXEC Writer] Order persisted: %s id=%s", order["symbol"], row.get("id") if row else "?")

    except Exception as exc:
        log.error("[EXEC Writer] create_order failed: %s", exc, exc_info=True)


def _db_fill_order(db, data: dict):
    """Transition order to FILLED and create paper_trade row."""
    order_id = data.get("order_id")
    fill_price = data["fill_price"]
    fill_time = data.get("fill_time")
    order_data = data.get("order_data", {})
    stock_data = order_data.get("stock_data", {})
    quantity = data.get("quantity", 1)

    try:
        # 1. Update order status
        if order_id:
            db.execute_db("""
                UPDATE paper_orders SET
                    status='FILLED', filled_at=?, triggered_at=?
                WHERE id=?
            """, (fill_time, fill_time, order_id))

        # 2. Calculate execution latency
        exec_latency_ms = None
        signal_time_str = data.get("signal_time") or order_data.get("signal_time")
        if signal_time_str and fill_time:
            try:
                sig_dt = datetime.fromisoformat(signal_time_str)
                fill_dt = datetime.fromisoformat(fill_time)
                # Handle timezone-naive vs aware
                if sig_dt.tzinfo and not fill_dt.tzinfo:
                    fill_dt = fill_dt.replace(tzinfo=sig_dt.tzinfo)
                elif fill_dt.tzinfo and not sig_dt.tzinfo:
                    sig_dt = sig_dt.replace(tzinfo=fill_dt.tzinfo)
                exec_latency_ms = int((fill_dt - sig_dt).total_seconds() * 1000)
            except Exception:
                pass

        # 3. Get market context
        nifty_price = None
        market_regime = "unknown"
        try:
            nifty_meta = db.get_meta("nifty50_price")
            if nifty_meta:
                nifty_price = float(nifty_meta)
            market_regime = db.get_meta("market_regime", "unknown")
        except Exception:
            pass

        entry_date = fill_time[:10] if fill_time else _date.today().isoformat()

        # 4. Insert paper_trade
        import json
        _trade_query = """
            INSERT INTO paper_trades (
                symbol, sector, entry_date, entry_price, target_price, stop_loss,
                virtual_capital, quantity,
                score_at_entry, grade_at_entry,
                technical_score, fundamental_score, earnings_momentum_score, earnings_grade,
                smart_money_score, sector_rotation_score, catalyst_score, news_sentiment_score,
                risk_score, risk_reward,
                model_version, market_regime, nifty_entry,
                high_conviction, is_golden, signals_json, earnings_signals_json,
                weight_version, confidence_score, entry_rank,
                breadth_advances, breadth_declines, breadth_ratio,
                status, entry_time, order_id, fill_price, execution_latency_ms,
                scan_id, recommendation_id, source
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """
        _trade_params = (
            order_data.get("symbol", stock_data.get("symbol", "")),
            stock_data.get("sector", ""),
            entry_date,
            fill_price,
            order_data.get("target_price", stock_data.get("target_price")),
            order_data.get("stop_loss", stock_data.get("stop_loss")),
            VIRTUAL_CAPITAL,
            quantity,
            stock_data.get("score", 0),
            stock_data.get("grade", ""),
            stock_data.get("technical_score", 0),
            stock_data.get("fundamental_score", 0),
            stock_data.get("earnings_momentum_score", 0),
            stock_data.get("earnings_grade", ""),
            stock_data.get("smart_money_score", 0),
            stock_data.get("sector_rotation_score", 0),
            stock_data.get("marketaux_catalyst_score", 0),
            stock_data.get("news_sentiment_score", 0),
            stock_data.get("risk_score", 0),
            stock_data.get("risk_reward", 0),
            db._canon_model_version(stock_data.get("model_version", "")),  # canonical sink tag
            market_regime,
            nifty_price,
            1 if stock_data.get("high_conviction") else 0,
            1 if stock_data.get("is_golden") else 0,
            json.dumps(stock_data.get("signals", [])[:10]),
            json.dumps(stock_data.get("earnings_signals", [])[:10]),
            stock_data.get("weight_version", "R2.1"),
            stock_data.get("confidence_score", 0),
            stock_data.get("_entry_rank", 0),
            stock_data.get("_breadth_advances", 0),
            stock_data.get("_breadth_declines", 0),
            stock_data.get("_breadth_ratio", 0),
            "OPEN",
            fill_time,
            order_id,
            fill_price,
            exec_latency_ms,
            order_data.get("scan_context", {}).get("scan_id", ""),
            _derive_recommendation_id(
                order_data.get("symbol", stock_data.get("symbol", "")),
                order_data.get("scan_context", {}).get("scan_id", ""),
            ),
            "QUANT",
        )
        db.execute_db(_trade_query, _trade_params)
        
        # P0.1E: Queue to governance DLQ for PG replay if currently on SQLite fallback
        if not db.is_postgresql() or db.pg_cooldown_active():
            db.queue_governance_write(_trade_query, _trade_params, artifact_type="paper_trades_open")

        # 5. Get trade ID and update in-memory position
        row = db.execute_db(
            "SELECT MAX(id) as id FROM paper_trades WHERE symbol = ? AND status = 'OPEN'",
            (order_data.get("symbol", stock_data.get("symbol", "")),), fetch="one"
        )
        trade_id = row["id"] if row else None

        if trade_id:
            symbol = order_data.get("symbol", stock_data.get("symbol", "")).upper().replace(".NS", "")
            with _state_lock:
                positions = _active_positions.get(symbol, [])
                for pos in positions:
                    if pos.get("order_id") == order_id or pos.get("id") is None:
                        pos["id"] = trade_id
                        break

        # 6. Invalidate caches
        try:
            import cache_layer
            cache_layer.invalidate_stats()
        except Exception:
            pass

        log.info("[EXEC Writer] Trade created: %s id=%s @ ₹%.2f | latency=%sms",
                 order_data.get("symbol"), trade_id, fill_price, exec_latency_ms)

    except Exception as exc:
        log.error("[EXEC Writer] fill_order failed: %s", exc, exc_info=True)


def _db_close_position(db, data: dict):
    """Persist position closure."""
    trade_id = data.get("trade_id")
    symbol = data.get("symbol")
    exit_price = data["exit_price"]
    exit_time = data.get("exit_time")
    exit_reason = data["exit_reason"]
    entry_price = data.get("entry_price", 0)
    entry_date = data.get("entry_date", "")

    try:
        # If trade_id is None (position loaded before DB assigned id), look it up
        if not trade_id and symbol:
            row = db.execute_db(
                "SELECT id FROM paper_trades WHERE symbol = ? AND status = 'OPEN' ORDER BY id DESC LIMIT 1",
                (symbol,), fetch="one"
            )
            if row:
                trade_id = row["id"]

        if not trade_id:
            log.warning("[EXEC Writer] Cannot close position: no trade_id for %s", symbol)
            return

        # Calculate return and alpha
        return_pct = ((exit_price - entry_price) / entry_price) * 100 if entry_price > 0 else 0

        alpha_pct = None
        try:
            trade = db.execute_db("SELECT nifty_entry FROM paper_trades WHERE id = ?", (trade_id,), fetch="one")
            nifty_entry = trade.get("nifty_entry") if trade else None
            nifty_price = None
            nifty_meta = db.get_meta("nifty50_price")
            if nifty_meta:
                nifty_price = float(nifty_meta)
            if nifty_entry and nifty_price and nifty_entry > 0:
                nifty_return = ((nifty_price - nifty_entry) / nifty_entry) * 100
                alpha_pct = return_pct - nifty_return
        except Exception:
            pass

        # Days held
        days_held = 0
        try:
            if entry_date:
                entry_dt = _date.fromisoformat(entry_date[:10])
                days_held = (_date.today() - entry_dt).days
        except Exception:
            pass

        exit_date = exit_time[:10] if exit_time else _date.today().isoformat()

        _close_query = """
            UPDATE paper_trades SET
                exit_date=?, exit_price=?, exit_reason=?,
                nifty_exit=?, days_held=?, return_pct=?, alpha_pct=?,
                status='CLOSED', exit_time=?, updated_at=?
            WHERE id=?
        """
        _close_params = (
            exit_date, exit_price, exit_reason,
            float(db.get_meta("nifty50_price") or 0) or None,
            days_held, round(return_pct, 2),
            round(alpha_pct, 2) if alpha_pct is not None else None,
            exit_time, datetime.now(_IST).isoformat(),
            trade_id,
        )
        db.execute_db(_close_query, _close_params)
        
        # P0.1E: Queue to governance DLQ for PG replay if currently on SQLite fallback
        if not db.is_postgresql() or db.pg_cooldown_active():
            db.queue_governance_write(_close_query, _close_params, artifact_type="paper_trades_close")

        log.info("[EXEC Writer] Trade closed: %s id=%s | %s | return=%.2f%%",
                 symbol, trade_id, exit_reason, return_pct)

        # Invalidate caches
        try:
            import cache_layer
            cache_layer.invalidate_stats()
        except Exception:
            pass

        # R1 Evidence Collection
        try:
            from pathlib import Path
            _R1_DEPLOY_DATE = "2026-06-08"
            _obs_day = (_date.today() - _date.fromisoformat(_R1_DEPLOY_DATE)).days + 1
            _scan_id = db.get_meta("current_scan_id") or "manual"
            _outcomes_path = Path(__file__).parent / "release_audits" / "trade_outcomes.csv"
            _outcomes_path.parent.mkdir(parents=True, exist_ok=True)
            _write_header = not _outcomes_path.exists()
            import csv as _csv
            with open(_outcomes_path, "a", newline="", encoding="utf-8") as f:
                w = _csv.writer(f)
                if _write_header:
                    w.writerow([
                        "Release Version", "Observation Day", "Scan ID",
                        "Date Opened", "Date Closed", "Symbol",
                        "Entry Price", "Exit Price", "Exit Reason",
                        "Return %", "Win/Loss",
                    ])
                w.writerow([
                    "R2.0-EXEC", _obs_day, _scan_id,
                    entry_date, exit_date, symbol,
                    entry_price, exit_price, exit_reason,
                    round(return_pct, 2), "WIN" if return_pct > 0 else "LOSS",
                ])
        except Exception:
            pass

    except Exception as exc:
        log.error("[EXEC Writer] close_position failed: %s", exc, exc_info=True)


# ═══════════════════════════════════════════════════════════════════════════════
# ENGINE LIFECYCLE
# ═══════════════════════════════════════════════════════════════════════════════

def initialize_engine():
    """Start the execution engine. Called once from app.py on startup."""
    global _engine_running, _writer_thread
    if _engine_running:
        log.warning("[EXEC] Engine already running")
        return

    _engine_running = True

    # Start DB writer thread
    _writer_thread = threading.Thread(target=_db_writer_loop, daemon=True, name="exec-writer")
    _writer_thread.start()

    # Load state from DB
    _load_state()

    log.info("[EXEC] ═══ Execution Engine STARTED ═══")
    log.info("[EXEC] Pending orders: %d | Active positions: %d",
             sum(len(v) for v in _pending_orders.values()),
             sum(len(v) for v in _active_positions.values()))


def stop_engine():
    """Gracefully stop the execution engine."""
    global _engine_running
    _engine_running = False
    log.info("[EXEC] Engine stop requested. Writer queue depth: %d", _write_queue.qsize())


def _load_state():
    """Load pending orders and open positions from DB into memory."""
    import db

    # 1. Load pending orders
    try:
        pending = db.execute_db(
            "SELECT * FROM paper_orders WHERE status = 'PENDING' ORDER BY order_created_at",
            fetch="all"
        ) or []

        with _state_lock:
            _pending_orders.clear()
            for o in pending:
                sym = o["symbol"]
                if sym not in _pending_orders:
                    _pending_orders[sym] = []
                _pending_orders[sym].append({
                    "order_id": o["id"],
                    "symbol": sym,
                    "model_version": o.get("model_version") or "legacy",  # model-aware dedup across restarts
                    "entry_low": o.get("entry_low"),
                    "entry_high": o.get("entry_high"),
                    "target_price": o.get("target_price"),
                    "stop_loss": o.get("stop_loss"),
                    "score_at_signal": o.get("score_at_signal", 0),
                    "signal_time": str(o.get("signal_time", "")),
                    "expires_at": str(o.get("expires_at", "")),
                    "signal_source": o.get("signal_source", "scanner"),
                    "stock_data": {},  # Not stored in DB — only needed at submission time
                    "scan_context": {},
                })
        log.info("[EXEC] Loaded %d pending orders from DB", len(pending))
    except Exception as exc:
        log.warning("[EXEC] Failed to load pending orders: %s", exc)

    # 2. Load open positions
    try:
        positions = db.execute_db(
            "SELECT * FROM paper_trades WHERE status = 'OPEN' ORDER BY entry_date",
            fetch="all"
        ) or []

        with _state_lock:
            _active_positions.clear()
            for p in positions:
                sym = p["symbol"]
                if sym not in _active_positions:
                    _active_positions[sym] = []
                _active_positions[sym].append({
                    "id": p["id"],
                    "symbol": sym,
                    "model_version": p.get("model_version") or "legacy",  # model-aware dedup across restarts
                    "entry_price": p["entry_price"],
                    "entry_date": p.get("entry_date", ""),
                    "entry_time_str": str(p.get("entry_time") or p.get("entry_date", "")),
                    "target_price": p.get("target_price"),
                    "stop_loss": p.get("stop_loss"),
                    "quantity": p.get("quantity", 0),
                    "max_drawdown_pct": p.get("max_drawdown_pct", 0),
                    "max_runup_pct": p.get("max_runup_pct", 0),
                    "order_id": p.get("order_id"),
                })
        log.info("[EXEC] Loaded %d open positions from DB", len(positions))
    except Exception as exc:
        log.warning("[EXEC] Failed to load open positions: %s", exc)

    # 3. Subscribe loaded symbols to live feed
    try:
        import live_feed
        all_syms = set()
        with _state_lock:
            all_syms.update(_pending_orders.keys())
            all_syms.update(_active_positions.keys())
        if all_syms:
            live_feed.subscribe(list(all_syms))
            log.info("[EXEC] Subscribed %d symbols to live feed", len(all_syms))
    except Exception:
        pass


def expire_stale_orders():
    """Expire PENDING orders older than ORDER_EXPIRY_DAYS. Called once daily."""
    import db

    try:
        cutoff = (datetime.now(_IST) - timedelta(days=ORDER_EXPIRY_DAYS)).isoformat()
        expired = db.execute_db(
            "UPDATE paper_orders SET status='EXPIRED', cancelled_at=? WHERE status='PENDING' AND order_created_at < ?",
            (datetime.now(_IST).isoformat(), cutoff), fetch="rowcount"
        )

        # Also remove from in-memory
        with _state_lock:
            for sym in list(_pending_orders.keys()):
                orders = _pending_orders[sym]
                _pending_orders[sym] = [
                    o for o in orders
                    if o.get("expires_at", "") > cutoff
                ]
                if not _pending_orders[sym]:
                    del _pending_orders[sym]

        if expired:
            log.info("[EXEC] Expired %d stale pending orders", expired)
    except Exception as exc:
        log.warning("[EXEC] Order expiry failed: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _is_market_hours(tick_time: datetime) -> bool:
    """Check if tick_time is within NSE market hours (09:15 - 15:30 IST)."""
    if tick_time is None:
        return False
    # Ensure IST
    if tick_time.tzinfo:
        ist_time = tick_time.astimezone(_IST)
    else:
        ist_time = tick_time
    if ist_time.weekday() >= 5:
        return False
    mins = ist_time.hour * 60 + ist_time.minute
    return 555 <= mins <= 930  # 09:15 to 15:30


# ═══════════════════════════════════════════════════════════════════════════════
# PT-7: STATE RECONCILIATION — Permanent Production Monitor
# ═══════════════════════════════════════════════════════════════════════════════

def reconcile_state() -> dict:
    """Compare DB state vs in-memory state. Returns delta report.
    
    Used by PT-7 certification and as permanent production health check.
    Returns dict with:
      pending_delta, position_delta, order_delta
      All should be 0 in healthy state.
    """
    import db

    report = {
        "pending_db": 0,
        "pending_memory": 0,
        "pending_delta": 0,
        "position_db": 0,
        "position_memory": 0,
        "position_delta": 0,
        "pending_symbol_diff": [],
        "position_symbol_diff": [],
        "healthy": False,
    }

    try:
        # Pending orders
        db_pending = db.execute_db(
            "SELECT symbol, COUNT(*) as cnt FROM paper_orders WHERE status = 'PENDING' GROUP BY symbol",
            fetch="all"
        ) or []
        db_pending_map = {r["symbol"]: r["cnt"] for r in db_pending}
        report["pending_db"] = sum(db_pending_map.values())

        with _state_lock:
            mem_pending_map = {sym: len(orders) for sym, orders in _pending_orders.items()}
        report["pending_memory"] = sum(mem_pending_map.values())
        report["pending_delta"] = report["pending_db"] - report["pending_memory"]

        # Find symbol-level differences
        all_syms = set(db_pending_map.keys()) | set(mem_pending_map.keys())
        for sym in all_syms:
            db_cnt = db_pending_map.get(sym, 0)
            mem_cnt = mem_pending_map.get(sym, 0)
            if db_cnt != mem_cnt:
                report["pending_symbol_diff"].append(
                    {"symbol": sym, "db": db_cnt, "memory": mem_cnt}
                )

        # Open positions
        db_positions = db.execute_db(
            "SELECT symbol, COUNT(*) as cnt FROM paper_trades WHERE status = 'OPEN' GROUP BY symbol",
            fetch="all"
        ) or []
        db_pos_map = {r["symbol"]: r["cnt"] for r in db_positions}
        report["position_db"] = sum(db_pos_map.values())

        with _state_lock:
            mem_pos_map = {sym: len(pos) for sym, pos in _active_positions.items()}
        report["position_memory"] = sum(mem_pos_map.values())
        report["position_delta"] = report["position_db"] - report["position_memory"]

        all_pos_syms = set(db_pos_map.keys()) | set(mem_pos_map.keys())
        for sym in all_pos_syms:
            db_cnt = db_pos_map.get(sym, 0)
            mem_cnt = mem_pos_map.get(sym, 0)
            if db_cnt != mem_cnt:
                report["position_symbol_diff"].append(
                    {"symbol": sym, "db": db_cnt, "memory": mem_cnt}
                )

        report["healthy"] = (report["pending_delta"] == 0 and report["position_delta"] == 0)

        if report["healthy"]:
            log.info("[EXEC Reconcile] State healthy: %d pending, %d positions",
                     report["pending_db"], report["position_db"])
        else:
            log.warning("[EXEC Reconcile] STATE DESYNC: pending_delta=%d, position_delta=%d",
                        report["pending_delta"], report["position_delta"])

    except Exception as exc:
        log.error("[EXEC Reconcile] Reconciliation failed: %s", exc)
        report["error"] = str(exc)

    return report

