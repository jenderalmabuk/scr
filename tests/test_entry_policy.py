from signal_copy.entry_policy import EntryAction, route_entry
from signal_copy.signal_schema import ParsedSignal, SignalSide


def _signal(side=SignalSide.LONG, entry_low=99.0, entry_high=101.0, sl=95.0, tps=None):
    return ParsedSignal(
        symbol="BTCUSDT",
        side=side,
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=sl,
        take_profits=tps or [105.0, 110.0],
    )


def test_high_score_clean_signal_routes_market_inside_zone(monkeypatch):
    monkeypatch.setattr("signal_copy.validation_config.AUTO_MARKET_MIN_SCORE", 65.0)
    sig = _signal()
    decision = route_entry(sig, 100.0, validation_score=70.0, metrics={"flow_direction": "BOTH_ALLOWED"})

    assert decision.action == EntryAction.MARKET
    assert decision.code == "PRICE_INSIDE_ENTRY_ZONE"


def test_mid_score_inside_zone_routes_to_pullback_limit(monkeypatch):
    monkeypatch.setattr("signal_copy.validation_config.AUTO_MARKET_MIN_SCORE", 65.0)
    monkeypatch.setattr("signal_copy.validation_config.AUTO_LIMIT_MIN_SCORE", 55.0)
    sig = _signal()
    decision = route_entry(sig, 100.0, validation_score=60.0, metrics={"flow_direction": "BOTH_ALLOWED"})

    assert decision.action == EntryAction.LIMIT
    assert decision.entry == 99.0
    assert decision.code == "AUTO_SCORE_MARKET_TO_LIMIT"


def test_adversarial_no_below_override_blocks_auto_market_when_enabled(monkeypatch):
    monkeypatch.setattr("signal_copy.validation_config.ADVERSARIAL_MODE", "soft")
    monkeypatch.setattr("signal_copy.validation_config.ADVERSARIAL_NO_OVERRIDE_MIN_SCORE", 75.0)
    sig = _signal(entry_low=100.0, entry_high=100.0)
    decision = route_entry(
        sig,
        100.0,
        validation_score=70.0,
        metrics={
            "flow_direction": "BOTH_ALLOWED",
            "legacy_adversarial": {"approved": False, "reason": "bad chase"},
        },
    )

    assert decision.action == EntryAction.WAIT_CONFIRMATION
    assert decision.code == "ADVERSARIAL_NO_BELOW_OVERRIDE_SCORE"


def test_adversarial_off_does_not_block_auto_market(monkeypatch):
    monkeypatch.setattr("signal_copy.validation_config.ADVERSARIAL_MODE", "off")
    monkeypatch.setattr("signal_copy.validation_config.ADVERSARIAL_NO_OVERRIDE_MIN_SCORE", 75.0)
    sig = _signal(entry_low=100.0, entry_high=100.0)
    decision = route_entry(
        sig,
        100.0,
        validation_score=70.0,
        metrics={
            "flow_direction": "BOTH_ALLOWED",
            "legacy_adversarial": {"approved": False, "reason": "bad chase"},
        },
    )

    assert decision.action == EntryAction.MARKET
    assert decision.code == "PRICE_INSIDE_ENTRY_ZONE"


def test_committee_shadow_does_not_block_auto_market(monkeypatch):
    monkeypatch.setattr("signal_copy.validation_config.COMMITTEE_MODE", "shadow")
    monkeypatch.setattr("signal_copy.validation_config.ADVERSARIAL_NO_OVERRIDE_MIN_SCORE", 75.0)
    sig = _signal(entry_low=100.0, entry_high=100.0)
    decision = route_entry(
        sig,
        100.0,
        validation_score=70.0,
        metrics={
            "flow_direction": "BOTH_ALLOWED",
            "adversarial_committee": {"final_vote": "NO", "no_votes": 1, "warn_votes": 0},
        },
    )

    assert decision.action == EntryAction.MARKET
    assert decision.code == "PRICE_INSIDE_ENTRY_ZONE"


