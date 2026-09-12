from __future__ import annotations

import asyncio
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from utils.logger import logger
from utils.persistence_paths import (
    JOURNAL_DIR,
    TRADE_HISTORY_CSV_PATH,
    TRADE_HISTORY_JSON_PATH,
    atomic_write_json,
    load_json,
)

JOURNAL_STOP = {"__journal_stop__": True}
MAX_HISTORY_ROWS = 5000

CSV_COLUMNS = [
    "timestamp_open",
    "timestamp_close",
    "symbol",
    "side",
    "entry_price",
    "exit_price",
    "pnl_pct",
    "pnl_usd",
    "reason",
    "raw_reason",
    "normalized_reason",
    "hold_minutes",
    "partial_pct",
    "is_partial",
    "balance_after",
    "equity",
    "regime",
    "quadrant",
    "priority_score",
    "score",
    "confidence",
    "notional",
    "qty_closed",
    "tp_original",
    "sl_original",
    "active_sl_at_exit",
    "sl_kind_at_exit",
    "active_sl_reason",
    "planned_risk_amount",
    "actual_risk_amount",
    "risk_amount",
    "cvd",
    "oi_15m_pct",
    "oi_1h_pct",
    "funding_pct",
    "poc_price",
    "vol_ratio",
    "rsi",
    "management_mode",
    "phase1_partial_done",
    "phase1_htf_bias",
    "thesis_health_status",
    "open_notification_sent",
    "is_vip",
    "vip_status",
    "vip_directional_bias",
    "accumulation_score",
    "accumulation_sources",
    "vip_trigger_ready",
    "entry_quality",
    "context_score",
    "auction_state",
    "structure_bias",
    "entry_quality_class",
    "entry_quality_score",
    "entry_quality_reason",
    "entry_quality_shadow_action",
    "entry_quality_would_allow",
    "thesis_class",
    "thesis_strength",
    "thesis_confidence",
    "thesis_warning_flags",
    "thesis_penalty_notes",
    "thesis_summary",
    "smc_clarity_bucket",
    "smc_stack_score",
    "smc_stack_reasons",
    "entry_location_score",
    "entry_structure_quality_score",
    "entry_zone_class",
    "entry_structure_bucket",
    "liquidity_sweep_detected",
    "structure_shift_detected",
    "valid_retest_detected",
    "valid_fvg_detected",
    "pd_location",
    "btc_permission",
    "btc_conflict",
    "btc_alignment",
    "flow_permission",
    "cvd_status",
    "oi_status",
    "funding_status",
    "volume_status",
    "cvd_value",
    "flow_oi_15m_pct",
    "flow_oi_1h_pct",
    "funding_rate",
    "volume_ratio",
    "entry_family_smc_family",
    "entry_family_btc_weight",
    "entry_family_bucket",
    "entry_family_permission",
    "entry_family_prune_candidate",
    "entry_family_watch_candidate",
    "protection_permission",
    "symbol_cooldown_active",
    "mode_cooldown_active",
    "loss_cluster_active",
    "profit_protection_state",
    "smc_has_liquidity_sweep",
    "smc_has_structure_shift",
    "smc_has_valid_retest",
    "smc_has_valid_fvg",
    "smc_entry_zone_class",
    "smc_entry_location_score",
    "smc_entry_structure_bucket",
    "smc_entry_structure_quality_score",
    "smc_entry_structure_reasons",
    "smc_structure_bias",
    "smc_auction_state",
    "smc_last_bos_dir",
    "smc_last_choch_dir",
    "smc_last_sweep_dir",
    "smc_inside_value_area",
    "smc_near_demand_zone",
    "smc_near_supply_zone",
    "btc_context",
    "btc_weight",
    "btc_weight_label",
    "btc_corr_value",
    "btc_corr_strength",
    "btc_corr_regime_note",
    "btc_decoupled",
    "btc_corr_quality_ok",
    "btc_gate_notes",
    "btc_soft_bias_reason",
    "btc_bias",
    "btc_bias_macro",
    "btc_regime",
    "btc_atr_pct",
    "btc_vol_ratio",
    "global_state",
    "global_confidence",
    "global_breadth_sentiment",
    "global_risk_off",
    "global_low_energy",
    "runtime_stage",
    "h1_zone_side",
    "h1_location_score",
    "h1_map_ready",
    "h1_location_grade",
    "h1_pullback_ready",
    "h1_rally_ready",
    "h1_channel_edge",
    "h1_sr_alignment",
    "h1_bias_alignment",
    "h1_near_prev_high",
    "h1_near_prev_low",
    "h1_near_ema21",
    "h1_near_poc",
    "h1_inside_value_area",
    "h1_edge_score",
    "h1_near_trendline",
    "h1_trendline_side",
    "h1_ema21_side",
    "h1_price_vs_poc",
    "h1_prev_high",
    "h1_prev_low",
    "h1_ema21",
    "h1_distance_to_prev_high_pct",
    "h1_distance_to_prev_low_pct",
    "h1_distance_to_ema21_pct",
    "h1_location_notes",
    "planned_entry_price",
    "actual_fill_price",
    "market_price_at_open",
    "entry_fill_source",
    "entry_integrity_status",
    "planned_tp1_price",
    "planned_tp_full_price",
    "planned_sl_price",
    "entry_price_drift_abs",
    "entry_price_drift_pct",
    "tp1_already_touched_at_open",
    "tp_full_already_touched_at_open",
    "sl_already_touched_at_open",
    "target_already_hit_at_open",
    "tp_distance_pct_at_plan",
    "tp_full_distance_pct_at_plan",
    "sl_distance_pct_at_plan",
    "target_overrun_pct",
    "realized_speed_pct_per_min",
    "exit_chain_type",
    "partial_then_full_same_open",
    "time_to_first_exit_seconds",
    "time_to_final_exit_seconds",
    "ultra_fast_audit_flag",
    "ultra_fast_peer_bucket",
    "signal_source",
    "source_chat_id",
    "signal_id",
    "signal_entry_low",
    "signal_entry_high",
    "signal_active_entry",
    "signal_entry_type",
    "signal_timeframe",
    "signal_leverage",
    "signal_tp_ladder",
    "signal_raw_text",
    "h1_location_map_json",
    "phase1_profile_json",
    "market_context_json",
    "adv_snapshot_json",
    "smc_snapshot_json",
    "btc_context_snapshot_json",
    "global_context_snapshot_json",
    "thesis_json",
]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, "", "None"):
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, "", "None"):
            return default
        return int(float(value))
    except Exception:
        return default


def _safe_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
    try:
        return bool(value)
    except Exception:
        return default


def _safe_str(value: Any, default: str = "") -> str:
    try:
        if value is None:
            return default
        text = str(value).strip()
        return text if text else default
    except Exception:
        return default


