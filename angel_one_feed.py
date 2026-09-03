"""
angel_one_feed.py
------------------
Real market data via Angel One's SmartAPI:
  - REST `getCandleData` for historical OHLCV bars (needed to compute
    VWAP/EMA/RSI/ADX, which require a full intraday series, not just LTP).
  - SmartWebSocketV2 for live tick-by-tick LTP, kept in an in-memory dict
    so the scan loop can use the freshest price between candle closes.

Requires the `smartapi-python` (aka `SmartApi`) and `pyotp` packages.
NOTE: Angel One's SDK method names/params have changed across versions in
the past — this is written against the commonly documented smartapi-python
interface as of early 2026. Before going live, verify against the current
docs at https://smartapi.angelbroking.com/docs and pin an exact package
version in requirements.txt.

Login flow: Angel One requires TOTP-based 2FA. Generate ANGEL_TOTP_SECRET
once from the QR code shown when enabling TOTP on your Angel One account,
then store it as an env var (never hardcode it).
"""

import json
import logging
import threading
import time
from datetime import datetime, timedelta

import pandas as pd
import requests

import config

log = logging.getLogger("angel_feed")

try:
    from SmartApi import SmartConnect
    from SmartApi.smartWebSocketV2 import SmartWebSocketV2
    import pyotp
    SMARTAPI_AVAILABLE = True
except ImportError:
    SMARTAPI_AVAILABLE = False
    log.warning(
        "smartapi-python / pyotp not installed. Install with "
        "`pip install smartapi-python pyotp` for live Angel One data. "
        "Falling back to unavailable-feed mode."
    )


