"""
Tunable thresholds + factor weights for the signal validation engine.

All weights are in arbitrary "points"; the engine normalizes earned/possible
to a 0-100 confluence score. Adjust weights to emphasize factors you trust.
"""

import os


def _float_env(name: str, default: float) -> float:
    try:
        raw = os.getenv(name, "").strip()
        return float(raw) if raw else default
    except (TypeError, ValueError):
        return default


def _int_env(name: str, default: int) -> int:
    try:
        raw = os.getenv(name, "").strip()
        return int(raw) if raw else default
    except (TypeError, ValueError):
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


# --- factor weights (max points each can contribute) ---
W_PRICE_FRESHNESS = 22.0   # is price still actionable vs the entry zone
W_GEOMETRY        = 18.0   # SL distance sane + RR acceptable
W_OI              = 14.0   # open-interest conviction
W_CVD             = 14.0   # taker order-flow alignment
W_FUNDING         = 10.0   # crowding / funding context
W_RSI             = 10.0   # momentum not exhausted
W_TREND           = 10.0   # short-term structure alignment
W_CHART_VISION    = 14.0   # chart/outlook image agrees with the trade (vision)

# --- verdict thresholds (normalized 0-100) ---
VALID_THRESHOLD = _float_env("SIGNAL_COPY_VALID_THRESHOLD", 60.0)     # >= this => VALID quality
WEAK_THRESHOLD  = _float_env("SIGNAL_COPY_WEAK_THRESHOLD", 45.0)      # >= this but < VALID => WEAK

# --- auto-execution policy thresholds ---
AUTO_MARKET_MIN_SCORE = _float_env("SIGNAL_COPY_AUTO_MARKET_MIN_SCORE", 65.0)
AUTO_LIMIT_MIN_SCORE = _float_env("SIGNAL_COPY_AUTO_LIMIT_MIN_SCORE", 55.0)
ADVERSARIAL_NO_OVERRIDE_MIN_SCORE = _float_env("SIGNAL_COPY_ADVERSARIAL_NO_OVERRIDE_MIN_SCORE", 75.0)
ADVERSARIAL_MODE = os.getenv("SIGNAL_COPY_ADVERSARIAL_MODE", "off").strip().lower()
COMMITTEE_MODE = os.getenv("SIGNAL_COPY_COMMITTEE_MODE", "shadow").strip().lower()
COMMITTEE_WARN_DOWNGRADE_COUNT = _int_env("SIGNAL_COPY_COMMITTEE_WARN_DOWNGRADE_COUNT", 2)
FLOW_NO_TRADE_MARKET_BLOCK = _bool_env("SIGNAL_COPY_FLOW_NO_TRADE_MARKET_BLOCK", True)
PRICE_CHASE_MARKET_BLOCK = _bool_env("SIGNAL_COPY_PRICE_CHASE_MARKET_BLOCK", False)

# --- adaptive entry & chase controls ---
MAX_CHASE_R = _float_env("SIGNAL_COPY_MAX_CHASE_R", 0.35)
MAX_DISCOUNT_R = _float_env("SIGNAL_COPY_MAX_DISCOUNT_R", 0.60)
MIN_REMAINING_RR = _float_env("SIGNAL_COPY_MIN_REMAINING_RR", 0.70)
DISCOUNT_FILL_ENABLED = _bool_env("SIGNAL_COPY_DISCOUNT_FILL_ENABLED", True)

# --- entry-zone freshness ---
ENTRY_ZONE_TOLERANCE_MULT = 0.5    # tolerance = zone_width * this ...
ENTRY_ZONE_TOLERANCE_PCT  = 0.25   # ... plus this percent of price
LIMIT_REACHABLE_PCT       = 5.0    # a limit entry within this % of price is "reachable"

# --- geometry / risk-reward (informational only; NOT scored) ---
# Score no longer depends on TP/SL because many channels post TP as an image
# (1R/2R/3R). These remain for display + a loose safety cap only.
SAFETY_MAX_SL_DISTANCE_PCT = 20.0  # reject only clearly-broken stops (safety, not score)
MAX_SL_DISTANCE_PCT = 5.0
MIN_SL_DISTANCE_PCT = 0.3
MIN_RR_RATIO        = 1.2
GOOD_RR_RATIO       = 2.5          # RR at/above this earns full geometry points

# --- open interest ---
OI_RISE_MIN_PCT    = 0.5
OI_RISE_STRONG_PCT = 2.0

# --- CVD / order flow ---
CVD_MIN_ZSCORE    = 0.5
CVD_STRONG_ZSCORE = 2.0

# --- funding rate (percent) ---
FUNDING_NEUTRAL_PCT = 0.01     # |funding%| below this = neutral
FUNDING_EXTREME_PCT = 0.05     # |funding%| above this = crowded/extreme

# --- RSI ---
RSI_OVERBOUGHT = 72.0
RSI_OVERSOLD   = 28.0

# --- trend ---
TREND_FLAT_PCT = 0.15          # |15m change%| below this = flat/neutral