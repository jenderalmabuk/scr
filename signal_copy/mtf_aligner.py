"""Multi-timeframe alignment detection for trade signals."""

from typing import Dict, List, Optional


def _compute_ema(closes: List[float], period: int = 20) -> Optional[float]:
    if not closes or len(closes) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * k + ema * (1 - k)
    return ema


def _trend_direction(closes: List[float]) -> str:
    """Determine trend: UP, DOWN, or FLAT using EMA20 vs price and slope."""
    if len(closes) < 25:
        return "FLAT"
    ema20 = _compute_ema(closes, 20)
    if ema20 is None:
        return "FLAT"
    last_price = closes[-1]
    price_vs_ema = (last_price - ema20) / ema20 * 100.0
    slope = (closes[-1] - closes[-10]) / closes[-10] * 100.0 if closes[-10] > 0 else 0.0
    if price_vs_ema > 0.5 and slope > 0.5:
        return "UP"
    if price_vs_ema < -0.5 and slope < -0.5:
        return "DOWN"
    return "FLAT"


class MTFAligner:
    """Align entry timeframe signal with 4h and 1D trends."""

    async def analyze(self, symbol: str) -> Dict:
        """Return MTF alignment data for a symbol."""
        from .chart_generator import _fetch_klines
        results = {}
        for tf, label in [("15m", "entry_tf"), ("4h", "tf_4h"), ("1d", "tf_daily")]:
            try:
                kl = await _fetch_klines(symbol, interval=tf, limit=50)
                if kl and len(kl) >= 25:
                    closes = [k[4] for k in kl]
                    results[label] = {
                        "trend": _trend_direction(closes),
                        "price": closes[-1],
                        "change_10": ((closes[-1] - closes[-10]) / closes[-10] * 100.0),
                    }
                else:
                    results[label] = {"trend": "FLAT", "price": 0.0, "change_10": 0.0}
            except Exception:
                results[label] = {"trend": "FLAT", "price": 0.0, "change_10": 0.0}
        return results

    async def get_alignment_score(self, symbol: str, side: str) -> float:
        """Score alignment of signal side with higher timeframe trends. 0-100."""
        data = await self.analyze(symbol)
        entry = data.get("entry_tf", {})
        tf4h = data.get("tf_4h", {})
        day = data.get("tf_daily", {})

        direction = "UP" if side == "LONG" else "DOWN"
        score = 50.0

        if tf4h.get("trend") == direction:
            score += 15.0
        elif tf4h.get("trend") == ("DOWN" if side == "LONG" else "UP"):
            score -= 20.0

        if day.get("trend") == direction:
            score += 20.0
        elif day.get("trend") == ("DOWN" if side == "LONG" else "UP"):
            score -= 25.0

        return max(10.0, min(95.0, score))
