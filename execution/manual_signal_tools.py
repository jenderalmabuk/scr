"""Helpers for MANUAL/imported positions.

Manual entries and exchange-imported positions do not pass through the Telegram
parser, but they still need the same execution metadata used by dynamic exits.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Tuple

logger = logging.getLogger(__name__)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _price_at_r(side: str, entry: float, risk: float, rr: float) -> float:
    return entry + risk * rr if side == "LONG" else entry - risk * rr


def _rr_for_price(side: str, entry: float, risk: float, price: float) -> float:
    if risk <= 0:
        return 0.0
    return (price - entry) / risk if side == "LONG" else (entry - price) / risk


async def _fetch_live_price(symbol: str) -> Dict[str, Any]:
    try:
        import aiohttp
    except Exception:
        return {}

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        try:
            url = f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}"
            async with session.get(url) as resp:
                data = await resp.json()
            row = ((data.get("result") or {}).get("list") or [{}])[0]
            price = _safe_float(row.get("markPrice") or row.get("lastPrice"))
            if price > 0:
                return {
                    "price": price,
                    "price_source": "bybit_linear_rest",
                    "price_observed_at": datetime.now(timezone.utc).isoformat(),
                    "price_age_sec": 0.0,
                    "price_fresh": True,
                }
        except Exception as exc:
            logger.debug("[MANUAL_CONTEXT] Bybit price fetch failed %s: %s", symbol, exc)

        try:
            url = f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}"
            async with session.get(url) as resp:
                data = await resp.json()
            price = _safe_float(data.get("price"))
            if price > 0:
                return {
                    "price": price,
                    "price_source": "binance_futures_rest",
                    "price_observed_at": datetime.now(timezone.utc).isoformat(),
                    "price_age_sec": 0.0,
                    "price_fresh": True,
                }
        except Exception as exc:
            logger.debug("[MANUAL_CONTEXT] Binance price fetch failed %s: %s", symbol, exc)
    return {}


def normalize_manual_tp_ladder(
    side: str,
    entry: float,
    sl: float,
    tps: Iterable[Any] | None,
) -> Tuple[List[float], Dict[str, Any]]:
    """Build an R-based TP ladder for manual/imported orders when TP data is thin.

    Existing multi-TP provider ladders are preserved. A single final TP is expanded
    into staged partial exits so the existing 25%-per-TP engine can de-risk before
    the final target.
    """
    side = str(side or "").upper()
    entry = _safe_float(entry)
    sl = _safe_float(sl)
    risk = abs(entry - sl)
    raw = [_safe_float(tp) for tp in (tps or [])]
    if side == "LONG":
        clean = sorted({tp for tp in raw if tp > entry})
    else:
        clean = sorted({tp for tp in raw if 0 < tp < entry}, reverse=True)

    info: Dict[str, Any] = {
        "source": "provider",
        "input_tp_count": len(clean),
        "risk_distance": risk,
    }
    if entry <= 0 or risk <= 0 or side not in {"LONG", "SHORT"}:
        return clean, {**info, "source": "invalid_geometry"}
    if len(clean) >= 2:
        return clean, info

    final_rr = _rr_for_price(side, entry, risk, clean[-1]) if clean else _safe_float(
        os.getenv("MANUAL_DEFAULT_FINAL_R"), 2.0
    )
    if final_rr <= 0:
        return clean, {**info, "source": "invalid_final_rr"}

    if final_rr >= 2.0:
        rungs = [1.0, 1.0 + (final_rr - 1.0) / 3.0, 1.0 + 2.0 * (final_rr - 1.0) / 3.0, final_rr]
    elif final_rr >= 1.0:
        rungs = [final_rr / 3.0, 2.0 * final_rr / 3.0, (2.0 * final_rr / 3.0 + final_rr) / 2.0, final_rr]
    else:
        rungs = [final_rr]

    precision = max(6, len(str(entry).split(".")[-1]) if "." in str(entry) else 2)
    ladder = []
    for rr in rungs:
        price = round(_price_at_r(side, entry, risk, rr), precision)
        if (side == "LONG" and price > entry) or (side == "SHORT" and 0 < price < entry):
            if price not in ladder:
                ladder.append(price)
    if side == "LONG":
        ladder = sorted(ladder)
    else:
        ladder = sorted(ladder, reverse=True)
    return ladder, {**info, "source": "r_ladder", "final_rr": round(final_rr, 4), "rungs": [round(x, 4) for x in rungs]}


async def fetch_manual_signal_context(
    symbol: str,
    side: str,
    *,
    timeframe: str = "15m",
) -> Dict[str, Any]:
    """Fetch parser-equivalent context for MANUAL/imported positions.

    Best-effort: any failed data source is skipped so enrichment never blocks
    position sync or manual order execution.
    """
    symbol = str(symbol or "").upper()
    side = str(side or "").upper()
    metrics: Dict[str, Any] = {
        "signal_timeframe": timeframe or "15m",
        "manual_context_enriched": True,
        "setup_type": os.getenv("MANUAL_DEFAULT_SETUP_TYPE", "EARLY_ENTRY").upper(),
        "scratch_exit_profile": "STRUCTURE_HOLD",
    }

    try:
        from nexus.data_bridge import NexusDataBridge

        bridge = NexusDataBridge()
        advanced = await bridge.get_advanced_metrics(symbol)
        if isinstance(advanced, dict):
            metrics.update(advanced)
    except Exception as exc:
        metrics["manual_metrics_error"] = str(exc)[:160]
        logger.debug("[MANUAL_CONTEXT] metrics fetch failed %s: %s", symbol, exc)

    live_price = await _fetch_live_price(symbol)
    if live_price:
        metrics.update(live_price)

    try:
        from signal_copy.mtf_aligner import MTFAligner

        mtf = MTFAligner()
        mtf_data = await mtf.analyze(symbol)
        if mtf_data:
            mtf_score = await mtf.get_alignment_score(symbol, side)
            metrics["mtf_alignment"] = {
                "score": mtf_score,
                "entry_trend": mtf_data.get("entry_tf", {}).get("trend", "FLAT"),
                "tf4h_trend": mtf_data.get("tf_4h", {}).get("trend", "FLAT"),
                "d1_trend": mtf_data.get("tf_daily", {}).get("trend", "FLAT"),
            }
    except Exception as exc:
        metrics["manual_mtf_error"] = str(exc)[:160]
        logger.debug("[MANUAL_CONTEXT] MTF fetch failed %s: %s", symbol, exc)

    try:
        from signal_copy.tradingview_factor import TradingViewFactor

        tv = TradingViewFactor()
        tv_data = await tv.fetch(symbol)
        if tv_data and tv_data.get("_ta"):
            metrics["tradingview"] = tv.compute_confluence(tv_data, side)
        else:
            metrics["tradingview"] = {
                "score": 30.0,
                "details": ["TV data unavailable for manual/imported enrichment"],
                "rsi": None,
                "ema20": None,
                "ema50": None,
            }
            metrics["manual_tv_status"] = "unavailable"
    except Exception as exc:
        metrics["manual_tv_error"] = str(exc)[:160]
        metrics["tradingview"] = {
            "score": 30.0,
            "details": ["TV data unavailable for manual/imported enrichment"],
            "rsi": None,
            "ema20": None,
            "ema50": None,
        }
        logger.debug("[MANUAL_CONTEXT] TV fetch failed %s: %s", symbol, exc)

    return metrics
