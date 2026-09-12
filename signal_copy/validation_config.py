"""
Tunable thresholds + factor weights for the signal validation engine.

All weights are in arbitrary "points"; the engine normalizes earned/possible
to a 0-100 confluence score. Adjust weights to emphasize factors you trust.
"""

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
VALID_THRESHOLD = 55.0     # >= this => VALID (eligible to execute)
WEAK_THRESHOLD  = 40.0     # >= this but < VALID => WEAK (notify, no auto-exec)

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