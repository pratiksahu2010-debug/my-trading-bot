"""
main.py
-------
Entry point deployed on Render.com as a Web Service.

Render web services must bind to $PORT and respond to HTTP, so this runs
a tiny Flask app (for health checks / manual trigger endpoints) alongside
an APScheduler background scheduler that does the actual work:

  - Every 15 min, 09:15-15:30 IST (Mon-Fri): checkAllSymbols() for all 3 bots
  - 09:10 IST daily: reset cooldowns
  - 09:15 IST daily: health-check "bot is alive" ping
  - 15:45 IST daily: daily summary
  - 16:00 IST daily: error summary

Run locally with:  python main.py
Deploy on Render:  gunicorn main:app  (see Procfile)
"""

import logging
import sys
from datetime import datetime

from flask import Flask, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import config
import telegram_notify
from storage import BotStorage
from cooldown_manager import CooldownManager
from angel_one_feed import feed
from indicators import enrich_dataframe
from scoring import evaluate, should_alert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("main")

app = Flask(__name__)

# ---------------------------------------------------------------------- #
# Bootstrap: storage + cooldown manager per bot
# ---------------------------------------------------------------------- #
_bot_runtime = {}
for bot_id, bot_cfg in config.BOTS.items():
    storage = BotStorage(bot_cfg["sqlite_path"], bot_cfg["symbols"])
    _bot_runtime[bot_id] = {
        "storage": storage,
        "cooldown": CooldownManager(storage),
    }


def _env(name):
    import os
    return os.environ.get(name, "")


# ---------------------------------------------------------------------- #
# Core per-symbol processing
# ---------------------------------------------------------------------- #
def process_symbol(bot_id: str, symbol: str):
    """
    Fetch data -> compute indicators -> score -> (maybe) alert -> log.
    Every failure path logs to ErrorLog and returns cleanly (never raises).
    """
    bot_cfg = config.BOTS[bot_id]
    rt = _bot_runtime[bot_id]
    storage, cooldown = rt["storage"], rt["cooldown"]

    try:
        df_raw = feed.get_historical_candles(symbol)
        if df_raw.empty or len(df_raw) < 21:
            storage.log_error(symbol, "NO_DATA", "Insufficient candle data returned")
            storage.record_failure(symbol, config.MAX_CONSECUTIVE_FAILS_BROKEN,
                                    config.MAX_CONSECUTIVE_FAILS_DISABLE)
            return

        df = enrich_dataframe(df_raw)

        # Prefer live websocket LTP for the "current price" if available
        live_ltp = feed.get_live_ltp(symbol)
        if live_ltp:
            df.iloc[-1, df.columns.get_loc("close")] = live_ltp

        result = evaluate(df)

        if result.reject_reason == "VWAP_UNAVAILABLE":
            storage.log_error(symbol, "VWAP_MISSING", "VWAP is N/A - alert skipped (mandatory rule)")
            return  # NOT a failure, just a legitimate skip per strict rules

        if not should_alert(result):
            storage.reset_fail_counts_if_healthy(symbol)
            return  # scored below threshold, or no clear direction - no alert, no error

        if not cooldown.can_alert(symbol):
            return  # in cooldown window, skip silently

        # --- fire the alert ---
        token = _env(bot_cfg["token_env"])
        chat_id = _env(bot_cfg["chat_id_env"])
        message_id = telegram_notify.send_trade_alert(
            token, chat_id, bot_cfg["name"], result, symbol
        )
        storage.log_alert(
            symbol, result.direction, result.price, result.vwap, result.rsi,
            result.adx, result.ema9, result.ema21, result.volume,
            result.confidence, result.score, message_id,
        )
        cooldown.start_cooldown(symbol)
        storage.reset_fail_counts_if_healthy(symbol)
        log.info(f"[{bot_id}] ALERT SENT: {symbol} {result.direction} score={result.score}/10")

    except Exception as e:
        log.exception(f"[{bot_id}] Unhandled error processing {symbol}")
        storage.log_error(symbol, "UNHANDLED_EXCEPTION", str(e))
        storage.record_failure(symbol, config.MAX_CONSECUTIVE_FAILS_BROKEN,
                                config.MAX_CONSECUTIVE_FAILS_DISABLE)


