from execution.manual_signal_tools import normalize_manual_tp_ladder


def test_manual_single_2r_tp_expands_with_tp1_at_1r():
    ladder, info = normalize_manual_tp_ladder("LONG", 100.0, 95.0, [110.0])

    assert info["source"] == "r_ladder"
    assert info["final_rr"] == 2.0
    assert ladder == [105.0, 106.666667, 108.333333, 110.0]


def test_manual_single_1_5r_tp_is_split_for_early_derisk():
    ladder, info = normalize_manual_tp_ladder("LONG", 100.0, 90.0, [115.0])

    assert info["source"] == "r_ladder"
    assert info["rungs"] == [0.5, 1.0, 1.25, 1.5]
    assert ladder == [105.0, 110.0, 112.5, 115.0]


def test_manual_short_single_final_tp_expands_in_correct_direction():
    ladder, info = normalize_manual_tp_ladder("SHORT", 100.0, 105.0, [90.0])

    assert info["source"] == "r_ladder"
    assert ladder == [95.0, 93.333333, 91.666667, 90.0]


def test_manual_multi_tp_ladder_is_preserved():
    ladder, info = normalize_manual_tp_ladder("LONG", 100.0, 95.0, [104.0, 107.0, 110.0])

    assert info["source"] == "provider"
    assert ladder == [104.0, 107.0, 110.0]


def test_manual_no_tp_uses_default_2r_ladder():
    ladder, info = normalize_manual_tp_ladder("LONG", 100.0, 95.0, [])

    assert info["source"] == "r_ladder"
    assert info["final_rr"] == 2.0
    assert ladder[-1] == 110.0
