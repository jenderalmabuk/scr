import os

from execution.smart_scratch_exit import (
    calculate_smart_scratch_timeout,
    evaluate_damage_reducer_gate,
    evaluate_scratch_exit_gate,
    infer_timeframe,
    update_scratch_excursion,
)


def _pos(side="LONG", entry=100.0, sl=95.0, tps=None):
    return {
        "symbol": "TESTUSDT",
        "side": side,
        "entry_price": entry,
        "sl_price": sl,
        "tp_prices": tps or [110.0],
        "tp_hit": [],
        "metadata": {
            "source_chat_id": "-1002128890109",
            "adv_snapshot": {
                "regime_label": "TRENDING",
                "validation_score": 82,
                "mtf_alignment": {"score": 70},
            },
        },
    }


def test_smart_timeout_reads_live_bybit_tp_and_metadata():
    pos = _pos(tps=[106.0])

    assert infer_timeframe(pos, {"-1002128890109": "1h"}) == "1h"
    timeout = calculate_smart_scratch_timeout(pos, channel_config={"-1002128890109": "1h"})

    assert timeout >= 120.0


def test_scratch_exit_waits_after_confirmed_tp():
    pos = _pos()
    pos["tp_hit"] = [0]

    decision = evaluate_scratch_exit_gate(pos, 100.1, 90.0, 45.0, 0.1, 0.4)

    assert decision["should_exit"] is False
    assert decision["reason"] == "TP_ALREADY_HIT"


def test_scratch_exit_defers_after_favorable_progress():
    pos = _pos()
    update_scratch_excursion(pos, 104.0)

    decision = evaluate_scratch_exit_gate(pos, 100.1, 90.0, 45.0, 0.1, 0.4)

    assert decision["should_exit"] is False
    assert decision["reason"] == "PROGRESS_SEEN"
    assert decision["max_favorable_r"] >= 0.6


def test_scratch_exit_closes_stale_near_breakeven_without_progress():
    pos = _pos()

    decision = evaluate_scratch_exit_gate(pos, 100.1, 90.0, 45.0, 0.1, 0.4)

    assert decision["should_exit"] is True
    assert decision["reason"] == "STALE_NEAR_BREAKEVEN"


def test_short_scratch_excursion_tracks_favorable_drop():
    pos = _pos(side="SHORT", entry=100.0, sl=105.0, tps=[90.0])
    update_scratch_excursion(pos, 96.0)

    decision = evaluate_scratch_exit_gate(pos, 99.9, 90.0, 45.0, 0.1, 0.4)

    assert decision["should_exit"] is False
    assert decision["reason"] == "PROGRESS_SEEN"
    assert decision["max_favorable_r"] >= 0.6


def test_normal_setup_defers_after_point_35r_progress():
    old = os.environ.get("SCRATCH_EXIT_PROGRESS_R_THRESHOLD")
    os.environ["SCRATCH_EXIT_PROGRESS_R_THRESHOLD"] = "0.35"
    try:
        pos = _pos()
        update_scratch_excursion(pos, 101.8)

        decision = evaluate_scratch_exit_gate(pos, 100.1, 90.0, 45.0, 0.1, 0.4)

        assert decision["should_exit"] is False
        assert decision["reason"] == "PROGRESS_SEEN"
        assert decision["max_favorable_r"] >= 0.35
    finally:
        if old is None:
            os.environ.pop("SCRATCH_EXIT_PROGRESS_R_THRESHOLD", None)
        else:
            os.environ["SCRATCH_EXIT_PROGRESS_R_THRESHOLD"] = old


def test_strong_setup_extends_timeout_before_scratch():
    pos = _pos()
    pos["metadata"]["adv_snapshot"].update({
        "tradingview": {"score": 76},
        "flow_direction": "LONG_ONLY",
        "data_stale": False,
    })

    decision = evaluate_scratch_exit_gate(pos, 100.1, 60.0, 45.0, 0.1, 0.4)

    assert decision["should_exit"] is False
    assert decision["reason"] == "STRONG_SETUP_EXTENDED_TIMEOUT"
    assert decision["effective_scratch_timeout_min"] == 67.5
    assert decision["setup_context"]["strong_setup"] is True


