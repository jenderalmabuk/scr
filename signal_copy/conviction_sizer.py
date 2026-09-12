"""Dynamic Conviction-Based Position Sizing.

Determines how much to risk per trade based on:
- Channel historical performance (win rate)
- Multi-timeframe alignment (trend following)
- Whale pressure (accumulation vs distribution)
- Signal quality (TP/SL clarity, entry type)
"""

from typing import Dict, Any, Optional
from .channel_performance import get_tracker


class ConvictionSizer:
    """Calculate risk percentage dynamically based on signal conviction."""

    # Base risk is the floor — even the weakest signal gets this.
    BASE_RISK_PCT = 0.005  # 0.5%
    MAX_RISK_PCT = 0.010   # hard maximum 1.0%

    def __init__(self):
        self.ch_perf = get_tracker()

    def calc(self, signal, metrics: Dict[str, Any]) -> float:
        """Return risk_pct based on multiple conviction factors."""
        risk = self.BASE_RISK_PCT

        # 1. Channel reputation (up to +0.5%)
        src_id = getattr(signal, "source_chat_id", None)
        if src_id is not None:
            rep = self.ch_perf.get_reputation_score(src_id)
            risk += (rep - 50.0) / 100.0 * 0.005  # scale
            risk += max(0, (self.ch_perf.get_channel_stats(src_id).get("signals", 0) - 10)) * 0.0002

        # 2. MTF alignment (up to +0.4%)
        mtf = metrics.get("mtf_alignment")
        if mtf and isinstance(mtf, dict):
            score = float(mtf.get("score", 50.0))
            risk += (score - 50.0) / 50.0 * 0.004

        # 3. Whale pressure (up to +0.3% if accumulation)
        try:
            from whales.redis_db import get_whale_pressure
            wp = get_whale_pressure(signal.symbol, window_minutes=240)
            if wp["net_vol"] > 0 and wp["count"] > 0:
                factor = min(wp["net_vol"] / 1_000_000, 1.0)
                risk += factor * 0.003
            elif wp["net_vol"] < -500_000:
                risk -= 0.003  # reduce risk on heavy distribution
        except Exception:
            pass

        # 4. Signal clarity (up to +0.3%)
        has_tp = bool(signal.take_profits)
        has_sl = signal.stop_loss is not None and signal.stop_loss > 0
        clarity = (has_tp + has_sl) / 2.0  # 0, 0.5 or 1.0
        risk += clarity * 0.003

        # 5. Entry type bonus/penalty
        if getattr(signal, "entry_type", "limit") == "limit":
            risk += 0.001  # bonus for limit entries (controlled)
        else:
            risk += 0.002  # market entries have slippage risk

        # Clamp
        return max(self.BASE_RISK_PCT, min(risk, self.MAX_RISK_PCT))
