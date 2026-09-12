"""Independent paper-only Arm C virtual portfolio."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from execution.paper_mainnet_trader import PaperMainnetTrader


class SmartVirtualPortfolioBook:
    def __init__(self, trader: Any, *, state_path: Any = None, outcomes_path: Any = None,
                 equity: float | None = None, max_positions: int = 30,
                 open_risk_cap: float = 0.10):
        self.trader = trader
        self.state_path = Path(state_path or os.getenv(
            "SMART_VIRTUAL_BOOK_STATE", "journal/smart_virtual_portfolio.json"))
        self.outcomes_path = Path(outcomes_path or os.getenv(
            "SMART_VIRTUAL_BOOK_OUTCOMES", "journal/smart_virtual_outcomes.jsonl"))
        self.max_positions = int(max_positions)
        self.open_risk_cap = float(open_risk_cap)
        self.positions: dict[str, dict[str, Any]] = {}
        self.equity = float(equity if equity is not None else os.getenv("STARTING_BALANCE", "1000"))
        self._load()

    @property
    def open_risk_usd(self) -> float:
        return sum(float(pos["risk_usd"]) for pos in self.positions.values())

    def _load(self) -> None:
        try:
            data = json.loads(self.state_path.read_text())
            self.equity = float(data["equity"])
            self.positions = dict(data.get("positions") or {})
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            pass

    def _persist(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(json.dumps({"equity": self.equity, "positions": self.positions},
                                  separators=(",", ":")))
        os.replace(tmp, self.state_path)

    async def capture(self, intent: Any) -> dict[str, Any]:
        symbol = str(intent.symbol)
        if symbol in self.positions:
            return {"opened": False, "reason": "DUPLICATE_SYMBOL"}
        if len(self.positions) >= self.max_positions:
            return {"opened": False, "reason": "MAX_VIRTUAL_POSITIONS"}
        quote = await self.trader._get_fresh_open_quote(symbol)
        if not isinstance(quote, dict) or quote.get("price_fresh") is not True:
            return {"opened": False, "reason": "FRESH_EXECUTION_QUOTE_UNAVAILABLE"}
        try:
            raw_entry = float(quote["price"])
        except (KeyError, TypeError, ValueError):
            return {"opened": False, "reason": "FRESH_EXECUTION_QUOTE_UNAVAILABLE"}
        if raw_entry <= 0:
            return {"opened": False, "reason": "FRESH_EXECUTION_QUOTE_UNAVAILABLE"}
        entry = PaperMainnetTrader._adverse_fill(raw_entry, str(intent.side).upper(), opening=True)
        bars = await self.trader._get_completed_bars(symbol, tf="15m", limit=21)
        seed = {"symbol": symbol, "side": str(intent.side).upper(), "entry_price": entry,
                "adv_snapshot": dict(intent.adv_snapshot or {})}
        now = datetime.now(timezone.utc).isoformat()
        plan = PaperMainnetTrader._build_smart_exit_router_plan(
            seed, bars, equity=self.equity,
            risk_pct=float(os.getenv("SIGNAL_COPY_RISK_PCT", "0.01")), opened_at=now)
        if not plan.get("candidate_available"):
            return {"opened": False, "reason": plan.get("reason", "ARM_C_INELIGIBLE")}
        risk = float(plan["economic_risk_usd"])
        if self.open_risk_usd + risk > self.equity * self.open_risk_cap:
            return {"opened": False, "reason": "VIRTUAL_OPEN_RISK_CAP"}
        qty = float(plan["qty"])
        entry_fee = PaperMainnetTrader._fee(entry * qty)
        self.equity -= entry_fee
        self.positions[symbol] = {
            "symbol": symbol, "side": seed["side"], "entry_price": entry,
            "qty": qty, "risk_usd": risk, "entry_fee_usd": entry_fee,
            "opened_at": now, "intent_id": intent.intent_id,
            "execution_quote": dict(quote), "raw_entry_price": raw_entry, "plan": plan,
        }
        self._persist()
        return {"opened": True, "symbol": symbol, "risk_usd": risk}

    async def advance(self) -> list[dict[str, Any]]:
        closed = []
        for symbol, position in list(self.positions.items()):
            bars = await self.trader._get_completed_bars(symbol, tf="15m", limit=21)
            plan = position["plan"]
            pending = []
            for row in bars[:-1]:
                stamp = row.get("open_time") or row.get("timestamp") or row.get("time")
                if stamp and str(stamp) > str(plan.get("last_candle_id") or ""):
                    pending.append((str(stamp), row))
            for stamp, row in sorted(pending):
                try:
                    opened = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                    candle = {key: float(row[key]) for key in ("open", "high", "low", "close")}
                except (KeyError, TypeError, ValueError):
                    continue
                candle["closed_at"] = (opened + timedelta(minutes=15)).isoformat()
                plan["last_candle_id"] = stamp
                PaperMainnetTrader._advance_smart_exit_plan(plan, candle)
                if plan.get("status") == "CLOSED":
                    closed.append(self._close(symbol, position))
                    break
        self._persist()
        return closed

    def _close(self, symbol: str, position: dict[str, Any]) -> dict[str, Any]:
        plan = position["plan"]
        trigger = float(plan["exit_price"])
        fill = PaperMainnetTrader._adverse_fill(trigger, position["side"], opening=False)
        qty, entry = float(position["qty"]), float(position["entry_price"])
        gross = ((fill - entry) if position["side"] == "LONG" else (entry - fill)) * qty
        exit_fee = PaperMainnetTrader._fee(fill * qty)
        entry_fee = float(position["entry_fee_usd"])
        net = gross - entry_fee - exit_fee
        self.equity += gross - exit_fee
        row = {"symbol": symbol, "side": position["side"], "intent_id": position["intent_id"],
               "entry_price": entry, "exit_price": fill, "qty": qty,
               "exit_reason": plan["exit_reason"], "gross_pnl_usd": gross,
               "entry_fee_usd": entry_fee, "exit_fee_usd": exit_fee,
               "net_pnl_usd": net, "risk_usd": position["risk_usd"],
               "opened_at": position["opened_at"], "closed_at": plan.get("exited_at"),
               "selected_plan": plan.get("selected_plan")}
        self.outcomes_path.parent.mkdir(parents=True, exist_ok=True)
        with self.outcomes_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")
        del self.positions[symbol]
        return row
