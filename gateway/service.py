"""ExecutionGateway — ONE RiskManager + ONE trader for every signal source.

This is the "one hand" of the system. It wraps the components that already
exist and are battle-tested in the repo:

    risk/risk_engine.py            -> RiskManager   (unified portfolio limits)
    execution/binance_testnet_trader.py -> trader   (submit_open contract,
                                                     partial TP, trailing, …)

Flow for every OrderIntent, regardless of which engine sent it:

    validate -> risk gate (can_open_new_position) -> size (if needed)
             -> reserve_open_risk -> trader.submit_open -> commit / release

This mirrors exactly what signal_copy/executor.py already does — the gateway
simply makes that path the ONLY path, shared by all engines.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from gateway.order_intent import OrderIntent

logger = logging.getLogger("gateway")


def trader_rejected(response) -> bool:
    """Normalize bool/dict trader contracts into one rejection decision."""
    return not response or (
        isinstance(response, dict)
        and not response.get("ok", response.get("success", True))
    )


@dataclass
class GatewayResult:
    ok: bool
    reason: str
    intent_id: str = ""
    symbol: str = ""
    side: str = ""
    notional: float = 0.0
    risk_amount: float = 0.0
    trader_response: Optional[Dict[str, Any]] = field(default=None)
    risk_details: Optional[Dict[str, Any]] = field(default=None)
    position_confirmed: bool = False  # Add field for orchestrator compatibility

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "intent_id": self.intent_id,
            "symbol": self.symbol,
            "side": self.side,
            "notional": self.notional,
            "risk_amount": self.risk_amount,
            "trader_response": self.trader_response,
            "risk_details": self.risk_details,
        }


class ExecutionGateway:
    """Single choke-point for order execution.

    Args:
        trader:   any object exposing the fusion async ``submit_open(**kwargs)``
                  contract (BinanceTestnetTrader today; swappable later).
        risk_mgr: risk.risk_engine.RiskManager instance (the ONE portfolio).
        submit_timeout_sec: how long to wait for the trader router queue.
    """

    def __init__(self, trader: Any, risk_mgr: Any, *, submit_timeout_sec: float = 30.0):
        self.trader = trader
        self.risk_mgr = risk_mgr
        self.submit_timeout_sec = float(submit_timeout_sec)
        self.history: List[Dict[str, Any]] = []   # last N results, for /portfolio & debugging
        self._history_max = 200
        self.smart_virtual_book = None

    # ── public API ───────────────────────────────────────────

    async def execute(self, intent: OrderIntent, *, dry_run: bool = False) -> GatewayResult:
        """Run one intent through the full unified pipeline."""
        res = GatewayResult(ok=False, reason="", intent_id=intent.intent_id,
                            symbol=intent.symbol, side=str(intent.side).upper())

        # 1) validate the contract itself
        err = intent.validate()
        if err:
            res.reason = f"invalid intent: {err}"
            return self._record(intent, res)

        symbol = intent.symbol
        side = str(intent.side).upper()
        entry = float(intent.entry_price)
        sl = float(intent.sl_price)

        # Independent Arm C paper book observes every valid Signal Copy intent before
        # canonical capacity/risk gates. Failure never changes canonical decision.
        if (intent.source == "SIGNAL_COPY" and
                os.getenv("GATEWAY_PAPER_MAINNET", "true").lower() in ("1", "true", "yes")):
            try:
                if self.smart_virtual_book is None:
                    from execution.smart_virtual_portfolio import SmartVirtualPortfolioBook
                    self.smart_virtual_book = SmartVirtualPortfolioBook(
                        self.trader, equity=float(self.risk_mgr.get_current_equity()))
                    self.trader.smart_virtual_book = self.smart_virtual_book
                await self.smart_virtual_book.capture(intent)
            except Exception as exc:
                logger.warning("[GATEWAY] smart virtual capture failed %s: %s", symbol, exc)

        # 2) Hard cap counts running positions only. Pending setup limits stay unlimited.
        max_running = self.risk_mgr.get_max_running_positions()
        if self.risk_mgr._position_count() >= max_running:
            res.reason = f"max running positions reached ({max_running})"
            return self._record(intent, res)

        # A previously validated pending limit may fill later. Do not apply
        # entry-arrival cooldowns again; running-position cap still applies.
        pending_fill = str(intent.tag).startswith("fusion_quantum_pending_")

        # 3) unified portfolio gate — the whole point of the gateway.
        try:
            gate = self.risk_mgr.check_risk_limits(
                symbol=symbol,
                is_vip=intent.is_vip,
                side=side,
                skip_entry_cooldown=pending_fill,
                skip_cluster_limit=intent.source == "SIGNAL_COPY",
            )
        except Exception as exc:  # never let a risk-engine bug open an unchecked trade
            res.reason = f"risk check error: {exc}"
            return self._record(intent, res)
        if not gate.get("can_trade", False):
            res.reason = f"blocked by portfolio risk: {gate.get('reason', 'unknown')}"
            return self._record(intent, res)

        # 3) sizing — respect the intent's own SL for risk math
        notional, risk_amount, size_err = self._size(intent, entry, sl, side)
        if size_err:
            res.reason = size_err
            return self._record(intent, res)
        # Paper HARD_SL economic risk = adverse entry + adverse exit@SL + round-trip fees.
        # Size/reserve on that, not raw signal distance — LAUSDT blew 1% on fees/exit-slip.
        if os.getenv("GATEWAY_PAPER_MAINNET", "true").lower() in ("1", "true", "yes"):
            try:
                notional, risk_amount, price_quote = await self._paper_econ_size(
                    symbol, side, entry, sl, notional, intent.source,
                )
            except (TypeError, ValueError) as exc:
                res.reason = f"PRICE_VALIDATION_FAILED: {exc}"
                return self._record(intent, res)
        res.notional = notional
        res.risk_amount = risk_amount

        # 4) build the trader payload (same keys signal_copy already uses,
        #    so BinanceTestnetTrader needs ZERO changes)
        payload = self._build_payload(intent, entry, sl, side, notional, risk_amount)
        if os.getenv("GATEWAY_PAPER_MAINNET", "true").lower() in ("1", "true", "yes"):
            payload["execution_price_quote"] = price_quote

        if dry_run:
            res.ok = True
            res.reason = "DRY_RUN (no order placed)"
            logger.info("[GATEWAY] DRY_RUN %s src=%s %s entry=%.6f notional=%.2f sl=%.6f",
                        symbol, intent.source, side, entry, notional, sl)
            return self._record(intent, res)

        # 5) reserve -> submit -> commit/release (identical semantics to signal_copy)
        try:
            reserve = await self.risk_mgr.reserve_open_risk_details(symbol, risk_amount)
        except Exception as exc:
            res.reason = f"RESERVATION_ERROR: {exc}"
            res.risk_details = {"reserved": False, "code": "RESERVATION_ERROR"}
            return self._record(intent, res)
        res.risk_details = reserve
        if not reserve["reserved"]:
            res.reason = f"open-risk reservation blocked: {reserve['code']}"
            return self._record(intent, res)

        try:
            opened = await self.trader.submit_open(timeout_sec=self.submit_timeout_sec, **payload)
        except Exception as exc:
            logger.exception("[GATEWAY] submit_open failed %s: %s", symbol, exc)
            await self._safe_release(symbol)
            res.reason = f"submit_open error: {exc}"
            return self._record(intent, res)

        if trader_rejected(opened):
            await self._safe_release(symbol)
            reason_detail = ""
            if isinstance(opened, dict) and opened.get("error"):
                reason_detail = f" | {opened['error']}"
            elif isinstance(opened, dict) and opened.get("reason"):
                reason_detail = f" | {opened['reason']}"
            res.reason = f"trader rejected open: no position created{reason_detail}"
            res.trader_response = opened if isinstance(opened, dict) else {"raw": str(opened)}
            logger.warning("[GATEWAY] %s %s: %s", symbol, side, res.reason)
            return self._record(intent, res)

        try:
            actual_fill_risk = (opened.get("max_loss_usd_at_fill")
                                if isinstance(opened, dict) else None)
            committed_risk = float(actual_fill_risk) if actual_fill_risk is not None else risk_amount
            await self.risk_mgr.commit_open_trade(symbol, risk_amount=committed_risk, is_vip=intent.is_vip)
        except Exception as exc:
            logger.warning("[GATEWAY] commit_open_trade error %s: %s", symbol, exc)

        res.ok = True
        res.reason = "opened"
        res.position_confirmed = True  # Signal orchestrator that position was confirmed
        res.trader_response = opened if isinstance(opened, dict) else {"raw": str(opened)}
        logger.info("[GATEWAY] OPENED %s src=%s %s notional=%.2f risk=%.2f intent=%s",
                    symbol, intent.source, side, notional, risk_amount, intent.intent_id)
        return self._record(intent, res)

    def portfolio(self) -> Dict[str, Any]:
        """One unified view of the whole book — every engine included."""
        rm = self.risk_mgr
        out: Dict[str, Any] = {}
        for name, fn in (
            ("equity", "get_current_equity"),
            ("daily_pnl_pct", "get_daily_pnl_pct"),
            ("total_exposure_pct", "get_total_exposure_pct"),
            ("reserved_risk_total", "get_reserved_risk_total"),
            ("active_open_risk_total", "get_active_open_risk_total"),
            ("total_open_risk", "get_total_open_risk"),
            ("global_open_risk_budget", "get_parallel_open_risk_budget"),
        ):
            try:
                out[name] = float(getattr(rm, fn)())
            except Exception:
                out[name] = None
        try:
            out["open_position_count"] = int(rm._position_count())
            out["open_symbols"] = sorted(rm._position_symbols())
        except Exception:
            out["open_position_count"] = None
            out["open_symbols"] = []
        try:
            out["daily_loss_limit_hit"] = bool(rm.is_daily_loss_limit_hit())
            out["exposure_limit_exceeded"] = bool(rm.is_exposure_limit_exceeded())
        except Exception:
            pass
        try:
            positions = getattr(rm.trader, "positions", {}) or {}
            out["open_positions"] = [dict(pos) for pos in positions.values()]
            for pos in out["open_positions"]:
                for key, value in list(pos.items()):
                    if hasattr(value, "isoformat"):
                        pos[key] = value.isoformat()
        except Exception:
            out["open_positions"] = []
        out["recent_intents"] = self.history[-20:]
        return out

    # ── internals ────────────────────────────────────────────

    async def _paper_fill_entry(self, symbol: str, side: str, signal_entry: float):
        """Fresh quote used once for sizing and submission."""
        slip = max(0.0, float(os.getenv("PAPER_SLIPPAGE_PCT", "0.0005")))
        getter = getattr(self.trader, "_get_fresh_open_quote", None)
        if not callable(getter):
            raise ValueError("fresh price provenance unavailable")
        quote = await getter(symbol)
        if not isinstance(quote, dict) or quote.get("price_fresh") is not True:
            raise ValueError("fresh market price unavailable")
        base = float(quote.get("price") or 0.0)
        if base <= 0:
            raise ValueError("fresh market price unavailable")
        buy = side == "LONG"
        return base * (1.0 + slip if buy else 1.0 - slip), quote

    @staticmethod
    def _paper_hard_sl_econ(side: str, slipped_entry: float, sl: float, notional: float) -> float:
        """USD loss if HARD_SL hits: entry slip already in price, exit adverse@SL + both fees."""
        fee = max(0.0, float(os.getenv("PAPER_TAKER_FEE_PCT", "0.0005")))
        slip = max(0.0, float(os.getenv("PAPER_SLIPPAGE_PCT", "0.0005")))
        qty = float(notional) / max(float(slipped_entry), 1e-9)
        if side == "LONG":
            adverse_exit = float(sl) * (1.0 - slip)
            gross = (adverse_exit - float(slipped_entry)) * qty
        else:
            adverse_exit = float(sl) * (1.0 + slip)
            gross = (float(slipped_entry) - adverse_exit) * qty
        entry_fee = abs(float(notional)) * fee
        exit_fee = abs(adverse_exit * qty) * fee
        return max(0.0, -gross + entry_fee + exit_fee)

    async def _paper_econ_size(
        self, symbol: str, side: str, entry: float, sl: float, notional: float, source: str,
    ):
        slipped_entry, quote = await self._paper_fill_entry(symbol, side, entry)
        risk_amount = self._paper_hard_sl_econ(side, slipped_entry, sl, notional)
        if source == "SIGNAL_COPY":
            current_equity = float(self.risk_mgr.get_current_equity())
            max_trade_risk = current_equity * 0.01
            if risk_amount > max_trade_risk and risk_amount > 0:
                scale = max_trade_risk / risk_amount
                notional = float(notional) * scale
                risk_amount = self._paper_hard_sl_econ(side, slipped_entry, sl, notional)
        return float(notional), float(risk_amount), quote

    def _size(self, intent: OrderIntent, entry: float, sl: float, side: str):
        """Return (notional, risk_amount, error)."""
        sl_frac = abs(entry - sl) / entry
        if sl_frac <= 0:
            return 0.0, 0.0, "SL distance is zero"

        # Per-position notional cap: no single trade may consume more than
        # max_notional_pct of equity. This is the fix for the exposure-lock bug
        # where an explicit `notional` (from signal_copy sizing) bypassed the cap
        # entirely and one BTC position ate 91% of the book. The cap now applies
        # to BOTH the explicit-notional path and the risk_pct path.
        # Config: MAX_NOTIONAL_PCT_OF_BALANCE. Value may be stored as a percent
        # (e.g. 20.0) or a fraction (0.20) -> normalize both to a fraction.
        try:
            equity = float(self.risk_mgr.get_current_equity())
        except Exception as exc:
            return 0.0, 0.0, f"cannot read equity: {exc}"
        _mnp = getattr(self.risk_mgr, "max_notional_pct", 0.20)
        _frac = (_mnp / 100.0) if _mnp > 1 else _mnp
        _frac = _frac if _frac > 0 else 0.20
        cap = max(10.0, equity * _frac) if equity > 0 else None

        if intent.notional is not None:
            notional = float(intent.notional)
            if intent.source == "SIGNAL_COPY":
                notional = min(notional, equity * 0.01 / sl_frac)
            if cap is not None:
                notional = min(notional, cap)
            return notional, notional * sl_frac, None

        # size from risk_pct against the INTENT's own stop (like signal_copy does)
        if equity <= 0:
            return 0.0, 0.0, "equity is zero"

        risk_budget = equity * float(intent.risk_pct)
        notional = risk_budget / sl_frac
        if cap is not None:
            notional = min(notional, cap)
        notional = max(10.0, notional)
        return notional, notional * sl_frac, None

    def _build_payload(self, intent: OrderIntent, entry: float, sl: float,
                       side: str, notional: float, risk_amount: float) -> Dict[str, Any]:
        tps = [float(t) for t in (intent.tps or [])]
        tp_payload = {f"tp{i}": tp for i, tp in enumerate(tps, start=1)}
        tp_full = tps[-1] if tps else 0.0
        payload: Dict[str, Any] = {
            "symbol": intent.symbol,
            "side": side,
            "direction": side,
            "entry_price": entry,
            "sl_price": sl,
            "sl": sl,
            **tp_payload,
            "tp_full": tp_full,
            "notional": notional,
            "size_usd": notional,
            "base_notional": notional,
            "regime": str(intent.regime or "TRENDING"),
            "confidence": float(intent.confidence),
            "actual_risk_amount": risk_amount,
            "risk_amount": risk_amount,
            "planned_risk_amount": risk_amount,
            "source": intent.source,
            "signal_id": intent.intent_id,
            "tag": intent.tag,
            "adv": dict(intent.adv_snapshot or {}),
            "adv_snapshot": dict(intent.adv_snapshot or {}),
        }
        if intent.leverage:
            payload["leverage"] = int(intent.leverage)
        return payload

    async def _safe_release(self, symbol: str) -> None:
        try:
            await self.risk_mgr.release_open_risk(symbol)
        except Exception:
            pass

    def _record(self, intent: OrderIntent, res: GatewayResult) -> GatewayResult:
        self.history.append({
            "intent": intent.to_dict(),
            "result": {k: v for k, v in res.to_dict().items() if k != "trader_response"},
        })
        if len(self.history) > self._history_max:
            self.history = self.history[-self._history_max:]
        if not res.ok:
            logger.info("[GATEWAY] REJECTED %s src=%s: %s", intent.symbol, intent.source, res.reason)
        return res
