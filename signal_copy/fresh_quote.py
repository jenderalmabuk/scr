"""Execution-safe multi-exchange quotes. No cache fallback."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import aiohttp


def choose_quote(symbol: str, binance: float, bybit: float) -> dict[str, Any]:
    binance, bybit = float(binance or 0), float(bybit or 0)
    now = datetime.now(timezone.utc).isoformat()
    if binance > 0 and bybit > 0:
        divergence = abs(binance - bybit) / min(binance, bybit) * 100
        if divergence > 10.0:
            return {"price": 0.0, "price_source": "strong_multi_exchange_conflict",
                    "price_observed_at": now, "price_age_sec": 0.0,
                    "price_fresh": False, "secondary_price": bybit,
                    "secondary_source": "bybit_linear_rest",
                    "cross_exchange_divergence_pct": divergence}
        price, source = binance, "binance_futures_rest"
    elif binance > 0:
        price, source, divergence = binance, "binance_futures_rest", None
    elif bybit > 0:
        price, source, divergence = bybit, "bybit_linear_rest", None
    else:
        return {"price": 0.0, "price_source": "fresh_price_unavailable",
                "price_observed_at": now, "price_age_sec": 0.0, "price_fresh": False}
    return {"price": price, "price_source": source, "price_observed_at": now,
            "price_age_sec": 0.0, "price_fresh": True,
            "secondary_price": bybit if source == "binance_futures_rest" and bybit > 0 else None,
            "secondary_source": "bybit_linear_rest" if source == "binance_futures_rest" and bybit > 0 else None,
            "cross_exchange_divergence_pct": divergence}


async def fetch_fresh_quote(symbol: str) -> dict[str, Any]:
    symbol = str(symbol).upper()
    timeout = aiohttp.ClientTimeout(total=4)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async def binance() -> float:
            try:
                async with session.get("https://fapi.binance.com/fapi/v1/ticker/price", params={"symbol": symbol}) as r:
                    return float((await r.json()).get("price", 0)) if r.status == 200 else 0.0
            except Exception:
                return 0.0

        async def bybit() -> float:
            try:
                async with session.get("https://api.bybit.com/v5/market/tickers",
                                       params={"category": "linear", "symbol": symbol}) as r:
                    data = await r.json() if r.status == 200 else {}
                    rows = ((data.get("result") or {}).get("list") or [])
                    return float(rows[0].get("lastPrice", 0)) if rows else 0.0
            except Exception:
                return 0.0

        # Fast listings can diverge briefly. One sample is advisory, never a
        # terminal strategy veto. Retry transport failures; no stale fallback.
        prices = (0.0, 0.0)
        for attempt in range(3):
            prices = await asyncio.gather(binance(), bybit())
            if any(float(value or 0) > 0 for value in prices):
                break
            if attempt < 2:
                await asyncio.sleep(0.25 * (attempt + 1))
        return choose_quote(symbol, *prices)