def _safe_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _safe_list_str(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if "|" in text:
            return [item.strip() for item in text.split("|") if item.strip()]
        if "," in text:
            return [item.strip() for item in text.split(",") if item.strip()]
        return [text]
    if isinstance(value, dict):
        out: list[str] = []
        for key, item in value.items():
            key_text = _safe_str(key)
            item_text = _safe_str(item)
            if key_text and item_text:
                out.append(f"{key_text}:{item_text}")
            elif key_text:
                out.append(key_text)
        return out
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            text = _safe_str(item)
            if text:
                out.append(text)
        return out
    return []


def _json_dumps_safe(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return json.dumps(str(value), ensure_ascii=False)


def _normalize_btc_context(raw: Any) -> str:
    text = _safe_str(raw, "NEUTRAL").upper()
    if "BEAR" in text:
        return "BEAR_CONFIRMED"
    if "BULL" in text:
        return "BULL_CONFIRMED"
    return "NEUTRAL"


def _normalize_btc_weight_label(explicit: Any, weight: float) -> str:
    raw = _safe_str(explicit, "").upper()
    if raw:
        mapping = {
            "FOLLOWING_BTC": "FOLLOWING_BTC",
            "FOLLOWING": "FOLLOWING_BTC",
            "PARTIAL_DECOUPLE": "PARTIAL_DECOUPLE",
            "PARTIAL_DECOUPLED": "PARTIAL_DECOUPLE",
            "DECOUPLED": "DECOUPLED",
            "NEGATIVE_RELATION": "DECOUPLED",
            "SELF_BTC": "FOLLOWING_BTC",
            "BTC_CORR_UNAVAILABLE": "NEUTRAL",
            "UNKNOWN": "NEUTRAL",
        }
        return mapping.get(raw, raw)

    if weight >= 0.70:
        return "FOLLOWING_BTC"
    if weight >= 0.40:
        return "PARTIAL_DECOUPLE"
    if weight <= 0.15:
        return "DECOUPLED"
    return "NEUTRAL"


def _normalize_btc_weight_bucket(weight: float) -> str:
    if weight <= 0.15:
        return "W015"
    if weight <= 0.40:
        return "W040"
    if weight <= 0.70:
        return "W070"
    return "W100"


CONFLUENCE_OBSERVABILITY_DEFAULTS: Dict[str, Any] = {
    "entry_quality_class": "UNKNOWN",
    "entry_quality_score": 0.0,
    "entry_quality_reason": "",
    "entry_quality_shadow_action": "OBSERVE_ONLY",
    "entry_quality_would_allow": 1,
    "thesis_class": "UNKNOWN",
    "thesis_strength": 0.0,
    "thesis_confidence": 0.0,
    "thesis_warning_flags": [],
    "thesis_penalty_notes": [],
    "thesis_summary": "",
    "smc_clarity_bucket": "UNKNOWN",
    "smc_stack_score": 0.0,
    "smc_stack_reasons": [],
    "entry_location_score": 0.0,
    "entry_structure_quality_score": 0.0,
    "entry_zone_class": "UNKNOWN",
    "entry_structure_bucket": "UNKNOWN",
    "liquidity_sweep_detected": 0,
    "structure_shift_detected": 0,
    "valid_retest_detected": 0,
    "valid_fvg_detected": 0,
    "pd_location": "UNKNOWN",
    "btc_permission": "UNKNOWN",
    "btc_conflict": 0,
    "btc_alignment": "UNKNOWN",
    "flow_permission": "UNKNOWN",
    "cvd_status": "UNKNOWN",
    "oi_status": "UNKNOWN",
    "funding_status": "UNKNOWN",
    "volume_status": "UNKNOWN",
    "cvd_value": 0.0,
    "flow_oi_15m_pct": 0.0,
    "flow_oi_1h_pct": 0.0,
    "funding_rate": 0.0,
    "volume_ratio": 0.0,
    "entry_family_smc_family": "UNKNOWN",
    "entry_family_btc_weight": "UNKNOWN",
    "entry_family_bucket": "UNKNOWN",
    "entry_family_permission": "UNKNOWN",
    "entry_family_prune_candidate": 0,
    "entry_family_watch_candidate": 0,
    "protection_permission": "UNKNOWN",
    "symbol_cooldown_active": 0,
    "mode_cooldown_active": 0,
    "loss_cluster_active": 0,
    "profit_protection_state": "UNKNOWN",
}


def _first_non_missing(*values: Any, default: Any = None) -> Any:
    for value in values:
        if not _is_missing(value):
            return value
    return default


def _resolve_confluence_observability(
    *,
    trade: Dict[str, Any],
    phase1_profile: Dict[str, Any],
    market_context: Dict[str, Any],
    smc_snapshot: Dict[str, Any],
    btc_context_snapshot: Dict[str, Any],
) -> Dict[str, Any]:
    """Resolve observability fields for CSV/JSON without changing bot behavior."""
    out = dict(CONFLUENCE_OBSERVABILITY_DEFAULTS)

    for key, default in CONFLUENCE_OBSERVABILITY_DEFAULTS.items():
        out[key] = _first_non_missing(
            trade.get(key),
            phase1_profile.get(key),
            market_context.get(key),
            default=default,
        )

    out["entry_quality_class"] = _safe_str(out.get("entry_quality_class"), "UNKNOWN").upper()
    out["entry_quality_score"] = _safe_float(out.get("entry_quality_score"), 0.0)
    out["entry_quality_would_allow"] = _bool_to_int(_safe_bool(out.get("entry_quality_would_allow"), True))

    out["thesis_class"] = _safe_str(out.get("thesis_class"), "UNKNOWN").upper()
    out["thesis_strength"] = _safe_float(out.get("thesis_strength"), 0.0)
    out["thesis_confidence"] = _safe_float(out.get("thesis_confidence"), 0.0)
    out["thesis_warning_flags"] = _safe_list_str(out.get("thesis_warning_flags"))
    out["thesis_penalty_notes"] = _safe_list_str(out.get("thesis_penalty_notes"))

    out["smc_clarity_bucket"] = _safe_str(
        _first_non_missing(
            out.get("smc_clarity_bucket"),
            phase1_profile.get("entry_family_smc_family"),
            smc_snapshot.get("smc_clarity_bucket"),
            default="UNKNOWN",
        ),
        "UNKNOWN",
    ).upper()
    out["smc_stack_score"] = _safe_float(out.get("smc_stack_score"), 0.0)
    out["smc_stack_reasons"] = _safe_list_str(out.get("smc_stack_reasons"))
    out["entry_location_score"] = _safe_float(
        _first_non_missing(out.get("entry_location_score"), smc_snapshot.get("entry_location_score"), trade.get("smc_entry_location_score"), default=0.0),
        0.0,
    )
    out["entry_structure_quality_score"] = _safe_float(
        _first_non_missing(out.get("entry_structure_quality_score"), smc_snapshot.get("entry_structure_quality_score"), trade.get("smc_entry_structure_quality_score"), default=0.0),
        0.0,
    )
    out["entry_zone_class"] = _safe_str(
        _first_non_missing(out.get("entry_zone_class"), smc_snapshot.get("entry_zone_class"), trade.get("smc_entry_zone_class"), default="UNKNOWN"),
        "UNKNOWN",
    ).upper()
    out["entry_structure_bucket"] = _safe_str(
        _first_non_missing(out.get("entry_structure_bucket"), smc_snapshot.get("entry_structure_bucket"), trade.get("smc_entry_structure_bucket"), default="UNKNOWN"),
        "UNKNOWN",
    ).upper()
    out["liquidity_sweep_detected"] = _bool_to_int(_safe_bool(_first_non_missing(out.get("liquidity_sweep_detected"), smc_snapshot.get("has_liquidity_sweep"), trade.get("smc_has_liquidity_sweep"), default=False)))
    out["structure_shift_detected"] = _bool_to_int(_safe_bool(_first_non_missing(out.get("structure_shift_detected"), smc_snapshot.get("has_structure_shift"), trade.get("smc_has_structure_shift"), default=False)))
    out["valid_retest_detected"] = _bool_to_int(_safe_bool(_first_non_missing(out.get("valid_retest_detected"), smc_snapshot.get("has_valid_retest"), trade.get("smc_has_valid_retest"), default=False)))
    out["valid_fvg_detected"] = _bool_to_int(_safe_bool(_first_non_missing(out.get("valid_fvg_detected"), smc_snapshot.get("has_valid_fvg"), trade.get("smc_has_valid_fvg"), default=False)))
    out["pd_location"] = _safe_str(out.get("pd_location"), out["entry_zone_class"]).upper()

    out["btc_permission"] = _safe_str(out.get("btc_permission"), "UNKNOWN").upper()
    out["btc_conflict"] = _bool_to_int(_safe_bool(out.get("btc_conflict"), False))
    out["btc_alignment"] = _safe_str(out.get("btc_alignment"), "UNKNOWN").upper()

    out["flow_permission"] = _safe_str(out.get("flow_permission"), "UNKNOWN").upper()
    out["cvd_status"] = _safe_str(out.get("cvd_status"), "UNKNOWN").upper()
    out["oi_status"] = _safe_str(out.get("oi_status"), "UNKNOWN").upper()
    out["funding_status"] = _safe_str(out.get("funding_status"), "UNKNOWN").upper()
    out["volume_status"] = _safe_str(out.get("volume_status"), "UNKNOWN").upper()
    out["cvd_value"] = _safe_float(_first_non_missing(out.get("cvd_value"), trade.get("cvd"), default=0.0), 0.0)
    out["flow_oi_15m_pct"] = _safe_float(_first_non_missing(out.get("flow_oi_15m_pct"), trade.get("oi_15m_pct"), default=0.0), 0.0)
    out["flow_oi_1h_pct"] = _safe_float(_first_non_missing(out.get("flow_oi_1h_pct"), trade.get("oi_1h_pct"), default=0.0), 0.0)
    out["funding_rate"] = _safe_float(_first_non_missing(out.get("funding_rate"), trade.get("funding_pct"), default=0.0), 0.0)
    out["volume_ratio"] = _safe_float(_first_non_missing(out.get("volume_ratio"), trade.get("vol_ratio"), default=0.0), 0.0)

    out["entry_family_smc_family"] = _safe_str(
        _first_non_missing(out.get("entry_family_smc_family"), out.get("smc_clarity_bucket"), phase1_profile.get("entry_family_smc_family"), default="UNKNOWN"),
        "UNKNOWN",
    ).upper()
    out["entry_family_btc_weight"] = _safe_str(
        _first_non_missing(out.get("entry_family_btc_weight"), phase1_profile.get("entry_family_btc_weight_norm"), trade.get("btc_weight"), btc_context_snapshot.get("btc_weight"), default="UNKNOWN"),
        "UNKNOWN",
    )
    out["entry_family_bucket"] = _safe_str(
        _first_non_missing(out.get("entry_family_bucket"), phase1_profile.get("entry_family_bucket"), default="UNKNOWN"),
        "UNKNOWN",
    ).upper()
    out["entry_family_permission"] = _safe_str(out.get("entry_family_permission"), "UNKNOWN").upper()
    out["entry_family_prune_candidate"] = _bool_to_int(_safe_bool(out.get("entry_family_prune_candidate"), False))
    out["entry_family_watch_candidate"] = _bool_to_int(_safe_bool(out.get("entry_family_watch_candidate"), False))

    out["protection_permission"] = _safe_str(out.get("protection_permission"), "UNKNOWN").upper()
    out["symbol_cooldown_active"] = _bool_to_int(_safe_bool(out.get("symbol_cooldown_active"), False))
    out["mode_cooldown_active"] = _bool_to_int(_safe_bool(out.get("mode_cooldown_active"), False))
    out["loss_cluster_active"] = _bool_to_int(_safe_bool(out.get("loss_cluster_active"), False))
    out["profit_protection_state"] = _safe_str(out.get("profit_protection_state"), "UNKNOWN").upper()

    return out

def _is_missing(value: Any) -> bool:
    return value in (None, "", "None")


def _bool_to_int(value: bool) -> int:
    return 1 if bool(value) else 0


def _derive_exit_chain_type(trade: Dict[str, Any]) -> str:
    explicit = _safe_str(trade.get("exit_chain_type"), "").upper()
    if explicit:
        return explicit
    reason = _safe_str(trade.get("reason", trade.get("normalized_reason", trade.get("raw_reason", ""))), "").upper()
    is_partial = _safe_bool(trade.get("is_partial"), False) or _safe_bool(trade.get("partial_then_full_same_open"), False)
    if reason == "TP_FULL" and not is_partial:
        return "FULL_DIRECT"
    if reason in {"SL", "HARD_SL", "STOP_LOSS", "STOPLOSS", "FORCED_SL", "DEFENSIVE_STOP", "DYNAMIC_SL"} and not is_partial:
        return "SL_DIRECT"
    if reason == "PARTIAL_TP1" or is_partial:
        return "OTHER"
    return "OTHER"


def _build_ultra_fast_peer_bucket(normalized: Dict[str, Any]) -> str:
    mode = _safe_str(normalized.get("management_mode"), "UNKNOWN").upper()
    regime = _safe_str(normalized.get("regime"), "UNKNOWN").upper()
    side = _safe_str(normalized.get("side"), "UNKNOWN").upper()
    weight = _safe_float(normalized.get("btc_weight"), 0.50)
    return f"{mode}|{regime}|{side}|{_normalize_btc_weight_bucket(weight)}"


def _derive_execution_integrity_fields(trade: Dict[str, Any], phase1_profile: Dict[str, Any]) -> Dict[str, Any]:
    side = _safe_str(trade.get("side"), "UNKNOWN").upper()

    planned_entry_raw = trade.get("planned_entry_price")
    planned_entry = _safe_float(
        planned_entry_raw,
        _safe_float(trade.get("entry_price"), 0.0),
    )

    actual_fill_raw = trade.get("actual_fill_price", trade.get("fill_price", trade.get("executed_entry_price")))
    actual_fill = _safe_float(actual_fill_raw, planned_entry)

    market_open_raw = trade.get("market_price_at_open", trade.get("entry_market_price", trade.get("price_at_open")))
    market_at_open = _safe_float(market_open_raw, actual_fill if actual_fill > 0 else planned_entry)

    tp1_raw = trade.get("planned_tp1_price", phase1_profile.get("partial_tp", trade.get("tp1_price", trade.get("tp_original"))))
    tp_full_raw = trade.get("planned_tp_full_price", phase1_profile.get("full_tp", trade.get("tp_full_price", trade.get("tp_original"))))
    sl_raw = trade.get("planned_sl_price", trade.get("sl_original", trade.get("stop_price")))

    planned_tp1 = _safe_float(tp1_raw, 0.0)
    planned_tp_full = _safe_float(tp_full_raw, _safe_float(trade.get("tp_original"), 0.0))
    planned_sl = _safe_float(sl_raw, 0.0)
    exit_price = _safe_float(trade.get("exit_price"), 0.0)
    hold_minutes = _safe_float(trade.get("hold_minutes"), 0.0)

    if side == "SHORT":
        tp1_hit = bool(planned_tp1 > 0 and market_at_open > 0 and market_at_open <= planned_tp1)
        tp_full_hit = bool(planned_tp_full > 0 and market_at_open > 0 and market_at_open <= planned_tp_full)
        sl_hit = bool(planned_sl > 0 and market_at_open > 0 and market_at_open >= planned_sl)
        target_overrun = max(0.0, (planned_tp_full - exit_price) / max(abs(actual_fill), 1e-9)) if planned_tp_full > 0 and exit_price > 0 else 0.0
    elif side == "LONG":
        tp1_hit = bool(planned_tp1 > 0 and market_at_open > 0 and market_at_open >= planned_tp1)
        tp_full_hit = bool(planned_tp_full > 0 and market_at_open > 0 and market_at_open >= planned_tp_full)
        sl_hit = bool(planned_sl > 0 and market_at_open > 0 and market_at_open <= planned_sl)
        target_overrun = max(0.0, (exit_price - planned_tp_full) / max(abs(actual_fill), 1e-9)) if planned_tp_full > 0 and exit_price > 0 else 0.0
    else:
        tp1_hit = False
        tp_full_hit = False
        sl_hit = False
        target_overrun = 0.0

    drift_abs = abs(actual_fill - planned_entry) if actual_fill > 0 and planned_entry > 0 else 0.0
    drift_pct = drift_abs / max(abs(planned_entry), 1e-9) if planned_entry > 0 else 0.0
    tp1_distance = abs(planned_entry - planned_tp1) / max(abs(planned_entry), 1e-9) if planned_entry > 0 and planned_tp1 > 0 else 0.0
    tp_full_distance = abs(planned_entry - planned_tp_full) / max(abs(planned_entry), 1e-9) if planned_entry > 0 and planned_tp_full > 0 else 0.0
    sl_distance = abs(planned_entry - planned_sl) / max(abs(planned_entry), 1e-9) if planned_entry > 0 and planned_sl > 0 else 0.0
    realized_speed = (abs(exit_price - actual_fill) / max(abs(actual_fill), 1e-9)) / max(hold_minutes, 1e-9) if exit_price > 0 and actual_fill > 0 and hold_minutes > 0 else 0.0

    fill_source = _safe_str(trade.get("entry_fill_source"), "")
    if not fill_source:
        fill_source = "PLANNED_FALLBACK" if _is_missing(actual_fill_raw) else "ACTUAL_FILL"

    explicit_status = _safe_str(trade.get("entry_integrity_status"), "").upper()
    if explicit_status:
        integrity_status = explicit_status
    elif tp1_hit or tp_full_hit:
        integrity_status = "STALE"
    elif drift_pct >= 0.03:
        integrity_status = "FILL_MISMATCH"
    elif not _is_missing(actual_fill_raw):
        integrity_status = "OK"
    else:
        integrity_status = "UNKNOWN"

    exit_chain_type = _derive_exit_chain_type(trade)
    partial_then_full_same_open = _safe_bool(trade.get("partial_then_full_same_open"), False)
    time_to_first_exit_seconds = _safe_float(trade.get("time_to_first_exit_seconds"), hold_minutes * 60.0 if _safe_str(trade.get("reason"), "").upper() == "PARTIAL_TP1" else 0.0)
    time_to_final_exit_seconds = _safe_float(trade.get("time_to_final_exit_seconds"), hold_minutes * 60.0 if exit_price > 0 else 0.0)

    reason = _safe_str(trade.get("reason", trade.get("normalized_reason", trade.get("raw_reason", ""))), "").upper()
    ultra_fast_flag = bool((hold_minutes < 1.0 and reason in {"PARTIAL_TP1", "TP_FULL"}) or (exit_chain_type == "PARTIAL_THEN_FULL" and hold_minutes < 1.5))

    return {
        "planned_entry_price": planned_entry,
        "actual_fill_price": actual_fill,
        "market_price_at_open": market_at_open,
        "entry_fill_source": fill_source,
        "entry_integrity_status": integrity_status,
        "planned_tp1_price": planned_tp1,
        "planned_tp_full_price": planned_tp_full,
        "planned_sl_price": planned_sl,
        "entry_price_drift_abs": drift_abs,
        "entry_price_drift_pct": drift_pct,
        "tp1_already_touched_at_open": tp1_hit,
        "tp_full_already_touched_at_open": tp_full_hit,
        "sl_already_touched_at_open": sl_hit,
        "target_already_hit_at_open": bool(tp1_hit or tp_full_hit),
        "tp_distance_pct_at_plan": tp1_distance,
        "tp_full_distance_pct_at_plan": tp_full_distance,
        "sl_distance_pct_at_plan": sl_distance,
        "target_overrun_pct": target_overrun,
        "realized_speed_pct_per_min": realized_speed,
        "exit_chain_type": exit_chain_type,
        "partial_then_full_same_open": partial_then_full_same_open,
        "time_to_first_exit_seconds": time_to_first_exit_seconds,
        "time_to_final_exit_seconds": time_to_final_exit_seconds,
        "ultra_fast_audit_flag": ultra_fast_flag,
    }


def _normalize_trade_record(row: Dict[str, Any]) -> Dict[str, Any]:
    trade = dict(row or {})
    adv_snapshot = _safe_dict(trade.get("adv_snapshot"))
    market_context = _safe_dict(trade.get("market_context"))
    phase1_profile = _safe_dict(trade.get("phase1_profile"))
    smc_existing = _safe_dict(trade.get("smc_snapshot"))
    btc_existing = _safe_dict(trade.get("btc_context_snapshot"))
    global_existing = _safe_dict(trade.get("global_context_snapshot"))

    btc_weight = _safe_float(
        trade.get(
            "btc_weight",
            btc_existing.get(
                "btc_weight",
                market_context.get("btc_weight", market_context.get("btc_influence_weight", 0.50)),
            ),
        ),
        0.50,
    )
    btc_context = _normalize_btc_context(
        trade.get(
            "btc_context",
            btc_existing.get(
                "btc_context",
                market_context.get(
                    "btc_fast_state",
                    market_context.get(
                        "btc_context_state",
                        market_context.get("btc_bias_macro", market_context.get("btc_bias", "NEUTRAL")),
                    ),
                ),
            ),
        )
    )
    btc_weight_label = _normalize_btc_weight_label(
        trade.get(
            "btc_weight_label",
            btc_existing.get(
                "btc_weight_label",
                market_context.get("btc_weight_label", market_context.get("btc_corr_regime_note", "")),
            ),
        ),
        btc_weight,
    )

    smc_snapshot = {
        "has_liquidity_sweep": _safe_bool(
            trade.get("smc_has_liquidity_sweep", smc_existing.get("has_liquidity_sweep", market_context.get("has_liquidity_sweep", False)))
        ),
        "has_structure_shift": _safe_bool(
            trade.get("smc_has_structure_shift", smc_existing.get("has_structure_shift", market_context.get("has_structure_shift", False)))
        ),
        "has_valid_retest": _safe_bool(
            trade.get("smc_has_valid_retest", smc_existing.get("has_valid_retest", market_context.get("has_valid_retest", False)))
        ),
        "has_valid_fvg": _safe_bool(
            trade.get("smc_has_valid_fvg", smc_existing.get("has_valid_fvg", market_context.get("has_valid_fvg", False)))
        ),
        "entry_zone_class": _safe_str(
            trade.get("smc_entry_zone_class", smc_existing.get("entry_zone_class", market_context.get("entry_zone_class", "NONE"))),
            "NONE",
        ).upper(),
        "entry_location_score": _safe_float(
            trade.get("smc_entry_location_score", smc_existing.get("entry_location_score", market_context.get("entry_location_score", 0.0)))
        ),
        "entry_structure_bucket": _safe_str(
            trade.get(
                "smc_entry_structure_bucket",
                smc_existing.get("entry_structure_bucket", market_context.get("entry_structure_bucket", "UNKNOWN")),
            ),
            "UNKNOWN",
        ).upper(),
        "entry_structure_quality_score": _safe_float(
            trade.get(
                "smc_entry_structure_quality_score",
                smc_existing.get("entry_structure_quality_score", market_context.get("entry_structure_quality_score", 0.0)),
            )
        ),
        "entry_structure_reasons": _safe_list_str(
            trade.get(
                "smc_entry_structure_reasons",
                smc_existing.get("entry_structure_reasons", market_context.get("entry_structure_reasons", [])),
            )
        ),
        "structure_bias": _safe_str(
            trade.get("smc_structure_bias", smc_existing.get("structure_bias", trade.get("structure_bias", market_context.get("structure_bias", "NEUTRAL")))),
            "NEUTRAL",
        ).upper(),
        "auction_state": _safe_str(
            trade.get("smc_auction_state", smc_existing.get("auction_state", trade.get("auction_state", market_context.get("auction_state", "UNKNOWN")))),
            "UNKNOWN",
        ).upper(),
        "last_bos_dir": _safe_str(
            trade.get("smc_last_bos_dir", smc_existing.get("last_bos_dir", market_context.get("last_bos_dir", "NONE"))),
            "NONE",
        ).upper(),
        "last_choch_dir": _safe_str(
            trade.get("smc_last_choch_dir", smc_existing.get("last_choch_dir", market_context.get("last_choch_dir", "NONE"))),
            "NONE",
        ).upper(),
        "last_sweep_dir": _safe_str(
            trade.get("smc_last_sweep_dir", smc_existing.get("last_sweep_dir", market_context.get("last_sweep_dir", "NONE"))),
            "NONE",
        ).upper(),
        "inside_value_area": _safe_bool(
            trade.get("smc_inside_value_area", smc_existing.get("inside_value_area", market_context.get("inside_value_area", False)))
        ),
        "near_demand_zone": _safe_bool(
            trade.get("smc_near_demand_zone", smc_existing.get("near_demand_zone", market_context.get("near_demand_zone", False)))
        ),
        "near_supply_zone": _safe_bool(
            trade.get("smc_near_supply_zone", smc_existing.get("near_supply_zone", market_context.get("near_supply_zone", False)))
        ),
    }

    btc_context_snapshot = {
        "btc_context": btc_context,
        "btc_weight": btc_weight,
        "btc_weight_label": btc_weight_label,
        "btc_corr_value": _safe_float(
            trade.get("btc_corr_value", btc_existing.get("btc_corr_value", market_context.get("btc_corr_15m_20", 0.0)))
        ),
        "btc_corr_strength": _safe_str(
            trade.get("btc_corr_strength", btc_existing.get("btc_corr_strength", market_context.get("btc_corr_strength", "UNKNOWN"))),
            "UNKNOWN",
        ).upper(),
        "btc_corr_regime_note": _safe_str(
            trade.get(
                "btc_corr_regime_note",
                btc_existing.get("btc_corr_regime_note", market_context.get("btc_corr_regime_note", "")),
            ),
            "",
        ).upper(),
        "btc_decoupled": _safe_bool(
            trade.get("btc_decoupled", btc_existing.get("btc_decoupled", market_context.get("btc_decoupled", False)))
        ),
        "btc_corr_quality_ok": _safe_bool(
            trade.get(
                "btc_corr_quality_ok",
                btc_existing.get("btc_corr_quality_ok", market_context.get("btc_corr_quality_ok", False)),
            )
        ),
        "btc_gate_notes": _safe_list_str(
            trade.get("btc_gate_notes", btc_existing.get("btc_gate_notes", market_context.get("btc_gate_notes", [])))
        ),
        "btc_soft_bias_reason": _safe_str(
            trade.get(
                "btc_soft_bias_reason",
                btc_existing.get("btc_soft_bias_reason", market_context.get("btc_soft_bias_reason", "")),
            ),
            "",
        ).upper(),
        "btc_bias": _safe_str(
            trade.get("btc_bias", btc_existing.get("btc_bias", market_context.get("btc_bias", "NEUTRAL"))),
            "NEUTRAL",
        ).upper(),
        "btc_bias_macro": _safe_str(
            trade.get(
                "btc_bias_macro",
                btc_existing.get("btc_bias_macro", market_context.get("btc_bias_macro", "NEUTRAL")),
            ),
            "NEUTRAL",
        ).upper(),
        "btc_regime": _safe_str(
            trade.get(
                "btc_regime",
                btc_existing.get("btc_regime", market_context.get("btc_regime", adv_snapshot.get("btc_regime", "UNKNOWN"))),
            ),
            "UNKNOWN",
        ).upper(),
        "btc_atr_pct": _safe_float(
            trade.get(
                "btc_atr_pct",
                btc_existing.get("btc_atr_pct", market_context.get("btc_atr_pct", adv_snapshot.get("btc_atr_pct", 0.0))),
            )
        ),
        "btc_vol_ratio": _safe_float(
            trade.get(
                "btc_vol_ratio",
                btc_existing.get("btc_vol_ratio", market_context.get("btc_vol_ratio", adv_snapshot.get("btc_vol_ratio", 0.0))),
            )
        ),
    }

    global_context_snapshot = {
        "global_state": _safe_str(
            trade.get(
                "global_state",
                global_existing.get("global_state", market_context.get("global_state", market_context.get("state", "UNKNOWN"))),
            ),
            "UNKNOWN",
        ).upper(),
        "global_confidence": _safe_float(
            trade.get(
                "global_confidence",
                global_existing.get("global_confidence", market_context.get("global_confidence", market_context.get("confidence", 0.0))),
            )
        ),
        "global_breadth_sentiment": _safe_str(
            trade.get(
                "global_breadth_sentiment",
                global_existing.get(
                    "global_breadth_sentiment",
                    market_context.get("global_breadth_sentiment", market_context.get("breadth_sentiment", "NEUTRAL")),
                ),
            ),
            "NEUTRAL",
        ).upper(),
        "global_risk_off": _safe_bool(
            trade.get(
                "global_risk_off",
                global_existing.get("global_risk_off", market_context.get("global_risk_off", market_context.get("risk_off", False))),
            )
        ),
        "global_low_energy": _safe_bool(
            trade.get(
                "global_low_energy",
                global_existing.get("global_low_energy", market_context.get("global_low_energy", market_context.get("low_energy", False))),
            )
        ),
        "runtime_stage": _safe_str(
            trade.get(
                "runtime_stage",
                global_existing.get("runtime_stage", market_context.get("runtime_stage", market_context.get("stage", "UNKNOWN"))),
            ),
            "UNKNOWN",
        ).upper(),
    }

    normalized = dict(trade)
    normalized["phase1_profile"] = phase1_profile
    normalized["adv_snapshot"] = adv_snapshot
    normalized["market_context"] = market_context
    normalized["smc_snapshot"] = smc_snapshot
    normalized["btc_context_snapshot"] = btc_context_snapshot
    normalized["global_context_snapshot"] = global_context_snapshot

    normalized["entry_quality"] = _safe_str(trade.get("entry_quality"), "C")
    normalized["context_score"] = _safe_float(trade.get("context_score"), 0.0)
    normalized["auction_state"] = _safe_str(trade.get("auction_state", smc_snapshot["auction_state"]), "UNKNOWN").upper()
    normalized["structure_bias"] = _safe_str(trade.get("structure_bias", smc_snapshot["structure_bias"]), "NEUTRAL").upper()

    normalized.update(
        _resolve_confluence_observability(
            trade=trade,
            phase1_profile=phase1_profile,
            market_context=market_context,
            smc_snapshot=smc_snapshot,
            btc_context_snapshot=btc_context_snapshot,
        )
    )

    normalized["btc_context"] = btc_context_snapshot["btc_context"]
    normalized["btc_weight"] = btc_context_snapshot["btc_weight"]
    normalized["btc_weight_label"] = btc_context_snapshot["btc_weight_label"]
    normalized["btc_corr_value"] = btc_context_snapshot["btc_corr_value"]
    normalized["btc_corr_strength"] = btc_context_snapshot["btc_corr_strength"]
    normalized["btc_corr_regime_note"] = btc_context_snapshot["btc_corr_regime_note"]
    normalized["btc_decoupled"] = btc_context_snapshot["btc_decoupled"]
    normalized["btc_corr_quality_ok"] = btc_context_snapshot["btc_corr_quality_ok"]
    normalized["btc_gate_notes"] = btc_context_snapshot["btc_gate_notes"]
    normalized["btc_soft_bias_reason"] = btc_context_snapshot["btc_soft_bias_reason"]
    normalized["btc_bias"] = btc_context_snapshot["btc_bias"]
    normalized["btc_bias_macro"] = btc_context_snapshot["btc_bias_macro"]
    normalized["btc_regime"] = btc_context_snapshot["btc_regime"]
    normalized["btc_atr_pct"] = btc_context_snapshot["btc_atr_pct"]
    normalized["btc_vol_ratio"] = btc_context_snapshot["btc_vol_ratio"]

    normalized["smc_has_liquidity_sweep"] = smc_snapshot["has_liquidity_sweep"]
    normalized["smc_has_structure_shift"] = smc_snapshot["has_structure_shift"]
    normalized["smc_has_valid_retest"] = smc_snapshot["has_valid_retest"]
    normalized["smc_has_valid_fvg"] = smc_snapshot["has_valid_fvg"]
    normalized["smc_entry_zone_class"] = smc_snapshot["entry_zone_class"]
    normalized["smc_entry_location_score"] = smc_snapshot["entry_location_score"]
    normalized["smc_entry_structure_bucket"] = smc_snapshot["entry_structure_bucket"]
    normalized["smc_entry_structure_quality_score"] = smc_snapshot["entry_structure_quality_score"]
    normalized["smc_entry_structure_reasons"] = smc_snapshot["entry_structure_reasons"]
    normalized["smc_structure_bias"] = smc_snapshot["structure_bias"]
    normalized["smc_auction_state"] = smc_snapshot["auction_state"]
    normalized["smc_last_bos_dir"] = smc_snapshot["last_bos_dir"]
    normalized["smc_last_choch_dir"] = smc_snapshot["last_choch_dir"]
    normalized["smc_last_sweep_dir"] = smc_snapshot["last_sweep_dir"]
    normalized["smc_inside_value_area"] = smc_snapshot["inside_value_area"]
    normalized["smc_near_demand_zone"] = smc_snapshot["near_demand_zone"]
    normalized["smc_near_supply_zone"] = smc_snapshot["near_supply_zone"]

    normalized["global_state"] = global_context_snapshot["global_state"]
    normalized["global_confidence"] = global_context_snapshot["global_confidence"]
    normalized["global_breadth_sentiment"] = global_context_snapshot["global_breadth_sentiment"]
    normalized["global_risk_off"] = global_context_snapshot["global_risk_off"]
    normalized["global_low_energy"] = global_context_snapshot["global_low_energy"]
    normalized["runtime_stage"] = global_context_snapshot["runtime_stage"]

    h1_location_map = _safe_dict(trade.get("h1_location_map"))
    if not h1_location_map:
        h1_location_map = _safe_dict(market_context.get("h1_location_map"))

    normalized["h1_zone_side"] = _safe_str(
        trade.get("h1_zone_side", h1_location_map.get("h1_zone_side", market_context.get("h1_zone_side", "UNKNOWN"))),
        "UNKNOWN",
    ).upper()
    normalized["h1_location_score"] = _safe_float(
        trade.get("h1_location_score", h1_location_map.get("h1_location_score", market_context.get("h1_location_score", 0.0)))
    )
    normalized["h1_map_ready"] = _safe_bool(
        trade.get("h1_map_ready", h1_location_map.get("h1_map_ready", market_context.get("h1_map_ready", False)))
    )
    normalized["h1_location_grade"] = _safe_str(
        trade.get("h1_location_grade", h1_location_map.get("h1_location_grade", market_context.get("h1_location_grade", "D"))),
        "D",
    ).upper()
    normalized["h1_pullback_ready"] = _safe_bool(
        trade.get("h1_pullback_ready", h1_location_map.get("h1_pullback_ready", market_context.get("h1_pullback_ready", False)))
    )
    normalized["h1_rally_ready"] = _safe_bool(
        trade.get("h1_rally_ready", h1_location_map.get("h1_rally_ready", market_context.get("h1_rally_ready", False)))
    )
    normalized["h1_channel_edge"] = _safe_str(
        trade.get("h1_channel_edge", h1_location_map.get("h1_channel_edge", market_context.get("h1_channel_edge", "NONE"))),
        "NONE",
    ).upper()
    normalized["h1_sr_alignment"] = _safe_str(
        trade.get("h1_sr_alignment", h1_location_map.get("h1_sr_alignment", market_context.get("h1_sr_alignment", "BAD"))),
        "BAD",
    ).upper()
    normalized["h1_bias_alignment"] = _safe_str(
        trade.get("h1_bias_alignment", h1_location_map.get("h1_bias_alignment", market_context.get("h1_bias_alignment", "NEUTRAL"))),
        "NEUTRAL",
    ).upper()
    normalized["h1_near_prev_high"] = _safe_bool(
        trade.get("h1_near_prev_high", h1_location_map.get("h1_near_prev_high", market_context.get("h1_near_prev_high", False)))
    )
    normalized["h1_near_prev_low"] = _safe_bool(
        trade.get("h1_near_prev_low", h1_location_map.get("h1_near_prev_low", market_context.get("h1_near_prev_low", False)))
    )
    normalized["h1_near_ema21"] = _safe_bool(
        trade.get("h1_near_ema21", h1_location_map.get("h1_near_ema21", market_context.get("h1_near_ema21", False)))
    )
    normalized["h1_near_poc"] = _safe_bool(
        trade.get("h1_near_poc", h1_location_map.get("h1_near_poc", market_context.get("h1_near_poc", False)))
    )
    normalized["h1_inside_value_area"] = _safe_bool(
        trade.get("h1_inside_value_area", h1_location_map.get("h1_inside_value_area", market_context.get("h1_inside_value_area", False)))
    )
    normalized["h1_edge_score"] = _safe_float(
        trade.get("h1_edge_score", h1_location_map.get("h1_edge_score", market_context.get("h1_edge_score", 0.0)))
    )
    normalized["h1_near_trendline"] = _safe_bool(
        trade.get("h1_near_trendline", h1_location_map.get("h1_near_trendline", market_context.get("h1_near_trendline", False)))
    )
    normalized["h1_trendline_side"] = _safe_str(
        trade.get("h1_trendline_side", h1_location_map.get("h1_trendline_side", market_context.get("h1_trendline_side", "NONE"))),
        "NONE",
    ).upper()
    normalized["h1_ema21_side"] = _safe_str(
        trade.get("h1_ema21_side", h1_location_map.get("h1_ema21_side", market_context.get("h1_ema21_side", "MIXED"))),
        "MIXED",
    ).upper()
    normalized["h1_price_vs_poc"] = _safe_float(
        trade.get("h1_price_vs_poc", h1_location_map.get("h1_price_vs_poc", market_context.get("h1_price_vs_poc", 0.0)))
    )
    normalized["h1_prev_high"] = _safe_float(
        trade.get("h1_prev_high", h1_location_map.get("h1_prev_high", market_context.get("h1_prev_high", 0.0)))
    )
    normalized["h1_prev_low"] = _safe_float(
        trade.get("h1_prev_low", h1_location_map.get("h1_prev_low", market_context.get("h1_prev_low", 0.0)))
    )
    normalized["h1_ema21"] = _safe_float(
        trade.get("h1_ema21", h1_location_map.get("h1_ema21", market_context.get("h1_ema21", 0.0)))
    )
    normalized["h1_distance_to_prev_high_pct"] = _safe_float(
        trade.get("h1_distance_to_prev_high_pct", h1_location_map.get("h1_distance_to_prev_high_pct", market_context.get("h1_distance_to_prev_high_pct", 0.0)))
    )
    normalized["h1_distance_to_prev_low_pct"] = _safe_float(
        trade.get("h1_distance_to_prev_low_pct", h1_location_map.get("h1_distance_to_prev_low_pct", market_context.get("h1_distance_to_prev_low_pct", 0.0)))
    )
    normalized["h1_distance_to_ema21_pct"] = _safe_float(
        trade.get("h1_distance_to_ema21_pct", h1_location_map.get("h1_distance_to_ema21_pct", market_context.get("h1_distance_to_ema21_pct", 0.0)))
    )
    normalized["h1_location_notes"] = _safe_list_str(
        trade.get("h1_location_notes", h1_location_map.get("h1_location_notes", market_context.get("h1_location_notes", [])))
    )
    normalized.update(_derive_execution_integrity_fields(trade, phase1_profile))
    normalized["ultra_fast_peer_bucket"] = _build_ultra_fast_peer_bucket(normalized)

    normalized["h1_location_map"] = h1_location_map or {
        "h1_zone_side": normalized["h1_zone_side"],
        "h1_location_score": normalized["h1_location_score"],
        "h1_map_ready": normalized["h1_map_ready"],
        "h1_location_grade": normalized["h1_location_grade"],
        "h1_pullback_ready": normalized["h1_pullback_ready"],
        "h1_rally_ready": normalized["h1_rally_ready"],
        "h1_channel_edge": normalized["h1_channel_edge"],
        "h1_sr_alignment": normalized["h1_sr_alignment"],
        "h1_bias_alignment": normalized["h1_bias_alignment"],
        "h1_near_prev_high": normalized["h1_near_prev_high"],
        "h1_near_prev_low": normalized["h1_near_prev_low"],
        "h1_near_ema21": normalized["h1_near_ema21"],
        "h1_near_poc": normalized["h1_near_poc"],
        "h1_inside_value_area": normalized["h1_inside_value_area"],
        "h1_edge_score": normalized["h1_edge_score"],
        "h1_near_trendline": normalized["h1_near_trendline"],
        "h1_trendline_side": normalized["h1_trendline_side"],
        "h1_ema21_side": normalized["h1_ema21_side"],
        "h1_price_vs_poc": normalized["h1_price_vs_poc"],
        "h1_prev_high": normalized["h1_prev_high"],
        "h1_prev_low": normalized["h1_prev_low"],
        "h1_ema21": normalized["h1_ema21"],
        "h1_distance_to_prev_high_pct": normalized["h1_distance_to_prev_high_pct"],
        "h1_distance_to_prev_low_pct": normalized["h1_distance_to_prev_low_pct"],
        "h1_distance_to_ema21_pct": normalized["h1_distance_to_ema21_pct"],
        "h1_location_notes": normalized["h1_location_notes"],
    }

    # --- surface signal-copy audit fields to root for easy querying ---
    adv = trade.get("adv_snapshot") or {}
    for k in (
        "signal_source",
        "source_chat_id",
        "signal_id",
        "signal_entry_low",
        "signal_entry_high",
        "signal_active_entry",
        "signal_entry_type",
        "signal_timeframe",
        "signal_leverage",
        "signal_tp_ladder",
        "signal_raw_text",
    ):
        if adv.get(k) is not None:
            normalized[k] = adv[k]

    return normalized


def _flatten_trade_for_csv(normalized: Dict[str, Any]) -> Dict[str, Any]:
    phase1_profile = _safe_dict(normalized.get("phase1_profile"))
    adv_snapshot = _safe_dict(normalized.get("adv_snapshot"))
    market_context = _safe_dict(normalized.get("market_context"))
    smc_snapshot = _safe_dict(normalized.get("smc_snapshot"))
    btc_context_snapshot = _safe_dict(normalized.get("btc_context_snapshot"))
    global_context_snapshot = _safe_dict(normalized.get("global_context_snapshot"))

    flat: Dict[str, Any] = {key: "" for key in CSV_COLUMNS}
    for key in flat:
        if key in normalized:
            flat[key] = normalized.get(key)

    flat["accumulation_sources"] = "|".join(_safe_list_str(normalized.get("accumulation_sources")))
    flat["smc_entry_structure_reasons"] = "|".join(_safe_list_str(normalized.get("smc_entry_structure_reasons")))
    flat["btc_gate_notes"] = "|".join(_safe_list_str(normalized.get("btc_gate_notes")))
    flat["h1_location_notes"] = "|".join(_safe_list_str(normalized.get("h1_location_notes")))
    flat["thesis_warning_flags"] = "|".join(_safe_list_str(normalized.get("thesis_warning_flags")))
    flat["thesis_penalty_notes"] = "|".join(_safe_list_str(normalized.get("thesis_penalty_notes")))
    flat["smc_stack_reasons"] = "|".join(_safe_list_str(normalized.get("smc_stack_reasons")))
    flat["h1_location_map_json"] = _json_dumps_safe(_safe_dict(normalized.get("h1_location_map"))) if normalized.get("h1_location_map") else ""

    # Signal-copy audit fields live inside adv_snapshot — surface them as
    # first-class columns so channel-attribution/audit doesn't need JSON parsing.
    _sig_cols = (
        "signal_source", "source_chat_id", "signal_id", "signal_entry_low",
        "signal_entry_high", "signal_active_entry", "signal_entry_type",
        "signal_timeframe", "signal_leverage", "signal_tp_ladder", "signal_raw_text",
    )
    for _sc in _sig_cols:
        if not flat.get(_sc) and adv_snapshot.get(_sc) is not None:
            flat[_sc] = adv_snapshot.get(_sc)

    flat["phase1_profile_json"] = _json_dumps_safe(phase1_profile)
    flat["market_context_json"] = _json_dumps_safe(market_context)
    flat["adv_snapshot_json"] = _json_dumps_safe(adv_snapshot)
    flat["smc_snapshot_json"] = _json_dumps_safe(smc_snapshot)
    flat["btc_context_snapshot_json"] = _json_dumps_safe(btc_context_snapshot)
    flat["global_context_snapshot_json"] = _json_dumps_safe(global_context_snapshot)
    flat["thesis_json"] = _json_dumps_safe(normalized.get("thesis")) if normalized.get("thesis") is not None else ""

    for key, value in list(flat.items()):
        if isinstance(value, bool):
            flat[key] = 1 if value else 0
        elif isinstance(value, (list, tuple, set)):
            flat[key] = "|".join(_safe_list_str(value))
        elif isinstance(value, dict):
            flat[key] = _json_dumps_safe(value)
        elif value is None:
            flat[key] = ""

    return flat


class TradeJournalWriter:
    def __init__(self, out_dir: str = "journal"):
        requested_dir = Path(out_dir)
        self.out_dir = JOURNAL_DIR if requested_dir == Path("journal") else requested_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.queue: asyncio.Queue = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="paper_trade_journal")

    async def write_trade(self, trade: Dict[str, Any]):
        await self.queue.put(dict(trade))

    async def shutdown(self):
        if self._task is None:
            return
        await self.queue.put(dict(JOURNAL_STOP))
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self):
        csv_path = self.out_dir / TRADE_HISTORY_CSV_PATH.name
        json_path = self.out_dir / TRADE_HISTORY_JSON_PATH.name
        while True:
            item = await self.queue.get()
            try:
                if item.get("__journal_stop__"):
                    return
                await asyncio.to_thread(self._append_trade, csv_path, json_path, item)
            except Exception as exc:
                logger.error("TradeJournalWriter error: %s", exc, exc_info=True)
            finally:
                self.queue.task_done()

    def _append_trade(self, csv_path: Path, json_path: Path, row: Dict[str, Any]):
        normalized = _normalize_trade_record(row)
        self._append_json_snapshot(json_path, normalized)
        self._append_csv(csv_path, normalized)

    def _append_csv(self, path: Path, normalized: Dict[str, Any]):
        self._ensure_csv_schema(path)
        flat_row = _flatten_trade_for_csv(normalized)
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            writer.writerow(flat_row)

    def _ensure_csv_schema(self, path: Path) -> None:
        if not path.exists():
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
                writer.writeheader()
            return

        try:
            with path.open("r", newline="", encoding="utf-8") as handle:
                reader = csv.reader(handle)
                existing_header = next(reader, [])
        except Exception as exc:
            logger.warning("Failed reading existing journal CSV header %s: %s", path, exc)
            existing_header = []

        if list(existing_header) == list(CSV_COLUMNS):
            return

        logger.warning("Trade journal CSV schema drift detected. Migrating %s to stable analytic schema.", path)
        try:
            existing_rows: list[dict] = []
            with path.open("r", newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    existing_rows.append(dict(row))
        except Exception as exc:
            logger.error("Failed loading existing journal CSV rows for migration: %s", exc, exc_info=True)
            existing_rows = []

        migrated_rows = [_flatten_trade_for_csv(_normalize_trade_record(row)) for row in existing_rows]
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with tmp_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            for row in migrated_rows[-MAX_HISTORY_ROWS:]:
                writer.writerow(row)
        tmp_path.replace(path)

    def _append_json_snapshot(self, path: Path, normalized: Dict[str, Any]):
        history = load_json(path, default=[])
        if not isinstance(history, list):
            history = []

        def json_safe(value: Any) -> Any:
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, dict):
                return {key: json_safe(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [json_safe(item) for item in value]
            return value

        history.append(json_safe(normalized))
        if len(history) > MAX_HISTORY_ROWS:
            history = history[-MAX_HISTORY_ROWS:]
        atomic_write_json(path, history)