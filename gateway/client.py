"""GatewayClient — how every engine talks to the one execution hand.

Engines never import the trader or RiskManager anymore. They build an
OrderIntent and POST it here. If the gateway is down, the engine simply
does not trade (fail-closed — never fall back to direct exchange access).

Usage (async):

    from gateway.client import GatewayClient
    from gateway.order_intent import OrderIntent

    client = GatewayClient()  # reads GATEWAY_URL + GATEWAY_TOKEN from env
    result = await client.execute(OrderIntent(
        source="M30H1_ENGINE", symbol="BTCUSDT", side="LONG",
        entry_price=65000.0, sl_price=64200.0, tps=[65800.0, 66600.0, 68000.0],
        risk_pct=0.01, tag="FVG+OB m30",
    ))
    if result["ok"]:
        ...
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, Optional

import aiohttp

from gateway.order_intent import OrderIntent

logger = logging.getLogger("gateway.client")

DEFAULT_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:8787/gateway")


class GatewayClient:
    def __init__(self, base_url: str = "", token: str = "", *,
                 timeout_sec: float = 35.0, retries: int = 2):
        self.base_url = (base_url or DEFAULT_URL).rstrip("/")
        self.token = token or os.getenv("GATEWAY_TOKEN", "")
        if not self.token:
            raise RuntimeError("GATEWAY_TOKEN env var (or token arg) is required")
        self.timeout = aiohttp.ClientTimeout(total=timeout_sec)
        self.retries = int(retries)

    @property
    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def execute(self, intent: OrderIntent, *, dry_run: bool = False) -> Dict[str, Any]:
        """POST the intent. Returns the GatewayResult dict.

        Fail-closed: on any transport error after retries, returns
        {"ok": False, "reason": "..."} — the engine must NOT trade directly.
        """
        body = intent.to_dict()
        body["dry_run"] = dry_run
        last_err: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                async with aiohttp.ClientSession(timeout=self.timeout) as session:
                    async with session.post(f"{self.base_url}/execute",
                                            json=body, headers=self._headers) as resp:
                        data = await resp.json()
                        if resp.status != 200:
                            return {"ok": False,
                                    "reason": f"gateway HTTP {resp.status}: {data}"}
                        return data
            except Exception as exc:
                last_err = exc
                logger.warning("[GATEWAY_CLIENT] attempt %d failed: %s", attempt + 1, exc)
                await asyncio.sleep(min(2.0 * (attempt + 1), 5.0))
        return {"ok": False, "reason": f"gateway unreachable: {last_err}"}

    async def portfolio(self) -> Dict[str, Any]:
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.get(f"{self.base_url}/portfolio", headers=self._headers) as resp:
                return await resp.json()

    async def position_action(self, symbol: str, action: str, price: float | None = None) -> Dict[str, Any]:
        body = {"symbol": symbol, "action": action, "price": price}
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.post(f"{self.base_url}/position-action", json=body, headers=self._headers) as resp:
                return await resp.json()
