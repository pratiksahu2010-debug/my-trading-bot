"""
indicators.py
-------------
Pure-function technical indicator calculations. Takes a pandas DataFrame
of intraday candles with columns: ['timestamp','open','high','low','close','volume']
and returns indicator series/values. No I/O, no side effects — easy to unit test.
"""

import numpy as np
import pandas as pd
import logging

log = logging.getLogger("indicators")


def calculate_vwap(df: pd.DataFrame) -> pd.Series:
    """
    Session VWAP = cumulative(typical_price * volume) / cumulative(volume)
    Typical price = (High + Low + Close) / 3.
    Assumes df is already filtered to the CURRENT trading session only
    (VWAP resets every day) and sorted ascending by timestamp.
    """
    if df.empty:
        return pd.Series(dtype=float)
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    cum_tp_vol = (typical_price * df["volume"]).cumsum()
    cum_vol = df["volume"].cumsum().replace(0, np.nan)
    vwap = cum_tp_vol / cum_vol
    return vwap


def calculate_ema(df: pd.DataFrame, period: int, column: str = "close") -> pd.Series:
    """Standard exponential moving average."""
    if df.empty:
        return pd.Series(dtype=float)
    return df[column].ewm(span=period, adjust=False).mean()


def calculate_rsi(df: pd.DataFrame, period: int = 14, column: str = "close") -> pd.Series:
    """
    Wilder's RSI (the standard RSI used by most charting platforms).
    """
    if df.empty or len(df) < period + 1:
        return pd.Series([np.nan] * len(df), index=df.index)

    delta = df[column].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.fillna(50)  # neutral default before enough data
    return rsi


def calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Wilder's ADX (Average Directional Index), 0-100. Values > 25 generally
    indicate a trending (vs. ranging) market.
    """
    if df.empty or len(df) < period + 1:
        return pd.Series([np.nan] * len(df), index=df.index)

    high, low, close = df["high"], df["low"], df["close"]

    plus_dm = high.diff()
    minus_dm = -low.diff()

    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    tr1 = (high - low)
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = true_range.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean() / atr.replace(0, np.nan))
    minus_di = 100 * (minus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean() / atr.replace(0, np.nan))

    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    adx = dx.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    return adx.fillna(0)


def enrich_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """
    Convenience helper: attach every indicator this system needs onto a
    copy of the candle dataframe. Returns the enriched dataframe; last row
    is "now".
    """
    if df.empty or len(df) < max(21, 15):
        raise ValueError("Not enough candles to compute indicators (need 21+)")

    out = df.copy().reset_index(drop=True)
    out["vwap"] = calculate_vwap(out)
    out["ema9"] = calculate_ema(out, 9)
    out["ema21"] = calculate_ema(out, 21)
    out["rsi14"] = calculate_rsi(out, 14)
    out["adx14"] = calculate_adx(out, 14)
    out["vol_avg20"] = out["volume"].rolling(20, min_periods=1).mean()
    return out
