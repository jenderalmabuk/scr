"""
Standalone config for nexus signal-copy execution layer.

Replaces 663-line fusion_xomegabot config.py with env-based values.
All values have sensible defaults; override via .env.
"""
from __future__ import annotations
import os

# ============================================================
# RISK MANAGER CONFIG
# ============================================================

# Global cooldown between trades (minutes)
COOLDOWN_GLOBAL_MIN = int(os.getenv("COOLDOWN_GLOBAL_MIN", "5"))

# Per-symbol cooldown (minutes)
COOLDOWN_SYMBOL_MIN = int(os.getenv("COOLDOWN_SYMBOL_MIN", "15"))

# Max correlated cluster positions
MAX_CORRELATED_CLUSTER_POS = int(os.getenv("MAX_CORRELATED_CLUSTER_POS", "2"))

# Max daily loss % (of starting balance)
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "5.0"))

# Max drawdown % (of peak balance)
MAX_DRAWDOWN_PCT = float(os.getenv("MAX_DRAWDOWN_PCT", "10.0"))

# Max notional per trade as % of balance
MAX_NOTIONAL_PCT_OF_BALANCE = float(os.getenv("MAX_NOTIONAL_PCT_OF_BALANCE", "20.0"))

# Max open positions globally
MAX_OPEN_POS_GLOBAL = int(os.getenv("MAX_OPEN_POS_GLOBAL", "5"))

# Max same-direction positions; 0 disables this extra cap. SignalCopy uses the
# global pending+active capacity instead.
MAX_SAME_DIRECTION_POS = int(os.getenv("MAX_SAME_DIRECTION_POS", "0"))

# Max total exposure %
MAX_TOTAL_EXPOSURE_PCT = float(os.getenv("MAX_TOTAL_EXPOSURE_PCT", "80.0"))

# Risk per trade %
RISK_PER_TRADE_PCT = float(os.getenv("RISK_PER_TRADE_PCT", "1.0"))

# ============================================================
# TRADER CONFIG
# ============================================================

# Hard max hold time (minutes)
HARD_MAX_HOLD_MINUTES = int(os.getenv("HARD_MAX_HOLD_MINUTES", "480"))

# Regime-specific max hold
MAX_HOLD_TRENDING = int(os.getenv("MAX_HOLD_TRENDING", "480"))
MAX_HOLD_RANGING = int(os.getenv("MAX_HOLD_RANGING", "240"))
MAX_HOLD_HIGH_VOL = int(os.getenv("MAX_HOLD_HIGH_VOL", "120"))
MAX_HOLD_MINUTES = int(os.getenv("MAX_HOLD_MINUTES", "300"))

# Profit lock
PROFIT_LOCK_MIN_MINUTES = int(os.getenv("PROFIT_LOCK_MIN_MINUTES", "15"))
PROFIT_LOCK_TRIGGER_PCT = float(os.getenv("PROFIT_LOCK_TRIGGER_PCT", "1.0"))
PROFIT_LOCK_BUFFER_PCT = float(os.getenv("PROFIT_LOCK_BUFFER_PCT", "0.3"))

# Trailing stop
TRAIL_SL_PCT = float(os.getenv("TRAIL_SL_PCT", "0.8"))
TRAIL_SL_ATR_MULTIPLIER = float(os.getenv("TRAIL_SL_ATR_MULTIPLIER", "1.5"))

# Order confirmation timeout (seconds)
ORDER_CONFIRMATION_TIMEOUT = int(os.getenv("ORDER_CONFIRMATION_TIMEOUT", "30"))

# Scratch exit
SCRATCH_EXIT_ENABLED = os.getenv("SCRATCH_EXIT_ENABLED", "true").lower() in ("1", "true", "yes")
SCRATCH_EXIT_RANGING_MINUTES = float(os.getenv("SCRATCH_EXIT_RANGING_MINUTES", "30.0"))
SCRATCH_EXIT_TRENDING_MINUTES = float(os.getenv("SCRATCH_EXIT_TRENDING_MINUTES", "45.0"))
SCRATCH_EXIT_MAX_ABS_PNL_PCT = float(os.getenv("SCRATCH_EXIT_MAX_ABS_PNL_PCT", "0.4"))

