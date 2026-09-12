"""Adapter: signal_copy pipeline -> Execution Gateway.

signal_copy/executor.py is already trader-agnostic: it only needs an object
with ``async submit_open(**payload)`` and a risk manager. So instead of
changing signal_copy at all, we swap what we inject into it:

    OLD wiring:
        trader   = BinanceTestnetTrader(...)          # its OWN exchange conn
        risk_mgr = RiskManager(...)                   # its OWN portfolio
        executor = SignalExecutor(trader, risk_mgr)

    NEW wiring (this file):
        from gateway.adapters.signal_copy_adapter import GatewayTraderShim, RemoteRiskStub
        shim     = GatewayTraderShim()                # posts to the gateway
        risk_stub = RemoteRiskStub(shim)              # real checks happen in gateway
        executor = SignalExecutor(shim, risk_stub)

The gateway then runs the REAL RiskManager checks + sizing caps, so signals
and the M30/H1 engine finally share one portfolio.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from gateway.client import GatewayClient
from gateway.order_intent import OrderIntent

logger = logging.getLogger("gateway.signal_copy")


class GatewayTraderShim:
    """Looks like a trader (has submit_open) but forwards to the gateway.

    It converts the signal_copy payload (symbol/side/entry/sl/tp1..tpN/notional)
    into an OrderIntent with an EXPLICIT notional, so the sizing that
    SignalExecutor computed against the signal's own SL is preserved.
    """

    def __init__(self, client: Optional[GatewayClient] = None):
        self.client = client or GatewayClient()

    async def submit_open(self, timeout_sec: float = 30.0, **payload) -> Optional[Dict[str, Any]]:
        del timeout_sec  # gateway client has its own timeout
        # collect the TP ladder back out of tp1..tpN keys
        tps = []
        i = 1
        while f"tp{i}" in payload:
            tp = float(payload[f"tp{i}"] or 0)
            if tp > 0:
                tps.append(tp)
            i += 1
        if not tps and float(payload.get("tp_full", 0) or 0) > 0:
            tps = [float(payload["tp_full"])]

        intent = OrderIntent(
            source="SIGNAL_COPY",
            symbol=str(payload.get("symbol", "")),
            side=str(payload.get("side", payload.get("direction", ""))).upper(),
            entry_price=float(payload.get("entry_price", 0) or 0),
            sl_price=float(payload.get("sl", payload.get("sl_price", 0)) or 0),
            tps=tps,
            notional=float(payload.get("notional", payload.get("size_usd", 0)) or 0) or None,
            regime=str(payload.get("regime", "TRENDING")),
            confidence=float(payload.get("confidence", 0.5) or 0.5),
            is_vip=bool(payload.get("is_vip", False)),
            leverage=int(payload["leverage"]) if payload.get("leverage") else None,
            tag=str(payload.get("signal_id", "") or payload.get("signal_source", "")),
            adv_snapshot=dict(payload.get("adv_snapshot", payload.get("adv", {})) or {}),
        )
        result = await self.client.execute(intent)
        if result.get("ok"):
            trader = result.get("trader_response") or {}
            fill_entry = float(trader.get("entry_price") or 0.0)
            fill_notional = float(trader.get("notional") or 0.0)
            return {
                **result,
                "entry_price": fill_entry,
                "notional": fill_notional,
                "position_confirmed": fill_entry > 0 and fill_notional > 0,
            }
        logger.info("[SIGNAL_COPY->GW] rejected %s: %s", intent.symbol, result.get("reason"))
        return result


class RemoteRiskStub:
    """Minimal risk facade for SignalExecutor when the REAL RiskManager lives
    inside the gateway. Reserve/commit/release become no-ops (the gateway does
    them for real); equity is fetched from the gateway and cached briefly so
    SignalExecutor's local sizing math still works."""

    def __init__(self, shim: GatewayTraderShim, *, equity_ttl_sec: float = 20.0):
        self._client = shim.client
        self._equity: float = 0.0
        self._equity_ts: float = 0.0
        self._ttl = float(equity_ttl_sec)

    def get_current_equity(self) -> float:
        # sync call sites: return cached value; refresh happens opportunistically
        return self._equity if self._equity > 0 else 0.0

    async def refresh_equity(self) -> float:
        now = time.time()
        if now - self._equity_ts < self._ttl and self._equity > 0:
            return self._equity
        try:
            snap = await self._client.portfolio()
            self._equity = float(snap.get("equity") or 0.0)
            self._equity_ts = now
        except Exception as exc:
            logger.warning("[SIGNAL_COPY->GW] equity refresh failed: %s", exc)
        return self._equity

    # reservation semantics are enforced inside the gateway — always allow here
    async def reserve_open_risk(self, symbol: str, risk_amount: float) -> bool:
        return True

    async def release_open_risk(self, symbol: str) -> None:
        return None

    async def commit_open_trade(self, symbol: str, risk_amount: float = 0.0,
                                is_vip: bool = False) -> None:
        return None

    def compute_position_size(self, **kwargs) -> Dict[str, Any]:
        """SignalExecutor calls this for the synthetic-SL branch. Provide a
        conservative fallback; the gateway re-caps everything anyway."""
        equity = self.get_current_equity() or 1000.0
        entry = max(float(kwargs.get("entry_price", 0) or 0), 1e-9)
        risk_pct = float(kwargs.get("adaptive_risk_pct", 0.01) or 0.01)
        atr_pct = max(float(kwargs.get("atr_pct", 0.01) or 0.01), 0.0005)
        sl_pct = max(0.8, min(atr_pct * 2.5 * 100.0, 8.0))
        side = str(kwargs.get("side", "LONG")).upper()
        sl_price = entry * (1 - sl_pct / 100.0) if side == "LONG" else entry * (1 + sl_pct / 100.0)
        risk_amount = equity * risk_pct
        notional = max(10.0, risk_amount / (sl_pct / 100.0))
        return {
            "notional": notional,
            "sl_price": sl_price,
            "sl_distance_pct": sl_pct,
            "risk_amount": risk_amount,
            "actual_risk_amount": risk_amount,
            "planned_risk_amount": risk_amount,
            "rejected": False,
        }
