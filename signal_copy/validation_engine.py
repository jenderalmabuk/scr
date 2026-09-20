"""
Deep validation engine for external trade-call signals.

Takes a ParsedSignal + live market metrics and produces a ValidationResult
with a per-factor breakdown and a final verdict (VALID / WEAK / REJECT).

Factors evaluated (each contributes to a 0-100 confluence score):
  1. Price-vs-entry-zone freshness   (is price still near the entry zone?)
  2. Geometry / Risk-Reward          (SL distance sane, RR to TP1 acceptable)
  3. Open Interest alignment         (OI rising with the trade direction)
  4. CVD / order-flow alignment      (taker flow agrees with side)
  5. Funding-rate context            (not fighting an over-crowded side)
  6. RSI / momentum context          (not entering exhausted)
  7. Trend / regime alignment        (price structure agrees with side)

The engine is deliberately conservative: missing data lowers confidence but
does not fabricate confluence. Thresholds live in validation_config.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .signal_schema import ParsedSignal, SignalSide
from . import validation_config as vc
from .mtf_aligner import MTFAligner


class Verdict(str, Enum):
    VALID = "VALID"
    WEAK = "WEAK"
    REJECT = "REJECT"


@dataclass
class Factor:
    name: str
    score: float          # contribution actually earned
    max_score: float      # maximum this factor could contribute
    passed: bool
    detail: str

    @property
    def pct(self) -> float:
        return (self.score / self.max_score * 100.0) if self.max_score else 0.0


@dataclass
class ValidationResult:
    signal: ParsedSignal
    verdict: Verdict
    score: float                      # 0-100 normalized confluence
    factors: List[Factor] = field(default_factory=list)
    hard_blocks: List[str] = field(default_factory=list)
    metrics_snapshot: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return self.verdict == Verdict.VALID

    def factor_lines(self) -> List[str]:
        lines = []
        for f in self.factors:
            mark = "✅" if f.passed else "❌"
            lines.append(f"{mark} {f.name}: {f.detail} ({f.score:.0f}/{f.max_score:.0f})")
        return lines


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _canonical_supported(symbol: str) -> bool:
    paths = [
        Path("/app/runtime/revo/canonical_universe.json"),
        Path(__file__).resolve().parents[1] / "runtime" / "revo" / "canonical_universe.json",
    ]
    for path in paths:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
            pairs = data.get("pairs", data)
            return symbol.upper() in {str(p).upper() for p in pairs}
        except Exception:
            continue
    return True


def _factor_price_freshness(sig: ParsedSignal, m: Dict[str, Any]) -> Factor:
    price = _f(m.get("price"))
    mx = vc.W_PRICE_FRESHNESS
    if price <= 0:
        return Factor("Price/Entry", 0.0, mx, False, "no live price")

    low, high = sig.entry_low, sig.entry_high

    # Limit orders WAIT for price to come to the entry — being away from the
    # zone is normal, not a penalty. Score on whether it's still within reach.
    if getattr(sig, "entry_type", "market") == "limit":
        ref = sig.entry_mid
        dist_pct = abs(price - ref) / ref * 100.0 if ref else 999
        if dist_pct <= vc.LIMIT_REACHABLE_PCT:
            return Factor("Price/Entry", mx, mx, True,
                          f"limit @ {ref:g}, price {price:g} ({dist_pct:.2f}% away, reachable)")
        return Factor("Price/Entry", mx * 0.4, mx, True,
                      f"limit @ {ref:g}, price {price:g} ({dist_pct:.2f}% away)")

    zone_w = max(high - low, high * 0.001)
    # tolerance band around the zone scaled by config
    tol = zone_w * vc.ENTRY_ZONE_TOLERANCE_MULT + high * vc.ENTRY_ZONE_TOLERANCE_PCT / 100.0

    if low - tol <= price <= high + tol:
        # inside or within tolerance
        if low <= price <= high:
            return Factor("Price/Entry", mx, mx, True, f"price {price:g} inside zone")
        return Factor("Price/Entry", mx * 0.7, mx, True, f"price {price:g} near zone")

    # Price is outside the tolerance band — but off-zone is a ROUTING input,
    # not a quality defect. A good-confluence signal that's off-zone should
    # become a limit / pending entry (see orchestrator._wait_for_limit), NOT a
    # reject. So we score it PARTIAL + passed instead of zeroing it out, which
    # is what used to silently kill high-confluence calls (e.g. BNB score 72
    # rejected only because price had drifted from the entry zone).
    dist_pct = (min(abs(price - low), abs(price - high)) / high) * 100.0 if high else 999
    # Did price move to give a BETTER entry (pullback) or CHASE past the zone?
    if sig.is_long:
        better_entry = price < low    # dipped below a LONG zone = discount
    else:
        better_entry = price > high   # popped above a SHORT zone = premium
    if better_entry:
        return Factor("Price/Entry", mx * 0.75, mx, True,
                      f"price {price:g} {dist_pct:.2f}% off-zone (better entry — limit)")
    return Factor("Price/Entry", mx * 0.5, mx, True,
                  f"price {price:g} chased {dist_pct:.2f}% past zone (route to pullback/limit)")


def _factor_geometry(sig: ParsedSignal, m: Dict[str, Any]) -> Factor:
    mx = vc.W_GEOMETRY
    sl_pct = sig.sl_distance_pct()
    rr_tp1 = sig.rr_ratio(0)
    rr_best = sig.rr_best()
    if sl_pct is None or rr_tp1 is None:
        return Factor("Geometry/RR", mx * 0.3, mx, False, "missing SL or TP")

    problems = []
    if sl_pct > vc.MAX_SL_DISTANCE_PCT:
        problems.append(f"SL too wide {sl_pct:.2f}%")
    if sl_pct < vc.MIN_SL_DISTANCE_PCT:
        problems.append(f"SL too tight {sl_pct:.2f}%")
    # Judge on best achievable target (bot scales out + trails), but require
    # TP1 to at least clear the hard floor so the first scale-out isn't a loss.
    if rr_best < vc.MIN_RR_RATIO:
        problems.append(f"best RR {rr_best:.2f}<{vc.MIN_RR_RATIO}")

    if problems:
        return Factor("Geometry/RR", 0.0, mx, False, "; ".join(problems))
    bonus = min(rr_best / vc.GOOD_RR_RATIO, 1.0)
    return Factor("Geometry/RR", mx * bonus, mx, True,
                  f"SL {sl_pct:.2f}% | RR tp1 {rr_tp1:.2f}/best {rr_best:.2f}")


def _factor_oi(sig: ParsedSignal, m: Dict[str, Any]) -> Factor:
    mx = vc.W_OI
    oi15 = _f(m.get("oi_change_15m_pct"))
    oi1h = _f(m.get("oi_change_1h_pct"))
    if oi15 == 0.0 and oi1h == 0.0:
        return Factor("Open Interest", mx * 0.3, mx, False, "no OI data")

    # Rising OI + price in trade direction = conviction.
    # For LONG we want OI building up (fresh longs). For SHORT same (fresh shorts).
    rising = (oi15 + oi1h) / 2.0
    if rising >= vc.OI_RISE_STRONG_PCT:
        return Factor("Open Interest", mx, mx, True, f"OI rising {rising:+.2f}% (conviction)")
    if rising >= vc.OI_RISE_MIN_PCT:
        return Factor("Open Interest", mx * 0.7, mx, True, f"OI rising {rising:+.2f}%")
    if rising <= -vc.OI_RISE_STRONG_PCT:
        return Factor("Open Interest", mx * 0.2, mx, False, f"OI falling {rising:+.2f}% (unwind)")
    return Factor("Open Interest", mx * 0.4, mx, False, f"OI flat {rising:+.2f}%")


def _factor_cvd(sig: ParsedSignal, m: Dict[str, Any]) -> Factor:
    mx = vc.W_CVD
    cvd_z = _f(m.get("cvd_zscore"))
    imbalance = _f(m.get("imbalance"))
    # Positive CVD/imbalance => buyers in control (good for LONG), and vice versa.
    flow = cvd_z if cvd_z != 0 else imbalance * 3.0
    aligned = flow > 0 if sig.is_long else flow < 0
    strength = min(abs(flow) / vc.CVD_STRONG_ZSCORE, 1.0)

    if aligned and abs(flow) >= vc.CVD_MIN_ZSCORE:
        return Factor("CVD/Flow", mx * (0.6 + 0.4 * strength), mx, True,
                      f"flow {flow:+.2f} agrees with {sig.side.value}")
    if not aligned and abs(flow) >= vc.CVD_STRONG_ZSCORE:
        return Factor("CVD/Flow", 0.0, mx, False,
                      f"flow {flow:+.2f} against {sig.side.value}")
    return Factor("CVD/Flow", mx * 0.4, mx, False, f"flow weak {flow:+.2f}")


def _factor_funding(sig: ParsedSignal, m: Dict[str, Any]) -> Factor:
    mx = vc.W_FUNDING
    fr = _f(m.get("funding_rate")) * 100.0  # to percent
    # Very positive funding => longs crowded/overpaying => risky to LONG.
    # Very negative funding => shorts crowded => risky to SHORT.
    if abs(fr) < vc.FUNDING_NEUTRAL_PCT:
        return Factor("Funding", mx, mx, True, f"funding neutral {fr:+.4f}%")
    crowded_long = fr > 0
    fighting = (crowded_long and sig.is_long) or ((not crowded_long) and (not sig.is_long))
    if fighting and abs(fr) >= vc.FUNDING_EXTREME_PCT:
        return Factor("Funding", mx * 0.1, mx, False, f"funding {fr:+.4f}% (crowded {sig.side.value})")
    if fighting:
        return Factor("Funding", mx * 0.5, mx, True, f"funding {fr:+.4f}% (mild crowd)")
    # funding favors the trade (contrarian edge)
    return Factor("Funding", mx, mx, True, f"funding {fr:+.4f}% (favors {sig.side.value})")


def _factor_rsi(sig: ParsedSignal, m: Dict[str, Any]) -> Factor:
    mx = vc.W_RSI
    rsi = _f(m.get("rsi"), 50.0)
    if sig.is_long:
        if rsi >= vc.RSI_OVERBOUGHT:
            return Factor("RSI/Momentum", mx * 0.2, mx, False, f"RSI {rsi:.0f} overbought for LONG")
        if rsi <= vc.RSI_OVERSOLD:
            return Factor("RSI/Momentum", mx, mx, True, f"RSI {rsi:.0f} oversold (LONG dip)")
        return Factor("RSI/Momentum", mx * 0.75, mx, True, f"RSI {rsi:.0f} ok for LONG")
    else:
        if rsi <= vc.RSI_OVERSOLD:
            return Factor("RSI/Momentum", mx * 0.2, mx, False, f"RSI {rsi:.0f} oversold for SHORT")
        if rsi >= vc.RSI_OVERBOUGHT:
            return Factor("RSI/Momentum", mx, mx, True, f"RSI {rsi:.0f} overbought (SHORT)")
        return Factor("RSI/Momentum", mx * 0.75, mx, True, f"RSI {rsi:.0f} ok for SHORT")


def _factor_trend(sig: ParsedSignal, m: Dict[str, Any]) -> Factor:
    mx = vc.W_TREND
    chg15 = _f(m.get("price_change_15m_pct"))
    regime = str(m.get("regime_label", "")).upper()
    aligned = chg15 > 0 if sig.is_long else chg15 < 0
    if abs(chg15) < vc.TREND_FLAT_PCT:
        return Factor("Trend/Regime", mx * 0.5, mx, True, f"{regime or 'flat'} ({chg15:+.2f}%)")
    if aligned:
        return Factor("Trend/Regime", mx, mx, True, f"{regime} momentum {chg15:+.2f}% with {sig.side.value}")
    return Factor("Trend/Regime", mx * 0.25, mx, False, f"{regime} momentum {chg15:+.2f}% against {sig.side.value}")


def _factor_mtf_alignment(sig: ParsedSignal, m: Dict[str, Any]) -> Factor:
    """Multi-timeframe alignment: entry TF vs 4h/daily trend."""
    mx = 15.0
    mtf_raw = m.get("mtf_alignment")
    if not mtf_raw or not isinstance(mtf_raw, dict):
        return Factor("MTF Alignment", mx * 0.5, mx, True, "no higher-TF data")
    score = float(mtf_raw.get("score", 50.0))
    entry_trend = str(mtf_raw.get("entry_trend", "FLAT"))
    tf4h_trend = str(mtf_raw.get("tf4h_trend", "FLAT"))
    d1_trend = str(mtf_raw.get("d1_trend", "FLAT"))
    direction = sig.side.value
    aligned_4h = tf4h_trend == direction
    aligned_d1 = d1_trend == direction
    if aligned_4h and aligned_d1:
        earn = mx * (score / 100.0)
        return Factor("MTF Alignment", earn, mx, True,
                      f"4h={tf4h_trend} d1={d1_trend} aligned score={score:.0f}")
    elif aligned_4h or aligned_d1:
        earn = mx * 0.5 * (score / 100.0)
        return Factor("MTF Alignment", earn, mx, True,
                      f"4h={tf4h_trend} d1={d1_trend} partial score={score:.0f}")
    return Factor("MTF Alignment", mx * 0.15, mx, False,
                  f"4h={tf4h_trend} d1={d1_trend} vs {direction} — misaligned")

def _factor_chart_vision(sig: ParsedSignal, m: Dict[str, Any]) -> Optional[Factor]:
    """Confluence from the analyst's chart image (vision). Only contributes when
    a chart was actually read — agreement strengthens, conflict weakens."""
    mx = vc.W_CHART_VISION
    v = m.get("chart_vision") or {}
    if not isinstance(v, dict) or not v:
        return None
    trend = str(v.get("trend") or "").upper()
    vside = str(v.get("side") or "").upper()
    conf = _f(v.get("confidence"), 0.5)
    if not trend and not vside:
        return None  # nothing usable read from the chart

    details: List[str] = []
    agree = False
    conflict = False
    if trend in ("UP", "DOWN", "SIDEWAYS"):
        if (trend == "UP" and sig.is_long) or (trend == "DOWN" and not sig.is_long):
            agree = True
            details.append(f"trend {trend} with {sig.side.value}")
        elif trend == "SIDEWAYS":
            details.append("trend sideways")
        else:
            conflict = True
            details.append(f"trend {trend} against {sig.side.value}")
    if vside in ("LONG", "SHORT"):
        if vside == sig.side.value:
            details.append("chart side matches")
            agree = True
        else:
            conflict = True
            details.append(f"chart suggests {vside}")

    detail = "; ".join(details) or "chart read"
    if conflict and not agree:
        return Factor("Chart/Vision", 0.0, mx, False, f"{detail} (conf {conf:.0%})")
    if agree and not conflict:
        return Factor("Chart/Vision", mx * (0.6 + 0.4 * min(conf, 1.0)), mx, True,
                      f"{detail} (conf {conf:.0%})")
    return Factor("Chart/Vision", mx * 0.5, mx, True, f"{detail} (conf {conf:.0%})")



def _factor_tradingview(sig: ParsedSignal, m: Dict[str, Any]) -> Factor:
    """TradingView indicator confluence (RSI, EMA, BB, TV study)."""
    mx = 15.0
    tv = m.get("tradingview")
    if not tv or not isinstance(tv, dict):
        return Factor("TradingView", mx * 0.3, mx, True, "no TV data")
    score = float(tv.get("score", 50.0))
    details = tv.get("details", [])
    detail = "; ".join(details[:3]) if details else f"TV score={score:.0f}"
    if score >= 70:
        return Factor("TradingView", mx * 0.9, mx, True, detail)
    elif score >= 55:
        return Factor("TradingView", mx * 0.6, mx, True, detail)
    return Factor("TradingView", mx * 0.2, mx, False, detail)
    
def _set_active_entry(sig: ParsedSignal, price: float) -> None:
    levels = [x for x in (sig.entry_low, sig.entry_high) if x and x > 0]
    if not levels:
        return
    if price <= 0:
        sig.active_entry = sig.entry_mid
        return
    sig.active_entry = min(levels, key=lambda x: abs(price - x))


def validate_signal(sig: ParsedSignal, metrics: Optional[Dict[str, Any]]) -> ValidationResult:
    """Run all factors and compute a verdict.
    
    Legacy mode (LEGACY_VALIDATION=1): Minimal checks — price reachable, SL present,
    basic trend alignment. Let the user decide. This mirrors the old bot behavior
    where almost everything was sent to Telegram for manual review.
    """
    import os
    legacy_mode = os.getenv("SIGNAL_COPY_LEGACY_VALIDATION", "0").strip().lower() in ("1", "true", "yes")
    
    if legacy_mode:
        return _legacy_validate(sig, metrics)
    
    metrics = metrics or {}
    _set_active_entry(sig, _f(metrics.get("price")))
    hard_blocks: List[str] = []

    # --- hard blocks (instant reject regardless of score) ---
    if not _canonical_supported(sig.symbol):
        hard_blocks.append("symbol not in canonical Bybit∩Binance USDT perp universe")
    if not metrics.get("data_valid", False) and _f(metrics.get("price")) <= 0:
        hard_blocks.append("no valid market data for symbol")
    if "MALFORMED_PROVIDER_LEVELS" in getattr(sig, "level_anomalies", []):
        hard_blocks.append("MALFORMED_PROVIDER_LEVELS: provider supplied no usable TP or SL")

    # Geometry (TP/SL/RR) is NOT scored anymore — it only shapes the execution
    # plan. We keep only a loose safety cap to avoid clearly-broken stops.
    sl_pct = sig.sl_distance_pct()
    if sl_pct is not None and sl_pct > vc.SAFETY_MAX_SL_DISTANCE_PCT:
        hard_blocks.append(f"SL distance {sl_pct:.1f}% exceeds safety cap {vc.SAFETY_MAX_SL_DISTANCE_PCT}%")

    factors = [
        _factor_price_freshness(sig, metrics),
        _factor_oi(sig, metrics),
        _factor_cvd(sig, metrics),
        _factor_funding(sig, metrics),
        _factor_rsi(sig, metrics),
        _factor_trend(sig, metrics),
        _factor_mtf_alignment(sig, metrics),
        _factor_tradingview(sig, metrics),
    ]

    # Chart/vision confluence only counts when a chart image was actually read.
    _cv = _factor_chart_vision(sig, metrics)
    if _cv is not None:
        factors.append(_cv)

    earned = sum(f.score for f in factors)
    possible = sum(f.max_score for f in factors)
    score = (earned / possible * 100.0) if possible else 0.0

    if hard_blocks:
        verdict = Verdict.REJECT
    elif score >= vc.VALID_THRESHOLD:
        verdict = Verdict.VALID
    elif score >= vc.WEAK_THRESHOLD:
        verdict = Verdict.WEAK
    else:
        verdict = Verdict.REJECT

    return ValidationResult(
        signal=sig,
        verdict=verdict,
        score=score,
        factors=factors,
        hard_blocks=hard_blocks,
        metrics_snapshot={
            k: metrics.get(k)
            for k in (
                "price", "oi_change_5m_pct", "oi_change_15m_pct", "oi_change_1h_pct",
                "cvd_zscore", "imbalance", "funding_rate", "rsi", "atr_pct",
                "vol_ratio", "price_change_15m_pct", "regime_label", "qvol_5m",
                "flow_direction", "flow_source", "data_quality", "data_stale",
                "flow_lookup_status", "flow_symbol_found", "mtf_alignment",
                "tradingview", "chart_vision",
            )
        },
    )


def _legacy_validate(sig: ParsedSignal, metrics: Dict[str, Any]) -> ValidationResult:
    """Legacy validation — permissive, mirrors old bot behavior.
    
    Only hard-blocks:
    - No live price
    - SL missing or absurdly wide (>20%)
    - Price already ran far past entry in profit direction (chasing)
    
    Everything else gets VALID (user decides via confirmation bot).
    """
    metrics = metrics or {}
    hard_blocks: List[str] = []
    factors = []
    
    price = _f(metrics.get("price"))
    _set_active_entry(sig, price)
    if "MALFORMED_PROVIDER_LEVELS" in getattr(sig, "level_anomalies", []):
        hard_blocks.append("MALFORMED_PROVIDER_LEVELS: provider supplied no usable TP or SL")
    
    # Hard block: no price data
    if price <= 0:
        hard_blocks.append("no live price data")
        return ValidationResult(
            signal=sig, verdict=Verdict.REJECT, score=0.0,
            factors=[Factor("Price", 0, 100, False, "no live price")],
            hard_blocks=hard_blocks, metrics_snapshot=metrics
        )
    
    # Hard block: SL missing or absurdly wide
    sl_pct = sig.sl_distance_pct()
    if sl_pct is None:
        hard_blocks.append("missing stop loss")
    elif sl_pct > 20.0:  # safety cap
        hard_blocks.append(f"SL too wide {sl_pct:.1f}%")
    
    # NOTE: no "chase" hard-block here. Price drift from the entry is a ROUTING
    # decision, not a reject reason — orchestrator._wait_for_limit turns a
    # chased/off-zone signal into a pending-limit that waits for a pullback so
    # risk stays ~1%. Hard-blocking it (as an earlier build did) threw away
    # perfectly good calls. The old xomegabot never had this block.
    
    # Factors (informational, don't gate execution)
    mx_price = 20.0
    if sig.entry_type == "limit":
        ref = sig.entry_mid
        dist_pct = abs(price - ref) / ref * 100.0 if ref else 999
        if dist_pct <= 5.0:
            factors.append(Factor("Price/Entry", mx_price, mx_price, True, f"limit @ {ref:g}, {dist_pct:.2f}% away"))
        else:
            factors.append(Factor("Price/Entry", mx_price * 0.5, mx_price, True, f"limit @ {ref:g}, {dist_pct:.2f}% away (far)"))
    else:
        low, high = sig.entry_low, sig.entry_high
        zone_w = max(high - low, high * 0.001)
        tol = zone_w * 0.5 + high * 0.25 / 100.0
        if low - tol <= price <= high + tol:
            factors.append(Factor("Price/Entry", mx_price, mx_price, True, f"price {price:g} near zone"))
        else:
            factors.append(Factor("Price/Entry", mx_price * 0.6, mx_price, True, f"price {price:g} off-zone (legacy: still allowed)"))
    
    # OI - informational only
    oi15 = _f(metrics.get("oi_change_15m_pct"))
    oi1h = _f(metrics.get("oi_change_1h_pct"))
    rising = (oi15 + oi1h) / 2.0 if (oi15 != 0 or oi1h != 0) else 0
    mx_oi = 15.0
    if rising > 0:
        factors.append(Factor("OI", mx_oi, mx_oi, True, f"OI rising {rising:+.2f}%"))
    else:
        factors.append(Factor("OI", mx_oi * 0.5, mx_oi, True, f"OI flat/falling {rising:+.2f}%"))
    
    # CVD - informational
    cvd_z = _f(metrics.get("cvd_zscore"))
    imbalance = _f(metrics.get("imbalance"))
    flow = cvd_z if cvd_z != 0 else imbalance * 3.0
    aligned = flow > 0 if sig.is_long else flow < 0
    mx_cvd = 15.0
    if aligned and abs(flow) > 0.5:
        factors.append(Factor("CVD/Flow", mx_cvd, mx_cvd, True, f"flow {flow:+.2f} with {sig.side.value}"))
    else:
        factors.append(Factor("CVD/Flow", mx_cvd * 0.6, mx_cvd, True, f"flow {flow:+.2f} neutral"))
    
    # RSI - informational
    rsi = _f(metrics.get("rsi"), 50.0)
    mx_rsi = 15.0
    if sig.is_long:
        if rsi >= 75:
            factors.append(Factor("RSI", mx_rsi * 0.5, mx_rsi, True, f"RSI {rsi:.0f} high but allowed"))
        elif rsi <= 30:
            factors.append(Factor("RSI", mx_rsi, mx_rsi, True, f"RSI {rsi:.0f} oversold (dip buy)"))
        else:
            factors.append(Factor("RSI", mx_rsi, mx_rsi, True, f"RSI {rsi:.0f} ok"))
    else:
        if rsi <= 25:
            factors.append(Factor("RSI", mx_rsi * 0.5, mx_rsi, True, f"RSI {rsi:.0f} low but allowed"))
        elif rsi >= 70:
            factors.append(Factor("RSI", mx_rsi, mx_rsi, True, f"RSI {rsi:.0f} overbought (sell rally)"))
        else:
            factors.append(Factor("RSI", mx_rsi, mx_rsi, True, f"RSI {rsi:.0f} ok"))
    
    # Trend - informational
    chg15 = _f(metrics.get("price_change_15m_pct"))
    regime = str(metrics.get("regime_label", "")).upper()
    mx_trend = 15.0
    aligned_trend = chg15 > 0 if sig.is_long else chg15 < 0
    if aligned_trend:
        factors.append(Factor("Trend", mx_trend, mx_trend, True, f"{regime} {chg15:+.2f}% with {sig.side.value}"))
    else:
        factors.append(Factor("Trend", mx_trend * 0.6, mx_trend, True, f"{regime} {chg15:+.2f}% against {sig.side.value} (legacy: allowed)"))
    
    # Geometry - just informational
    rr_tp1 = sig.rr_ratio(0)
    rr_best = sig.rr_best()
    mx_geo = 20.0
    if sl_pct is not None and rr_tp1 is not None:
        factors.append(Factor("Geometry/RR", mx_geo, mx_geo, True, f"SL {sl_pct:.2f}% | RR tp1 {rr_tp1:.2f}/best {rr_best:.2f}"))
    else:
        factors.append(Factor("Geometry/RR", mx_geo * 0.5, mx_geo, True, "incomplete TP/SL"))
    
    earned = sum(f.score for f in factors)
    possible = sum(f.max_score for f in factors)
    score = (earned / possible * 100.0) if possible else 0.0
    
    if hard_blocks:
        verdict = Verdict.REJECT
    else:
        verdict = Verdict.VALID  # Legacy: almost everything is VALID
    
    return ValidationResult(
        signal=sig,
        verdict=verdict,
        score=score,
        factors=factors,
        hard_blocks=hard_blocks,
        metrics_snapshot=metrics,
    )