# Damage reducer
DAMAGE_REDUCER_ENABLED = os.getenv("DAMAGE_REDUCER_ENABLED", "true").lower() in ("1", "true", "yes")
DAMAGE_REDUCER_MIN_HOLD_MINUTES = float(os.getenv("DAMAGE_REDUCER_MIN_HOLD_MINUTES", "45.0"))
DAMAGE_REDUCER_MAX_LOSS_PCT = float(os.getenv("DAMAGE_REDUCER_MAX_LOSS_PCT", "-2.5"))

# Close positions on shutdown
CLOSE_POSITIONS_ON_SHUTDOWN = os.getenv("CLOSE_POSITIONS_ON_SHUTDOWN", "false").lower() in ("1", "true", "yes")

# Geometry gate
MAX_SL_DISTANCE_PCT = float(os.getenv("MAX_SL_DISTANCE_PCT", "3.0"))
MIN_SL_DISTANCE_PCT = float(os.getenv("MIN_SL_DISTANCE_PCT", "0.2"))
MIN_RR_RATIO = float(os.getenv("MIN_RR_RATIO", "1.0"))
ENTRY_CONFIRMATION_ENABLED = os.getenv("ENTRY_CONFIRMATION_ENABLED", "true").lower() in ("1", "true", "yes")
RANGING_TP1_MULTIPLIER = float(os.getenv("RANGING_TP1_MULTIPLIER", "0.5"))
TRENDING_TP1_MULTIPLIER = float(os.getenv("TRENDING_TP1_MULTIPLIER", "1.0"))

# Coin filter
def is_coin_allowed(symbol: str) -> bool:
    """Allow all coins by default; override via env if needed."""
    blocked = os.getenv("BLOCKED_COINS", "").split(",")
    blocked = [b.strip().upper() for b in blocked if b.strip()]
    return symbol.upper() not in blocked

# ============================================================
# DEFAULT VALUES (for reference)
# ============================================================
_DEFAULTS = {
    "COOLDOWN_GLOBAL_MIN": 5,
    "COOLDOWN_SYMBOL_MIN": 15,
    "MAX_CORRELATED_CLUSTER_POS": 2,
    "MAX_DAILY_LOSS_PCT": 5.0,
    "MAX_DRAWDOWN_PCT": 10.0,
    "MAX_NOTIONAL_PCT_OF_BALANCE": 20.0,
    "MAX_OPEN_POS_GLOBAL": 5,
    "MAX_SAME_DIRECTION_POS": 0,
    "MAX_TOTAL_EXPOSURE_PCT": 80.0,
    "RISK_PER_TRADE_PCT": 1.0,
    "HARD_MAX_HOLD_MINUTES": 480,
    "MAX_HOLD_TRENDING": 480,
    "MAX_HOLD_RANGING": 240,
    "MAX_HOLD_HIGH_VOL": 120,
    "MAX_HOLD_MINUTES": 300,
    "PROFIT_LOCK_MIN_MINUTES": 15,
    "PROFIT_LOCK_TRIGGER_PCT": 1.0,
    "PROFIT_LOCK_BUFFER_PCT": 0.3,
    "TRAIL_SL_PCT": 0.8,
    "TRAIL_SL_ATR_MULTIPLIER": 1.5,
    "ORDER_CONFIRMATION_TIMEOUT": 30,
    "SCRATCH_EXIT_ENABLED": True,
    "SCRATCH_EXIT_RANGING_MINUTES": 30,
    "SCRATCH_EXIT_TRENDING_MINUTES": 45,
    "SCRATCH_EXIT_MAX_ABS_PNL_PCT": 0.5,
    "DAMAGE_REDUCER_ENABLED": True,
    "DAMAGE_REDUCER_MIN_HOLD_MINUTES": 10,
    "DAMAGE_REDUCER_MAX_LOSS_PCT": 1.0,
    "CLOSE_POSITIONS_ON_SHUTDOWN": True,
}

def get_config() -> dict:
    """Get all config values as dict (for debugging)."""
    import inspect
    frame = inspect.currentframe()
    try:
        module = inspect.getmodule(frame)
        return {k: v for k, v in module.__dict__.items() 
                if k.isupper() and not k.startswith('_')}
    finally:
        del frame