"""TradingView TA factor for signal validation.

Uses tradingview-ta library (Python-native, no Node.js required) to fetch
real-time TA summaries from TradingView across multiple timeframes.
Computes a confluence score aligned to the signal direction.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from tradingview_ta import TA_Handler, Interval, Exchange
from utils.logger import logger

# Multi-TF weights for confluence scoring
TF_WEIGHTS = [
    ("15m", Interval.INTERVAL_15_MINUTES, 3),
    ("1h",  Interval.INTERVAL_1_HOUR,       2),
    ("4h",  Interval.INTERVAL_4_HOURS,       2),
    ("1D",  Interval.INTERVAL_1_DAY,         1),
]


class TradingViewFactor:
    """Fetch TradingView TA summary and compute confluence score."""

    def __init__(self, timeout: int = 15):
        self.timeout = timeout  # reserved for future httpx timeout

    async def fetch(self, symbol: str, indicators: Optional[list] = None) -> Dict[str, Any]:
        """Fetch TA summaries for all timeframes (runs in thread for async compat)."""
        import concurrent.futures

        def _fetch_sync():
            ta_result = {}
            for tf_name, interval, _weight in TF_WEIGHTS:
                try:
                    handler = TA_Handler(
                        symbol=symbol.upper(),
                        screener="crypto",
                        exchange="BINANCE",
                        interval=interval,
                        timeout=12,
                    )
                    analysis = handler.get_analysis()
                    ta_result[f"tf_{tf_name}"] = {
                        "recommendation": analysis.summary.get("RECOMMENDATION", "NEUTRAL"),
                        "buy": analysis.summary.get("BUY", 0),
                        "sell": analysis.summary.get("SELL", 0),
                        "neutral": analysis.summary.get("NEUTRAL", 0),
                        "oscillators": analysis.oscillators.get("RECOMMENDATION", "NEUTRAL"),
                        "moving_averages": analysis.moving_averages.get("RECOMMENDATION", "NEUTRAL"),
                        "rsi": _safe_rsi(analysis.oscillators.get("COMPUTE", {})),
                    }
                except Exception as exc:
                    logger.debug("[TV_FACTOR] %s %s: %s", symbol, tf_name, exc)
                    ta_result[f"tf_{tf_name}"] = {"error": str(exc)}
            return ta_result

        loop = asyncio.get_running_loop()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                ta = await loop.run_in_executor(pool, _fetch_sync)
            if not ta or all(v.get("error") for v in ta.values()):
                return {}
            return {"symbol": symbol, "_ta": ta}
        except Exception as exc:
            logger.warning("[TV_FACTOR] Fetch failed for %s: %s", symbol, exc)
            return {}

    def compute_confluence(self, data: Dict[str, Any], side: str) -> Dict[str, Any]:
        """Compute confluence score from TradingView TA summaries across TFs."""
        score = 50.0
        details: list = []
        ta = data.get("_ta", {})

        if not isinstance(ta, dict) or all(
            v.get("error") for v in ta.values() if isinstance(v, dict)
        ):
            return {"score": 30.0, "details": ["No TV data"], "rsi": None, "ema20": None, "ema50": None}

        for tf_name, _interval, weight in TF_WEIGHTS:
            tf_key = f"tf_{tf_name}"
            tf_data = ta.get(tf_key, {})
            if not isinstance(tf_data, dict) or tf_data.get("error"):
                continue

            recommendation = tf_data.get("recommendation", "NEUTRAL")
            osc = tf_data.get("oscillators", "NEUTRAL")
            ma = tf_data.get("moving_averages", "NEUTRAL")
            buy_count = tf_data.get("buy", 0)
            sell_count = tf_data.get("sell", 0)

            # Strong alignment: TA says BUY + signal is LONG, or SELL + signal is SHORT
            if recommendation in ("BUY", "STRONG_BUY") and side == "LONG":
                score += 4 * weight
                details.append(f"TV {tf_name}: {recommendation} ✅")
            elif recommendation in ("SELL", "STRONG_SELL") and side == "SHORT":
                score += 4 * weight
                details.append(f"TV {tf_name}: {recommendation} ✅")
            elif recommendation in ("STRONG_BUY", "STRONG_SELL"):
                # Strong opposition
                score -= 3 * weight
                details.append(f"TV {tf_name}: {recommendation} vs {side} ⚠️")
            elif recommendation == "NEUTRAL":
                # Check oscillator + MA alignment
                osc_aligned = (osc in ("BUY", "STRONG_BUY") and side == "LONG") or \
                              (osc in ("SELL", "STRONG_SELL") and side == "SHORT")
                ma_aligned = (ma in ("BUY", "STRONG_BUY") and side == "LONG") or \
                             (ma in ("SELL", "STRONG_SELL") and side == "SHORT")

                if osc_aligned and ma_aligned:
                    score += 2 * weight
                    details.append(f"TV {tf_name}: osc+ma aligned (NEUTRAL)")
                elif osc_aligned or ma_aligned:
                    score += 1 * weight
                    details.append(f"TV {tf_name}: partial alignment")
                elif buy_count > sell_count + 3 and side == "SHORT":
                    score -= 1 * weight
                    details.append(f"TV {tf_name}: slight bullish bias vs SHORT")
                elif sell_count > buy_count + 3 and side == "LONG":
                    score -= 1 * weight
                    details.append(f"TV {tf_name}: slight bearish bias vs LONG")
            else:
                score -= 1 * weight
                details.append(f"TV {tf_name}: {recommendation} vs {side}")

        # Bonus for multi-TF alignment
        aligned_tfs = sum(1 for d in details if "✅" in d)
        if aligned_tfs >= 2:
            score += 5

        return {
            "score": max(0, min(100, score)),
            "details": details[:5],
            "rsi": _extract_rsi(ta),
            "ema20": None,
            "ema50": None,
        }


def _safe_rsi(compute: dict) -> Optional[float]:
    rsi_raw = compute.get("RSI", None)
    if rsi_raw is None:
        return None
    try:
        return float(rsi_raw)
    except (ValueError, TypeError):
        return None


def _extract_rsi(ta: dict) -> Optional[float]:
    for k in ta:
        tf_data = ta.get(k, {})
        if isinstance(tf_data, dict):
            rsi = tf_data.get("rsi")
            if rsi is not None:
                return rsi
    return None


__all__ = ["TradingViewFactor"]