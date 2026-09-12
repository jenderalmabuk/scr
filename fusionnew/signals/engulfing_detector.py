# signals/engulfing_detector.py
"""
Bullish/Bearish Engulfing Pattern Detector untuk Multi-Timeframe
Designed untuk confluence dengan Order Block strategy
"""

import pandas as pd
from typing import Dict, Any, Tuple
from utils.logger import logger


def detect_bullish_engulfing(df: pd.DataFrame, min_body_ratio: float = 1.3, volume_spike: float = 1.2) -> Tuple[bool, Dict[str, Any]]:
    """
    Detect Bullish Engulfing pattern dengan volume confirmation
    
    Args:
        df: OHLCV DataFrame (minimal 2 candles)
        min_body_ratio: Ratio minimum body current vs previous (default 1.3x)
        volume_spike: Volume multiplier threshold (default 1.2x avg)
    
    Returns:
        (is_engulfing, details_dict)
    """
    if df is None or len(df) < 2:
        return False, {}
    
    try:
        # Last 2 candles
        prev = df.iloc[-2]
        curr = df.iloc[-1]
        
        # Previous candle: Bearish (red)
        prev_open = float(prev['open'])
        prev_close = float(prev['close'])
        prev_bearish = prev_close < prev_open
        prev_body = abs(prev_close - prev_open)
        
        # Current candle: Bullish (green)
        curr_open = float(curr['open'])
        curr_close = float(curr['close'])
        curr_bullish = curr_close > curr_open
        curr_body = abs(curr_close - curr_open)
        
        # Engulfing condition
        engulfs = (curr_open <= prev_close) and (curr_close >= prev_open)
        
        # Body ratio check
        body_ratio = curr_body / max(prev_body, 1e-9)
        body_strong = body_ratio >= min_body_ratio
        
        # Volume confirmation
        if 'volume' in df.columns and len(df) >= 10:
            vol_avg = df['volume'].iloc[-10:-1].mean()
            curr_vol = float(curr['volume'])
            vol_confirmed = curr_vol >= (vol_avg * volume_spike)
        else:
            vol_confirmed = False
        
        is_bullish_engulfing = prev_bearish and curr_bullish and engulfs and body_strong
        
        details = {
            'pattern': 'BULLISH_ENGULFING' if is_bullish_engulfing else 'NO_PATTERN',
            'engulfs': engulfs,
            'body_ratio': round(body_ratio, 2),
            'body_strong': body_strong,
            'volume_confirmed': vol_confirmed,
            'prev_body': round(prev_body, 6),
            'curr_body': round(curr_body, 6),
            'curr_close': curr_close,
            'curr_open': curr_open,
            'quality_score': _compute_engulfing_quality(
                body_ratio, vol_confirmed, engulfs, curr_bullish
            )
        }
        
        if is_bullish_engulfing:
            logger.info(
                "[BULLISH_ENGULFING] body_ratio=%.2f | vol_confirmed=%d | quality=%.1f",
                body_ratio,
                1 if vol_confirmed else 0,
                details['quality_score']
            )
        
        return is_bullish_engulfing, details
        
    except Exception as e:
        logger.error(f"Error detecting bullish engulfing: {e}")
        return False, {}


def detect_bearish_engulfing(df: pd.DataFrame, min_body_ratio: float = 1.3, volume_spike: float = 1.2) -> Tuple[bool, Dict[str, Any]]:
    """
    Detect Bearish Engulfing pattern dengan volume confirmation
    """
    if df is None or len(df) < 2:
        return False, {}
    
    try:
        prev = df.iloc[-2]
        curr = df.iloc[-1]
        
        # Previous: Bullish (green)
        prev_open = float(prev['open'])
        prev_close = float(prev['close'])
        prev_bullish = prev_close > prev_open
        prev_body = abs(prev_close - prev_open)
        
        # Current: Bearish (red)
        curr_open = float(curr['open'])
        curr_close = float(curr['close'])
        curr_bearish = curr_close < curr_open
        curr_body = abs(curr_close - curr_open)
        
        # Engulfing
        engulfs = (curr_open >= prev_close) and (curr_close <= prev_open)
        
        # Body ratio
        body_ratio = curr_body / max(prev_body, 1e-9)
        body_strong = body_ratio >= min_body_ratio
        
        # Volume
        if 'volume' in df.columns and len(df) >= 10:
            vol_avg = df['volume'].iloc[-10:-1].mean()
            curr_vol = float(curr['volume'])
            vol_confirmed = curr_vol >= (vol_avg * volume_spike)
        else:
            vol_confirmed = False
        
        is_bearish_engulfing = prev_bullish and curr_bearish and engulfs and body_strong
        
        details = {
            'pattern': 'BEARISH_ENGULFING' if is_bearish_engulfing else 'NO_PATTERN',
            'engulfs': engulfs,
            'body_ratio': round(body_ratio, 2),
            'body_strong': body_strong,
            'volume_confirmed': vol_confirmed,
            'prev_body': round(prev_body, 6),
            'curr_body': round(curr_body, 6),
            'curr_close': curr_close,
            'curr_open': curr_open,
            'quality_score': _compute_engulfing_quality(
                body_ratio, vol_confirmed, engulfs, curr_bearish
            )
        }
        
        if is_bearish_engulfing:
            logger.info(
                "[BEARISH_ENGULFING] body_ratio=%.2f | vol_confirmed=%d | quality=%.1f",
                body_ratio,
                1 if vol_confirmed else 0,
                details['quality_score']
            )
        
        return is_bearish_engulfing, details
        
    except Exception as e:
        logger.error(f"Error detecting bearish engulfing: {e}")
        return False, {}


