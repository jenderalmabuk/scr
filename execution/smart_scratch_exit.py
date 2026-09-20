"""
Ultra-Smart SCRATCH_EXIT Calculator
Phase 1: Multi-factor adaptive timeout based on timeframe, regime, and trade characteristics.
"""

import logging
import os
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


# Timeframe to minutes mapping
TF_MAP = {
    '1m': 1,
    '5m': 5,
    '15m': 15,
    '30m': 30,
    '1h': 60,
    '2h': 120,
    '4h': 240,
    '6h': 360,
    '8h': 480,
    '12h': 720,
    '1d': 1440,
    '1w': 10080,
}

# Average price move per candle (empirical, in %)
AVG_MOVE_PER_CANDLE = {
    '1m': 0.05,
    '5m': 0.15,
    '15m': 0.3,
    '1h': 0.8,
    '4h': 2.5,
    '1d': 5.0,
}

# Regime base multipliers
REGIME_MULT = {
    'SCALP': 0.75,
    'RANGING': 1.0,
    'TRENDING': 1.5,
    'SWING': 2.0,
}

# Trend strength multipliers
TREND_MULT = {
    'STRONG': 1.5,
    'MODERATE': 1.0,
    'WEAK': 0.75,
}


def infer_timeframe(
    position: Dict[str, Any],
    channel_config: Optional[Dict] = None
) -> str:
    """
    5-layer hybrid timeframe detection.
    
    Layer 0: Manual timeframe tag (for imported positions)
    Layer 1: Signal text (if parsed from Telegram)
    Layer 2: Channel-specific config (optional)
    Layer 3: TP distance heuristic
    Layer 4: Safe default
    """
    metadata, adv_snapshot = _position_metadata(position)
    
    # Layer 0: Manual timeframe tag (for imported positions without signal data)
    manual_tf = metadata.get('manual_timeframe') or metadata.get('timeframe') or adv_snapshot.get('timeframe')
    if manual_tf:
        logger.debug(f"TF from manual tag: {manual_tf}")
        return manual_tf
    
    # Layer 1: From signal text
    tf = adv_snapshot.get('signal_timeframe') if adv_snapshot else None
    if tf:
        logger.debug(f"TF from signal text: {tf}")
        return tf
    
    # Layer 2: Channel-specific config
    source_chat_id = metadata.get('source_chat_id') or adv_snapshot.get('source_chat_id')
    if channel_config and source_chat_id:
        tf = channel_config.get(str(source_chat_id)) or channel_config.get(source_chat_id)
        if tf:
            logger.debug(f"TF from channel config: {tf}")
            return tf
    
    # Layer 3: TP distance heuristic
    tps = _position_tps(position)
    if tps and len(tps) > 0:
        tp1 = tps[0]
        entry = _safe_float(position.get('entry_price'))
        
        if entry > 0 and tp1 > 0:
            tp_dist_pct = abs(tp1 - entry) / entry * 100
            
            if tp_dist_pct < 0.5:
                inferred = '5m'
            elif tp_dist_pct < 1.5:
                inferred = '15m'
            elif tp_dist_pct < 3.0:
                inferred = '1h'
            elif tp_dist_pct < 10.0:
                inferred = '4h'
            else:
                inferred = '1d'
            
            logger.debug(f"TF from TP distance ({tp_dist_pct:.2f}%): {inferred}")
            return inferred
    
    # Layer 4: Safe default
    logger.debug("TF defaulted to 15m (no metadata available)")
    return '15m'


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _score_to_100(value: Any, default: float = 0.0) -> float:
    score = _safe_float(value, default)
    return score * 100.0 if 0 < score <= 1 else score


