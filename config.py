"""
config.py
---------
Single source of truth for all 3 bots. Add/remove symbols here (or edit
bots_config/symbols.json directly) — nothing else in the codebase needs
to change.

All secrets (Telegram tokens, Angel One credentials) come from environment
variables so nothing sensitive is committed to source control. Set these
in Render.com's dashboard under "Environment".
"""

import os
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Load symbol universes (edit bots_config/symbols.json to add/remove symbols
# without touching any code)
# ---------------------------------------------------------------------------
with open(BASE_DIR / "bots_config" / "symbols.json") as f:
    _SYMBOLS = json.load(f)

# ---------------------------------------------------------------------------
# Strict rule thresholds — identical across all 3 bots, as specified
# ---------------------------------------------------------------------------
RSI_LONG_MIN, RSI_LONG_MAX = 40, 65
RSI_SHORT_MIN, RSI_SHORT_MAX = 35, 60
ADX_MIN = 25
VWAP_MAX_DISTANCE_PCT = 2.0          # hard gate: ignore if >2% from VWAP
CONFIDENCE_HIGH_PCT = 0.5
CONFIDENCE_MEDIUM_PCT = 1.5
VOLUME_LOOKBACK = 20                 # 20-period average volume
EMA_FAST, EMA_SLOW = 9, 21
RSI_PERIOD = 14
ADX_PERIOD = 14

SCORE_ALERT_THRESHOLD = 8            # out of 10 -> send alert
COOLDOWN_HOURS = 2
CANDLE_INTERVAL = "FIVE_MINUTE"      # Angel One interval string
SCAN_INTERVAL_MINUTES = 15

MARKET_OPEN = "09:15"
MARKET_CLOSE = "15:30"
MORNING_RESET_TIME = "09:10"
DAILY_SUMMARY_TIME = "15:45"
ERROR_SUMMARY_TIME = "16:00"
TIMEZONE = "Asia/Kolkata"

MAX_CONSECUTIVE_FAILS_BROKEN = 3     # mark symbol BROKEN in Settings
MAX_CONSECUTIVE_FAILS_DISABLE = 5    # auto-disable symbol entirely

# ---------------------------------------------------------------------------
# Per-bot definitions
# ---------------------------------------------------------------------------
BOTS = {
    "BOT1": {
        "name": "NIFTY 50 + MIDCAP 150 BOT",
        "telegram_name": "@NiftyMidcapAlertBot",
        "token_env": "8862722059:AAE2O4EcVgKIx2Nl-J-J9qfwcN3Hr1166jc",
        "chat_id_env": "1950770162",
        "symbols": _SYMBOLS["BOT1"],
        "sqlite_path": str(BASE_DIR / "data" / "bot1.db"),
    },
    "BOT2": {
        "name": "SECTOR LEADERS BOT",
        "telegram_name": "@SectorLeaderAlertBot",
        "token_env": "8506285410:AAHHRLYVSbOIHyIzguT3G8eZFUz4v0Vm5uU",
        "chat_id_env": "1950770162",
        "symbols": _SYMBOLS["BOT2"],
        "sqlite_path": str(BASE_DIR / "data" / "bot2.db"),
    },
    "BOT3": {
        "name": "SMALL CAP & EMERGING BOT",
        "telegram_name": "@SmallCapAlertBot",
        "token_env": "8362967421:AAHrEda0SHs27BxAy4ZVr0BM_gkL4ZN9oCk",
        "chat_id_env": "8991485495",
        "symbols": _SYMBOLS["BOT3"],
        "sqlite_path": str(BASE_DIR / "data" / "bot3.db"),
    },
}

# ---------------------------------------------------------------------------
# Angel One SmartAPI credentials (single trading account feeds all 3 bots —
# one websocket connection, symbols split only for alert routing/scoring)
# ---------------------------------------------------------------------------
ANGEL_API_KEY = os.environ.get("ANGEL_API_KEY", "jMD159Do")
ANGEL_CLIENT_CODE = os.environ.get("ANGEL_CLIENT_CODE", "AABJ053052")
ANGEL_PIN = os.environ.get("ANGEL_PIN", "2499")
ANGEL_TOTP_SECRET = os.environ.get("ANGEL_TOTP_SECRET", "NZ2WDX4EP4LWXALWJFPJXJ6KGU")

# Angel One publishes a master instrument list mapping tradingsymbol -> token.
# We download and cache it rather than hardcoding tokens (they can change).
ANGEL_INSTRUMENT_MASTER_URL = (
    "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
)
INSTRUMENT_CACHE_PATH = str(BASE_DIR / "data" / "instrument_master.json")

# Dry-run mode: if True, no real orders/alerts of consequence happen and
# Angel One calls are skipped/mocked — useful for first deploy sanity checks.
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

PORT = int(os.environ.get("PORT", "10000"))  # Render provides $PORT