def check_all_symbols(bot_id: str):
    """
    Master scan function for one bot: loops through its active Settings
    rows and processes each symbol. Rate-limited to be gentle on the API
    (batches of 5, ~1s between calls as requested).
    """
    if not _is_market_hours():
        log.info(f"[{bot_id}] Outside market hours, skipping scan")
        return

    rt = _bot_runtime[bot_id]
    active_rows = rt["storage"].get_active_symbols()
    symbols = [r["symbol"] for r in active_rows]
    log.info(f"[{bot_id}] Scanning {len(symbols)} active symbols...")

    import time
    batch_size = 5
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        for sym in batch:
            process_symbol(bot_id, sym)
            time.sleep(1)  # rate limit: 1s between calls


def _is_market_hours() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:  # Sat/Sun
        return False
    open_t = datetime.strptime(config.MARKET_OPEN, "%H:%M").time()
    close_t = datetime.strptime(config.MARKET_CLOSE, "%H:%M").time()
    return open_t <= now.time() <= close_t


# ---------------------------------------------------------------------- #
# Scheduled jobs
# ---------------------------------------------------------------------- #
def job_scan_all_bots():
    for bot_id in config.BOTS:
        check_all_symbols(bot_id)


def job_morning_reset():
    for bot_id, bot_cfg in config.BOTS.items():
        _bot_runtime[bot_id]["cooldown"].reset_all()
        token, chat_id = _env(bot_cfg["token_env"]), _env(bot_cfg["chat_id_env"])
        telegram_notify.send_health_check(token, chat_id, bot_cfg["name"], len(bot_cfg["symbols"]))
    log.info("Morning reset + health check complete for all bots")


def job_daily_summary():
    for bot_id, bot_cfg in config.BOTS.items():
        storage = _bot_runtime[bot_id]["storage"]
        total = storage.count_alerts_today()
        top = storage.top_symbols_today()
        token, chat_id = _env(bot_cfg["token_env"]), _env(bot_cfg["chat_id_env"])
        telegram_notify.send_daily_summary(token, chat_id, bot_cfg["name"], total, top)


def job_error_summary():
    for bot_id, bot_cfg in config.BOTS.items():
        storage = _bot_runtime[bot_id]["storage"]
        total, breakdown = storage.errors_today_summary()
        token, chat_id = _env(bot_cfg["token_env"]), _env(bot_cfg["chat_id_env"])
        telegram_notify.send_error_summary(token, chat_id, bot_cfg["name"], total, breakdown)


# ---------------------------------------------------------------------- #
# Startup: Angel One login + websocket + scheduler
# ---------------------------------------------------------------------- #
def bootstrap():
    log.info("Booting NSE Alert Bot system...")
    feed.login()

    all_symbols = sorted({s for cfg in config.BOTS.values() for s in cfg["symbols"]})
    feed.load_instrument_master(all_symbols)
    feed.start_websocket(all_symbols)

    scheduler = BackgroundScheduler(timezone=config.TIMEZONE)

    open_h, open_m = map(int, config.MARKET_OPEN.split(":"))
    close_h, close_m = map(int, config.MARKET_CLOSE.split(":"))

    scheduler.add_job(
        job_scan_all_bots, CronTrigger(
            day_of_week="mon-fri",
            hour=f"{open_h}-{close_h}",
            minute=f"*/{config.SCAN_INTERVAL_MINUTES}",
        ),
        id="scan_all_bots",
    )

    reset_h, reset_m = map(int, config.MORNING_RESET_TIME.split(":"))
    scheduler.add_job(job_morning_reset, CronTrigger(day_of_week="mon-fri", hour=reset_h, minute=reset_m),
                       id="morning_reset")

    sum_h, sum_m = map(int, config.DAILY_SUMMARY_TIME.split(":"))
    scheduler.add_job(job_daily_summary, CronTrigger(day_of_week="mon-fri", hour=sum_h, minute=sum_m),
                       id="daily_summary")

    err_h, err_m = map(int, config.ERROR_SUMMARY_TIME.split(":"))
    scheduler.add_job(job_error_summary, CronTrigger(day_of_week="mon-fri", hour=err_h, minute=err_m),
                       id="error_summary")

    scheduler.start()
    log.info("Scheduler started with 4 jobs (scan/reset/summary/errors)")
    return scheduler


