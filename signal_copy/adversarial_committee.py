"""Deterministic adversarial committee for signal-copy trade calls.

This module is intentionally shadow/advisory first: it explains risk using
focused specialists and returns a structured decision, while the orchestrator
controls whether that decision can mutate the live validation verdict.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import signal_copy_config as scfg

VERSION = "signal_copy_committee_v1"
YES = "YES"
NO = "NO"
WARN = "WARN"
ABSTAIN = "ABSTAIN"
ACTION_NONE = "NONE"
ACTION_DOWNGRADE = "DOWNGRADE_WEAK"
ACTION_REJECT = "REJECT"


@dataclass
class CommitteeVote:
    specialist: str
    vote: str
    severity: int
    confidence: float
    reason: str
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "specialist": self.specialist,
            "vote": self.vote,
            "severity": int(self.severity),
            "confidence": float(self.confidence),
            "reason": self.reason,
            "evidence": _json_safe(self.evidence),
        }


@dataclass
class CommitteeDecision:
    version: str
    mode: str
    final_vote: str
    action: str
    score: float
    no_votes: int
    warn_votes: int
    votes: List[CommitteeVote]
    legacy_bull_bear: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "mode": self.mode,
            "final_vote": self.final_vote,
            "action": self.action,
            "score": round(float(self.score), 2),
            "no_votes": int(self.no_votes),
            "warn_votes": int(self.warn_votes),
            "top_reasons": self.top_reasons(),
            "votes": [v.to_dict() for v in self.votes],
            "legacy_bull_bear": _json_safe(self.legacy_bull_bear),
        }

    def top_reasons(self, limit: int = 3) -> List[str]:
        ranked = sorted(
            [v for v in self.votes if v.vote in {NO, WARN}],
            key=lambda v: (v.vote != NO, -v.severity, -v.confidence),
        )
        return [f"{v.specialist}: {v.reason}" for v in ranked[:limit]]


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value, default=str)
        return value
    except Exception:
        if isinstance(value, dict):
            return {str(k): _json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [_json_safe(v) for v in value]
        return str(value)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _side(sig: Any) -> str:
    side = getattr(sig, "side", "")
    return side.value if hasattr(side, "value") else str(side).upper()


def _is_long(sig: Any) -> bool:
    return _side(sig) == "LONG"


def _entry(sig: Any) -> float:
    return _f(getattr(sig, "active_entry", None) or getattr(sig, "rr_entry", None) or getattr(sig, "entry_mid", 0.0))


def _risk(sig: Any, entry: Optional[float] = None) -> float:
    ref = _f(entry, 0.0) or _entry(sig)
    sl = _f(getattr(sig, "stop_loss", 0.0))
    return abs(ref - sl) if ref > 0 and sl > 0 else 0.0


def _taken_targets(sig: Any, price: float) -> List[float]:
    tps = [_f(tp) for tp in (getattr(sig, "take_profits", None) or []) if _f(tp) > 0]
    if _is_long(sig):
        return [tp for tp in tps if price >= tp]
    return [tp for tp in tps if price <= tp]


def _remaining_r(sig: Any, price: float) -> float:
    risk = _risk(sig)
    if risk <= 0:
        return 0.0
    tps = [_f(tp) for tp in (getattr(sig, "take_profits", None) or []) if _f(tp) > 0]
    remaining = []
    for tp in tps:
        reward = (tp - price) if _is_long(sig) else (price - tp)
        if reward > 0:
            remaining.append(reward / risk)
    return max(remaining) if remaining else 0.0


def _flow_side(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text in {"LONG", "BULL", "BULLISH", "BUY", "BUYERS", "UP"}:
        return "LONG"
    if text in {"SHORT", "BEAR", "BEARISH", "SELL", "SELLERS", "DOWN"}:
        return "SHORT"
    if "NO_TRADE" in text or "NO TRADE" in text:
        return "NO_TRADE"
    if "BULL" in text or "BUY" in text:
        return "LONG"
    if "BEAR" in text or "SELL" in text:
        return "SHORT"
    return "NEUTRAL"


def _trend_side(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text in {"LONG", "UP", "BULL", "BULLISH"}:
        return "LONG"
    if text in {"SHORT", "DOWN", "BEAR", "BEARISH"}:
        return "SHORT"
    return "NEUTRAL"


def _mk_vote(specialist: str, vote: str, severity: int, confidence: float, reason: str, **evidence: Any) -> CommitteeVote:
    return CommitteeVote(
        specialist=specialist,
        vote=vote,
        severity=max(0, min(int(severity), 3)),
        confidence=max(0.0, min(float(confidence), 1.0)),
        reason=reason,
        evidence=evidence,
    )


def _entry_freshness(sig: Any, metrics: Dict[str, Any]) -> CommitteeVote:
    price = _f(metrics.get("price"))
    if price <= 0:
        return _mk_vote("entry_freshness", WARN, 2, 0.70, "no live price for freshness check")

    tps = [_f(tp) for tp in (getattr(sig, "take_profits", None) or []) if _f(tp) > 0]
    passed = _taken_targets(sig, price)
    remaining_r = _remaining_r(sig, price)
    entry = _entry(sig)
    risk = _risk(sig, entry)
    low = _f(getattr(sig, "entry_low", 0.0))
    high = _f(getattr(sig, "entry_high", 0.0))

    if tps and len(passed) == len(tps):
        return _mk_vote(
            "entry_freshness", NO, 3, 0.98, "all provider targets already passed",
            price=price, passed_targets=passed, take_profits=tps,
        )
    if passed and remaining_r < 1.0:
        return _mk_vote(
            "entry_freshness", NO, 3, 0.92, "price already past early target with less than 1R remaining",
            price=price, passed_targets=passed, remaining_r=remaining_r,
        )

    if entry > 0 and risk > 0:
        drift = (price - entry) if _is_long(sig) else (entry - price)
        drift_r = drift / risk
        max_warn = _f(getattr(scfg, "COMMITTEE_ENTRY_WARN_DRIFT_R", 0.50), 0.50)
        if drift_r > max_warn:
            return _mk_vote(
                "entry_freshness", WARN, 2, 0.82, "price chased beyond entry zone",
                price=price, entry=entry, drift_r=round(drift_r, 3), entry_low=low, entry_high=high,
            )

    if bool(metrics.get("data_stale")):
        return _mk_vote("entry_freshness", WARN, 2, 0.78, "market data is stale", price=price)
    return _mk_vote("entry_freshness", YES, 0, 0.70, "entry still actionable", price=price)


def _geometry_rr(sig: Any, metrics: Dict[str, Any]) -> CommitteeVote:
    entry = _entry(sig)
    sl = _f(getattr(sig, "stop_loss", 0.0))
    tps = [_f(tp) for tp in (getattr(sig, "take_profits", None) or []) if _f(tp) > 0]
    if entry <= 0 or sl <= 0 or not tps:
        return _mk_vote("geometry_rr", NO, 3, 0.95, "missing usable entry, SL, or TP", entry=entry, sl=sl, take_profits=tps)
    if (_is_long(sig) and sl >= entry) or ((not _is_long(sig)) and sl <= entry):
        return _mk_vote("geometry_rr", NO, 3, 0.98, "stop loss is on the wrong side of entry", entry=entry, sl=sl)
    wrong_tps = [tp for tp in tps if not (tp > entry if _is_long(sig) else tp < entry)]
    if wrong_tps:
        return _mk_vote("geometry_rr", NO, 3, 0.96, "take profit is on the wrong side of entry", entry=entry, wrong_tps=wrong_tps)

    risk = abs(entry - sl)
    rr_values = [abs(tp - entry) / risk for tp in tps] if risk > 0 else []
    best_rr = max(rr_values) if rr_values else 0.0
    tp1_rr = rr_values[0] if rr_values else 0.0
    sl_pct = risk / entry * 100.0 if entry > 0 else 0.0

    if best_rr < 1.0:
        return _mk_vote("geometry_rr", NO, 3, 0.93, "best target offers less than 1R", best_rr=round(best_rr, 3), tp1_rr=round(tp1_rr, 3))
    if tp1_rr < _f(getattr(scfg, "COMMITTEE_TP1_WARN_RR", 0.80), 0.80):
        return _mk_vote("geometry_rr", WARN, 2, 0.80, "TP1 reward is weak", tp1_rr=round(tp1_rr, 3), best_rr=round(best_rr, 3))
    safety_cap = _f(getattr(scfg, "COMMITTEE_SL_WARN_DISTANCE_PCT", 20.0), 20.0)
    if sl_pct > safety_cap:
        return _mk_vote("geometry_rr", WARN, 2, 0.85, "SL distance is unusually wide", sl_pct=round(sl_pct, 3), cap=safety_cap)
    min_sl = _f(getattr(scfg, "COMMITTEE_SL_MIN_DISTANCE_PCT", 0.10), 0.10)
    if 0 < sl_pct < min_sl:
        return _mk_vote("geometry_rr", WARN, 1, 0.70, "SL distance is suspiciously tight", sl_pct=round(sl_pct, 3), floor=min_sl)
    return _mk_vote("geometry_rr", YES, 0, 0.78, "RR and SL geometry are acceptable", tp1_rr=round(tp1_rr, 3), best_rr=round(best_rr, 3), sl_pct=round(sl_pct, 3))


def _flow_alignment(sig: Any, metrics: Dict[str, Any]) -> CommitteeVote:
    side = _side(sig)
    flow = _flow_side(metrics.get("flow_direction"))
    cvd = _f(metrics.get("cvd_zscore"))
    imbalance = _f(metrics.get("imbalance"))
    oi5 = _f(metrics.get("oi_change_5m_pct"))
    oi15 = _f(metrics.get("oi_change_15m_pct"))
    oi1h = _f(metrics.get("oi_change_1h_pct"))
    funding = _f(metrics.get("funding_rate"))

    cvd_opposes = (cvd < -1.0 if side == "LONG" else cvd > 1.0)
    imbalance_opposes = (imbalance < -0.25 if side == "LONG" else imbalance > 0.25)
    flow_opposes = flow not in {"NEUTRAL", "NO_TRADE", side}
    oi_unwind = (oi5 + oi15 + oi1h) / 3.0 < -0.5
    crowded = (funding > 0.0005 and side == "LONG") or (funding < -0.0005 and side == "SHORT")

    if (flow_opposes and cvd_opposes) or (cvd_opposes and imbalance_opposes and oi_unwind):
        return _mk_vote(
            "flow_alignment", NO, 3, 0.88, "order flow strongly conflicts with trade side",
            flow_direction=metrics.get("flow_direction"), cvd_zscore=cvd, imbalance=imbalance,
            oi_5m=oi5, oi_15m=oi15, oi_1h=oi1h,
        )
    if flow == "NO_TRADE" and (cvd_opposes or oi_unwind):
        return _mk_vote("flow_alignment", NO, 2, 0.78, "flow engine says no-trade with confirming weakness", flow_direction=metrics.get("flow_direction"), cvd_zscore=cvd)
    if flow == "NO_TRADE" or flow_opposes or cvd_opposes or oi_unwind or crowded:
        return _mk_vote(
            "flow_alignment", WARN, 2, 0.70, "flow is weak, crowded, or partially conflicting",
            flow_direction=metrics.get("flow_direction"), cvd_zscore=cvd, funding_rate=funding,
            oi_avg=round((oi5 + oi15 + oi1h) / 3.0, 3),
        )
    return _mk_vote("flow_alignment", YES, 0, 0.70, "flow does not conflict", flow_direction=metrics.get("flow_direction"), cvd_zscore=cvd)


def _mtf_regime(sig: Any, metrics: Dict[str, Any]) -> CommitteeVote:
    side = _side(sig)
    mtf = metrics.get("mtf_alignment") if isinstance(metrics.get("mtf_alignment"), dict) else {}
    mtf_score = _f(mtf.get("score"), 50.0)
    tf4h = _trend_side(mtf.get("tf4h_trend"))
    d1 = _trend_side(mtf.get("d1_trend"))
    regime = str(metrics.get("regime_label") or "").upper()
    chg15 = _f(metrics.get("price_change_15m_pct"))
    short_term_against = chg15 < -0.5 if side == "LONG" else chg15 > 0.5

    if mtf_score < 25 and tf4h not in {"NEUTRAL", side} and d1 not in {"NEUTRAL", side}:
        return _mk_vote("mtf_regime", NO, 3, 0.86, "4h and daily trends oppose the setup", mtf_score=mtf_score, tf4h=tf4h, d1=d1)
    if mtf_score < 45 or short_term_against:
        return _mk_vote("mtf_regime", WARN, 2, 0.70, "trend/regime support is weak", mtf_score=mtf_score, tf4h=tf4h, d1=d1, regime=regime, price_change_15m_pct=chg15)
    return _mk_vote("mtf_regime", YES, 0, 0.70, "trend/regime is acceptable", mtf_score=mtf_score, regime=regime)


def _liquidity_quality(sig: Any, metrics: Dict[str, Any]) -> CommitteeVote:
    qvol = _f(metrics.get("qvol_5m"))
    quality = str(metrics.get("data_quality") or "").upper()
    stale = bool(metrics.get("data_stale"))
    symbol_found = metrics.get("flow_symbol_found")
    low_floor = _f(getattr(scfg, "COMMITTEE_LOW_QVOL_5M", 50000.0), 50000.0)
    critical_floor = _f(getattr(scfg, "COMMITTEE_CRITICAL_QVOL_5M", 10000.0), 10000.0)

    poor_quality = quality in {"LOW", "POOR", "BAD", "STALE"} or stale
    if qvol > 0 and qvol < critical_floor and poor_quality:
        return _mk_vote("liquidity_quality", NO, 3, 0.82, "very low liquidity with poor/stale data", qvol_5m=qvol, data_quality=quality, data_stale=stale)
    if (qvol > 0 and qvol < low_floor) or poor_quality or symbol_found is False:
        return _mk_vote("liquidity_quality", WARN, 2, 0.68, "liquidity/data quality is weak", qvol_5m=qvol, data_quality=quality, data_stale=stale, flow_symbol_found=symbol_found)
    return _mk_vote("liquidity_quality", YES, 0, 0.65, "liquidity/data quality is usable", qvol_5m=qvol, data_quality=quality)


def _crowding_chase(sig: Any, metrics: Dict[str, Any]) -> CommitteeVote:
    price = _f(metrics.get("price"))
    entry = _entry(sig)
    risk = _risk(sig, entry)
    rsi = _f(metrics.get("rsi"), 50.0)
    chg15 = _f(metrics.get("price_change_15m_pct"))
    funding = _f(metrics.get("funding_rate"))
    if entry <= 0 or risk <= 0 or price <= 0:
        return _mk_vote("crowding_chase", ABSTAIN, 0, 0.40, "insufficient data for chase check")

    drift = (price - entry) if _is_long(sig) else (entry - price)
    drift_r = drift / risk
    long_extreme = _is_long(sig) and rsi >= 75 and chg15 >= 2.0 and drift_r > 0.35
    short_extreme = (not _is_long(sig)) and rsi <= 25 and chg15 <= -2.0 and drift_r > 0.35
    funding_crowded = (funding > 0.0005 and _is_long(sig)) or (funding < -0.0005 and not _is_long(sig))

    if long_extreme or short_extreme:
        return _mk_vote("crowding_chase", NO, 3, 0.84, "momentum looks crowded and entry is chased", rsi=rsi, price_change_15m_pct=chg15, drift_r=round(drift_r, 3))
    if drift_r > 0.25 and (funding_crowded or rsi >= 72 or rsi <= 28):
        return _mk_vote("crowding_chase", WARN, 2, 0.70, "late entry with crowding/exhaustion risk", rsi=rsi, funding_rate=funding, drift_r=round(drift_r, 3))
    return _mk_vote("crowding_chase", YES, 0, 0.62, "no extreme chase/crowding detected", drift_r=round(drift_r, 3), rsi=rsi)


def _risk_score(votes: Iterable[CommitteeVote]) -> float:
    total = 0.0
    for vote in votes:
        if vote.vote == NO:
            total += 35.0 if vote.severity >= 3 else 24.0
        elif vote.vote == WARN:
            total += 14.0 if vote.severity >= 2 else 8.0
    return min(100.0, total)


def _final_vote(votes: List[CommitteeVote]) -> str:
    no_votes = [v for v in votes if v.vote == NO]
    warn_votes = [v for v in votes if v.vote == WARN]
    min_no = int(getattr(scfg, "COMMITTEE_MIN_NO_VOTES", 2) or 2)
    if any(v.severity >= 3 for v in no_votes) or len(no_votes) >= min_no:
        return NO
    if no_votes or warn_votes:
        return WARN
    return YES


def _action(final_vote: str, votes: List[CommitteeVote], validation_score: float) -> str:
    mode = str(getattr(scfg, "COMMITTEE_MODE", "shadow") or "shadow").strip().lower()
    if mode in {"shadow", "advisory", "off"} or final_vote != NO:
        return ACTION_NONE
    floor = _f(getattr(scfg, "COMMITTEE_SOFT_FLOOR", 90.0), 90.0)
    if mode == "soft":
        return ACTION_DOWNGRADE if validation_score < floor else ACTION_NONE
    if mode == "hard":
        hard_no = int(getattr(scfg, "COMMITTEE_HARD_NO_VOTES", 3) or 3)
        no_votes = [v for v in votes if v.vote == NO]
        if len(no_votes) >= hard_no or any(v.severity >= 3 for v in no_votes):
            return ACTION_REJECT
    return ACTION_NONE


def evaluate_committee(
    sig: Any,
    validation_result: Any,
    metrics: Optional[Dict[str, Any]],
    *,
    legacy_result: Optional[Dict[str, Any]] = None,
) -> CommitteeDecision:
    metrics = metrics or {}
    votes = [
        _entry_freshness(sig, metrics),
        _geometry_rr(sig, metrics),
        _flow_alignment(sig, metrics),
        _mtf_regime(sig, metrics),
        _liquidity_quality(sig, metrics),
        _crowding_chase(sig, metrics),
    ]
    final = _final_vote(votes)
    validation_score = _f(getattr(validation_result, "score", 0.0))
    no_count = sum(1 for v in votes if v.vote == NO)
    warn_count = sum(1 for v in votes if v.vote == WARN)
    return CommitteeDecision(
        version=VERSION,
        mode=str(getattr(scfg, "COMMITTEE_MODE", "shadow") or "shadow").strip().lower(),
        final_vote=final,
        action=_action(final, votes, validation_score),
        score=_risk_score(votes),
        no_votes=no_count,
        warn_votes=warn_count,
        votes=votes,
        legacy_bull_bear=legacy_result,
    )


def record_decision(sig: Any, validation_result: Any, decision: CommitteeDecision, metrics: Optional[Dict[str, Any]] = None) -> None:
    path = Path(getattr(scfg, "COMMITTEE_DECISION_JOURNAL", "runtime/state/committee_decisions.jsonl"))
    row = {
        "ts": time.time(),
        "signal_id": getattr(sig, "signal_id", ""),
        "symbol": getattr(sig, "symbol", ""),
        "side": _side(sig),
        "source_chat_id": getattr(sig, "source_chat_id", None),
        "source_name": getattr(sig, "source_name", ""),
        "validation_verdict": getattr(getattr(validation_result, "verdict", ""), "value", str(getattr(validation_result, "verdict", ""))),
        "validation_score": _f(getattr(validation_result, "score", 0.0)),
        "committee": decision.to_dict(),
    }
    if metrics:
        row["market"] = {
            "price": metrics.get("price"),
            "rsi": metrics.get("rsi"),
            "cvd_zscore": metrics.get("cvd_zscore"),
            "flow_direction": metrics.get("flow_direction"),
            "qvol_5m": metrics.get("qvol_5m"),
            "regime_label": metrics.get("regime_label"),
        }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(_json_safe(row), default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        return
