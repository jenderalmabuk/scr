from signal_copy.signal_schema import ParsedSignal, SignalSide
from signal_copy.telegram_formatter import build_parser_report
from signal_copy.validation_engine import ValidationResult, Verdict


def test_parser_report_renders_advisory_committee_block():
    sig = ParsedSignal(
        symbol="BTCUSDT",
        side=SignalSide.LONG,
        entry_low=100.0,
        entry_high=100.0,
        stop_loss=95.0,
        take_profits=[110.0],
    )
    result = ValidationResult(
        signal=sig,
        verdict=Verdict.VALID,
        score=80.0,
        metrics_snapshot={"price": 100.0, "rsi": 50.0},
    )
    report = build_parser_report(
        sig,
        result,
        source_name="test",
        committee={
            "mode": "advisory",
            "final_vote": "NO",
            "action": "NONE",
            "score": 72.0,
            "no_votes": 2,
            "warn_votes": 1,
            "top_reasons": ["entry_freshness: price already near TP1"],
        },
    )

    assert "COMMITTEE: NO" in report
    assert "entry_freshness" in report


def test_parser_report_hides_shadow_committee_block():
    sig = ParsedSignal(
        symbol="BTCUSDT",
        side=SignalSide.LONG,
        entry_low=100.0,
        entry_high=100.0,
        stop_loss=95.0,
        take_profits=[110.0],
    )
    result = ValidationResult(signal=sig, verdict=Verdict.VALID, score=80.0)
    report = build_parser_report(
        sig,
        result,
        source_name="test",
        committee={"mode": "shadow", "final_vote": "NO", "action": "NONE"},
    )

    assert "COMMITTEE:" not in report
