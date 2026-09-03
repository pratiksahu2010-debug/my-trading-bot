"""
telegram_notify.py
-------------------
All outbound Telegram messages go through here. Uses the raw Bot API over
HTTPS (no extra SDK dependency needed beyond `requests`).

Each bot in config.BOTS has its own token + chat id, so alerts for BOT1
never leak into BOT2's chat, etc.
"""

import logging
import requests
from datetime import datetime

log = logging.getLogger("telegram")

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"


def _send(token: str, chat_id: str, text: str, retries: int = 2) -> str:
    """
    Low-level send with retry. Returns the Telegram message_id on success,
    or '' on failure (caller should log to ErrorLog).
    """
    if not token or not chat_id:
        log.warning("[TELEGRAM] Missing token/chat_id - message not sent (DRY RUN?)")
        return ""

    url = TELEGRAM_API_BASE.format(token=token)
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}

    last_err = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("ok"):
                return str(data["result"]["message_id"])
            last_err = data
        except Exception as e:
            last_err = e
        log.warning(f"[TELEGRAM] send attempt {attempt+1} failed: {last_err}")
    log.error(f"[TELEGRAM] All attempts failed: {last_err}")
    return ""


def send_trade_alert(token, chat_id, bot_name, signal_result, symbol) -> str:
    """
    Formats and sends the trade alert exactly per the requested template.
    Returns the Telegram message_id (stored back into AlertLog).
    """
    r = signal_result
    checkmark = "✅"
    now_ist = datetime.now().strftime("%Y-%m-%d %H:%M:%S IST")

    text = (
        f"📊 *{bot_name}*\n"
        f"🚨 TRADE ALERT: {symbol}\n"
        f"📈 Signal: {r.direction}\n"
        f"💰 Price: ₹{r.price:.2f}\n"
        f"📊 RSI: {r.rsi:.1f} {checkmark}\n"
        f"📈 ADX: {r.adx:.1f} {checkmark}\n"
        f"📉 VWAP: ₹{r.vwap:.2f} (MANDATORY ✓)\n"
        f"🔴 9 EMA: ₹{r.ema9:.2f}\n"
        f"🟡 21 EMA: ₹{r.ema21:.2f}\n"
        f"📊 Volume: {int(r.volume):,} (Above Avg: {'YES' if r.volume > r.vol_avg20 else 'NO'})\n"
        f"📏 Distance from VWAP: {r.vwap_distance_pct:.2f}%\n"
        f"⭐ Confidence: {r.confidence}\n"
        f"🎯 Score: {r.score}/10\n"
        f"⏰ Time: {now_ist}"
    )
    return _send(token, chat_id, text)


def send_daily_summary(token, chat_id, bot_name, total_alerts, top_symbols):
    lines = [f"📋 *{bot_name} - DAILY SUMMARY*", f"🚨 Total alerts today: {total_alerts}"]
    if top_symbols:
        lines.append("🔥 Most active symbols:")
        for sym, count in top_symbols:
            lines.append(f"   • {sym}: {count} alert(s)")
    _send(token, chat_id, "\n".join(lines))


def send_error_summary(token, chat_id, bot_name, total_errors, breakdown):
    lines = [f"⚠️ *{bot_name} - ERROR SUMMARY*", f"Total errors today: {total_errors}"]
    for err_type, count in breakdown:
        lines.append(f"   • {err_type}: {count}")
    if not breakdown:
        lines.append("No errors today ✅")
    _send(token, chat_id, "\n".join(lines))


def send_health_check(token, chat_id, bot_name, symbol_count):
    text = (
        f"🤖 *{bot_name} INITIALIZED*\n"
        f"📊 Monitoring: {symbol_count} symbols\n"
        f"⏰ Schedule: Every 15 minutes (9:15 AM - 3:30 PM IST)\n"
        f"📋 VWAP: MANDATORY\n"
        f"🔒 Strict Mode: score ≥ 8/10 required\n"
        f"✅ Bot is LIVE and scanning!"
    )
    _send(token, chat_id, text)
