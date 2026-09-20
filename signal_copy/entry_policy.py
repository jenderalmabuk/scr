"""Mechanical entry routing and pending-signal thesis checks."""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from . import validation_config as vc


class EntryAction(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    WAIT_CONFIRMATION = "WAIT_CONFIRMATION"
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
    conflicts: list[str] = field(default_factory=list)


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


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _nearest_pullback_entry(sig, price: float) -> float | None:
    low = float(getattr(sig, "entry_low", 0.0) or 0.0)
    high = float(getattr(sig, "entry_high", 0.0) or 0.0)
    if low <= 0 or high <= 0 or abs(high - low) <= max(high, low) * 1e-7:
        return None
    if low <= price <= high:
        return low if sig.is_long else high
    return low if price < low else high


def _profit_drift_r(sig, price: float) -> float:
    entry = float(getattr(sig, "active_entry", None) or getattr(sig, "entry_mid", 0.0) or 0.0)
    sl = float(getattr(sig, "stop_loss", 0.0) or 0.0)
    if price <= 0 or entry <= 0 or sl <= 0:
        return 0.0
    drift = price - entry if sig.is_long else entry - price
    risk = abs(entry - sl)
    return max(0.0, drift / risk) if risk > 0 else 0.0


def _market_conflicts(sig, price: float, metrics: dict[str, Any], score: float) -> list[str]:
    conflicts: list[str] = []
    committee = metrics.get("adversarial_committee")
    committee_mode = str(getattr(vc, "COMMITTEE_MODE", "shadow") or "shadow").lower()
    if committee_mode != "shadow" and isinstance(committee, dict):
        no_votes = _i(committee.get("no_votes"))
        warn_votes = _i(committee.get("warn_votes"))
        final_vote = str(committee.get("final_vote") or "").upper()
        if final_vote == "NO" and score < vc.ADVERSARIAL_NO_OVERRIDE_MIN_SCORE:
            conflicts.append("COMMITTEE_NO_BELOW_OVERRIDE_SCORE")
        elif warn_votes >= vc.COMMITTEE_WARN_DOWNGRADE_COUNT or no_votes > 0:
            conflicts.append("COMMITTEE_WARN_MARKET_BLOCK")
    legacy = metrics.get("legacy_adversarial")
    adversarial_mode = str(getattr(vc, "ADVERSARIAL_MODE", "off") or "off").lower()
    if adversarial_mode != "off" and isinstance(legacy, dict) and legacy.get("approved") is False and score < vc.ADVERSARIAL_NO_OVERRIDE_MIN_SCORE:
        conflicts.append("ADVERSARIAL_NO_BELOW_OVERRIDE_SCORE")
    flow = str(metrics.get("flow_direction") or "").upper().replace(" ", "_")
    if vc.FLOW_NO_TRADE_MARKET_BLOCK and flow == "NO_TRADE":
        conflicts.append("FLOW_NO_TRADE_MARKET_BLOCK")
    if vc.PRICE_CHASE_MARKET_BLOCK and _profit_drift_r(sig, price) > _f(getattr(vc, "MAX_CHASE_R", 0.35), 0.35):
        conflicts.append("PRICE_CHASE_MARKET_BLOCK")
    return conflicts


def pending_fill_allowed(sig, price: float, max_drift_r: float = 0.20) -> tuple[bool, float]:
    """Allow triggered pending market fill only near its active boundary.
    
    Regime-aware thresholds:
    - TRENDING: 0.35R (fast momentum, need speed)
    - SCALP: 0.25R (quick entry window)
    - RANGING/SWING: 0.20R (default)
    """
    # Regime-specific threshold override
    regime = getattr(sig, "regime", "").lower()
    if regime == "trending":
        max_drift_r = 0.35
    elif regime == "scalp":
        max_drift_r = 0.25
    else:
        max_drift_r = 0.20
    
    boundary = float(getattr(sig, "active_entry", None) or (sig.entry_low if price < sig.entry_low else sig.entry_high))
    risk = abs(boundary - float(sig.stop_loss or 0.0))
    drift_r = abs(float(price) - boundary) / risk if risk > 0 else float("inf")
    return drift_r <= max_drift_r + 1e-7, drift_r


def route_entry(
    sig,
    price: float,
    *,
    validation_score: float | None = None,
    metrics: dict[str, Any] | None = None,
) -> EntryDecision:
    """Route quality-approved signals into MARKET, LIMIT, or manual review.

    Mechanical safety still rejects broken setups. Otherwise, MARKET requires a
    stronger score and no major conflict; middling or conflicted setups are
    parked as LIMIT/pullback when possible instead of being chased.
    """
    price = float(price or 0.0)
    metrics = metrics or {}
    score = _f(validation_score, _f(metrics.get("validation_score"), 0.0))
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
    if score and score < vc.AUTO_LIMIT_MIN_SCORE:
        return EntryDecision(EntryAction.WAIT_CONFIRMATION, price, "AUTO_SCORE_BELOW_LIMIT_MIN", passed)

    low, high = float(sig.entry_low), float(sig.entry_high)
    boundary = low if price < low else high
    
    # Check if price is in discount (cheaper than entry zone) or chase (past entry zone in profit)
    is_discount = price < low if sig.is_long else price > high
    is_chase = price > high if sig.is_long else price < low

    if low <= price <= high:
        entry, action, code = price, EntryAction.MARKET, "PRICE_INSIDE_ENTRY_ZONE"
    elif is_discount:
        # DISCOUNT ZONE: price is cheaper than recommended entry!
        orig_risk = abs(boundary - sl) if sl > 0 else boundary * 0.01
        discount_r = abs(boundary - price) / orig_risk if orig_risk > 0 else 0.0
        current_risk = abs(price - sl)
        max_discount_r = _f(getattr(vc, "MAX_DISCOUNT_R", 0.60), 0.60)
        discount_enabled = bool(getattr(vc, "DISCOUNT_FILL_ENABLED", True))
        
        # Guard: must not be sitting right on top of SL (at least 35% of original risk buffer remains)
        sl_buffer_ok = current_risk >= orig_risk * 0.35
        
        if discount_enabled and discount_r <= max_discount_r and sl_buffer_ok:
            entry, action, code = price, EntryAction.MARKET, "DISCOUNT_ENTRY_ZONE"
        else:
            entry, action, code = boundary, EntryAction.LIMIT, "DISCOUNT_TOO_DEEP_LIMIT"
    elif is_chase:
        # CHASE / MOMENTUM ZONE: price has already run in the profit direction
        orig_risk = abs(boundary - sl) if sl > 0 else boundary * 0.01
        drift_r = abs(price - boundary) / orig_risk if orig_risk > 0 else float("inf")
        max_chase_r = _f(getattr(vc, "MAX_CHASE_R", 0.35), 0.35)
        min_remaining_rr = _f(getattr(vc, "MIN_REMAINING_RR", 0.70), 0.70)
        
        # Check TP1
        tp1 = float(sig.take_profits[0]) if sig.take_profits else 0.0
        tp1_hit = (price >= tp1 if sig.is_long else price <= tp1) if tp1 > 0 else False
        
        # Remaining RR to TP1
        current_risk = abs(price - sl)
        remaining_profit_tp1 = abs(tp1 - price) if tp1 > 0 else 0.0
        remaining_rr_tp1 = remaining_profit_tp1 / current_risk if current_risk > 0 else 0.0
        
        # RSI exhaustion check
        rsi = _f(metrics.get("rsi"), 50.0)
        rsi_exhausted = (sig.is_long and rsi > 72.0) or (not sig.is_long and rsi < 28.0) if rsi > 0 else False
        
        if not tp1_hit and drift_r <= max_chase_r and remaining_rr_tp1 >= min_remaining_rr and not rsi_exhausted:
            entry, action, code = price, EntryAction.MARKET, "MOMENTUM_CHASE_ALLOWED"
        else:
            entry, action, code = boundary, EntryAction.LIMIT, "PRICE_OUTSIDE_ENTRY_ZONE"
    else:
        entry, action, code = boundary, EntryAction.LIMIT, "PRICE_OUTSIDE_ENTRY_ZONE"

    conflicts = _market_conflicts(sig, price, metrics, score)
    if action == EntryAction.MARKET:
        if score and score < vc.AUTO_MARKET_MIN_SCORE:
            conflicts.append("AUTO_SCORE_MARKET_TO_LIMIT")
        if conflicts:
            pullback = _nearest_pullback_entry(sig, price)
            if pullback is None:
                return EntryDecision(
                    EntryAction.WAIT_CONFIRMATION,
                    price,
                    conflicts[0],
                    passed,
                    _rr_ladder(sig, price),
                    conflicts,
                )
            return EntryDecision(
                EntryAction.LIMIT,
                pullback,
                conflicts[0],
                passed,
                _rr_ladder(sig, pullback),
                conflicts,
            )

    return EntryDecision(action, entry, code, passed, _rr_ladder(sig, entry), conflicts)


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