_scheduler = bootstrap()


# ---------------------------------------------------------------------- #
# HTTP endpoints (health check + manual trigger, required by Render)
# ---------------------------------------------------------------------- #
@app.route("/")
@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "time": datetime.now().isoformat(),
        "bots": {bid: len(cfg["symbols"]) for bid, cfg in config.BOTS.items()},
        "dry_run": config.DRY_RUN,
    })


@app.route("/trigger/<bot_id>")
def manual_trigger(bot_id):
    """
    Manual test endpoint, e.g. /trigger/BOT1 - runs one scan immediately.
    Add ?force=true to bypass the market-hours check (e.g. /trigger/BOT1?force=true)
    so you can test outside 9:15-15:30 IST without waiting.
    """
    from flask import request
    if bot_id not in config.BOTS:
        return jsonify({"error": "unknown bot_id"}), 404
    force = request.args.get("force", "false").lower() == "true"
    if force:
        log.info(f"[{bot_id}] Manual trigger with force=true, ignoring market hours")
        rt = _bot_runtime[bot_id]
        symbols = [r["symbol"] for r in rt["storage"].get_active_symbols()]
        for sym in symbols:
            process_symbol(bot_id, sym)
    else:
        check_all_symbols(bot_id)
    return jsonify({"status": "scan triggered", "bot_id": bot_id, "forced": force})


@app.route("/status/<bot_id>")
def bot_status(bot_id):
    """
    Diagnostic endpoint - answers "why am I not getting alerts?" without
    digging through Render logs. Hit e.g. /status/BOT1
    """
    if bot_id not in config.BOTS:
        return jsonify({"error": "unknown bot_id"}), 404

    bot_cfg = config.BOTS[bot_id]
    storage = _bot_runtime[bot_id]["storage"]
    token_set = bool(_env(bot_cfg["token_env"]))
    chat_id_set = bool(_env(bot_cfg["chat_id_env"]))

    active_rows = storage.get_active_symbols()
    broken = [r["symbol"] for r in active_rows if r["status"] == "BROKEN"]
    total_errors, error_breakdown = storage.errors_today_summary()

    with storage._conn() as conn:
        recent_errors = [
            dict(r) for r in conn.execute(
                "SELECT * FROM error_log ORDER BY id DESC LIMIT 10"
            ).fetchall()
        ]

    return jsonify({
        "bot_id": bot_id,
        "bot_name": bot_cfg["name"],
        "is_market_hours_now": _is_market_hours(),
        "dry_run": config.DRY_RUN,
        "angel_logged_in": feed._logged_in,
        "angel_instrument_tokens_resolved": len(feed.instrument_map),
        "telegram_token_configured": token_set,
        "telegram_chat_id_configured": chat_id_set,
        "active_symbols": len(active_rows),
        "broken_symbols": broken,
        "alerts_sent_today": storage.count_alerts_today(),
        "errors_today_total": total_errors,
        "errors_today_by_type": error_breakdown,
        "most_recent_errors": recent_errors,
    })


@app.route("/telegram_test/<bot_id>")
def telegram_test(bot_id):
    """
    Sends a plain test message to confirm the Telegram token/chat_id for
    this bot are wired correctly - independent of scoring/market data.
    Hit e.g. /telegram_test/BOT1 and check the chat.
    """
    if bot_id not in config.BOTS:
        return jsonify({"error": "unknown bot_id"}), 404
    bot_cfg = config.BOTS[bot_id]
    token, chat_id = _env(bot_cfg["token_env"]), _env(bot_cfg["chat_id_env"])
    if not token or not chat_id:
        return jsonify({
            "sent": False,
            "reason": "token or chat_id env var not set on Render",
            "token_env_name": bot_cfg["token_env"],
            "chat_id_env_name": bot_cfg["chat_id_env"],
        }), 400
    msg_id = telegram_notify._send(token, chat_id, f"✅ Test message from {bot_cfg['name']}")
    return jsonify({"sent": bool(msg_id), "message_id": msg_id})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.PORT)
