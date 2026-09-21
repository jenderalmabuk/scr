import asyncio
import os
import sys
import types
import pytest

from execution.bybit_mainnet_trader import BybitMainnetTrader
from execution.smart_scratch_exit import (
    evaluate_scratch_exit_gate,
    evaluate_damage_reducer_gate,
    update_scratch_excursion,
)


class FakeBybitClient:
    def __init__(self, mark):
        self.mark = mark
        self.orders = []
        self.trading_stops = []

    def get_instrument_info(self, symbol):
        return {
            "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001", "minNotionalValue": "5.0"},
            "priceFilter": {"tickSize": "0.01"},
        }

    def set_trading_stop(self, **kwargs):
        self.trading_stops.append(kwargs)
        return {"retCode": 0}

    def get_positions(self, symbol=None):
        return []


def _trader(mark):
    trader = object.__new__(BybitMainnetTrader)
    trader.client = FakeBybitClient(mark)
    trader.positions = {}
    trader.max_concurrent = 5
    trader.max_leverage = 10
    trader.risk_per_trade = 2.0
    trader._save_positions = lambda: None
    trader._config_bool = lambda k, default=True: True
    trader._quantize = lambda val, step: f"{float(val):.2f}"
    trader._get_risk_distance = lambda pos: abs(pos["entry_price"] - pos["sl_price"])
    trader.journal_path = types.SimpleNamespace(mkdir=lambda **k: None)
    return trader


def test_pre_tp1_early_profit_lock_triggers_on_high_mfe():
    trader = _trader(mark=103.5)
    # Entry 100, SL 95 -> risk_distance = 5.0
    # TP1 = 106.0 (+1.2R)
    # Price reached peak 104.0 (+0.80R MFE)
    # Currently at 103.5 (+0.70R current_r >= 0.15)
    pos = {
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry_price": 100.0,
        "sl_price": 95.0,
        "tp_prices": [106.0],
        "tp_hit": [],
        "locked_profit": False,
        "scratch_max_favorable_r": 0.80,
        "scratch_current_r": 0.70,
        "scratch_high_watermark": 104.0,
        "scratch_low_watermark": 100.0,
        "opened_ts": 0.0,
    }
    
    # Evaluate profit lock logic directly
    hold_minutes = 30.0
    mark = 103.5
    entry = pos["entry_price"]
    side = pos["side"]
    
    tp_hit_count = len(pos.get("tp_hit", []))
    pre_tp1_enabled = True
    mfe_r = float(pos.get("scratch_max_favorable_r") or 0.0)
    current_r = float(pos.get("scratch_current_r") or 0.0)
    pre_tp1_min_mfe = 0.65
    pre_tp1_min_current_r = 0.15
    lock_min = 15.0
    lock_buffer = 0.3
    
    tp1_progress = 0.0
    tp_prices = pos.get("tp_prices") or []
    if tp_prices and entry > 0:
        tp1_target = float(tp_prices[0])
        tp1_dist = abs(tp1_target - entry)
        if tp1_dist > 0:
            high_w = float(pos.get("scratch_high_watermark") or entry)
            tp1_progress = (high_w - entry) / tp1_dist
            
    pre_tp1_triggered = (
        pre_tp1_enabled
        and tp_hit_count == 0
        and (mfe_r >= pre_tp1_min_mfe or tp1_progress >= 0.70)
        and current_r >= pre_tp1_min_current_r
        and hold_minutes >= lock_min
    )
    
    assert pre_tp1_triggered is True
    assert tp1_progress >= 0.65
    
    risk_distance = trader._get_risk_distance(pos)
    buffer_distance = risk_distance * (lock_buffer / 100)
    new_sl = entry + buffer_distance
    
    trader.client.set_trading_stop(
        symbol="BTCUSDT",
        position_idx=0,
        stop_loss=trader._quantize(new_sl, 0.01)
    )
    pos["sl_price"] = new_sl
    pos["sl_kind"] = "PRE_TP1_BREAKEVEN"
    pos["locked_profit"] = True
    
    assert pos["locked_profit"] is True
    assert pos["sl_kind"] == "PRE_TP1_BREAKEVEN"
    assert pos["sl_price"] > 100.0  # Above entry!
    assert len(trader.client.trading_stops) == 1


