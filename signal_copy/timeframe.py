"""
Timeframe-aware analysis.

When a signal states a timeframe (e.g. "1h", "30m"), recompute the momentum/
volatility metrics (RSI, ATR%, recent price change, regime) on THAT timeframe so
validation matches the trader's intended horizon. OI/CVD/funding are left as-is
(they are not single-timeframe quantities). If no timeframe is given, the base
metrics (default ~15m) are used unchanged.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from utils.logger import logger
from .chart_generator import _fetch_klines, _rsi

# signal timeframe -> Binance USDⓈ-M kline interval
_TF_TO_BINANCE = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "45m": "30m",  # no native 45m; fall back to 30m
    "1h": "1h", "2h": "2h", "3h": "4h", "4h": "4h", "6h": "6h",
    "8h": "8h", "12h": "12h", "1d": "1d", "3d": "3d", "1w": "1w",
}


def binance_interval(tf: Optional[str]) -> Optional[str]:
    if not tf:
        return None
    return _TF_TO_BINANCE.get(tf.lower())


def _f(v, d=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


async def apply_timeframe_metrics(sig, metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Override RSI/ATR%/price-change/regime in `metrics` using the signal's TF."""
    interval = binance_interval(getattr(sig, "timeframe", None))
    if not interval:
        return metrics
    try:
        kl = await _fetch_klines(sig.symbol, interval=interval, limit=120)
    except Exception as exc:
        logger.warning("[TF] kline fetch failed %s %s: %s", sig.symbol, interval, exc)
        return metrics
    if not kl or len(kl) < 20:
        return metrics

    highs = [k[2] for k in kl]
    lows = [k[3] for k in kl]
    closes = [k[4] for k in kl]

    # RSI(14) on this timeframe
    rsi_series = _rsi(closes, 14)
    rsi = next((r for r in reversed(rsi_series) if r is not None), None)

    # ATR% (14) on this timeframe
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    atr_pct = 0.0
    if len(trs) >= 14 and closes[-1] > 0:
        atr_pct = (sum(trs[-14:]) / 14.0) / closes[-1] * 100.0

    # recent price change over last 4 candles on this timeframe
    price_change = 0.0
    if len(closes) >= 5 and closes[-5] > 0:
        price_change = (closes[-1] - closes[-5]) / closes[-5] * 100.0

    # regime label from this timeframe's volatility/trend
    regime = "RANGING"
    if atr_pct >= 2.0:
        regime = "HIGH_VOL"
    elif abs(price_change) >= 0.5:
        regime = "TRENDING"

    if rsi is not None:
        metrics["rsi"] = rsi
    if atr_pct > 0:
        metrics["atr_pct"] = atr_pct
    metrics["price_change_15m_pct"] = price_change   # trend factor reads this key
    metrics["regime_label"] = regime
    metrics["analysis_timeframe"] = sig.timeframe
    logger.info("[TF] %s analyzed on %s | rsi=%.1f atr%%=%.2f chg=%.2f regime=%s",
                sig.symbol, sig.timeframe, rsi or 0.0, atr_pct, price_change, regime)
    return metrics
