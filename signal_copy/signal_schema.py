"""
Canonical schema for an externally-sourced trade-call signal.

A trade-call signal is what crypto groups post, e.g.:

    🚨 CRYPTO WORLD UPDATES SIGNAL 🚨
    🔹 Pair: ZEC/USDT
    🔹 Position: LONG
    🔹 Leverage: 20X
    📍 Entry Zone: 358 - 350
    🎯 Targets: TP1 365, TP2 415
    ⛔️ Stop Loss: 339

This is distinct from "whale activity" events handled by whales/.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import List, Optional, Dict, Any


class SignalSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class SignalSource(str, Enum):
    TELEGRAM = "TELEGRAM"
    DISCORD = "DISCORD"
    MANUAL = "MANUAL"


@dataclass
class ParsedSignal:
    """A normalized trade-call signal ready for validation."""

    symbol: str                       # normalized, e.g. "ZECUSDT"
    side: SignalSide

    # Entry zone. entry_low <= entry_high. A single price sets both equal.
    entry_low: float
    entry_high: float

    stop_loss: Optional[float] = None
    take_profits: List[float] = field(default_factory=list)
    leverage: Optional[float] = None
    active_entry: Optional[float] = None

    # "market" or "limit" (e.g. "Entry limit 0.0275")
    entry_type: str = "market"
    # timeframe stated in the signal, normalized e.g. "1h", "30m", "15m" (or None)
    timeframe: Optional[str] = None

    # Provenance
    source: SignalSource = SignalSource.TELEGRAM
    source_name: str = ""             # channel/group/server name
    source_chat_id: Optional[int] = None
    raw_text: str = ""

    # How TP/SL were obtained: "signal" (from the message) or "improvised"
    tp_source: str = "signal"
    sl_source: str = "signal"
    level_anomalies: List[str] = field(default_factory=list)

    # Identity / lifecycle
    signal_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    received_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.symbol = (self.symbol or "").upper().strip()
        if isinstance(self.side, str):
            self.side = SignalSide(self.side.upper().strip())
        if isinstance(self.source, str):
            self.source = SignalSource(self.source.upper().strip())
        # Guarantee ordering of the entry zone.
        if self.entry_low is not None and self.entry_high is not None:
            if self.entry_low > self.entry_high:
                self.entry_low, self.entry_high = self.entry_high, self.entry_low
        # Sort TPs in the direction of the trade so TP1 is nearest entry.
        if self.take_profits:
            reverse = self.side == SignalSide.SHORT
            self.take_profits = sorted(
                [float(t) for t in self.take_profits if t is not None],
                reverse=reverse,
            )

    @property
    def entry_mid(self) -> float:
        return (self.entry_low + self.entry_high) / 2.0

    @property
    def rr_entry(self) -> float:
        return float(self.active_entry or self.entry_mid)

    @property
    def is_long(self) -> bool:
        return self.side == SignalSide.LONG

    def rr_ratio(self, tp_index: int = 0) -> Optional[float]:
        """Reward:risk to a given take-profit using entry_mid and stop_loss."""
        if self.stop_loss is None or not self.take_profits:
            return None
        if tp_index >= len(self.take_profits):
            tp_index = 0
        entry = self.rr_entry
        tp = self.take_profits[tp_index]
        risk = abs(entry - self.stop_loss)
        if risk <= 0:
            return None
        reward = abs(tp - entry)
        return reward / risk

    def rr_best(self) -> Optional[float]:
        """Best reward:risk across all take-profits (final target with trailing)."""
        if self.stop_loss is None or not self.take_profits:
            return None
        entry = self.rr_entry
        risk = abs(entry - self.stop_loss)
        if risk <= 0:
            return None
        return max(abs(tp - entry) / risk for tp in self.take_profits)

    def sl_distance_pct(self) -> Optional[float]:
        if self.stop_loss is None:
            return None
        entry = self.rr_entry
        if entry <= 0:
            return None
        return abs(entry - self.stop_loss) / entry * 100.0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["side"] = self.side.value
        d["source"] = self.source.value
        return d

    def summary(self) -> str:
        tps = ", ".join(f"{t:g}" for t in self.take_profits) if self.take_profits else "-"
        sl = f"{self.stop_loss:g}" if self.stop_loss is not None else "-"
        lev = f"{self.leverage:g}x" if self.leverage else "-"
        return (
            f"{self.symbol} {self.side.value} | "
            f"entry {self.entry_low:g}-{self.entry_high:g} | "
            f"TP [{tps}] | SL {sl} | lev {lev} | src {self.source_name or self.source.value}"
        )