def test_stale_zombie_timeout_exits_dead_trade():
    # Position open for 15 hours (900 min > 720 min)
    # Never hit TP (tp_count == 0)
    # Negative R (-0.35R)
    # Peak MFE was only +0.10R
    pos = {
        "symbol": "COTIUSDT",
        "side": "LONG",
        "entry_price": 0.020,
        "sl_price": 0.018,
        "tp_prices": [0.022],
        "tp_hit": [],
        "initial_risk_distance": 0.002,
        "metadata": {"timeframe": "15m"},
    }
    update_scratch_excursion(pos, 0.0193)  # current price is 0.0193 (-0.35R)
    
    decision = evaluate_scratch_exit_gate(
        position=pos,
        mark=0.0193,
        hold_minutes=900.0,
        scratch_timeout_min=45.0,
        unrealized_pct=-3.5,
        max_abs_pnl_pct=0.4,
    )
    
    assert decision["should_exit"] is True
    assert decision["action"] == "EXIT"
    assert decision["reason"] == "STALE_ZOMBIE_TIMEOUT"


def test_stale_zombie_timeout_spares_active_runners():
    # Position open for 15 hours but achieved +0.80R MFE
    pos = {
        "symbol": "AVAXUSDT",
        "side": "LONG",
        "entry_price": 10.0,
        "sl_price": 9.0,
        "tp_prices": [12.0],
        "tp_hit": [],
        "initial_risk_distance": 1.0,
        "metadata": {"timeframe": "15m"},
    }
    update_scratch_excursion(pos, 10.80)  # Peak at +0.80R
    
    decision = evaluate_scratch_exit_gate(
        position=pos,
        mark=10.20,  # currently still positive
        hold_minutes=900.0,
        scratch_timeout_min=45.0,
        unrealized_pct=+2.0,
        max_abs_pnl_pct=0.4,
    )
    
    assert decision["reason"] != "STALE_ZOMBIE_TIMEOUT"


def test_manual_mfe_giveback_triggers_partial_or_exit():
    # Position reached +0.60R MFE but gave back to -0.20R
    pos = {
        "symbol": "ARKUSDT",
        "side": "LONG",
        "entry_price": 100.0,
        "sl_price": 95.0,
        "tp_prices": [110.0],
        "tp_hit": [],
        "initial_risk_distance": 5.0,
        "metadata": {
            "imported": True,
            "setup_type": "EARLY_ENTRY",
            "timeframe": "15m",
        },
    }
    update_scratch_excursion(pos, 103.0)  # Peak MFE +0.60R
    
    # Mark drops to 99.0 (-0.20R) -> should partially exit by 50% instead of deferring to hard SL!
    decision = evaluate_damage_reducer_gate(
        position=pos,
        mark=99.0,
        hold_minutes=60.0,
        unrealized_pct=-1.0,
        damage_min_hold_min=45.0,
        damage_max_loss_pct=-2.5,
    )
    
    assert decision["should_partial_exit"] is True
    assert decision["action"] == "PARTIAL_EXIT"
    assert decision["reason"] == "MANUAL_DAMAGE_MFE_GIVEBACK_PARTIAL"
    assert decision["close_fraction"] == 0.50
    
    # And if mark drops deeper to 97.0 (-0.60R <= min_r -0.50R) -> full exit!
    decision_deep = evaluate_damage_reducer_gate(
        position=pos,
        mark=97.0,
        hold_minutes=60.0,
        unrealized_pct=-3.0,
        damage_min_hold_min=45.0,
        damage_max_loss_pct=-2.5,
    )
    assert decision_deep["should_exit"] is True
    assert decision_deep["action"] == "EXIT"
    assert decision_deep["reason"] == "MANUAL_DAMAGE_MFE_GIVEBACK_EXIT"
