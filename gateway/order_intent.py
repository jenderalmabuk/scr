"""OrderIntent — the single contract between every signal brain and the one execution hand.

Every engine (M30/H1 clean_core, Signal Copy, and optionally Revo) produces an
OrderIntent instead of talking to the exchange directly. The Execution Gateway
is the only component that holds exchange credentials and portfolio state.

Design rules:
- All prices are absolute (not percentages).
- `tps` is an ordered ladder: tps[0] = TP1 (first partial), tps[-1] = final TP.
- Sizing is OPTIONAL: if `notional` is omitted, the gateway sizes the trade
  itself with RiskManager.compute_position_size using `risk_pct`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional
import time
import uuid

VALID_SIDES = ("LONG", "SHORT")
VALID_SOURCES = ("M30H1_ENGINE", "SIGNAL_COPY", "REVO_ADAPTIVE", "MANUAL", "TEST")


@dataclass
class OrderIntent:
    # ── identity ─────────────────────────────────────────────
    source: str                       # which brain produced this (VALID_SOURCES)
    symbol: str                       # e.g. "BTCUSDT"
    side: str                         # "LONG" | "SHORT"

    # ── trade plan ───────────────────────────────────────────
    entry_price: float                # reference entry (gateway may use mark)
    sl_price: float                   # hard stop — REQUIRED, never 0
    tps: List[float] = field(default_factory=list)  # TP ladder, ordered

    # ── sizing (choose ONE) ──────────────────────────────────
    notional: Optional[float] = None  # explicit size in USD, or…
    risk_pct: Optional[float] = None  # …fraction of equity to risk (e.g. 0.01)

    # ── metadata (optional) ──────────────────────────────────
    regime: str = "TRENDING"
    confidence: float = 0.5           # 0..1
    tier: str = "Standard"            # Premium | Standard | Probe
    is_vip: bool = False
    leverage: Optional[int] = None
    tag: str = ""                     # free-form: setup name, signal id, …
    adv_snapshot: Dict[str, Any] = field(default_factory=dict)

    # ── auto-filled ──────────────────────────────────────────
    intent_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)

    # ─────────────────────────────────────────────────────────
    def validate(self) -> Optional[str]:
        """Return an error string, or None if the intent is well-formed."""
        if self.source not in VALID_SOURCES:
            return f"unknown source '{self.source}' (expected one of {VALID_SOURCES})"
        if not self.symbol:
            return "symbol is required"
        side = str(self.side).upper()
        if side not in VALID_SIDES:
            return f"side must be LONG or SHORT, got '{self.side}'"
        if self.entry_price <= 0:
            return "entry_price must be > 0"
        if self.sl_price <= 0:
            return "sl_price must be > 0 (a trade without a stop is not allowed)"
        # SL must be on the correct side of entry
        if side == "LONG" and self.sl_price >= self.entry_price:
            return "LONG: sl_price must be below entry_price"
        if side == "SHORT" and self.sl_price <= self.entry_price:
            return "SHORT: sl_price must be above entry_price"
        # TP ladder sanity
        for tp in self.tps:
            if tp <= 0:
                return "all take profits must be > 0"
            if side == "LONG" and tp <= self.entry_price:
                return "LONG: every TP must be above entry_price"
            if side == "SHORT" and tp >= self.entry_price:
                return "SHORT: every TP must be below entry_price"
        if self.notional is None and self.risk_pct is None:
            return "provide either notional (USD) or risk_pct (fraction of equity)"
        if self.notional is not None and self.notional <= 0:
            return "notional must be > 0"
        if self.risk_pct is not None and not (0 < self.risk_pct <= 0.05):
            return "risk_pct must be in (0, 0.05] — max 5% of equity per trade"
        return None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["side"] = str(self.side).upper()
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OrderIntent":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})
