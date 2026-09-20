from signal_copy.adversarial_committee import ACTION_NONE, ACTION_DOWNGRADE, NO, WARN, evaluate_committee
from signal_copy.signal_schema import ParsedSignal, SignalSide
from signal_copy.validation_engine import ValidationResult, Verdict


def _signal(side=SignalSide.LONG, entry=100.0, sl=95.0, tps=None):
    return ParsedSignal(
        symbol="BTCUSDT",
        side=side,
        entry_low=entry,
        entry_high=entry,
        stop_loss=sl,
        take_profits=tps or [110.0, 120.0],
    )


def _result(sig, score=80.0, verdict=Verdict.VALID):
    return ValidationResult(signal=sig, verdict=verdict, score=score)


def test_committee_flags_all_targets_passed_without_blocking_shadow(monkeypatch):
    monkeypatch.setattr("signal_copy.signal_copy_config.COMMITTEE_MODE", "shadow")
    sig = _signal(tps=[101.0, 102.0])
    decision = evaluate_committee(sig, _result(sig), {"price": 103.0})

    assert decision.final_vote == NO
    assert decision.action == ACTION_NONE
    assert any(v.specialist == "entry_freshness" and v.vote == NO for v in decision.votes)


def test_committee_flags_bad_rr(monkeypatch):
    monkeypatch.setattr("signal_copy.signal_copy_config.COMMITTEE_MODE", "shadow")
    sig = _signal(entry=100.0, sl=90.0, tps=[105.0])
    decision = evaluate_committee(sig, _result(sig), {"price": 100.0})

    assert decision.final_vote == NO
    assert any(v.specialist == "geometry_rr" and v.vote == NO for v in decision.votes)


def test_committee_warns_on_partial_flow_conflict(monkeypatch):
    monkeypatch.setattr("signal_copy.signal_copy_config.COMMITTEE_MODE", "shadow")
    sig = _signal()
    decision = evaluate_committee(sig, _result(sig), {
        "price": 100.0,
        "flow_direction": "NO_TRADE",
        "cvd_zscore": 0.1,
    })

    assert any(v.specialist == "flow_alignment" and v.vote == WARN for v in decision.votes)


def test_committee_soft_downgrades_only_below_floor(monkeypatch):
    monkeypatch.setattr("signal_copy.signal_copy_config.COMMITTEE_MODE", "soft")
    monkeypatch.setattr("signal_copy.signal_copy_config.COMMITTEE_SOFT_FLOOR", 90.0)
    sig = _signal(tps=[101.0, 102.0])

    low_score = evaluate_committee(sig, _result(sig, score=70.0), {"price": 103.0})
    high_score = evaluate_committee(sig, _result(sig, score=95.0), {"price": 103.0})

    assert low_score.action == ACTION_DOWNGRADE
    assert high_score.action == ACTION_NONE
