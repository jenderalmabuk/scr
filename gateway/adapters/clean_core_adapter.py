"""Adapter: fusionnew/clean_core (M30/H1 engine) -> Execution Gateway.

This RETIRES clean_core/executor.py's order-placement role. The engine keeps
100% of its signal logic (FVG+OB, adversarial_v2, regime detection) and only
the final "place the trade" step changes: instead of calling FuturesTestnet
order methods, it hands the finished setup to `open_via_gateway`.

Integration inside fusionnew/clean_core/engine.py (Trader class):

    # OLD (direct exchange access):
    #   self.ex.set_leverage(...); self.ex.limit_entry(...)
    #   self.ex.stop_market(...);  self.ex.take_profit_market(...)

    # NEW (one line — the gateway handles SL/TP/partials/trailing):
    from gateway.adapters.clean_core_adapter import open_via_gateway
    ok = await open_via_gateway(symbol=symbol, side=side,
                                entry=entry, sl=sl, tps=[tp1, tp2, tp3],
                                risk_pct=0.01, regime=regime,
                                confidence=prob, tag=setup_name)

Note: clean_core is partly synchronous. Use `open_via_gateway_sync` from
plain (non-async) code paths.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from gateway.client import GatewayClient
from gateway.order_intent import OrderIntent

logger = logging.getLogger("gateway.clean_core")

_client: Optional[GatewayClient] = None


def _get_client() -> GatewayClient:
    global _client
    if _client is None:
        _client = GatewayClient()
    return _client


def build_intent(*, symbol: str, side: str, entry: float, sl: float,
                 tps: List[float], risk_pct: float = 0.01,
                 regime: str = "TRENDING", confidence: float = 0.5,
                 tier: str = "Standard", leverage: Optional[int] = None,
                 tag: str = "", adv_snapshot: Optional[Dict[str, Any]] = None) -> OrderIntent:
    # Map clean_core BUY/SELL -> OrderIntent LONG/SHORT
    side_map = {"BUY": "LONG", "SELL": "SHORT", "LONG": "LONG", "SHORT": "SHORT"}
    mapped_side = side_map.get(str(side).upper(), str(side).upper())
    return OrderIntent(
        source="M30H1_ENGINE",
        symbol=symbol,
        side=mapped_side,
        entry_price=float(entry),
        sl_price=float(sl),
        tps=[float(t) for t in tps if t and t > 0],
        risk_pct=float(risk_pct),
        regime=regime,
        confidence=float(confidence),
        tier=tier,
        leverage=leverage,
        tag=tag,
        adv_snapshot=dict(adv_snapshot or {}),
    )


async def open_via_gateway(*, dry_run: bool = False, **kwargs) -> Dict[str, Any]:
    """Async path. kwargs = build_intent kwargs. Returns GatewayResult dict."""
    intent = build_intent(**kwargs)
    err = intent.validate()
    if err:
        logger.warning("[CLEAN_CORE->GW] invalid intent %s: %s", intent.symbol, err)
        return {"ok": False, "reason": f"invalid intent: {err}"}
    result = await _get_client().execute(intent, dry_run=dry_run)
    if result.get("ok"):
        logger.info("[CLEAN_CORE->GW] opened %s %s notional=%.2f",
                    intent.symbol, intent.side, float(result.get("notional", 0)))
    else:
        logger.info("[CLEAN_CORE->GW] rejected %s: %s", intent.symbol, result.get("reason"))
    return result


def open_via_gateway_sync(*, dry_run: bool = False, **kwargs) -> Dict[str, Any]:
    """Sync wrapper for clean_core's non-async code paths."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        # Called from inside an event loop — schedule and wait via thread.
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(
                lambda: asyncio.run(open_via_gateway(dry_run=dry_run, **kwargs))
            ).result()
    return asyncio.run(open_via_gateway(dry_run=dry_run, **kwargs))