def test_weak_setup_uses_fast_timeout_without_progress():
    pos = _pos()
    pos["metadata"]["adv_snapshot"].update({
        "mtf_alignment": {"score": 40},
        "tradingview": {"score": 42},
        "flow_direction": "NO_TRADE",
        "oi_change_15m_pct": -1.2,
        "rsi": 40,
    })

    decision = evaluate_scratch_exit_gate(pos, 100.1, 50.0, 120.0, 0.1, 0.4)

    assert decision["should_exit"] is True
    assert decision["reason"] == "WEAK_STALE_NEAR_BREAKEVEN"
    assert decision["effective_scratch_timeout_min"] == 45.0
    assert decision["setup_context"]["weak_setup"] is True


def test_manual_early_entry_defers_small_float_while_structure_intact():
    pos = _pos(entry=100.0, sl=95.0, tps=[105.0, 110.0])
    pos["metadata"].update({"imported": True, "setup_type": "EARLY_ENTRY"})
    pos["metadata"]["adv_snapshot"].update({
        "mtf_alignment": {"score": 30},
        "tradingview": {"score": 30},
        "flow_direction": "NO_TRADE",
    })

    decision = evaluate_scratch_exit_gate(pos, 99.5, 400.0, 45.0, -0.5, 0.6)

    assert decision["should_exit"] is False
    assert decision["reason"] == "MANUAL_STRUCTURE_INTACT_FLOAT_OK"
    assert decision["manual_structure_profile"]["active"] is True
    assert decision["current_r"] > -0.5


def test_manual_early_entry_exits_only_after_hard_r():
    pos = _pos(entry=100.0, sl=95.0, tps=[105.0, 110.0])
    pos["metadata"].update({"imported": True, "setup_type": "EARLY_ENTRY"})

    decision = evaluate_scratch_exit_gate(pos, 96.0, 200.0, 45.0, -4.0, 5.0)

    assert decision["should_exit"] is True
    assert decision["reason"] == "MANUAL_STRUCTURE_HARD_R"
    assert decision["current_r"] <= -0.75


def test_manual_early_entry_exits_when_structure_breaks():
    pos = _pos(entry=100.0, sl=95.0, tps=[105.0, 110.0])
    pos["metadata"].update({
        "imported": True,
        "setup_type": "TRENDLINE_HOLD",
        "structure_low": 99.0,
    })

    decision = evaluate_scratch_exit_gate(pos, 98.9, 200.0, 45.0, -1.1, 2.0)

    assert decision["should_exit"] is True
    assert decision["reason"] == "MANUAL_STRUCTURE_BROKEN"


def test_damage_reducer_waits_without_reversal_evidence():
    pos = _pos(entry=100.0, sl=95.0, tps=[105.0])

    decision = evaluate_damage_reducer_gate(pos, 97.5, 60.0, -2.5, 45.0, -2.5)

    assert decision["should_exit"] is False
    assert decision["reason"] == "DAMAGE_REVERSAL_NOT_CONFIRMED"


def test_damage_reducer_closes_confirmed_reversal():
    pos = _pos(entry=100.0, sl=95.0, tps=[105.0])
    pos["metadata"]["adv_snapshot"].update({
        "mtf_alignment": {"score": 30},
        "tradingview": {"score": 30},
        "flow_direction": "NO_TRADE",
    })

    decision = evaluate_damage_reducer_gate(pos, 97.5, 60.0, -2.5, 45.0, -2.5)

    assert decision["should_exit"] is True
    assert decision["reason"] == "DAMAGE_CONFIRMED_REVERSAL"
    assert decision["reversal_score"] >= 2


def test_manual_damage_reducer_protects_structure_hold_until_hard_r():
    pos = _pos(entry=100.0, sl=95.0, tps=[105.0])
    pos["metadata"].update({"imported": True, "setup_type": "EARLY_ENTRY"})
    pos["metadata"]["adv_snapshot"].update({
        "mtf_alignment": {"score": 30},
        "tradingview": {"score": 30},
        "flow_direction": "NO_TRADE",
    })

    protected = evaluate_damage_reducer_gate(pos, 97.6, 60.0, -2.4, 45.0, -2.0)
    hard = evaluate_damage_reducer_gate(pos, 96.0, 60.0, -4.0, 45.0, -2.0)

    assert protected["should_exit"] is False
    assert protected["reason"] == "MANUAL_DAMAGE_STRUCTURE_PROTECTED"
    assert hard["should_exit"] is True
    assert hard["reason"] == "MANUAL_DAMAGE_HARD_R"