def test_flow_no_trade_prevents_market_but_allows_limit(monkeypatch):
    monkeypatch.setattr("signal_copy.validation_config.FLOW_NO_TRADE_MARKET_BLOCK", True)
    sig = _signal()
    decision = route_entry(sig, 100.0, validation_score=80.0, metrics={"flow_direction": "NO_TRADE"})

    assert decision.action == EntryAction.LIMIT
    assert decision.code == "FLOW_NO_TRADE_MARKET_BLOCK"


def test_mid_score_single_entry_routes_to_smart_pullback_limit(monkeypatch):
    monkeypatch.setattr("signal_copy.validation_config.AUTO_MARKET_MIN_SCORE", 65.0)
    monkeypatch.setattr("signal_copy.validation_config.AUTO_LIMIT_MIN_SCORE", 55.0)
    monkeypatch.setattr("signal_copy.validation_config.SMART_PULLBACK_R_FRACTION", 0.15)
    # Entry 100.0, SL 95.0 -> risk = 5.0 -> 0.15R = 0.75
    sig = _signal(entry_low=100.0, entry_high=100.0, sl=95.0)
    decision = route_entry(sig, 100.0, validation_score=60.0, metrics={"flow_direction": "BOTH_ALLOWED"})

    assert decision.action == EntryAction.LIMIT
    assert round(decision.entry, 2) == 99.25
    assert decision.code == "AUTO_SCORE_MARKET_TO_LIMIT"


def test_mid_score_single_entry_price_above_entry_routes_to_retest(monkeypatch):
    monkeypatch.setattr("signal_copy.validation_config.AUTO_MARKET_MIN_SCORE", 65.0)
    monkeypatch.setattr("signal_copy.validation_config.AUTO_LIMIT_MIN_SCORE", 55.0)
    sig = _signal(entry_low=100.0, entry_high=100.0, sl=95.0)
    # Price is 100.5 (above entry for Long)
    decision = route_entry(sig, 100.5, validation_score=60.0, metrics={"flow_direction": "BOTH_ALLOWED"})

    assert decision.action == EntryAction.LIMIT
    assert decision.entry == 100.0
    assert decision.code == "AUTO_SCORE_MARKET_TO_LIMIT"


def test_mid_score_single_entry_short_routes_to_smart_pullback_limit(monkeypatch):
    monkeypatch.setattr("signal_copy.validation_config.AUTO_MARKET_MIN_SCORE", 65.0)
    monkeypatch.setattr("signal_copy.validation_config.AUTO_LIMIT_MIN_SCORE", 55.0)
    monkeypatch.setattr("signal_copy.validation_config.SMART_PULLBACK_R_FRACTION", 0.15)
    # Short: Entry 100.0, SL 105.0 -> risk = 5.0 -> 0.15R = 0.75
    sig = _signal(side=SignalSide.SHORT, entry_low=100.0, entry_high=100.0, sl=105.0, tps=[95.0, 90.0])
    decision = route_entry(sig, 100.0, validation_score=60.0, metrics={"flow_direction": "BOTH_ALLOWED"})

    assert decision.action == EntryAction.LIMIT
    assert round(decision.entry, 2) == 100.75
    assert decision.code == "AUTO_SCORE_MARKET_TO_LIMIT"


def test_solusdt_real_signal_routes_to_smart_pullback_limit(monkeypatch):
    monkeypatch.setattr("signal_copy.validation_config.AUTO_MARKET_MIN_SCORE", 65.0)
    monkeypatch.setattr("signal_copy.validation_config.AUTO_LIMIT_MIN_SCORE", 55.0)
    monkeypatch.setattr("signal_copy.validation_config.SMART_PULLBACK_R_FRACTION", 0.15)
    # Exact SOL setup from incident
    sig = _signal(
        side=SignalSide.LONG,
        entry_low=116.77,
        entry_high=116.77,
        sl=114.333,
        tps=[119.334],
    )
    decision = route_entry(sig, 116.76, validation_score=60.4, metrics={"flow_direction": "BOTH_ALLOWED"})

    assert decision.action == EntryAction.LIMIT
    assert decision.code == "AUTO_SCORE_MARKET_TO_LIMIT"
    # Risk = 116.77 - 114.333 = 2.437. 0.15R = 0.36555. 116.76 - 0.36555 = 116.39445
    assert round(decision.entry, 3) == 116.394
