"""
scoring.py
----------
Turns the last row of an indicator-enriched dataframe into a 0-10 score
for LONG and for SHORT, and decides whether to fire an alert.

VWAP is a HARD GATE (not just a scored point): if VWAP is missing/NaN or
price is >2% away from it, we bail out immediately with score=None and no
alert — regardless of everything else. This matches the "VWAP MANDATORY,
NO alert without it" rule.

Everything else contributes 1 point each toward a 10-point score. An alert
fires only when score >= config.SCORE_ALERT_THRESHOLD (default 8).
"""

import logging
from dataclasses import dataclass, field
from typing import Optional, Dict

import config

log = logging.getLogger("scoring")


@dataclass
class SignalResult:
    direction: Optional[str] = None       # "LONG" | "SHORT" | None
    score: int = 0
    max_score: int = 10
    confidence: Optional[str] = None      # "HIGH" | "MEDIUM" | "LOW"
    conditions: Dict[str, bool] = field(default_factory=dict)
    price: float = 0.0
    vwap: float = 0.0
    rsi: float = 0.0
    adx: float = 0.0
    ema9: float = 0.0
    ema21: float = 0.0
    volume: float = 0.0
    vol_avg20: float = 0.0
    vwap_distance_pct: float = 0.0
    reject_reason: Optional[str] = None


def _confidence_from_distance(distance_pct: float) -> str:
    if distance_pct <= config.CONFIDENCE_HIGH_PCT:
        return "HIGH"
    if distance_pct <= config.CONFIDENCE_MEDIUM_PCT:
        return "MEDIUM"
    return "LOW"


def evaluate(df) -> SignalResult:
    """
    df: indicator-enriched dataframe from indicators.enrich_dataframe().
    Returns a SignalResult. Caller decides whether to alert based on
    result.direction is not None and result.score >= SCORE_ALERT_THRESHOLD.
    """
    last = df.iloc[-1]
    prev2 = df.iloc[-2] if len(df) >= 2 else None

    price = float(last["close"])
    vwap = float(last["vwap"]) if last["vwap"] == last["vwap"] else None  # NaN check

    result = SignalResult(price=price)

    # ---- HARD GATE: VWAP mandatory --------------------------------------
    if vwap is None or vwap <= 0:
        result.reject_reason = "VWAP_UNAVAILABLE"
        return result

    result.vwap = vwap
    distance_pct = abs(price - vwap) / vwap * 100
    result.vwap_distance_pct = round(distance_pct, 3)

    if distance_pct > config.VWAP_MAX_DISTANCE_PCT:
        result.reject_reason = "OUTSIDE_VWAP_BAND"
        return result

    rsi = float(last["rsi14"])
    adx = float(last["adx14"])
    ema9 = float(last["ema9"])
    ema21 = float(last["ema21"])
    volume = float(last["volume"])
    vol_avg20 = float(last["vol_avg20"])

    result.rsi, result.adx, result.ema9, result.ema21 = rsi, adx, ema9, ema21
    result.volume, result.vol_avg20 = volume, vol_avg20

    # Candle direction helpers (last 2 candles both same color)
    def is_bullish(row):
        return row["close"] > row["open"]

    def is_bearish(row):
        return row["close"] < row["open"]

    last_bullish = is_bullish(last)
    last_bearish = is_bearish(last)
    prev_bullish = is_bullish(prev2) if prev2 is not None else False
    prev_bearish = is_bearish(prev2) if prev2 is not None else False

    ema9_prev = float(df.iloc[-2]["ema9"]) if len(df) >= 2 else ema9

    # ---- LONG conditions (10 points) ------------------------------------
    long_conditions = {
        "price_above_vwap": price > vwap,
        "vwap_within_2pct": distance_pct <= config.VWAP_MAX_DISTANCE_PCT,
        "rsi_in_range": config.RSI_LONG_MIN <= rsi <= config.RSI_LONG_MAX,
        "adx_above_25": adx > config.ADX_MIN,
        "ema9_above_ema21": ema9 > ema21,
        "last_2_candles_bullish": last_bullish and prev_bullish,
        "volume_above_avg20": volume > vol_avg20,
        "tight_to_vwap_bonus": distance_pct <= config.CONFIDENCE_HIGH_PCT,
        "ema9_rising": ema9 > ema9_prev,
        "adx_strong_trend": adx > (config.ADX_MIN + 5),  # extra momentum point
    }
    long_score = sum(long_conditions.values())

    # ---- SHORT conditions (10 points) ------------------------------------
    short_conditions = {
        "price_below_vwap": price < vwap,
        "vwap_within_2pct": distance_pct <= config.VWAP_MAX_DISTANCE_PCT,
        "rsi_in_range": config.RSI_SHORT_MIN <= rsi <= config.RSI_SHORT_MAX,
        "adx_above_25": adx > config.ADX_MIN,
        "ema9_below_ema21": ema9 < ema21,
        "last_2_candles_bearish": last_bearish and prev_bearish,
        "volume_above_avg20": volume > vol_avg20,
        "tight_to_vwap_bonus": distance_pct <= config.CONFIDENCE_HIGH_PCT,
        "ema9_falling": ema9 < ema9_prev,
        "adx_strong_trend": adx > (config.ADX_MIN + 5),
    }
    short_score = sum(short_conditions.values())

    # Pick whichever direction is plausible and higher-scoring.
    # A candidate direction must at minimum have price on the correct side
    # of VWAP with EMA alignment agreeing (mirrors the "hard rules" set),
    # otherwise we don't call it a direction at all.
    long_viable = long_conditions["price_above_vwap"] and long_conditions["ema9_above_ema21"]
    short_viable = short_conditions["price_below_vwap"] and short_conditions["ema9_below_ema21"]

    if long_viable and (not short_viable or long_score >= short_score):
        result.direction = "LONG"
        result.score = long_score
        result.conditions = long_conditions
    elif short_viable:
        result.direction = "SHORT"
        result.score = short_score
        result.conditions = short_conditions
    else:
        result.reject_reason = "NO_DIRECTIONAL_BIAS"
        return result

    result.confidence = _confidence_from_distance(distance_pct)
    return result


def should_alert(result: SignalResult) -> bool:
    return (
        result.direction is not None
        and result.reject_reason is None
        and result.score >= config.SCORE_ALERT_THRESHOLD
    )
