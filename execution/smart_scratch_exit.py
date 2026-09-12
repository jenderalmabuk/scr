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
    metadata = position.get('metadata', {})
    adv_snapshot = metadata.get('adv_snapshot', {})
    
    # Layer 0: Manual timeframe tag (for imported positions without signal data)
    manual_tf = metadata.get('manual_timeframe') or metadata.get('timeframe')
    if manual_tf:
        logger.debug(f"TF from manual tag: {manual_tf}")
        return manual_tf
    
    # Layer 1: From signal text
    tf = adv_snapshot.get('signal_timeframe') if adv_snapshot else None
    if tf:
        logger.debug(f"TF from signal text: {tf}")
        return tf
    
    # Layer 2: Channel-specific config
    source_chat_id = metadata.get('source_chat_id')
    if channel_config and source_chat_id:
        tf = channel_config.get(source_chat_id)
        if tf:
            logger.debug(f"TF from channel config: {tf}")
            return tf
    
    # Layer 3: TP distance heuristic
    tps = position.get('take_profits', [])
    if tps and len(tps) > 0:
        tp1 = tps[0]
        entry = position.get('entry_price', 0)
        
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


def detect_trend_strength(position: Dict[str, Any]) -> str:
    """
    Detect trend strength from signal metadata.
    
    Future: integrate with market data (price vs SMA, momentum indicators)
    Current: use signal confidence if available
    """
    metadata = position.get('metadata', {})
    confidence = metadata.get('adv_snapshot', {}).get('confidence', 0.5)
    
    if confidence > 0.8:
        return 'STRONG'
    elif confidence > 0.5:
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
    
    # Extract position data
    entry = position.get('entry_price', 0)
    tps = position.get('take_profits', [])
    sl = position.get('sl_price', 0)
    regime = position.get('regime', 'RANGING')
    
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
    
    # Safety bounds
    min_timeout = tf_minutes * 2   # At least 2 candles
    max_timeout = tf_minutes * 30  # Max 30 candles
    
    smart_timeout = max(min_timeout, min(smart_timeout, max_timeout))
    
    logger.info(
        f"[SMART_SCRATCH] {position.get('symbol')} {tf}: "
        f"{base_time:.0f} × {regime_mult:.2f} × {trend_mult:.2f} × {distance_mult:.2f} "
        f"= {smart_timeout:.0f}min ({smart_timeout/60:.1f}h)"
    )
    
    return smart_timeout
