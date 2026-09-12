"""
Signal normalization: make a ParsedSignal executable even when the channel
omits targets (many post TP only in an image, as 1R/2R/3R).

Rules (run AFTER live metrics are available):
- Clean junk take-profits: keep only targets on the correct side of entry and
  within a sane distance band (drops R-ordinals like 1/2/3 and absurd values).
- If no reliable TP but a stop loss exists: improvise TP1/TP2/TP3 = 1R/2R/3R
  measured from entry to stop (R = |entry - SL|).
- If the stop loss is missing: improvise it from ATR so the trade is still
  sizable and an R can be computed.

Geometry is intentionally NOT used to score the signal anymore; it only shapes
the execution plan (entry / SL / TP ladder).
"""

from __future__ import annotations

from typing import Any, Dict, List

from .signal_schema import ParsedSignal, SignalSide

# distance band (fraction of entry) for a take-profit to be considered "real"
_TP_MIN_DIST = 0.001    # 0.1%
_TP_MAX_DIST = 0.60     # 60% (beyond this it's almost certainly junk/ordinal)

# improvised SL sizing from ATR
_SL_ATR_MULT = 1.5
_SL_MIN_PCT = 1.0
_SL_MAX_PCT = 6.0

# R-multiples used when improvising targets
_R_MULTIPLES = (1.0, 2.0, 3.0)


def _f(v, d=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def normalize_signal(sig: ParsedSignal, metrics: Dict[str, Any]) -> ParsedSignal:
    metrics = metrics or {}
    is_long = sig.side == SignalSide.LONG
    entry = sig.entry_mid
    if entry <= 0:
        # Market entry ("Entry now") with no price in the text: use the live
        # price so the call becomes executable and an R can be computed.
        live = _f(metrics.get("price"), 0.0)
        if live > 0:
            sig.entry_low = sig.entry_high = live
            entry = live
            if sig.entry_type != "limit":
                sig.entry_type = "market"
        else:
            return sig

    provider_tps = list(sig.take_profits or [])
    provider_sl_missing = sig.stop_loss is None or sig.stop_loss <= 0

    # --- improvise SL from ATR if missing ---
    if provider_sl_missing:
        atr_pct = _f(metrics.get("atr_pct"), 0.0)
        sl_pct = atr_pct * _SL_ATR_MULT if atr_pct > 0 else 2.0
        sl_pct = max(_SL_MIN_PCT, min(sl_pct, _SL_MAX_PCT))
        dist = entry * sl_pct / 100.0
        sig.stop_loss = entry - dist if is_long else entry + dist
        sig.sl_source = "improvised_atr"

    # --- clean junk take-profits (side + distance band) ---
    clean: List[float] = []
    for t in (sig.take_profits or []):
        t = _f(t)
        if t <= 0:
            continue
        side_ok = (t > entry) if is_long else (t < entry)
        if not side_ok:
            continue
        dist = abs(t - entry) / entry
        if _TP_MIN_DIST <= dist <= _TP_MAX_DIST:
            clean.append(t)
        else:
            sig.level_anomalies.append(f"PROVIDER_TP_OUTLIER:{t:g}")

    # --- improvise TP ladder from SL (1R/2R/3R) if none reliable ---
    if not clean and sig.stop_loss and sig.stop_loss > 0:
        r = abs(entry - sig.stop_loss)
        if r > 0:
            if is_long:
                clean = [entry + m * r for m in _R_MULTIPLES]
            else:
                clean = [entry - m * r for m in _R_MULTIPLES]
            sig.tp_source = "improvised_R"

    if provider_sl_missing and provider_tps and sig.tp_source == "improvised_R":
        sig.level_anomalies.append("MALFORMED_PROVIDER_LEVELS")

    # keep ordering consistent with side (TP1 nearest entry)
    if clean:
        sig.take_profits = sorted(clean, reverse=not is_long)

    return sig
