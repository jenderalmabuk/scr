"""Standalone Execution Gateway service.

Run:
    export GATEWAY_TOKEN=$(openssl rand -hex 24)
    export BINANCE_TESTNET_API_KEY=...
    export BINANCE_TESTNET_API_SECRET=...
    export STARTING_BALANCE=1000
    uvicorn gateway.run_gateway:app --host 127.0.0.1 --port 8787

This is the ONLY process that talks to the exchange. All engines
(clean_core M30/H1, signal_copy, and optionally revo) POST OrderIntents to
http://127.0.0.1:8787/gateway/execute.

It reuses the mature components already in the repo:
    - risk.risk_engine.RiskManager        (one portfolio, one set of limits)
    - execution.binance_testnet_trader    (one exchange connection,
                                           partial TP / trailing / journal)
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from gateway.api import build_router
from gateway.service import ExecutionGateway

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("gateway.main")

_gateway: ExecutionGateway | None = None


def _build_gateway() -> ExecutionGateway:
    """Wire the ONE RiskManager + ONE trader. Adjust imports/ctor args here
    if your trader constructor differs — this is the single place to do it.

    GATEWAY_PAPER_MAINNET=true  -> PaperMainnetTrader (fills on REAL mainnet
    prices, no orders placed — valid edge validation; DEFAULT).
    GATEWAY_PAPER_MAINNET=false -> BinanceTestnetTrader (routes to Binance
    testnet; its fake orderbook causes divergent fills / instant stop-outs).
    """
    from risk.risk_engine import RiskManager

    starting_balance = float(os.getenv("STARTING_BALANCE", "1000"))
    risk_mgr = RiskManager(starting_balance=starting_balance)

    # Trader mode: bybit (real money), paper (simulated), testnet (binance testnet)
    trader_mode = os.getenv("GATEWAY_TRADER_MODE", "paper").lower()
    
    if trader_mode == "bybit":
        from execution.bybit_mainnet_trader import BybitMainnetTrader
        journal_path = os.getenv("JOURNAL_PATH", "/app/journal")
        trader = BybitMainnetTrader(journal_path=journal_path)
        logger.info("[GATEWAY] trader = BybitMainnetTrader (REAL MONEY - Bybit USDT Perpetuals)")
    elif trader_mode == "paper":
        from execution.paper_mainnet_trader import PaperMainnetTrader
        # Gateway runs on the HOST (not inside docker), so the docker-internal
        # DNS name `fastapi:8000` won't resolve — default to the published port.
        # Override with PAPER_NEXUS_API if the gateway ever moves into a container.
        nexus_api = os.getenv("PAPER_NEXUS_API", "http://localhost:8000")
        trader = PaperMainnetTrader(nexus_api=nexus_api)
        logger.info("[GATEWAY] trader = PaperMainnetTrader (mainnet-priced paper, nexus=%s)", nexus_api)
    elif trader_mode == "testnet":
        from execution.binance_testnet_trader import BinanceTestnetTrader
        api_key = os.getenv("BINANCE_TESTNET_API_KEY", "")
        api_secret = os.getenv("BINANCE_TESTNET_API_SECRET", "") or os.getenv("BINANCE_TESTNET_SECRET", "")
        if not api_key or not api_secret:
            raise RuntimeError("BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET must be set")
        trader = BinanceTestnetTrader(api_key=api_key, api_secret=api_secret)
        logger.info("[GATEWAY] trader = BinanceTestnetTrader (testnet)")
    else:
        raise ValueError(f"Unknown GATEWAY_TRADER_MODE: {trader_mode}. Use 'bybit', 'paper', or 'testnet'")

    # Let RiskManager see the trader's live positions for exposure/cluster math
    if hasattr(risk_mgr, "attach_trader"):
        risk_mgr.attach_trader(trader)
    elif hasattr(risk_mgr, "trader"):
        risk_mgr.trader = trader

    # Back-reference so the trader can feed realized PnL into equity on close.
    if hasattr(trader, "risk_mgr"):
        trader.risk_mgr = risk_mgr

    # Restore realized paper equity so restarts don't reset the book to
    # STARTING_BALANCE while open positions persist.
    if trader_mode == "paper":
        from execution.paper_mainnet_trader import load_equity_state
        saved = load_equity_state()
        if saved and saved["equity"] > 0:
            risk_mgr.sync_balance(saved["equity"], reset_daily_baseline="daily_start_balance" not in saved)
            if "daily_start_balance" in saved:
                risk_mgr.daily_start_balance = saved["daily_start_balance"]
            logger.info("[GATEWAY] restored paper equity = %.2f; daily baseline = %.2f",
                        saved["equity"], risk_mgr.daily_start_balance)

    return ExecutionGateway(trader=trader, risk_mgr=risk_mgr)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _gateway
    _gateway = _build_gateway()
    # start the trader's order-router / position-manager loops if it has them
    start = getattr(_gateway.trader, "start", None)
    task = None
    if callable(start):
        result = start()
        if asyncio.iscoroutine(result):
            task = asyncio.create_task(result)
    # Also start run_maintenance_loop if it exists (Bybit trader)
    elif hasattr(_gateway.trader, "run_maintenance_loop"):
        task = asyncio.create_task(_gateway.trader.run_maintenance_loop())
        logger.info("[GATEWAY] started trader.run_maintenance_loop()")
    logger.info("[GATEWAY] up — single execution point ready on /gateway")
    yield
    stop = getattr(_gateway.trader, "stop", None)
    if callable(stop):
        result = stop()
        if asyncio.iscoroutine(result):
            await result
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


class _GatewayProxy:
    """Routes are registered before startup, but the real gateway is only
    built inside lifespan (so the trader connects on the event loop).
    This proxy forwards every attribute access to the live instance."""

    def __getattr__(self, name):
        if _gateway is None:
            raise RuntimeError("gateway not initialized yet (still starting up)")
        return getattr(_gateway, name)


app = FastAPI(title="Fusion Execution Gateway", version="1.0.0", lifespan=lifespan)
app.include_router(build_router(_GatewayProxy()), prefix="/gateway")
