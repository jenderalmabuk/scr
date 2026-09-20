# risk/risk_engine.py - repo-compatible hardened risk manager + Sniper Risk Framework v1.1
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, Optional

# Use nexus standalone config
from signal_copy.execution_config import (
    COOLDOWN_GLOBAL_MIN,
    COOLDOWN_SYMBOL_MIN,
    MAX_CORRELATED_CLUSTER_POS,
    MAX_DAILY_LOSS_PCT,
    MAX_DRAWDOWN_PCT,
    MAX_NOTIONAL_PCT_OF_BALANCE,
    MAX_OPEN_POS_GLOBAL,
    MAX_SAME_DIRECTION_POS,
    MAX_TOTAL_EXPOSURE_PCT,
    RISK_PER_TRADE_PCT,
)

logger = logging.getLogger(__name__)

@dataclass
class ReservedRisk:
    symbol: str
    amount: float
    created_at: datetime

class RiskManager:
    def __init__(self, starting_balance: float):
        self.starting_balance = float(starting_balance)
        self.current_equity = float(starting_balance)
        self.peak_balance = float(starting_balance)
        self.daily_start_balance = float(starting_balance)
        self.daily_pnl = 0.0
        self.last_daily_reset = datetime.now(UTC).date()

        self.risk_per_trade_pct = float(RISK_PER_TRADE_PCT)
        self.max_notional_pct = float(MAX_NOTIONAL_PCT_OF_BALANCE)
        self.max_drawdown_pct = float(MAX_DRAWDOWN_PCT)
        self.max_daily_loss_pct = float(MAX_DAILY_LOSS_PCT)
        self.max_total_exposure_pct = float(MAX_TOTAL_EXPOSURE_PCT)
        paper_mode = os.getenv("GATEWAY_PAPER_MAINNET", "false").lower() in ("1", "true", "yes")
        self.ignore_max_drawdown_for_sample = paper_mode and os.getenv(
            "PAPER_SAMPLE_IGNORE_MAX_DRAWDOWN", "false"
        ).lower() in ("1", "true", "yes")

        self.last_trade_time = None
        self.last_trade_time_per_symbol = {}
        self.consecutive_losses = 0
        self.consecutive_hard_losses = 0
        self.consecutive_soft_losses = 0

        self.wins = 0
        self.losses = 0
        self.total_win_pnl = 0.0
        self.total_loss_pnl = 0.0
        self.avg_win_pct = 0.0
        self.avg_loss_pct = 0.0

        self.cooldown_global_min = float(COOLDOWN_GLOBAL_MIN)
        self.cooldown_symbol_min = float(COOLDOWN_SYMBOL_MIN)

        self.trading_enabled = True
        self.trading_disable_reason = ""
        self.trader = None
        self.daily_loss_hit = False
        self.temp_pause_until = None
        self.temp_pause_reason = ""

        self.equity_curve = []
        self._record_equity()

        self.vip_risk_budget_pct = 0.15
        self.vip_risk_used = 0.0

        default_global_risk = 0.20 if paper_mode else 0.05
        self.parallel_open_risk_budget_pct = float(
            os.getenv("MAX_GLOBAL_OPEN_RISK_PCT", str(default_global_risk))
        )
        self.open_risk_reservation_ttl_seconds = 120
        self._risk_lock = asyncio.Lock()
        self._reserved_open_risk: Dict[str, ReservedRisk] = {}

        self.daily_peak_equity = float(starting_balance)
        self.daily_peak_profit = 0.0
        self.giveback_lock_until = None
        self.hard_daily_stop_hit = False

    def _record_equity(self) -> None:
        self.equity_curve.append({"time": datetime.now(UTC).isoformat(), "balance": float(self.current_equity)})
        if len(self.equity_curve) > 1000:
            self.equity_curve = self.equity_curve[-500:]

    def _reset_daily_if_needed(self) -> None:
        today = datetime.now(UTC).date()
        if today != self.last_daily_reset:
            if self.trading_disable_reason in ("DAILY_LOSS", "HARD_DAILY_STOP"):
                self.trading_enabled = True
                self.trading_disable_reason = ""
            self.daily_start_balance = self.current_equity
            self.daily_pnl = 0.0
            self.daily_loss_hit = False
            self.temp_pause_until = None
            self.temp_pause_reason = ""
            self.consecutive_hard_losses = 0
            self.consecutive_soft_losses = 0
            self.last_daily_reset = today
            self.vip_risk_used = 0.0
            self._reserved_open_risk.clear()
            self.daily_peak_equity = self.current_equity
            self.daily_peak_profit = 0.0
            self.giveback_lock_until = None
            self.hard_daily_stop_hit = False
            logger.info("Daily PnL counter reset")

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            if value in (None, "", "None"):
                return default
            return float(value)
        except Exception:
            return default

    @staticmethod
    def _unwrap_position(pos: Any) -> Any:
        if isinstance(pos, tuple) and len(pos) == 2:
            return pos[1]
        return pos

    @staticmethod
    def _position_attr(pos: Any, name: str, default: Any = None) -> Any:
        pos = RiskManager._unwrap_position(pos)
        if pos is None:
            return default
        if hasattr(pos, name):
            return getattr(pos, name)
        if isinstance(pos, Mapping):
            return pos.get(name, default)
        return default

    def _iter_trader_positions(self) -> Iterable[Any]:
        if not self.trader:
            return ()
        if hasattr(self.trader, "iter_positions") and callable(self.trader.iter_positions):
            return self.trader.iter_positions()
        positions = getattr(self.trader, "positions", None)
        if isinstance(positions, Mapping):
            return positions.values()
        return ()

    def _position_count(self) -> int:
        if not self.trader:
            return 0
        if hasattr(self.trader, "get_open_position_count") and callable(self.trader.get_open_position_count):
            try:
                return int(self.trader.get_open_position_count())
            except Exception:
                pass
        positions = getattr(self.trader, "positions", None)
        if isinstance(positions, Mapping):
            return len(positions)
        return sum(1 for _ in self._iter_trader_positions())

    def _position_symbols(self) -> set[str]:
        if not self.trader:
            return set()
        positions = getattr(self.trader, "positions", None)
        if isinstance(positions, Mapping):
            return {str(sym) for sym in positions.keys()}
        out = set()
        for pos in self._iter_trader_positions():
            symbol = self._position_attr(pos, "symbol", "")
            if symbol:
                out.add(str(symbol))
        return out

    def _clear_temp_pause_if_needed(self) -> None:
        if self.temp_pause_until and datetime.now(UTC) >= self.temp_pause_until:
            self.temp_pause_until = None
            self.temp_pause_reason = ""
            self.consecutive_hard_losses = 0
            self.consecutive_soft_losses = 0

    def _set_temp_pause(self, minutes: float, reason: str) -> None:
        self.temp_pause_until = datetime.now(UTC) + timedelta(minutes=max(1.0, float(minutes)))
        self.temp_pause_reason = str(reason or "TEMP_PAUSE")

    def _clear_stale_open_risk_reservations(self) -> None:
        if not self._reserved_open_risk:
            return
        cutoff = datetime.now(UTC) - timedelta(seconds=max(30, int(self.open_risk_reservation_ttl_seconds)))
        stale = [s for s, r in list(self._reserved_open_risk.items()) if r.created_at < cutoff]
        for s in stale:
            self._reserved_open_risk.pop(s, None)

    def get_reserved_risk_total(self) -> float:
        self._clear_stale_open_risk_reservations()
        return sum(max(0.0, r.amount) for r in self._reserved_open_risk.values())

    def get_parallel_open_risk_budget(self) -> float:
        return max(0.0, float(self.current_equity) * float(self.parallel_open_risk_budget_pct))

    def get_active_open_risk_total(self) -> float:
        """Remaining economic loss to active stops, including fees and exit slip."""
        total = 0.0
        for pos in self._iter_trader_positions():
            entry = self._safe_float(self._position_attr(pos, "entry_price", 0.0))
            stop = self._safe_float(self._position_attr(pos, "sl_price", self._position_attr(pos, "sl", 0.0)))
            qty = self._safe_float(
                self._position_attr(pos, "qty_remaining", self._position_attr(pos, "qty", 0.0)),
                0.0,
            )
            side = str(self._position_attr(pos, "side", "") or "").upper()
            valid_stop = (side == "LONG" and 0 < stop < entry) or (side == "SHORT" and stop > entry)
            if entry > 0 and qty > 0 and valid_stop:
                fee = max(0.0, float(os.getenv("PAPER_TAKER_FEE_PCT", "0.0005")))
                slip = max(0.0, float(os.getenv("PAPER_SLIPPAGE_PCT", "0.0005")))
                exit_fill = stop * (1.0 - slip if side == "LONG" else 1.0 + slip)
                total += abs(entry - exit_fill) * qty + entry * qty * fee + exit_fill * qty * fee
        return total

    def get_total_open_risk(self) -> float:
        return self.get_active_open_risk_total() + self.get_reserved_risk_total()

    @staticmethod
    def get_max_running_positions() -> int:
        paper = os.getenv("GATEWAY_PAPER_MAINNET", "false").lower() in ("1", "true", "yes")
        key = "PAPER_MAX_RUNNING_POSITIONS" if paper else "REAL_MAX_RUNNING_POSITIONS"
        default = "30" if paper else "20"
        return max(1, int(os.getenv(key, os.getenv("FQ_MAX_RUNNING_POSITIONS", default))))

    async def reserve_open_risk_details(self, symbol: str, risk_amount: float) -> Dict[str, Any]:
        symbol_u = str(symbol or "").upper()
        risk_amount = max(0.0, float(risk_amount or 0.0))
        async with self._risk_lock:
            self._clear_stale_open_risk_reservations()
            active = self.get_active_open_risk_total()
            reserved_risk = self.get_reserved_risk_total()
            positions = self._position_count()
            reservations = len(self._reserved_open_risk)
            budget = self.get_parallel_open_risk_budget()
            max_running = self.get_max_running_positions()
            detail = {"symbol": symbol_u, "requested_risk": risk_amount,
                      "active_open_risk": active, "reserved_open_risk": reserved_risk,
                      "total_open_risk": active + reserved_risk, "risk_budget": budget,
                      "risk_budget_remaining": max(0.0, budget - active - reserved_risk),
                      "open_positions": positions, "inflight_reservations": reservations,
                      "max_running_positions": max_running}
            if not symbol_u:
                return {**detail, "reserved": False, "code": "INVALID_SYMBOL"}
            if risk_amount <= 0:
                return {**detail, "reserved": False, "code": "INVALID_RISK_AMOUNT"}
            if symbol_u in self._reserved_open_risk:
                return {**detail, "reserved": False, "code": "SYMBOL_ALREADY_RESERVED"}
            if symbol_u in self._position_symbols():
                return {**detail, "reserved": False, "code": "POSITION_ALREADY_OPEN"}
            # Reserve an in-flight running slot atomically with risk. Dormant
            # pending setups never call this and remain unlimited.
            if positions + reservations >= max_running:
                return {**detail, "reserved": False, "code": "RUNNING_SLOT_CAP"}
            if active + reserved_risk + risk_amount > budget:
                return {**detail, "reserved": False, "code": "OPEN_RISK_BUDGET"}
            self._reserved_open_risk[symbol_u] = ReservedRisk(symbol=symbol_u, amount=risk_amount, created_at=datetime.now(UTC))
            return {**detail, "reserved": True, "code": "RESERVED"}

    async def reserve_open_risk(self, symbol: str, risk_amount: float) -> bool:
        return bool((await self.reserve_open_risk_details(symbol, risk_amount))["reserved"])

    async def release_open_risk(self, symbol: str) -> None:
        symbol_u = str(symbol or "").upper()
        if not symbol_u:
            return
        async with self._risk_lock:
            self._reserved_open_risk.pop(symbol_u, None)

    async def commit_open_trade(self, symbol: str, risk_amount: float = 0.0, is_vip: bool = False) -> None:
        await self.release_open_risk(symbol)
        self.register_open_trade(symbol, risk_amount=risk_amount, is_vip=is_vip)

    def get_daily_pnl_pct(self) -> float:
        self._reset_daily_if_needed()
        if self.daily_start_balance <= 0:
            return 0.0
        return (self.current_equity - self.daily_start_balance) / self.daily_start_balance * 100.0

    def is_daily_loss_limit_hit(self) -> bool:
        daily_pnl_pct = self.get_daily_pnl_pct()
        if daily_pnl_pct <= -self.max_daily_loss_pct:
            if not self.daily_loss_hit:
                self.daily_loss_hit = True
                self.trading_enabled = False
                self.trading_disable_reason = "DAILY_LOSS"
            return True
        return False

    def get_total_exposure_pct(self) -> float:
        total_notional = sum(self._safe_float(self._position_attr(pos, "notional", 0.0), 0.0) for pos in self._iter_trader_positions())
        if self.current_equity <= 0:
            return 0.0
        return total_notional / self.current_equity * 100.0

    def is_exposure_limit_exceeded(self) -> bool:
        return self.get_total_exposure_pct() > self.max_total_exposure_pct

    def get_major_cluster_count(self) -> int:
        major = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT"}
        return sum(1 for sym in self._position_symbols() if sym in major)

    def is_cluster_limit_exceeded(self) -> bool:
        return self.get_major_cluster_count() >= MAX_CORRELATED_CLUSTER_POS

    def _same_direction_position_count(self, side: Optional[str] = None) -> int:
        if not side:
            return 0
        side_u = str(side).upper()
        count = 0
        for pos in self._iter_trader_positions():
            if str(self._position_attr(pos, "side", "") or "").upper() == side_u:
                count += 1
        return count

    def check_risk_limits(self, symbol: Optional[str] = None, is_vip: bool = False, side: Optional[str] = None, skip_entry_cooldown: bool = False, skip_cluster_limit: bool = False) -> Dict[str, Any]:
        self._clear_temp_pause_if_needed()
        self._clear_stale_open_risk_reservations()

        if self.is_daily_loss_limit_hit():
            return {"can_trade": False, "reason": "Daily loss limit reached"}
        if self.temp_pause_until and datetime.now(UTC) < self.temp_pause_until:
            remaining = (self.temp_pause_until - datetime.now(UTC)).total_seconds() / 60.0
            return {"can_trade": False, "reason": f"Risk pause {remaining:.1f} min"}
        if not self.trading_enabled:
            owner = self.trading_disable_reason or "UNKNOWN"
            return {"can_trade": False, "reason": f"Trading disabled: {owner}"}
        if self.giveback_lock_until and datetime.now(UTC) < self.giveback_lock_until:
            remaining = (self.giveback_lock_until - datetime.now(UTC)).total_seconds() / 60.0
            return {"can_trade": False, "reason": f"Giveback guard active ({remaining:.1f} min)"}
        if self.hard_daily_stop_hit:
            return {"can_trade": False, "reason": "Hard daily stop hit"}
        if self.is_exposure_limit_exceeded():
            return {"can_trade": False, "reason": "Total exposure limit exceeded"}
        if not skip_cluster_limit and self.is_cluster_limit_exceeded():
            return {"can_trade": False, "reason": "Major cluster limit reached"}
        if self.get_reserved_risk_total() > self.get_parallel_open_risk_budget():
            return {"can_trade": False, "reason": "Pending open-risk budget exhausted"}

        now = datetime.now(UTC)
        if not skip_entry_cooldown and self.last_trade_time:
            delta = (now - self.last_trade_time).total_seconds() / 60.0
            if delta < self.cooldown_global_min:
                return {"can_trade": False, "reason": f"Global cooldown {delta:.1f} min"}
        if not skip_entry_cooldown and symbol and symbol in self.last_trade_time_per_symbol:
            delta = (now - self.last_trade_time_per_symbol[symbol]).total_seconds() / 60.0
            if delta < self.cooldown_symbol_min:
                return {"can_trade": False, "reason": f"{symbol} cooldown {delta:.1f} min"}
        if MAX_SAME_DIRECTION_POS > 0 and side and self._same_direction_position_count(side) >= MAX_SAME_DIRECTION_POS:
            return {"can_trade": False, "reason": f"Same direction position limit reached:{side}"}
        if not is_vip and self._position_count() >= self.get_max_running_positions():
            return {"can_trade": False, "reason": "Max open positions reached"}

        drawdown = self.get_current_drawdown()
        if drawdown >= self.max_drawdown_pct and not self.ignore_max_drawdown_for_sample:
            self.trading_enabled = False
            self.trading_disable_reason = "MAX_DRAWDOWN"
            return {"can_trade": False, "reason": "Max drawdown reached"}
        return {"can_trade": True, "reason": "OK"}

    def can_trade(self, is_vip: bool = False, symbol: Optional[str] = None, side: Optional[str] = None) -> bool:
        return self.check_risk_limits(symbol=symbol, is_vip=is_vip, side=side).get("can_trade", False)

    def can_open_new_position(self, symbol: str, is_vip: bool = False, side: Optional[str] = None) -> bool:
        return self.can_trade(is_vip=is_vip, symbol=symbol, side=side)

    def can_open_vip_position(self, planned_risk_amount: float) -> bool:
        return (self.vip_risk_used + planned_risk_amount) <= (self.current_equity * self.vip_risk_budget_pct)

    def register_vip_trade(self, risk_amount: float) -> None:
        self.vip_risk_used += float(risk_amount)

    def release_vip_risk(self, risk_amount: float) -> None:
        self.vip_risk_used = max(0.0, self.vip_risk_used - float(risk_amount))

    def avoid_liquidity_clusters(self, entry_price: float, sl_price: float, side: str) -> float:
        leverage_clusters = [10, 25, 50, 100]
        for lev in leverage_clusters:
            if side.upper() == "LONG":
                liq_point = entry_price * (1.0 - (1.0 / lev))
            else:
                liq_point = entry_price * (1.0 + (1.0 / lev))
            distance = abs(sl_price - liq_point) / max(liq_point, 1e-9)
            if distance < 0.003:
                sl_price = liq_point * (0.995 if side.upper() == "LONG" else 1.005)
                break
        return sl_price

    def get_dynamic_risk_pct(self) -> float:
        total_trades = self.wins + self.losses
        if total_trades < 20 or self.losses == 0 or self.wins == 0:
            return self.risk_per_trade_pct
        win_rate = self.wins / total_trades
        loss_rate = 1.0 - win_rate
        if win_rate < 0.40 or self.avg_loss_pct == 0:
            return 0.005
        b = self.avg_win_pct / self.avg_loss_pct
        kelly_pct = (win_rate * b - loss_rate) / max(b, 1e-9)
        if kelly_pct <= 0:
            return 0.005
        return max(0.005, min(0.025, kelly_pct * 0.25))

    @staticmethod
    def _normalize_risk_pct(value: float) -> float:
        v = float(value)
        if v <= 0:
            return 0.005
        return v / 100.0 if v > 1.0 else v

    def _check_daily_guards(self) -> None:
        now = datetime.now(UTC)
        current_profit = self.current_equity - self.daily_start_balance
        if self.current_equity > self.daily_peak_equity:
            self.daily_peak_equity = self.current_equity
        if current_profit > self.daily_peak_profit:
            self.daily_peak_profit = current_profit
        if self.daily_peak_profit > 0:
            giveback_ratio = (self.daily_peak_profit - current_profit) / max(self.daily_peak_profit, 1e-9)
            if giveback_ratio >= 0.40:
                self.giveback_lock_until = now + timedelta(minutes=30)
                self.daily_peak_profit = 0.0
        daily_loss_pct = (self.current_equity - self.daily_start_balance) / max(self.daily_start_balance, 1e-9)
        if daily_loss_pct <= -0.018:
            self.hard_daily_stop_hit = True
            self.trading_enabled = False
            self.trading_disable_reason = "HARD_DAILY_STOP"

    def compute_position_size(
        self,
        probability: float,
        regime_label: str,
        atr_pct: float,
        entry_price: float,
        side: str,
        adaptive_risk_pct: Optional[float] = None,
        is_breakout: bool = False,
        symbol: Optional[str] = None,
        is_vip: bool = False,
        tier: str = "Standard",
        thesis_strength: float = 0.5,
    ) -> Dict[str, Any]:
        del thesis_strength
        prob = max(0.0, min(0.8, float(probability)))
        atr_pct = max(0.0005, float(atr_pct))
        entry_price = max(float(entry_price), 1e-9)

        multiplier = 3.5 if is_breakout else 2.5
        base_sl_pct = atr_pct * multiplier
        if regime_label == "HIGH_VOL":
            base_sl_pct *= 1.5
        elif regime_label == "TRENDING":
            base_sl_pct *= 1.2
        base_sl_pct = max(0.8, min(base_sl_pct, 8.0))

        raw_sl_price = entry_price * (1.0 - base_sl_pct / 100.0) if str(side).upper() == "LONG" else entry_price * (1.0 + base_sl_pct / 100.0)
        smart_sl_price = self.avoid_liquidity_clusters(entry_price, raw_sl_price, side)
        final_sl_pct = abs(entry_price - smart_sl_price) / entry_price * 100.0
        sl_fraction = max(final_sl_pct / 100.0, 1e-6)

        effective_max_notional_pct = 0.30 if self.current_equity < 500 else self.max_notional_pct
        max_notional_allowed = max(10.0, self.current_equity * effective_max_notional_pct)

        tier_caps = {"Premium": 0.010, "Standard": 0.0075, "Probe": 0.0045}
        max_risk_amount = self.current_equity * tier_caps.get(tier, 0.0075)

        base_risk_pct = self._normalize_risk_pct(adaptive_risk_pct if adaptive_risk_pct is not None else self.get_dynamic_risk_pct())
        if is_vip:
            base_risk_pct *= 1.2
        if prob >= 0.75:
            base_risk_pct *= 1.05
        elif prob <= 0.45:
            base_risk_pct *= 0.90

        effective_risk_pct = max(0.005, min(0.02, base_risk_pct))
        planned_risk_amount = min(self.current_equity * effective_risk_pct, max_risk_amount)

        uncapped_notional = planned_risk_amount / sl_fraction
        final_notional = max(10.0, min(uncapped_notional, max_notional_allowed))
        actual_risk_amount = final_notional * sl_fraction
        actual_risk_pct = actual_risk_amount / max(self.current_equity, 1e-9)

        return {
            "notional": float(final_notional),
            "uncapped_notional": float(uncapped_notional),
            "max_notional_allowed": float(max_notional_allowed),
            "sl_distance_pct": float(final_sl_pct),
            "sl_price": float(smart_sl_price),
            "max_loss": float(actual_risk_amount),
            "risk_pct": float(effective_risk_pct),
            "actual_risk_pct": float(actual_risk_pct),
            "planned_risk_amount": float(planned_risk_amount),
            "actual_risk_amount": float(actual_risk_amount),
            "risk_amount": float(actual_risk_amount),
            "is_vip": bool(is_vip),
            "tier": str(tier),
            "rejected": False,
        }

    def register_open_trade(self, symbol: str, risk_amount: float = 0.0, is_vip: bool = False) -> None:
        now = datetime.now(UTC)
        self.last_trade_time = now
        self.last_trade_time_per_symbol[str(symbol)] = now
        if is_vip:
            self.register_vip_trade(risk_amount)

    def update_after_trade(self, trade_or_pnl: Any) -> None:
        self._reset_daily_if_needed()
        self._clear_temp_pause_if_needed()

        if isinstance(trade_or_pnl, Mapping):
            trade = dict(trade_or_pnl)
            pnl_pct = self._safe_float(trade.get("pnl_pct", 0.0), 0.0)
            reason = str(trade.get("reason", "UNKNOWN") or "UNKNOWN").upper()
            is_vip = bool(trade.get("is_vip", False))
            risk_amount = self._safe_float(trade.get("risk_amount", trade.get("actual_risk_amount", 0.0)))
        else:
            pnl_pct = self._safe_float(trade_or_pnl, 0.0)
            reason = "UNKNOWN"
            is_vip = False
            risk_amount = 0.0

        self.daily_pnl += pnl_pct
        if is_vip and risk_amount > 0:
            self.release_vip_risk(risk_amount)

        meaningful_win = pnl_pct > 0.10
        scratch_like_loss = reason in {"TIME_EXIT", "SCRATCH_EXIT", "SCRATCH_EXIT_PATH_FAILURE"} and abs(pnl_pct) <= 0.35
        hard_loss = reason in {"SL", "STOPLOSS", "STOP_LOSS", "FORCED_SL", "THESIS_INVALIDATED"} or pnl_pct <= -1.0
        soft_loss = (pnl_pct < 0.0) and not scratch_like_loss and not hard_loss

        if meaningful_win:
            self.wins += 1
            self.total_win_pnl += pnl_pct
            self.avg_win_pct = self.total_win_pnl / max(self.wins, 1)
            self.consecutive_losses = 0
            self.consecutive_hard_losses = 0
            self.consecutive_soft_losses = 0
            self.temp_pause_until = None
            self.temp_pause_reason = ""
        elif pnl_pct < 0.0:
            self.losses += 1
            self.total_loss_pnl += abs(pnl_pct)
            self.avg_loss_pct = self.total_loss_pnl / max(self.losses, 1)
            self.consecutive_losses += 1
            if hard_loss:
                self.consecutive_hard_losses += 1
                self.consecutive_soft_losses = 0
            elif soft_loss:
                self.consecutive_soft_losses += 1

        if hard_loss and self.consecutive_hard_losses >= 3:
            self._set_temp_pause(20, "3_HARD_LOSSES")
        elif self.consecutive_soft_losses >= 6:
            self._set_temp_pause(10, "SOFT_LOSS_CLUSTER")

        self._record_equity()
        self._check_daily_guards()

    def get_current_equity(self) -> float:
        return float(self.current_equity)

    def get_current_drawdown(self) -> float:
        if self.peak_balance <= 0:
            return 0.0
        return max(0.0, (self.peak_balance - self.current_equity) / self.peak_balance * 100.0)

    async def update_unrealized_drawdown(self, trader, price_map: Optional[Dict[str, float]] = None) -> float:
        floating_pnl = 0.0
        if trader is not None and price_map:
            positions = getattr(trader, "positions", {})
            if isinstance(positions, Mapping):
                for symbol, pos in positions.items():
                    price = price_map.get(symbol)
                    if price is None:
                        continue
                    entry_price = self._safe_float(self._position_attr(pos, "entry_price", 0.0), 0.0)
                    notional = self._safe_float(self._position_attr(pos, "notional", 0.0), 0.0)
                    side = str(self._position_attr(pos, "side", "LONG") or "LONG").upper()
                    if entry_price <= 0 or notional <= 0:
                        continue
                    frac = ((float(price) - entry_price) if side == "LONG" else (entry_price - float(price))) / max(entry_price, 1e-9)
                    floating_pnl += frac * notional
        equity_now = self.current_equity + floating_pnl
        if self.peak_balance <= 0:
            return 0.0
        return max(0.0, (self.peak_balance - equity_now) / self.peak_balance * 100.0)

    def sync_balance(self, new_equity: float, *, reset_daily_baseline: bool = False) -> None:
        self.current_equity = float(new_equity)
        if reset_daily_baseline:
            self.daily_start_balance = self.current_equity
            self.daily_pnl = 0.0
            self.daily_loss_hit = False
            self.hard_daily_stop_hit = False
            self.trading_enabled = True
            self.trading_disable_reason = ""
        if self.current_equity > self.peak_balance:
            self.peak_balance = self.current_equity
        self._reset_daily_if_needed()

    def get_stats(self) -> Dict[str, Any]:
        total = self.wins + self.losses
        win_rate = (self.wins / total) if total > 0 else 0.0
        return {
            "equity": self.current_equity,
            "peak_balance": self.peak_balance,
            "drawdown_pct": self.get_current_drawdown(),
            "daily_pnl_pct": self.get_daily_pnl_pct(),
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": win_rate,
            "vip_risk_used": self.vip_risk_used,
            "reserved_open_risk": self.get_reserved_risk_total(),
            "parallel_open_risk_budget": self.get_parallel_open_risk_budget(),
            "daily_peak_profit": self.daily_peak_profit,
            "hard_daily_stop_hit": self.hard_daily_stop_hit,
        }
