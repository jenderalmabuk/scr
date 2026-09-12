"""Mechanical entry routing and pending-signal thesis checks."""
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable


class EntryAction(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    REJECT = "REJECT"


@dataclass(frozen=True)
class PricePath:
    high: float
    low: float


@dataclass(frozen=True)
class EntryDecision:
    action: EntryAction
    entry: float
    code: str
    passed_targets: list[float] = field(default_factory=list)
    rr_by_target: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class RevalidationResult:
    ok: bool
    code: str


def _passed_targets(sig, price: float) -> list[float]:
    if sig.is_long:
        return [tp for tp in sig.take_profits if price >= tp]
    return [tp for tp in sig.take_profits if price <= tp]


def _rr_ladder(sig, entry: float) -> list[float]:
    risk = abs(entry - float(sig.stop_loss or 0.0))
    return [abs(tp - entry) / risk for tp in sig.take_profits] if risk > 0 else []


def pending_fill_allowed(sig, price: float, max_drift_r: float = 0.10) -> tuple[bool, float]:
    """Allow triggered pending market fill only near its active boundary.
    
    Regime-aware thresholds:
    - TRENDING: 0.25R (fast momentum, need speed)
    - SCALP: 0.15R (quick entry window)
    - RANGING/SWING: 0.10R (default, patient entry OK)
    """
    # Regime-specific threshold override
    regime = getattr(sig, "regime", "").lower()
    if regime == "trending":
        max_drift_r = 0.25
    elif regime == "scalp":
        max_drift_r = 0.15
    # else: use default 0.10
    
    boundary = float(getattr(sig, "active_entry", None) or (sig.entry_low if price < sig.entry_low else sig.entry_high))
    risk = abs(boundary - float(sig.stop_loss or 0.0))
    drift_r = abs(float(price) - boundary) / risk if risk > 0 else float("inf")
    return drift_r <= max_drift_r + 1e-7, drift_r


def route_entry(sig, price: float) -> EntryDecision:
    """Market only inside provider zone; otherwise wait at nearest boundary."""
    price = float(price or 0.0)
    if price <= 0:
        return EntryDecision(EntryAction.REJECT, 0.0, "NO_MARKET_PRICE")
    midpoint = float(sig.entry_mid or 0.0)
    if midpoint > 0 and abs(price - midpoint) / min(price, midpoint) >= 1.0:
        return EntryDecision(EntryAction.REJECT, price, "ENTRY_PRICE_SCALE_MISMATCH")
    sl = float(sig.stop_loss or 0.0)
    if sl and ((sig.is_long and price <= sl) or (not sig.is_long and price >= sl)):
        return EntryDecision(EntryAction.REJECT, price, "SL_ALREADY_PASSED")
    passed = _passed_targets(sig, price)
    if sig.take_profits and len(passed) == len(sig.take_profits):
        return EntryDecision(EntryAction.REJECT, price, "ALL_TARGETS_PASSED", passed)
    low, high = float(sig.entry_low), float(sig.entry_high)
    if low <= price <= high:
        entry, action, code = price, EntryAction.MARKET, "PRICE_INSIDE_ENTRY_ZONE"
    else:
        boundary = low if price < low else high
        entry, action, code = boundary, EntryAction.LIMIT, "PRICE_OUTSIDE_ENTRY_ZONE"
    return EntryDecision(action, entry, code, passed, _rr_ladder(sig, entry))


def _effective_target(sig) -> float | None:
    for tp, rr in zip(sig.take_profits, _rr_ladder(sig, sig.entry_mid)):
        if rr >= 1.0:
            return tp
    return None


def revalidate_pending(
    sig,
    path: PricePath,
    current_price: float,
    conflicts: Iterable[str] = (),
) -> RevalidationResult:
    """Cancel only irreversible path invalidation or 3 independent conflicts."""
    sl = float(sig.stop_loss or 0.0)
    if sl and ((sig.is_long and path.low <= sl) or (not sig.is_long and path.high >= sl)):
        return RevalidationResult(False, "STOP_INVALIDATED_BEFORE_ENTRY")
    effective = _effective_target(sig)
    if effective is not None and (
        (sig.is_long and path.high >= effective) or
        (not sig.is_long and path.low <= effective)
    ):
        return RevalidationResult(False, "THESIS_TARGET_REACHED_BEFORE_ENTRY")
    if route_entry(sig, current_price).code == "ALL_TARGETS_PASSED":
        return RevalidationResult(False, "ALL_TARGETS_PASSED")
    independent = set(conflicts) & {"structure", "htf_regime", "flow", "volatility"}
    if len(independent) >= 3:
        return RevalidationResult(False, "MARKET_THESIS_INVALIDATED")
    return RevalidationResult(True, "THESIS_VALID")
