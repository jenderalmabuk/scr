"""
SignalExecutor: turns an approved, validated signal into a live position.

Trader-agnostic: works with any object exposing the fusion `submit_open`
contract (PaperExecutionEngine, binance/bybit testnet/live traders). Sizing
uses the existing RiskManager so the trade risks ~1% of equity by default.

Position management (partial TP at signal targets, trailing stop after TP1,
SL / invalidation exit) is handled by the engine's ManagedPosition lifecycle
once the position is open — we just hand it the signal's SL + targets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from .signal_schema import ParsedSignal, SignalSide
from .validation_engine import ValidationResult
from utils.logger import logger

try:
    import config
except Exception:  # pragma: no cover
    config = None


@dataclass
class ExecutionOutcome:
    ok: bool
    reason: str
    symbol: str
    side: str
    entry_price: float = 0.0
    notional: float = 0.0
    sl_price: float = 0.0
    tp1: float = 0.0
    tp_full: float = 0.0
    risk_amount: float = 0.0
    entry_drift_r: float = 0.0
    rr_tp1: float = 0.0
    rr_full: float = 0.0
    raw: Optional[Dict[str, Any]] = None
    execution_intended: bool = False
    gateway_accepted: bool = False
    position_confirmed: bool = False


class SignalExecutor:
    def __init__(self, trader: Any, risk_mgr: Any, *, risk_pct: float = 0.01,
                 submit_timeout_sec: float = 25.0):
        """
        trader:   object with async submit_open(**kwargs) -> dict|None
        risk_mgr: RiskManager (compute_position_size, reserve/commit/release)
        risk_pct: fraction of equity to risk per signal trade (default 1%).
        """
        self.trader = trader
        self.risk_mgr = risk_mgr
        self.risk_pct = float(risk_pct)
        self.submit_timeout_sec = float(submit_timeout_sec)

    def _resolve_entry_price(self, sig: ParsedSignal, metrics: Dict[str, Any]) -> float:
        live = 0.0
        try:
            live = float(metrics.get("price", 0.0) or 0.0)
        except Exception:
            live = 0.0
        if live > 0:
            # Prefer live price if it is still inside (or very near) the zone.
            lo, hi = sig.entry_low, sig.entry_high
            pad = (hi - lo) * 0.5 + hi * 0.003
            if lo - pad <= live <= hi + pad:
                return live
        return sig.entry_mid

    def _size(self, sig: ParsedSignal, entry_price: float, metrics: Dict[str, Any]) -> Dict[str, Any]:
        atr_pct = float(metrics.get("atr_pct", 1.0) or 1.0)
        regime = str(metrics.get("regime_label", "RANGING") or "RANGING")
        # Risk the configured fraction of equity by overriding adaptive risk pct.
        sizing = self.risk_mgr.compute_position_size(
            probability=0.6,
            regime_label=regime,
            atr_pct=atr_pct,
            entry_price=entry_price,
            side=sig.side.value,
            adaptive_risk_pct=self.risk_pct,
            symbol=sig.symbol,
        )
        return sizing

    async def execute(self, result: ValidationResult, *, dry_run: bool = False, risk_pct: float = None) -> ExecutionOutcome:
        sig = result.signal
        metrics = result.metrics_snapshot or {}
        entry_price = self._resolve_entry_price(sig, metrics)

        if entry_price <= 0:
            return ExecutionOutcome(False, "no usable entry price", sig.symbol, sig.side.value)

        # --- size against the SIGNAL's own stop loss so risk respects the call ---
        sizing = self._size(sig, entry_price, metrics)
        # If the signal provides an explicit SL, recompute notional from it so we
        # risk exactly risk_pct of equity to the signal's stop (not the synthetic one).
        sl_price = sig.stop_loss if sig.stop_loss is not None else float(sizing.get("sl_price", 0.0))
        notional = float(sizing.get("notional", 0.0))
        try:
            equity = float(self.risk_mgr.get_current_equity())
        except Exception:
            equity = 0.0

        if sig.stop_loss is not None and entry_price > 0 and equity > 0:
            sl_frac = abs(entry_price - sig.stop_loss) / entry_price
            if sl_frac > 0:
                effective_risk_pct = min(risk_pct if risk_pct is not None else self.risk_pct, self.risk_pct)
                risk_budget = equity * effective_risk_pct
                notional = risk_budget / sl_frac
                # Cap by leverage capacity (use up to 50% of leveraged buying
                # power as a safety margin), NOT by the tiny per-position
                # notional cap meant for the multi-position scanner bot.
                try:
                    lev = float(getattr(config, "LEVERAGE", 10) or 10)
                except Exception:
                    lev = 10.0
                margin_cap_notional = equity * lev * 0.5
                if margin_cap_notional > 0:
                    notional = min(notional, margin_cap_notional)

        risk_amount = notional * (abs(entry_price - sl_price) / entry_price) if entry_price > 0 else 0.0

        # Build TP ladder: all levels from signal
        tp_ladder = list(sig.take_profits) if sig.take_profits else []
        tp1 = tp_ladder[0] if tp_ladder else 0.0
        tp_full = tp_ladder[-1] if tp_ladder else 0.0

        # Reject only when the whole provider target ladder is already behind
        # market. TP1 may be a close partial and is not a validity boundary.
        live_price = float(metrics.get("price", 0.0) or 0.0)
        if live_price > 0 and tp_ladder:
            if sig.side == SignalSide.LONG and all(tp <= live_price for tp in tp_ladder):
                return ExecutionOutcome(
                    False, f"ALL_TARGETS_PASSED — price {live_price:g}",
                    sig.symbol, sig.side.value, entry_price, notional, sl_price, tp1, tp_full, risk_amount,
                )
            if sig.side == SignalSide.SHORT and all(tp >= live_price for tp in tp_ladder):
                return ExecutionOutcome(
                    False, f"ALL_TARGETS_PASSED — price {live_price:g}",
                    sig.symbol, sig.side.value, entry_price, notional, sl_price, tp1, tp_full, risk_amount,
                )
        
        # Map TP levels to payload keys (tp1, tp2, tp3, ... tpN)
        tp_payload = {}
        for i, tp in enumerate(tp_ladder, start=1):
            tp_payload[f"tp{i}"] = tp

        adv_snapshot = dict(metrics)
        adv_snapshot.update({
            "signal_source": sig.source_name or sig.source.value,
            "source_chat_id": sig.source_chat_id,
            "signal_id": sig.signal_id,
            "signal_entry_low": sig.entry_low,
            "signal_entry_high": sig.entry_high,
            "signal_active_entry": getattr(sig, "active_entry", None),
            "signal_entry_type": getattr(sig, "entry_type", ""),
            "signal_timeframe": getattr(sig, "timeframe", ""),
            "signal_leverage": sig.leverage,
            "signal_tp_ladder": list(sig.take_profits),
            "signal_raw_text": (sig.raw_text or "")[:1000],
        })

        payload = {
            "symbol": sig.symbol,
            "side": sig.side.value,
            "direction": sig.side.value,
            "entry_price": entry_price,
            "sl_price": sl_price,
            "sl": sl_price,
            **tp_payload,  # all TP levels: tp1, tp2, tp3, ...
            "tp_full": tp_full,
            "notional": notional,
            "size_usd": notional,
            "base_notional": notional,
            "regime": str(metrics.get("regime_label", "TRENDING") or "TRENDING"),
            "score": float(result.score),
            "confidence": float(result.score) / 100.0,
            "actual_risk_amount": risk_amount,
            "risk_amount": risk_amount,
            "planned_risk_amount": risk_amount,
            "source": "SIGNAL_COPY",
            "signal_id": sig.signal_id,
            "signal_source": sig.source_name or sig.source.value,
            "signal_take_profits": list(sig.take_profits),
            "leverage": sig.leverage,
            "adv": adv_snapshot,
            "adv_snapshot": adv_snapshot,
        }

        outcome = ExecutionOutcome(
            ok=False, reason="", symbol=sig.symbol, side=sig.side.value,
            entry_price=entry_price, notional=notional, sl_price=sl_price,
            tp1=tp1, tp_full=tp_full, risk_amount=risk_amount,
            execution_intended=True,
        )

        if dry_run:
            outcome.ok = True
            outcome.reason = "DRY_RUN (no order placed)"
            logger.info("[SIGNAL_EXEC] DRY_RUN %s %s entry=%.6f notional=%.2f sl=%.6f tp1=%.6f tpN=%.6f",
                        sig.symbol, sig.side.value, entry_price, notional, sl_price, tp1, tp_full)
            return outcome

        # --- reserve risk, submit, commit/release ---
        reserved = False
        try:
            reserved = await self.risk_mgr.reserve_open_risk(sig.symbol, risk_amount)
        except Exception as exc:
            logger.warning("[SIGNAL_EXEC] risk reserve error %s: %s", sig.symbol, exc)
        if not reserved:
            outcome.reason = "risk reservation blocked"
            return outcome

        try:
            opened = await self.trader.submit_open(timeout_sec=self.submit_timeout_sec, **payload)
        except Exception as exc:
            logger.exception("[SIGNAL_EXEC] submit_open failed %s: %s", sig.symbol, exc)
            try:
                await self.risk_mgr.release_open_risk(sig.symbol)
            except Exception:
                pass
            outcome.reason = f"submit_open error: {exc}"
            return outcome

        accepted = isinstance(opened, dict) and (
            opened.get("ok") is True or opened.get("success") is True
        )
        confirmed = accepted and (
            opened.get("position_confirmed") is True
            or (
                float(opened.get("notional") or 0.0) > 0
                and float(opened.get("entry_price") or 0.0) > 0
            )
        )
        outcome.gateway_accepted = accepted
        outcome.position_confirmed = confirmed
        if not confirmed:
            outcome.raw = opened if isinstance(opened, dict) else {"result": opened}
            try:
                await self.risk_mgr.release_open_risk(sig.symbol)
            except Exception:
                pass
            if isinstance(opened, dict) and opened.get("reason"):
                outcome.reason = str(opened["reason"])
            elif isinstance(opened, dict) and opened.get("error"):
                outcome.reason = str(opened["error"])
            else:
                outcome.reason = "gateway response did not confirm position"
            logger.warning("[SIGNAL_EXEC] %s %s: %s", sig.symbol, sig.side.value, outcome.reason)
            return outcome

        try:
            await self.risk_mgr.commit_open_trade(
                sig.symbol,
                risk_amount=opened.get("actual_risk_amount", risk_amount) if isinstance(opened, dict) else risk_amount,
                is_vip=False,
            )
        except Exception as exc:
            logger.warning("[SIGNAL_EXEC] commit_open_trade error %s: %s", sig.symbol, exc)

        outcome.ok = True
        outcome.reason = "opened"
        outcome.raw = opened if isinstance(opened, dict) else {"result": opened}
        if isinstance(opened, dict):
            outcome.entry_price = float(opened.get("entry_price", entry_price) or entry_price)
            outcome.notional = float(opened.get("notional", notional) or notional)
        boundary = float(getattr(sig, "active_entry", None) or sig.entry_mid or 0.0)
        stop_distance = abs(outcome.entry_price - sl_price)
        outcome.entry_drift_r = abs(outcome.entry_price - boundary) / abs(boundary - sl_price) if boundary and boundary != sl_price else 0.0
        outcome.rr_tp1 = abs(tp1 - outcome.entry_price) / stop_distance if tp1 and stop_distance else 0.0
        outcome.rr_full = abs(tp_full - outcome.entry_price) / stop_distance if tp_full and stop_distance else 0.0
        logger.info("[SIGNAL_EXEC] OPENED %s %s via signal %s fill=%.8g drift=%.3fR rr1=%.3f rrfull=%.3f", sig.symbol, sig.side.value, sig.signal_id, outcome.entry_price, outcome.entry_drift_r, outcome.rr_tp1, outcome.rr_full)
        return outcome
