"""HTTP surface of the Execution Gateway (FastAPI router).

Two ways to use it:

1. Mount into the existing api/main.py:

       from gateway.api import build_router
       app.include_router(build_router(gateway), prefix="/gateway")

2. Run standalone (see gateway/run_gateway.py):

       uvicorn gateway.run_gateway:app --port 8787

Endpoints:
    POST /execute    body = OrderIntent JSON  -> GatewayResult JSON
    GET  /portfolio  unified portfolio snapshot (all engines combined)
    GET  /health     liveness probe

Security: set GATEWAY_TOKEN env var; callers must send
``Authorization: Bearer <token>``. Without the env var the gateway refuses
to start — an execution endpoint must never be open.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from gateway.order_intent import OrderIntent
from gateway.service import ExecutionGateway


class OrderIntentBody(BaseModel):
    source: str
    symbol: str
    side: str
    entry_price: float
    sl_price: float
    tps: List[float] = Field(default_factory=list)
    notional: Optional[float] = None
    risk_pct: Optional[float] = None
    regime: str = "TRENDING"
    confidence: float = 0.5
    tier: str = "Standard"
    is_vip: bool = False
    leverage: Optional[int] = None
    tag: str = ""
    adv_snapshot: Dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = False


class PositionActionBody(BaseModel):
    symbol: str
    action: str
    price: Optional[float] = None
    reason: Optional[str] = None


def _auth_dependency():
    token = os.getenv("GATEWAY_TOKEN", "")
    if not token:
        raise RuntimeError(
            "GATEWAY_TOKEN environment variable is required. "
            "The execution endpoint must never run unauthenticated."
        )

    async def check(authorization: str = Header(default="")) -> None:
        if authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="invalid gateway token")

    return check


def build_router(gateway: ExecutionGateway) -> APIRouter:
    router = APIRouter(dependencies=[Depends(_auth_dependency())])

    @router.post("/execute")
    async def execute(body: OrderIntentBody):
        data = body.model_dump()
        dry_run = bool(data.pop("dry_run", False))
        intent = OrderIntent.from_dict(data)
        result = await gateway.execute(intent, dry_run=dry_run)
        return result.to_dict()

    @router.get("/portfolio")
    async def portfolio():
        return gateway.portfolio()

    @router.get("/health")
    async def health():
        return {"status": "ok", "service": "execution-gateway"}

    @router.post("/position-action")
    async def position_action(body: PositionActionBody):
        symbol = body.symbol.upper().replace("/", "")
        if body.action == "MOVE_SL" and body.price is not None:
            return await gateway.trader.update_stop(symbol, body.price)
        if body.action == "CLOSE":
            close_reason = str(body.reason or "PROVIDER_CLOSE").upper()
            allowed_reasons = {
                "PROVIDER_CLOSE",
                "DYNAMIC_EXIT_INVALIDATED",
                "TIME_EXIT_NO_PROGRESS",
            }
            if close_reason not in allowed_reasons:
                return {"ok": False, "code": "INVALID_CLOSE_REASON"}
            return await gateway.trader.close_position(symbol, close_reason)
        return {"ok": False, "code": "INVALID_POSITION_ACTION"}

    return router
