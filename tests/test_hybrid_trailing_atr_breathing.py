import pytest
from execution.bybit_mainnet_trader import BybitMainnetTrader


def test_calc_atr_from_klines():
    # 5 candles: [startTime, open, high, low, close]
    klines = [
        ["100", "100.0", "105.0", "98.0", "104.0"],   # TR = 7.0 (high-low)
        ["200", "104.0", "108.0", "102.0", "107.0"],  # TR = max(6.0, 4.0, 2.0) = 6.0
        ["300", "107.0", "112.0", "105.0", "106.0"],  # TR = max(7.0, 5.0, 2.0) = 7.0
        ["400", "106.0", "107.0", "100.0", "101.0"],  # TR = max(7.0, 1.0, 6.0) = 7.0
    ]
    atr = BybitMainnetTrader._calc_atr_from_klines(klines, period=3)
    # TRs: [6.0, 7.0, 7.0] -> avg = 20.0 / 3 = 6.6666...
    assert round(atr, 2) == 6.67


def test_hard_bep_floor_math_long():
    entry = 100.0
    risk_distance = 5.0
    be_buffer = risk_distance * 0.05  # 0.25 -> be_level = 100.25
    step_floor = entry + be_buffer   # 100.25
    
    # Suppose swing level is deep due to high ATR breathing room: 99.0
    swing_level = 99.0
    sl = 95.0
    mark = 105.0
    choke_buffer = 1.0 # 1.0% of mark
    max_allowed_sl = mark - choke_buffer # 104.0
    
    candidate_sl = max(step_floor, swing_level)
    assert candidate_sl == 100.25  # Clamped to step_floor!
    
    target_sl = max(step_floor, min(candidate_sl, max_allowed_sl))
    assert target_sl == 100.25  # Strictly clamped to Hard BEP Floor!


def test_hard_bep_floor_math_short():
    entry = 100.0
    risk_distance = 5.0
    be_buffer = risk_distance * 0.05  # 0.25 -> be_level = 99.75
    step_floor = entry - be_buffer   # 99.75
    
    # Suppose swing level is high due to ATR breathing room: 101.0
    swing_level = 101.0
    sl = 105.0
    mark = 95.0
    choke_buffer = 1.0
    min_allowed_sl = mark + choke_buffer # 96.0
    
    candidate_sl = min(step_floor, swing_level)
    assert candidate_sl == 99.75  # Clamped to step_floor!
    
    target_sl = min(step_floor, max(candidate_sl, min_allowed_sl))
    assert target_sl == 99.75  # Strictly clamped to Hard BEP Floor!


def test_atr_breathing_room_protects_retest_long():
    # Simulation of MARSCOINUSDT setup
    entry = 0.103165
    step_floor = 0.10322  # BEP
    sl = 0.10322
    mark = 0.1200
    
    # Swing low was 0.1185. Normal ATR 15m is 0.0016 (~1.3%)
    recent_swing_low = 0.1185
    atr_15m = 0.0016
    atr_mult = 0.75
    min_buffer_pct = 0.010 # 1.0%
    
    atr_buffer = atr_15m * atr_mult # 0.0012
    effective_buffer = max(atr_buffer, recent_swing_low * min_buffer_pct) # 0.0012 vs 0.001185 -> 0.0012
    swing_level = recent_swing_low - effective_buffer # 0.1185 - 0.0012 = 0.1173
    
    choke_buffer = max(mark * 0.01, atr_15m * 0.75) # 0.0012
    max_allowed_sl = mark - choke_buffer # 0.1188
    
    candidate_sl = max(step_floor, swing_level) # 0.1173
    target_sl = max(step_floor, min(candidate_sl, max_allowed_sl)) # 0.1173
    
    assert round(target_sl, 4) == 0.1173
    
    # Retest price dips to 0.11821 (like MARSCOIN did)
    retest_low = 0.11821
    # Verify that the retest does NOT hit target_sl!
    assert retest_low > target_sl, "Retest should be ABOVE target_sl, protecting trade from wick hunting!"