class AngelOneFeed:
    """
    Singleton-style wrapper: one login, one websocket connection, shared
    across all 3 bots (they just watch different symbol subsets).
    """

    def __init__(self):
        self.smart_connect = None
        self.ws = None
        self.jwt_token = None
        self.feed_token = None
        self.auth_token = None
        self.instrument_map = {}          # "RELIANCE.NS" -> {"token": "2885", "exch_seg": "NSE"}
        self.live_ltp = {}                # token -> latest LTP (float)
        self._ws_lock = threading.Lock()
        self._logged_in = False

    # ------------------------------------------------------------------ #
    # Auth
    # ------------------------------------------------------------------ #
    def login(self) -> bool:
        if config.DRY_RUN:
            log.info("[ANGEL] DRY_RUN=true, skipping real login")
            return True
        if not SMARTAPI_AVAILABLE:
            log.error("[ANGEL] SmartApi SDK not installed, cannot log in")
            return False
        if not all([config.ANGEL_API_KEY, config.ANGEL_CLIENT_CODE,
                    config.ANGEL_PIN, config.ANGEL_TOTP_SECRET]):
            log.error("[ANGEL] Missing one or more ANGEL_* environment variables")
            return False

        try:
            self.smart_connect = SmartConnect(api_key=config.ANGEL_API_KEY)
            totp = pyotp.TOTP(config.ANGEL_TOTP_SECRET).now()
            session = self.smart_connect.generateSession(
                config.ANGEL_CLIENT_CODE, config.ANGEL_PIN, totp
            )
            if not session or not session.get("status"):
                log.error(f"[ANGEL] Login failed: {session}")
                return False

            self.jwt_token = session["data"]["jwtToken"]
            self.auth_token = self.jwt_token
            self.feed_token = self.smart_connect.getfeedToken()
            self._logged_in = True
            log.info("[ANGEL] Login successful")
            return True
        except Exception as e:
            log.error(f"[ANGEL] Login exception: {e}")
            return False

    # ------------------------------------------------------------------ #
    # Instrument master (symbol -> token mapping)
    # ------------------------------------------------------------------ #
    def load_instrument_master(self, symbols_needed: list):
        """
        Downloads Angel One's published instrument master (large JSON) and
        caches it locally, then builds a lookup for just the symbols we need.
        `symbols_needed` uses Yahoo-style tickers like 'RELIANCE.NS'; Angel
        One's tradingsymbol format is usually 'RELIANCE-EQ'.
        """
        try:
            cache_path = config.INSTRUMENT_CACHE_PATH
            try:
                with open(cache_path) as f:
                    master = json.load(f)
                log.info(f"[ANGEL] Loaded cached instrument master ({len(master)} rows)")
            except (FileNotFoundError, json.JSONDecodeError):
                log.info("[ANGEL] Downloading instrument master (first run)...")
                resp = requests.get(config.ANGEL_INSTRUMENT_MASTER_URL, timeout=60)
                resp.raise_for_status()
                master = resp.json()
                import os
                os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                with open(cache_path, "w") as f:
                    json.dump(master, f)
                log.info(f"[ANGEL] Cached instrument master ({len(master)} rows)")

            wanted_bases = {s.replace(".NS", "").upper() for s in symbols_needed}
            for row in master:
                if row.get("exch_seg") != "NSE":
                    continue
                tsym = row.get("symbol", "")  # e.g. "RELIANCE-EQ"
                base = tsym.replace("-EQ", "").upper()
                if base in wanted_bases:
                    self.instrument_map[f"{base}.NS"] = {
                        "token": row["token"],
                        "exch_seg": "NSE",
                        "tradingsymbol": tsym,
                    }
            log.info(f"[ANGEL] Resolved {len(self.instrument_map)}/{len(symbols_needed)} symbol tokens")
        except Exception as e:
            log.error(f"[ANGEL] Failed to load instrument master: {e}")

    def token_for(self, symbol: str):
        entry = self.instrument_map.get(symbol)
        return entry["token"] if entry else None

    # ------------------------------------------------------------------ #
    # Historical candles (REST) - used to compute all indicators
    # ------------------------------------------------------------------ #
    def get_historical_candles(self, symbol: str, minutes_back: int = 375) -> pd.DataFrame:
        """
        Fetch today's intraday 5-min candles for `symbol` via Angel One's
        getCandleData. Returns a DataFrame with columns
        [timestamp, open, high, low, close, volume], sorted ascending.
        Empty DataFrame on any failure (caller must handle -> ErrorLog + skip).
        """
        if config.DRY_RUN or not self._logged_in:
            return self._mock_candles(symbol)

        token = self.token_for(symbol)
        if not token:
            log.error(f"[ANGEL] No instrument token found for {symbol}")
            return pd.DataFrame()

        try:
            now = datetime.now()
            from_dt = now - timedelta(minutes=minutes_back)
            params = {
                "exchange": "NSE",
                "symboltoken": token,
                "interval": config.CANDLE_INTERVAL,
                "fromdate": from_dt.strftime("%Y-%m-%d %H:%M"),
                "todate": now.strftime("%Y-%m-%d %H:%M"),
            }
            resp = self.smart_connect.getCandleData(params)
            if not resp or not resp.get("status"):
                log.error(f"[ANGEL] getCandleData failed for {symbol}: {resp}")
                return pd.DataFrame()

            rows = resp["data"]  # [[timestamp, o, h, l, c, v], ...]
            df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].astype(float)
            return df.sort_values("timestamp").reset_index(drop=True)
        except Exception as e:
            log.error(f"[ANGEL] Exception fetching candles for {symbol}: {e}")
            return pd.DataFrame()

    def _mock_candles(self, symbol: str) -> pd.DataFrame:
        """Synthetic candle series so the pipeline is testable without live credentials."""
        import numpy as np
        n = 60
        base = 1000 + (hash(symbol) % 500)
        rng = np.random.default_rng(abs(hash(symbol)) % (2**32))
        closes = base + np.cumsum(rng.normal(0, 2, n))
        highs = closes + rng.uniform(0.5, 2, n)
        lows = closes - rng.uniform(0.5, 2, n)
        opens = closes - rng.normal(0, 1, n)
        volumes = rng.integers(1000, 50000, n)
        now = datetime.now()
        timestamps = [(now - timedelta(minutes=5 * (n - i))).isoformat() for i in range(n)]
        return pd.DataFrame({
            "timestamp": timestamps, "open": opens, "high": highs,
            "low": lows, "close": closes, "volume": volumes,
        })

    # ------------------------------------------------------------------ #
    # Live websocket feed (LTP ticks)
    # ------------------------------------------------------------------ #
    def start_websocket(self, symbols: list):
        """
        Subscribes to LTP mode for the given symbols and keeps a background
        thread alive updating self.live_ltp[token] = last traded price.
        Safe no-op in DRY_RUN or if SDK missing.
        """
        if config.DRY_RUN or not SMARTAPI_AVAILABLE or not self._logged_in:
            log.info("[ANGEL] Websocket not started (DRY_RUN or not logged in)")
            return

        tokens = [self.token_for(s) for s in symbols if self.token_for(s)]
        if not tokens:
            log.warning("[ANGEL] No tokens resolved, websocket not started")
            return

        def on_data(wsapp, message):
            try:
                token = message.get("token")
                ltp = message.get("last_traded_price")
                if token and ltp is not None:
                    with self._ws_lock:
                        # Angel sends price *100 (paise) in some feed modes; verify against docs.
                        self.live_ltp[token] = float(ltp) / 100.0
            except Exception as e:
                log.error(f"[ANGEL WS] on_data error: {e}")

        def on_open(wsapp):
            log.info("[ANGEL WS] Connected, subscribing to tokens")
            correlation_id = "nse_alert_bot"
            mode = 1  # LTP mode
            token_list = [{"exchangeType": 1, "tokens": tokens}]
            self.ws.subscribe(correlation_id, mode, token_list)

        def on_error(wsapp, error):
            log.error(f"[ANGEL WS] Error: {error}")

        def on_close(wsapp):
            log.warning("[ANGEL WS] Connection closed")

        self.ws = SmartWebSocketV2(
            self.auth_token, config.ANGEL_API_KEY, config.ANGEL_CLIENT_CODE, self.feed_token
        )
        self.ws.on_open = on_open
        self.ws.on_data = on_data
        self.ws.on_error = on_error
        self.ws.on_close = on_close

        thread = threading.Thread(target=self.ws.connect, daemon=True)
        thread.start()
        log.info(f"[ANGEL WS] Websocket thread started for {len(tokens)} symbols")

    def get_live_ltp(self, symbol: str):
        """Returns latest websocket LTP for symbol, or None if not yet received."""
        token = self.token_for(symbol)
        if not token:
            return None
        with self._ws_lock:
            return self.live_ltp.get(token)


# Module-level singleton used by main.py
feed = AngelOneFeed()