def _position_metadata(position: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    metadata = position.get('metadata') or {}
    if not isinstance(metadata, dict):
        metadata = {}
    adv_snapshot = position.get('adv_snapshot') or metadata.get('adv_snapshot') or metadata.get('adv') or {}
    if not isinstance(adv_snapshot, dict):
        adv_snapshot = {}
    return metadata, adv_snapshot


def _position_tps(position: Dict[str, Any]) -> list:
    metadata, adv_snapshot = _position_metadata(position)
    for key, container in (
        ('take_profits', position),
        ('tp_prices', position),
        ('tp_ladder', position),
        ('signal_take_profits', metadata),
        ('signal_tp_ladder', adv_snapshot),
    ):
        raw = container.get(key) if isinstance(container, dict) else None
        if raw:
            if isinstance(raw, (list, tuple)):
                return [_safe_float(x) for x in raw if _safe_float(x) > 0]
            value = _safe_float(raw)
            return [value] if value > 0 else []
    tp1 = _safe_float(position.get('tp1_price'))
    return [tp1] if tp1 > 0 else []


def _position_regime(position: Dict[str, Any]) -> str:
    metadata, adv_snapshot = _position_metadata(position)
    raw = (
        position.get('regime')
        or metadata.get('regime')
        or metadata.get('regime_label')
        or adv_snapshot.get('regime_label')
        or adv_snapshot.get('regime')
        or 'RANGING'
    )
    text = str(raw or 'RANGING').upper()
    if 'SCALP' in text:
        return 'SCALP'
    if 'TREND' in text:
        return 'TRENDING'
    if 'SWING' in text:
        return 'SWING'
    return 'RANGING'


def _tp_hit_count(position: Dict[str, Any]) -> int:
    tp_hit = position.get('tp_hit')
    if isinstance(tp_hit, list):
        return len(tp_hit)
    if position.get('tp1_hit') is True:
        return 1
    return 0


def _nested_dict(*values: Any) -> Dict[str, Any]:
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def _flow_aligned(side: str, flow: str) -> Optional[bool]:
    flow = str(flow or '').upper()
    if not flow:
        return None
    if flow in {'BOTH_ALLOWED', 'BOTH', 'NEUTRAL', 'MIXED'}:
        return True
    if flow in {'NO_TRADE', 'BLOCK', 'BLOCKED'}:
        return False
    if side == 'LONG':
        return flow in {'LONG', 'LONG_ONLY', 'BUY', 'BULLISH'}
    if side == 'SHORT':
        return flow in {'SHORT', 'SHORT_ONLY', 'SELL', 'BEARISH'}
    return None


def _position_setup_type(position: Dict[str, Any]) -> str:
    metadata, adv_snapshot = _position_metadata(position)
    raw = metadata.get('setup_type') or adv_snapshot.get('setup_type')
    is_manual = (
        metadata.get('imported') is True
        or str(metadata.get('source') or '').upper() == 'MANUAL'
        or adv_snapshot.get('manual_entry') is True
        or adv_snapshot.get('manual_imported_position') is True
    )
    if not raw and is_manual:
        raw = os.getenv('MANUAL_DEFAULT_SETUP_TYPE', 'EARLY_ENTRY')
    return str(raw or '').upper()


def _manual_structure_profile(position: Dict[str, Any], mark: float, scratch_timeout_min: float) -> Dict[str, Any]:
    metadata, adv_snapshot = _position_metadata(position)
    side = str(position.get('side') or '').upper()
    setup_type = _position_setup_type(position)
    is_manual = (
        metadata.get('imported') is True
        or str(metadata.get('source') or '').upper() == 'MANUAL'
        or adv_snapshot.get('manual_entry') is True
        or adv_snapshot.get('manual_imported_position') is True
        or metadata.get('manual_context_enriched') is True
        or adv_snapshot.get('manual_context_enriched') is True
    )
    structure_types = {
        'EARLY_ENTRY', 'RETEST_OB', 'ORDER_BLOCK', 'OB_RETEST',
        'TRENDLINE_HOLD', 'STRUCTURE_HOLD', 'SWING',
    }
    active = bool(is_manual and setup_type in structure_types)

    tf = infer_timeframe(position)
    tf_minutes = TF_MAP.get(tf, 15)
    timeout_mult = max(1.0, _safe_float(os.getenv('MANUAL_EARLY_TIMEOUT_MULT'), 3.0))
    min_timeout = scratch_timeout_min * timeout_mult
    if setup_type == 'SWING' or tf_minutes >= 60:
        min_timeout = max(min_timeout, _safe_float(os.getenv('MANUAL_SWING_MIN_TIMEOUT_MIN'), 360.0))
    else:
        min_timeout = max(min_timeout, _safe_float(os.getenv('MANUAL_EARLY_MIN_TIMEOUT_MIN'), 135.0))
    max_timeout = _safe_float(os.getenv('MANUAL_EARLY_MAX_TIMEOUT_MIN'), 720.0)
    if max_timeout > 0:
        min_timeout = min(min_timeout, max_timeout)

    current_r = _safe_float(position.get('scratch_current_r'), 0.0)
    min_r = _safe_float(os.getenv('MANUAL_EARLY_SCRATCH_MIN_R'), -0.5)
    hard_r = _safe_float(os.getenv('MANUAL_EARLY_HARD_EXIT_R'), -0.75)
    max_hold_mult = max(1.0, _safe_float(os.getenv('MANUAL_EARLY_MAX_HOLD_MULT'), 2.0))

    invalidation = _safe_float(
        metadata.get('invalidation_price')
        or metadata.get('structure_invalidation')
        or adv_snapshot.get('invalidation_price')
        or adv_snapshot.get('structure_invalidation'),
        0.0,
    )
    if invalidation <= 0:
        if side == 'LONG':
            invalidation = _safe_float(
                metadata.get('structure_low') or metadata.get('ob_low') or metadata.get('trendline_price')
                or adv_snapshot.get('structure_low') or adv_snapshot.get('ob_low') or adv_snapshot.get('trendline_price'),
                0.0,
            )
        elif side == 'SHORT':
            invalidation = _safe_float(
                metadata.get('structure_high') or metadata.get('ob_high') or metadata.get('trendline_price')
                or adv_snapshot.get('structure_high') or adv_snapshot.get('ob_high') or adv_snapshot.get('trendline_price'),
                0.0,
            )
    if invalidation <= 0:
        invalidation = _safe_float(position.get('provider_sl_original') or position.get('sl_price'), 0.0)

    structure_broken = False
    if active and invalidation > 0 and mark > 0:
        structure_broken = mark <= invalidation if side == 'LONG' else mark >= invalidation

    return {
        'active': active,
        'is_manual': is_manual,
        'setup_type': setup_type,
        'timeframe': tf,
        'tf_minutes': tf_minutes,
        'min_timeout_min': round(min_timeout, 2),
        'timeout_multiplier': round(timeout_mult, 2),
        'scratch_min_r': min_r,
        'hard_exit_r': hard_r,
        'max_hold_multiplier': round(max_hold_mult, 2),
        'invalidation_price': invalidation,
        'structure_broken': structure_broken,
        'structure_intact': active and not structure_broken,
        'current_r': current_r,
    }


def _setup_context(position: Dict[str, Any]) -> Dict[str, Any]:
    metadata, adv_snapshot = _position_metadata(position)
    side = str(position.get('side') or '').upper()
    mtf = _nested_dict(metadata.get('mtf_alignment'), adv_snapshot.get('mtf_alignment'))
    tv = _nested_dict(metadata.get('tradingview'), adv_snapshot.get('tradingview'))
    mtf_score = _score_to_100(mtf.get('score'), 0.0)
    tv_score = _score_to_100(tv.get('score'), 0.0)
    flow = metadata.get('flow_direction') or adv_snapshot.get('flow_direction')
    data_stale = bool(metadata.get('data_stale', adv_snapshot.get('data_stale', False)))
    aligned = _flow_aligned(side, str(flow or ''))
    oi_15m = _safe_float(metadata.get('oi_change_15m_pct', adv_snapshot.get('oi_change_15m_pct')), 0.0)
    oi_1h = _safe_float(metadata.get('oi_change_1h_pct', adv_snapshot.get('oi_change_1h_pct')), 0.0)
    rsi = _safe_float(metadata.get('rsi', adv_snapshot.get('rsi')), 0.0)

    weak_reasons = []
    if data_stale:
        weak_reasons.append('data_stale')
    if aligned is False:
        weak_reasons.append('flow_conflict')
    if 0 < mtf_score < 50:
        weak_reasons.append('mtf_weak')
    if 0 < tv_score < 50:
        weak_reasons.append('tv_weak')
    if oi_15m <= -1.0 or oi_1h <= -2.0:
        weak_reasons.append('oi_down')
    if rsi > 0 and ((side == 'LONG' and rsi < 45) or (side == 'SHORT' and rsi > 55)):
        weak_reasons.append('rsi_unsupportive')

    strong = mtf_score >= 70 and tv_score >= 70 and aligned is True and not data_stale
    return {
        'mtf_score': mtf_score,
        'tv_score': tv_score,
        'flow_direction': str(flow or ''),
        'flow_aligned': aligned,
        'data_stale': data_stale,
        'oi_change_15m_pct': oi_15m,
        'oi_change_1h_pct': oi_1h,
        'rsi': rsi,
        'strong_setup': strong,
        'weak_reasons': weak_reasons,
        'weak_setup': len(weak_reasons) >= int(_safe_float(os.getenv('SCRATCH_EXIT_WEAK_MIN_REASONS'), 2)),
    }


def update_scratch_excursion(position: Dict[str, Any], mark: float) -> Dict[str, float]:
    """Track MFE/MAE from entry so scratch exit can distinguish dead trades from recovering trades."""
    entry = _safe_float(position.get('entry_price'))
    mark = _safe_float(mark)
    side = str(position.get('side') or '').upper()
    sl = _safe_float(position.get('provider_sl_original') or position.get('sl_price'))
    risk_distance = abs(entry - sl) if entry > 0 and sl > 0 else entry * 0.01

    if entry <= 0 or mark <= 0 or risk_distance <= 0:
        snapshot = {
            'current_r': 0.0,
            'max_favorable_r': 0.0,
            'max_adverse_r': 0.0,
            'high_watermark': mark,
            'low_watermark': mark,
        }
        position['scratch_excursion'] = snapshot
        return snapshot

    high = _safe_float(
        position.get('scratch_high_watermark') or position.get('high_watermark') or entry,
        entry,
    )
    low = _safe_float(
        position.get('scratch_low_watermark') or position.get('low_watermark') or entry,
        entry,
    )
    high = max(high, mark, entry)
    low = min(low, mark, entry)

    if side == 'SHORT':
        current_r = (entry - mark) / risk_distance
        max_favorable_r = max(0.0, (entry - low) / risk_distance)
        max_adverse_r = max(0.0, (high - entry) / risk_distance)
    else:
        current_r = (mark - entry) / risk_distance
        max_favorable_r = max(0.0, (high - entry) / risk_distance)
        max_adverse_r = max(0.0, (entry - low) / risk_distance)

    snapshot = {
        'current_r': round(current_r, 4),
        'max_favorable_r': round(max_favorable_r, 4),
        'max_adverse_r': round(max_adverse_r, 4),
        'high_watermark': high,
        'low_watermark': low,
        'risk_distance': risk_distance,
    }
    position['scratch_high_watermark'] = high
    position['scratch_low_watermark'] = low
    position['scratch_max_favorable_r'] = snapshot['max_favorable_r']
    position['scratch_max_adverse_r'] = snapshot['max_adverse_r']
    position['scratch_current_r'] = snapshot['current_r']
    position['scratch_excursion'] = snapshot
    return snapshot


def evaluate_scratch_exit_gate(
    position: Dict[str, Any],
    mark: float,
    hold_minutes: float,
    scratch_timeout_min: float,
    unrealized_pct: float,
    max_abs_pnl_pct: float,
) -> Dict[str, Any]:
    """Return a deterministic scratch-exit decision plus audit fields."""
    excursion = update_scratch_excursion(position, mark)
    tp_count = _tp_hit_count(position)
    tps = _position_tps(position)
    entry = _safe_float(position.get('entry_price'))
    side = str(position.get('side') or '').upper()

    near_breakeven = abs(unrealized_pct) <= max_abs_pnl_pct
    progress_threshold = _safe_float(os.getenv('SCRATCH_EXIT_PROGRESS_R_THRESHOLD'), 0.35)
    near_tp_fraction = _safe_float(os.getenv('SCRATCH_EXIT_NEAR_TP_PROGRESS_FRACTION'), 0.75)

    setup = _setup_context(position)
    manual_profile = _manual_structure_profile(position, mark, scratch_timeout_min)
    effective_timeout = scratch_timeout_min
    timeout_multiplier = 1.0
    if setup['strong_setup']:
        timeout_multiplier = max(1.0, min(_safe_float(os.getenv('SCRATCH_EXIT_STRONG_TIMEOUT_MULT'), 1.5), 2.0))
        effective_timeout = scratch_timeout_min * timeout_multiplier
    if manual_profile['active']:
        timeout_multiplier = max(timeout_multiplier, manual_profile['timeout_multiplier'])
        effective_timeout = max(effective_timeout, manual_profile['min_timeout_min'])

    tp1_progress = 0.0
    if tps and entry > 0:
        tp1 = tps[0]
        total = abs(tp1 - entry)
        if total > 0:
            favorable = (mark - entry) if side != 'SHORT' else (entry - mark)
            tp1_progress = max(0.0, favorable / total)

    low_tp_progress = tp1_progress < _safe_float(os.getenv('SCRATCH_EXIT_LOW_TP_PROGRESS_FRACTION'), 0.25)
    if setup['weak_setup'] and low_tp_progress and not manual_profile['active']:
        weak_timeout = _safe_float(os.getenv('SCRATCH_EXIT_WEAK_TIMEOUT_MIN'), 45.0)
        effective_timeout = min(effective_timeout, weak_timeout)
        progress_threshold = max(progress_threshold, _safe_float(os.getenv('SCRATCH_EXIT_WEAK_PROGRESS_R_THRESHOLD'), 0.6))

    time_due = hold_minutes >= effective_timeout
    reason = 'NOT_DUE'
    action = 'WAIT'
    should_exit = False
    if manual_profile['active']:
        max_hold_due = hold_minutes >= effective_timeout * manual_profile['max_hold_multiplier']
        current_r = excursion['current_r']
        if current_r <= manual_profile['hard_exit_r']:
            reason = 'MANUAL_STRUCTURE_DEFER_TO_DAMAGE_REDUCER'
            action = 'DEFER'
        elif manual_profile['structure_broken']:
            reason = 'MANUAL_STRUCTURE_DEFER_TO_DAMAGE_REDUCER'
            action = 'DEFER'
        elif max_hold_due and low_tp_progress and current_r <= manual_profile['scratch_min_r'] and near_breakeven:
            reason = 'MANUAL_STRUCTURE_STALE_MAX_HOLD'
            action = 'EXIT'
            should_exit = True
        elif time_due and near_breakeven:
            if tp_count >= 1:
                reason = 'TP_ALREADY_HIT'
                action = 'SKIP'
            elif tp1_progress >= near_tp_fraction:
                reason = 'NEAR_TP1'
                action = 'DEFER'
            elif excursion['max_favorable_r'] >= progress_threshold:
                reason = 'PROGRESS_SEEN'
                action = 'DEFER'
            else:
                reason = 'MANUAL_STRUCTURE_STALE_NEAR_BREAKEVEN'
                action = 'EXIT'
                should_exit = True
        elif time_due:
            reason = 'MANUAL_STRUCTURE_INTACT_FLOAT_OK'
            action = 'DEFER'
        elif setup['strong_setup']:
            reason = 'MANUAL_STRUCTURE_STRONG_SETUP_EXTENDED'
        elif setup['weak_setup'] and low_tp_progress:
            reason = 'MANUAL_STRUCTURE_WEAK_BUT_PROTECTED'
        else:
            reason = 'MANUAL_STRUCTURE_MIN_TIMEOUT'
    elif time_due and near_breakeven:
        if tp_count >= 1:
            reason = 'TP_ALREADY_HIT'
            action = 'SKIP'
        elif tp1_progress >= near_tp_fraction:
            reason = 'NEAR_TP1'
            action = 'DEFER'
        elif excursion['max_favorable_r'] >= progress_threshold:
            reason = 'PROGRESS_SEEN'
            action = 'DEFER'
        else:
            reason = 'WEAK_STALE_NEAR_BREAKEVEN' if setup['weak_setup'] else 'STALE_NEAR_BREAKEVEN'
            action = 'EXIT'
            should_exit = True
    elif time_due:
        reason = 'OUTSIDE_BREAKEVEN_BAND'
    elif setup['strong_setup']:
        reason = 'STRONG_SETUP_EXTENDED_TIMEOUT'
    elif setup['weak_setup'] and low_tp_progress:
        reason = 'WEAK_SETUP_FAST_TIMEOUT'

    return {
        'should_exit': should_exit,
        'action': action,
        'reason': reason,
        'hold_minutes': round(hold_minutes, 2),
        'scratch_timeout_min': round(scratch_timeout_min, 2),
        'effective_scratch_timeout_min': round(effective_timeout, 2),
        'timeout_multiplier': round(timeout_multiplier, 2),
        'unrealized_pct': round(unrealized_pct, 4),
        'max_abs_pnl_pct': max_abs_pnl_pct,
        'progress_threshold_r': progress_threshold,
        'time_due': time_due,
        'near_breakeven': near_breakeven,
        'tp_hit_count': tp_count,
        'tp1_progress': round(tp1_progress, 4),
        'setup_context': setup,
        'manual_structure_profile': manual_profile,
        **excursion,
    }


def evaluate_damage_reducer_gate(
    position: Dict[str, Any],
    mark: float,
    hold_minutes: float,
    unrealized_pct: float,
    damage_min_hold_min: float,
    damage_max_loss_pct: float,
) -> Dict[str, Any]:
    """Return a structure-aware DAMAGE_REDUCER decision.

    DAMAGE_REDUCER is for trades that are actually moving wrong, not for early
    entries that are still holding their structure. It now has three defensive
    stages before the final hard exit:
    - MFE giveback protection for trades that were already meaningfully green.
    - Reversal-first partial reduction when strong reversal evidence appears early.
    - Local-bottom guard to avoid full-closing a capitulation bounce blindly.
    """
    excursion = update_scratch_excursion(position, mark)
    setup = _setup_context(position)
    manual_profile = _manual_structure_profile(position, mark, damage_min_hold_min)
    current_r = _safe_float(excursion.get('current_r'), 0.0)
    max_favorable_r = _safe_float(excursion.get('max_favorable_r'), 0.0)
    max_adverse_r = _safe_float(excursion.get('max_adverse_r'), 0.0)
    tp_count = _tp_hit_count(position)
    tps = _position_tps(position)
    entry = _safe_float(position.get('entry_price'))
    side = str(position.get('side') or '').upper()
    tp1_progress = 0.0
    if tps and entry > 0:
        total = abs(tps[0] - entry)
        if total > 0:
            favorable = (mark - entry) if side != 'SHORT' else (entry - mark)
            tp1_progress = max(0.0, favorable / total)
    recovery_from_worst_r = max(0.0, max_adverse_r + current_r) if current_r < 0 else 0.0
    time_due = hold_minutes >= damage_min_hold_min
    pct_due = unrealized_pct <= damage_max_loss_pct

    reversal_evidence = list(setup.get('weak_reasons') or [])
    reversal_score = len(reversal_evidence)
    normal_min_r = _safe_float(os.getenv('DAMAGE_REDUCER_MIN_ADVERSE_R'), -0.45)
    normal_hard_r = _safe_float(os.getenv('DAMAGE_REDUCER_HARD_R'), -0.85)
    emergency_r = _safe_float(os.getenv('DAMAGE_REDUCER_EMERGENCY_R'), -1.10)
    normal_min_reversal = int(_safe_float(os.getenv('DAMAGE_REDUCER_MIN_REVERSAL_SIGNALS'), 2))
    early_min_r = _safe_float(os.getenv('DAMAGE_REVERSAL_FIRST_MIN_R'), -0.20)
    early_reversal_signals = int(_safe_float(os.getenv('DAMAGE_REVERSAL_FIRST_SIGNALS'), 3))
    partial_fraction = max(0.05, min(_safe_float(os.getenv('DAMAGE_REDUCER_PARTIAL_FRACTION'), 0.50), 0.95))
    mfe_enabled = str(os.getenv('DAMAGE_MFE_GIVEBACK_ENABLED', 'true')).lower() in {'1', 'true', 'yes', 'on'}
    mfe_min_r = _safe_float(os.getenv('DAMAGE_MFE_GIVEBACK_MIN_MFE_R'), 0.50)
    mfe_exit_r = _safe_float(os.getenv('DAMAGE_MFE_GIVEBACK_EXIT_R'), -0.15)
    local_guard_enabled = str(os.getenv('DAMAGE_LOCAL_BOTTOM_GUARD_ENABLED', 'true')).lower() in {'1', 'true', 'yes', 'on'}
    local_guard_bounce_r = _safe_float(os.getenv('DAMAGE_LOCAL_BOTTOM_BOUNCE_R'), 0.15)
    local_guard_bypass_signals = int(_safe_float(os.getenv('DAMAGE_LOCAL_BOTTOM_BYPASS_SIGNALS'), 4))
    manual_min_reversal = int(_safe_float(os.getenv('MANUAL_DAMAGE_MIN_REVERSAL_SIGNALS'), 3))
    manual_emergency_r = _safe_float(os.getenv('MANUAL_DAMAGE_EMERGENCY_R'), -1.10)

    mfe_giveback = mfe_enabled and tp_count < 1 and max_favorable_r >= mfe_min_r and current_r <= mfe_exit_r
    manual_active = bool(manual_profile.get('active'))
    guard_hard_r = _safe_float(manual_profile.get('hard_exit_r'), normal_hard_r) if manual_active else normal_hard_r
    guard_emergency_r = manual_emergency_r if manual_active else emergency_r
    local_bottom_guard = (
        local_guard_enabled
        and current_r <= guard_hard_r
        and current_r > guard_emergency_r
        and recovery_from_worst_r >= local_guard_bounce_r
        and reversal_score < local_guard_bypass_signals
    )

    action = 'WAIT'
    reason = 'NOT_DUE'
    should_exit = False
    should_partial_exit = False
    close_fraction = 0.0
    stage_key = None

    if manual_profile['active']:
        min_r = manual_profile['scratch_min_r']
        hard_r = manual_profile['hard_exit_r']
        manual_local_guard = local_bottom_guard and manual_profile.get('structure_intact')
        if manual_profile['structure_broken']:
            reason = 'MANUAL_DAMAGE_STRUCTURE_BROKEN'
            action = 'EXIT'
            should_exit = True
        elif current_r <= manual_emergency_r:
            reason = 'MANUAL_DAMAGE_EMERGENCY_R'
            action = 'EXIT'
            should_exit = True
        elif time_due and current_r <= hard_r and manual_local_guard:
            reason = 'MANUAL_DAMAGE_LOCAL_BOTTOM_GUARD'
            action = 'DEFER'
        elif time_due and current_r <= hard_r:
            reason = 'MANUAL_DAMAGE_HARD_R'
            action = 'EXIT'
            should_exit = True
        elif time_due and mfe_giveback:
            reason = 'MANUAL_DAMAGE_MFE_GIVEBACK_PROTECTED'
            action = 'DEFER'
        elif time_due and current_r <= min_r and reversal_score >= manual_min_reversal:
            reason = 'MANUAL_DAMAGE_REVERSAL_PARTIAL'
            action = 'PARTIAL_EXIT'
            should_partial_exit = True
            close_fraction = partial_fraction
            stage_key = reason
        elif time_due and (pct_due or current_r <= min_r):
            reason = 'MANUAL_DAMAGE_STRUCTURE_PROTECTED'
            action = 'DEFER'
        elif time_due:
            reason = 'MANUAL_DAMAGE_LOSS_NOT_DEEP'
        else:
            reason = 'MANUAL_DAMAGE_NOT_DUE'
    else:
        if time_due and mfe_giveback and (current_r <= normal_min_r or reversal_score >= normal_min_reversal):
            reason = 'DAMAGE_MFE_GIVEBACK_EXIT'
            action = 'EXIT'
            should_exit = True
        elif time_due and mfe_giveback:
            reason = 'DAMAGE_MFE_GIVEBACK_PARTIAL'
            action = 'PARTIAL_EXIT'
            should_partial_exit = True
            close_fraction = partial_fraction
            stage_key = reason
        elif current_r <= emergency_r:
            reason = 'DAMAGE_EMERGENCY_R'
            action = 'EXIT'
            should_exit = True
        elif time_due and current_r <= normal_hard_r and local_bottom_guard:
            reason = 'DAMAGE_HARD_R_LOCAL_BOTTOM_GUARD'
            action = 'PARTIAL_EXIT'
            should_partial_exit = True
            close_fraction = partial_fraction
            stage_key = reason
        elif time_due and current_r <= normal_hard_r:
            reason = 'DAMAGE_HARD_R'
            action = 'EXIT'
            should_exit = True
        elif time_due and normal_min_r < current_r <= early_min_r and reversal_score >= early_reversal_signals:
            reason = 'DAMAGE_REVERSAL_FIRST_PARTIAL'
            action = 'PARTIAL_EXIT'
            should_partial_exit = True
            close_fraction = partial_fraction
            stage_key = reason
        elif time_due and (pct_due or current_r <= normal_min_r):
            if reversal_score >= normal_min_reversal:
                reason = 'DAMAGE_CONFIRMED_REVERSAL'
                action = 'EXIT'
                should_exit = True
            else:
                reason = 'DAMAGE_REVERSAL_NOT_CONFIRMED'
                action = 'DEFER'
        elif time_due:
            reason = 'DAMAGE_LOSS_NOT_DEEP'

    done_stages = set(position.get('damage_partial_stages') or [])
    if should_partial_exit and stage_key in done_stages:
        should_partial_exit = False
        action = 'DEFER'
        reason = f"{stage_key}_ALREADY_EXECUTED"

    return {
        'should_exit': should_exit,
        'should_partial_exit': should_partial_exit,
        'action': action,
        'reason': reason,
        'close_fraction': round(close_fraction, 4),
        'stage_key': stage_key,
        'hold_minutes': round(hold_minutes, 2),
        'damage_min_hold_min': round(damage_min_hold_min, 2),
        'unrealized_pct': round(unrealized_pct, 4),
        'damage_max_loss_pct': damage_max_loss_pct,
        'time_due': time_due,
        'pct_due': pct_due,
        'tp_hit_count': tp_count,
        'tp1_progress': round(tp1_progress, 4),
        'current_r': round(current_r, 4),
        'normal_min_adverse_r': normal_min_r,
        'normal_hard_r': normal_hard_r,
        'emergency_r': emergency_r,
        'max_favorable_r': round(max_favorable_r, 4),
        'max_adverse_r': round(max_adverse_r, 4),
        'recovery_from_worst_r': round(recovery_from_worst_r, 4),
        'mfe_giveback': mfe_giveback,
        'mfe_giveback_min_r': mfe_min_r,
        'mfe_giveback_exit_r': mfe_exit_r,
        'local_bottom_guard': local_bottom_guard,
        'local_bottom_bounce_r': local_guard_bounce_r,
        'reversal_score': reversal_score,
        'reversal_evidence': reversal_evidence,
        'min_reversal_signals': manual_min_reversal if manual_profile['active'] else normal_min_reversal,
        'early_reversal_min_r': early_min_r,
        'early_reversal_signals': early_reversal_signals,
        'setup_context': setup,
        'manual_structure_profile': manual_profile,
        **excursion,
    }


def detect_trend_strength(position: Dict[str, Any]) -> str:
    """
    Detect trend strength from signal metadata already attached at entry.
    """
    metadata, adv_snapshot = _position_metadata(position)
    confidence = _score_to_100(
        metadata.get('confidence', adv_snapshot.get('confidence')),
        0.0,
    )
    validation_score = _score_to_100(
        metadata.get('score', adv_snapshot.get('validation_score')),
        0.0,
    )
    mtf = adv_snapshot.get('mtf_alignment') if isinstance(adv_snapshot.get('mtf_alignment'), dict) else {}
    tv = adv_snapshot.get('tradingview') if isinstance(adv_snapshot.get('tradingview'), dict) else {}
    mtf_score = _score_to_100(mtf.get('score'), 0.0)
    tv_score = _score_to_100(tv.get('score'), 0.0)
    composite = max(confidence, validation_score, mtf_score, tv_score)
    
    if composite >= 80:
        return 'STRONG'
    elif composite >= 55:
        return 'MODERATE'
    else:
        return 'WEAK'


def calculate_smart_scratch_timeout(
    position: Dict[str, Any],
    channel_config: Optional[Dict] = None,
    enable_smart: bool = True,
    phase1_4h_only: bool = False  # Changed from True to False for Phase 2
) -> float:
    """
    Calculate adaptive SCRATCH_EXIT timeout based on multiple factors.
    
    Phase 2 (ALL timeframes):
        - Apply smart logic to ALL timeframes (15m, 1h, 4h, 1d, etc.)
        - Adaptive timeout based on 6 factors
        - Bounded by [2 candles, 30 candles] for safety
    
    Formula:
        timeout = BASE_TIME × REGIME_MULT × TREND_MULT × DISTANCE_MULT
    
    where:
        BASE_TIME = estimated_candles_to_tp × timeframe_minutes
        REGIME_MULT = regime_base × rr_adjustment
        TREND_MULT = trend_strength_multiplier
        DISTANCE_MULT = tp/sl ratio adjustment
    
    Args:
        position: Position dict with entry, tp, sl, metadata
        channel_config: Optional channel-specific TF config
        enable_smart: If False, return legacy 45min
        phase1_4h_only: If True, only apply to 4H+ (Phase 1), if False apply to all (Phase 2)
    
    Returns:
        Scratch timeout in minutes
    """
    # Legacy fallback
    if not enable_smart:
        return 45.0
    
    # Detect timeframe
    tf = infer_timeframe(position, channel_config)
    tf_minutes = TF_MAP.get(tf, 15)
    
    # Phase 1 vs Phase 2: Only apply smart logic to 4H+ timeframes in Phase 1
    if phase1_4h_only and tf_minutes < 240:  # 240 = 4 hours
        logger.debug(f"Phase 1 mode: {tf} < 4h, using legacy 45min")
        return 45.0
    
    logger.info(f"[SMART_SCRATCH Phase 2] Calculating for TF={tf} ({tf_minutes}min)")
    
    # Extract position data. Live Bybit positions store TP ladder as `tp_prices`
    # and signal context inside metadata/adv_snapshot, so do not rely only on
    # paper-mode `take_profits` / top-level `regime` fields.
    entry = _safe_float(position.get('entry_price'))
    tps = _position_tps(position)
    sl = _safe_float(position.get('provider_sl_original') or position.get('sl_price'))
    regime = _position_regime(position)
    
    # Safety: need TP1 for calculation
    if not tps or len(tps) == 0 or entry == 0:
        logger.warning(f"[SMART_SCRATCH] Missing TP/entry, fallback to 2x TF = {tf_minutes * 2}min")
        return float(tf_minutes * 2)
    
    tp1 = tps[0]
    
    # ─────────────────────────────────────────────────────────
    # COMPONENT 1: ESTIMATED CANDLES TO TP
    # ─────────────────────────────────────────────────────────
    tp_dist_pct = abs(tp1 - entry) / entry * 100
    avg_move = AVG_MOVE_PER_CANDLE.get(tf, 0.3)
    
    # Conservative: assume 1.5x slower than average
    candles_to_tp = (tp_dist_pct / avg_move) * 1.5
    
    # Clamp to reasonable range
    candles_to_tp = max(3, min(candles_to_tp, 20))
    
    base_time = candles_to_tp * tf_minutes
    
    logger.debug(
        f"  TP dist: {tp_dist_pct:.2f}%, avg_move: {avg_move:.2f}%, "
        f"candles: {candles_to_tp:.1f}, base_time: {base_time:.0f}min"
    )
    
    # ─────────────────────────────────────────────────────────
    # COMPONENT 2: REGIME MULTIPLIER (with R:R adjustment)
    # ─────────────────────────────────────────────────────────
    regime_base = REGIME_MULT.get(regime, 1.0)
    
    # R:R ratio adjustment
    rr_adj = 1.0
    if sl and sl > 0:
        sl_dist_pct = abs(sl - entry) / entry * 100
        if sl_dist_pct > 0:
            rr_ratio = tp_dist_pct / sl_dist_pct
            
            # High R:R = provider confident = give more time
            if regime == 'TRENDING' and rr_ratio > 3:
                rr_adj = 1.3
            elif regime == 'SCALP' and rr_ratio > 2:
                rr_adj = 1.0
            elif regime == 'RANGING' and rr_ratio > 2.5:
                rr_adj = 1.2
            elif regime == 'SWING' and rr_ratio > 4:
                rr_adj = 1.5
            
            logger.debug(f"  R:R: {rr_ratio:.2f}, adjustment: {rr_adj:.2f}")
    
    regime_mult = regime_base * rr_adj
    
    logger.debug(f"  Regime: {regime}, base: {regime_base:.2f}, final: {regime_mult:.2f}")
    
    # ─────────────────────────────────────────────────────────
    # COMPONENT 3: TREND STRENGTH MULTIPLIER
    # ─────────────────────────────────────────────────────────
    trend_strength = detect_trend_strength(position)
    trend_mult = TREND_MULT.get(trend_strength, 1.0)
    
    logger.debug(f"  Trend strength: {trend_strength}, mult: {trend_mult:.2f}")
    
    # ─────────────────────────────────────────────────────────
    # COMPONENT 4: DISTANCE RATIO MULTIPLIER
    # ─────────────────────────────────────────────────────────
    distance_mult = 1.0
    if sl and sl > 0:
        sl_dist_pct = abs(sl - entry) / entry * 100
        if sl_dist_pct > 0:
            rr_ratio = tp_dist_pct / sl_dist_pct
            
            if rr_ratio > 4:
                distance_mult = 1.3
            elif rr_ratio > 2:
                distance_mult = 1.1
            elif rr_ratio > 1:
                distance_mult = 1.0
            else:
                distance_mult = 0.9
    
    logger.debug(f"  Distance mult: {distance_mult:.2f}")
    
    # ─────────────────────────────────────────────────────────
    # FINAL CALCULATION
    # ─────────────────────────────────────────────────────────
    smart_timeout = base_time * regime_mult * trend_mult * distance_mult
    
    # Safety bounds: allow adequate breathing room for retest & sideways consolidation
    if tf_minutes <= 15:
        min_timeout = max(90.0, tf_minutes * 6)  # At least 90min (6 candles on 15m)
    else:
        min_timeout = tf_minutes * 4             # At least 4 candles on 1h+
    max_timeout = tf_minutes * 30                # Max 30 candles
    
    smart_timeout = max(min_timeout, min(smart_timeout, max_timeout))
    
    logger.info(
        f"[SMART_SCRATCH] {position.get('symbol')} {tf}: "
        f"{base_time:.0f} × {regime_mult:.2f} × {trend_mult:.2f} × {distance_mult:.2f} "
        f"= {smart_timeout:.0f}min ({smart_timeout/60:.1f}h)"
    )
    
    return smart_timeout
