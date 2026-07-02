"""
telegram_alerts.py — Paper Trade Notifications via Telegram Bot
================================================================
Sends formatted alerts to a Telegram group/channel when:
  - Paper trade ENTRY (order filled)
  - Paper trade EXIT (target hit / SL hit / time exit)

Non-blocking: all sends happen in background threads.
Graceful: if Telegram is not configured, silently does nothing.

Env vars:
  TELEGRAM_BOT_TOKEN = bot token from @BotFather
  TELEGRAM_CHAT_ID   = group chat ID (negative number for groups)
"""

import os
import logging
import threading
from datetime import datetime, timezone, timedelta

log = logging.getLogger("screener")

_IST = timezone(timedelta(hours=5, minutes=30))

# ─── Config ──────────────────────────────────────────────────────────────────

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ENABLED = bool(BOT_TOKEN and CHAT_ID)

TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"


# ─── Core Send ───────────────────────────────────────────────────────────────

def _send_message(text: str, parse_mode: str = "HTML"):
    """Send a message to the configured Telegram chat. Blocking call."""
    if not ENABLED:
        return

    import requests
    try:
        resp = requests.post(
            TELEGRAM_API_URL,
            json={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            log.warning("[Telegram] Send failed: %s %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        log.warning("[Telegram] Send error: %s", exc)


def _send_async(text: str):
    """Fire-and-forget message send in background thread."""
    if not ENABLED:
        return
    threading.Thread(target=_send_message, args=(text,), daemon=True).start()


# ─── Paper Trade Alerts ─────────────────────────────────────────────────────

def send_entry_alert(trade_data: dict):
    """
    Send alert when a paper trade order is FILLED.
    
    trade_data should contain:
      symbol, entry_price, target_price, stop_loss, quantity,
      score_at_entry, grade_at_entry, confidence_score, risk_reward,
      sector, high_conviction, is_golden
    """
    sym = trade_data.get("symbol", "?")
    entry = trade_data.get("entry_price", 0)
    target = trade_data.get("target_price", 0)
    sl = trade_data.get("stop_loss", 0)
    qty = trade_data.get("quantity", 0)
    score = trade_data.get("score_at_entry", 0)
    grade = trade_data.get("grade_at_entry", "")
    confidence = trade_data.get("confidence_score", 0)
    rr = trade_data.get("risk_reward", 0)
    sector = trade_data.get("sector", "")
    hc = trade_data.get("high_conviction", False)
    golden = trade_data.get("is_golden", False)

    invested = round(entry * qty, 2)
    target_profit = round((target - entry) * qty, 2) if target and entry else 0
    sl_risk = round((entry - sl) * qty, 2) if sl and entry else 0
    target_pct = round(((target - entry) / entry) * 100, 1) if target and entry else 0
    sl_pct = round(((sl - entry) / entry) * 100, 1) if sl and entry else 0

    # Badge
    badge = ""
    if golden:
        badge = "🌟 GOLDEN PICK"
    elif hc:
        badge = "🔥 HIGH CONVICTION"
    else:
        badge = "📈 PAPER TRADE"

    now = datetime.now(_IST).strftime("%d-%b %H:%M")

    text = f"""<b>{badge} — ENTRY</b>

<b>📊 {sym}</b> | {sector}
━━━━━━━━━━━━━━━━━━━

💰 <b>Entry:</b> ₹{entry:,.2f}
📦 <b>Qty:</b> {qty} | <b>Invested:</b> ₹{invested:,.0f}

🎯 <b>Target:</b> ₹{target:,.2f} ({target_pct:+.1f}%)
   → Profit: ₹{target_profit:,.0f}
🛡 <b>SL:</b> ₹{sl:,.2f} ({sl_pct:+.1f}%)
   → Risk: ₹{sl_risk:,.0f}

⚖️ <b>R:R =</b> {rr:.1f} | <b>Score:</b> {score} | <b>Confidence:</b> {confidence:.0f}%
📊 <b>Grade:</b> {grade}

🕐 {now}"""

    _send_async(text)
    log.info("[Telegram] Entry alert sent: %s @ ₹%.2f", sym, entry)


def send_exit_alert(trade_data: dict):
    """
    Send alert when a paper trade position is CLOSED.
    
    trade_data should contain:
      symbol, entry_price, exit_price, exit_reason, return_pct,
      quantity, days_held, alpha_pct
    """
    sym = trade_data.get("symbol", "?")
    entry = trade_data.get("entry_price", 0)
    exit_price = trade_data.get("exit_price", 0)
    reason = trade_data.get("exit_reason", "UNKNOWN")
    return_pct = trade_data.get("return_pct", 0)
    qty = trade_data.get("quantity", 0)
    days = trade_data.get("days_held", 0)
    alpha = trade_data.get("alpha_pct")

    invested = round(entry * qty, 2) if entry and qty else 0
    exit_value = round(exit_price * qty, 2) if exit_price and qty else 0
    pnl = round(exit_value - invested, 2)

    # Emoji based on outcome
    if "TARGET" in reason:
        emoji = "✅"
        outcome = "TARGET HIT"
    elif "STOP" in reason or "SL" in reason:
        emoji = "❌"
        outcome = "STOPLOSS HIT"
    elif "TIME" in reason:
        emoji = "⏰"
        outcome = "TIME EXIT"
    else:
        emoji = "📤"
        outcome = reason

    # P/L emoji
    pnl_emoji = "🟢" if pnl >= 0 else "🔴"

    now = datetime.now(_IST).strftime("%d-%b %H:%M")

    text = f"""<b>{emoji} PAPER TRADE — {outcome}</b>

<b>📊 {sym}</b>
━━━━━━━━━━━━━━━━━━━

💰 <b>Entry:</b> ₹{entry:,.2f} → <b>Exit:</b> ₹{exit_price:,.2f}
📦 <b>Qty:</b> {qty}

{pnl_emoji} <b>P/L:</b> ₹{pnl:,.0f} ({return_pct:+.1f}%)
📅 <b>Held:</b> {days} days"""

    if alpha is not None:
        text += f"\n📊 <b>Alpha vs Nifty:</b> {alpha:+.1f}%"

    text += f"\n\n🕐 {now}"

    _send_async(text)
    log.info("[Telegram] Exit alert sent: %s | %s | %.1f%%", sym, reason, return_pct)


def send_scan_complete_alert(total_results: int, hc_count: int, golden_count: int,
                              duration_s: float, universe_size: int):
    """Send alert when a full scan completes."""
    now = datetime.now(_IST).strftime("%d-%b %H:%M")
    text = f"""<b>🔍 SCAN COMPLETE</b>

📊 <b>{total_results}</b> stocks analyzed from {universe_size} universe
🔥 <b>{hc_count}</b> High Conviction | 🌟 <b>{golden_count}</b> Golden Picks
⏱ Duration: {duration_s:.0f}s

🕐 {now}"""

    _send_async(text)


def send_test_alert():
    """Send a test message to verify Telegram is working."""
    if not ENABLED:
        log.warning("[Telegram] Not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
        print("❌ Telegram not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
        return False

    text = """<b>✅ AI Smart Scanner — Telegram Connected!</b>

Paper trade alerts will be sent to this chat.
Bot is working correctly."""

    _send_message(text)
    print("✅ Test message sent to Telegram!")
    return True