def test_mfe_giveback_partials_before_deep_loss():
    pos = _pos(entry=100.0, sl=95.0, tps=[105.0])
    update_scratch_excursion(pos, 103.0)

    decision = evaluate_damage_reducer_gate(pos, 99.0, 60.0, -1.0, 45.0, -2.5)

    assert decision["should_exit"] is False
    assert decision["should_partial_exit"] is True
    assert decision["reason"] == "DAMAGE_MFE_GIVEBACK_PARTIAL"
    assert decision["mfe_giveback"] is True
    assert decision["max_favorable_r"] >= 0.5
    assert decision["tp1_progress"] == 0.0


def test_mfe_giveback_full_exits_when_reversal_confirmed():
    pos = _pos(entry=100.0, sl=95.0, tps=[105.0])
    pos["metadata"]["adv_snapshot"].update({
        "mtf_alignment": {"score": 30},
        "tradingview": {"score": 30},
        "flow_direction": "NO_TRADE",
    })
    update_scratch_excursion(pos, 103.0)

    decision = evaluate_damage_reducer_gate(pos, 99.0, 60.0, -1.0, 45.0, -2.5)

    assert decision["should_exit"] is True
    assert decision["reason"] == "DAMAGE_MFE_GIVEBACK_EXIT"
    assert decision["reversal_score"] >= 2


def test_mfe_giveback_skips_after_tp1_hit():
    pos = _pos(entry=100.0, sl=95.0, tps=[105.0])
    pos["tp_hit"] = [0]
    update_scratch_excursion(pos, 103.0)

    decision = evaluate_damage_reducer_gate(pos, 99.0, 60.0, -1.0, 45.0, -2.5)

    assert decision["should_exit"] is False
    assert decision["should_partial_exit"] is False
    assert decision["mfe_giveback"] is False
    assert decision["tp_hit_count"] == 1


def test_reversal_first_partials_before_min_adverse_r():
    pos = _pos(entry=100.0, sl=95.0, tps=[105.0])
    pos["metadata"]["adv_snapshot"].update({
        "mtf_alignment": {"score": 30},
        "tradingview": {"score": 30},
        "flow_direction": "NO_TRADE",
    })

    decision = evaluate_damage_reducer_gate(pos, 99.0, 60.0, -1.0, 45.0, -2.5)

    assert decision["should_exit"] is False
    assert decision["should_partial_exit"] is True
    assert decision["reason"] == "DAMAGE_REVERSAL_FIRST_PARTIAL"
    assert decision["current_r"] == -0.2


def test_local_bottom_guard_partials_instead_of_full_hard_r():
    pos = _pos(entry=100.0, sl=95.0, tps=[105.0])
    update_scratch_excursion(pos, 94.5)

    decision = evaluate_damage_reducer_gate(pos, 95.7, 60.0, -4.3, 45.0, -2.5)

    assert decision["should_exit"] is False
    assert decision["should_partial_exit"] is True
    assert decision["reason"] == "DAMAGE_HARD_R_LOCAL_BOTTOM_GUARD"
    assert decision["local_bottom_guard"] is True
    assert decision["recovery_from_worst_r"] >= 0.15


def test_manual_mfe_giveback_does_not_close_structure_hold():
    pos = _pos(entry=100.0, sl=95.0, tps=[105.0])
    pos["metadata"].update({"imported": True, "setup_type": "EARLY_ENTRY"})
    update_scratch_excursion(pos, 103.0)

    decision = evaluate_damage_reducer_gate(pos, 99.0, 60.0, -1.0, 45.0, -2.5)

    assert decision["should_exit"] is False
    assert decision["should_partial_exit"] is False
    assert decision["reason"] == "MANUAL_DAMAGE_MFE_GIVEBACK_PROTECTED"