def _compute_engulfing_quality(body_ratio: float, vol_confirmed: bool, engulfs: bool, direction_ok: bool) -> float:
    """
    Compute engulfing pattern quality score (0-100)
    """
    score = 0.0
    
    # Base score for valid engulfing
    if engulfs and direction_ok:
        score += 40.0
    
    # Body ratio scoring
    if body_ratio >= 2.0:
        score += 30.0
    elif body_ratio >= 1.5:
        score += 20.0
    elif body_ratio >= 1.3:
        score += 10.0
    
    # Volume confirmation
    if vol_confirmed:
        score += 30.0
    
    return min(score, 100.0)


def check_engulfing_at_ob(
    *,
    symbol: str,
    df_15m: pd.DataFrame,
    df_5m: pd.DataFrame,
    df_3m: pd.DataFrame,
    ob_15m_zone: Dict[str, float],
    ob_4h_zone: Dict[str, float],
    direction: str = "LONG"
) -> Dict[str, Any]:
    """
    Check apakah ada bullish/bearish engulfing di Order Block zone
    
    Strategy Logic:
    1. Watchlist OB 15M & 4H
    2. Cek 5M & 3M untuk engulfing pattern
    3. Validate price di dalam OB zone
    4. Return confluence result
    
    Args:
        symbol: Trading symbol
        df_15m, df_5m, df_3m: OHLCV DataFrames
        ob_15m_zone: {'low': price, 'high': price} dari 15M OB
        ob_4h_zone: {'low': price, 'high': price} dari 4H OB
        direction: "LONG" atau "SHORT"
    
    Returns:
        {
            'engulfing_confirmed': bool,
            'timeframe': '5M' | '3M' | None,
            'at_ob_15m': bool,
            'at_ob_4h': bool,
            'engulfing_details': dict,
            'confluence_score': float (0-100)
        }
    """
    result = {
        'engulfing_confirmed': False,
        'timeframe': None,
        'at_ob_15m': False,
        'at_ob_4h': False,
        'engulfing_details': {},
        'confluence_score': 0.0
    }
    
    if df_5m is None or df_5m.empty:
        return result
    
    try:
        current_price = float(df_5m['close'].iloc[-1])
        
        # Check if price is in OB zones
        at_ob_15m = _price_in_zone(current_price, ob_15m_zone)
        at_ob_4h = _price_in_zone(current_price, ob_4h_zone)
        
        result['at_ob_15m'] = at_ob_15m
        result['at_ob_4h'] = at_ob_4h
        
        # If not at any OB, no point checking engulfing
        if not (at_ob_15m or at_ob_4h):
            return result
        
        # Check 5M engulfing first (higher priority)
        if direction == "LONG":
            engulfing_5m, details_5m = detect_bullish_engulfing(df_5m)
        else:
            engulfing_5m, details_5m = detect_bearish_engulfing(df_5m)
        
        if engulfing_5m:
            result['engulfing_confirmed'] = True
            result['timeframe'] = '5M'
            result['engulfing_details'] = details_5m
        elif df_3m is not None and not df_3m.empty:
            # Fallback to 3M
            if direction == "LONG":
                engulfing_3m, details_3m = detect_bullish_engulfing(df_3m)
            else:
                engulfing_3m, details_3m = detect_bearish_engulfing(df_3m)
            
            if engulfing_3m:
                result['engulfing_confirmed'] = True
                result['timeframe'] = '3M'
                result['engulfing_details'] = details_3m
        
        # Compute confluence score
        confluence = 0.0
        if result['engulfing_confirmed']:
            confluence += 40.0
        if at_ob_15m:
            confluence += 25.0
        if at_ob_4h:
            confluence += 25.0
        if result['engulfing_details'].get('volume_confirmed'):
            confluence += 10.0
        
        result['confluence_score'] = min(confluence, 100.0)
        
        if result['engulfing_confirmed']:
            logger.info(
                "[OB_ENGULFING_CONFLUENCE] %s | %s | tf=%s | at_15m=%d | at_4h=%d | score=%.1f",
                symbol,
                direction,
                result['timeframe'],
                1 if at_ob_15m else 0,
                1 if at_ob_4h else 0,
                result['confluence_score']
            )
        
        return result
        
    except Exception as e:
        logger.error(f"Error checking engulfing at OB for {symbol}: {e}")
        return result


def _price_in_zone(price: float, zone: Dict[str, float]) -> bool:
    """Check if price is within zone boundaries"""
    if not zone or 'low' not in zone or 'high' not in zone:
        return False
    return zone['low'] <= price <= zone['high']


def compute_fibonacci_entry(engulfing_candle: pd.Series, fib_level: float = 0.618) -> float:
    """
    Compute Fibonacci retracement entry price dari engulfing candle
    
    Args:
        engulfing_candle: Single row dari DataFrame (curr candle)
        fib_level: Fibonacci level (default 0.618)
    
    Returns:
        Entry price at fibonacci level
    """
    try:
        high = float(engulfing_candle['high'])
        low = float(engulfing_candle['low'])
        
        # For bullish engulfing: entry at 0.618 from low
        # For bearish engulfing: entry at 0.618 from high
        direction = 1 if engulfing_candle['close'] > engulfing_candle['open'] else -1
        
        if direction > 0:  # Bullish
            entry = low + ((high - low) * fib_level)
        else:  # Bearish
            entry = high - ((high - low) * fib_level)
        
        return round(entry, 8)
        
    except Exception as e:
        logger.error(f"Error computing fibonacci entry: {e}")
        return 0.0
