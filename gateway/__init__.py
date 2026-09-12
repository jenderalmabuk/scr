"""Execution Gateway — one RiskManager, one trader, one exchange connection.

All signal engines send OrderIntents here instead of executing directly.

    order_intent.py  -> the OrderIntent contract (validation included)
    service.py       -> ExecutionGateway (risk gate -> size -> submit)
    api.py           -> FastAPI router: POST /execute, GET /portfolio
    client.py        -> GatewayClient used by every engine
    run_gateway.py   -> standalone uvicorn entrypoint
    adapters/        -> drop-in bridges for clean_core and signal_copy
"""

from gateway.order_intent import OrderIntent
from gateway.service import ExecutionGateway, GatewayResult

__all__ = ["OrderIntent", "ExecutionGateway", "GatewayResult"]
