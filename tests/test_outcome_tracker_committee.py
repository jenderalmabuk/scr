import asyncio
import json

from signal_copy.outcome_tracker import SignalOutcomeTracker
from signal_copy.signal_schema import ParsedSignal, SignalSide


def _signal():
    return ParsedSignal(
        symbol="BTCUSDT",
        side=SignalSide.LONG,
        entry_low=100.0,
        entry_high=100.0,
        stop_loss=95.0,
        take_profits=[110.0],
        source_name="test",
        source_chat_id=123,
    )


def test_outcome_tracker_persists_committee_snapshot(monkeypatch, tmp_path):
    import signal_copy.outcome_tracker as mod

    monkeypatch.setattr(mod, "STORE", tmp_path / "signal_outcomes.json")
    tracker = SignalOutcomeTracker()
    committee = {"final_vote": "NO", "action": "NONE", "score": 72.0, "no_votes": 2}

    tracker.track(_signal(), {
        "price": 100.0,
        "validation_score": 80.0,
        "adversarial_committee": committee,
    }, verdict="VALID", execution_intended=True)

    saved = json.loads((tmp_path / "signal_outcomes.json").read_text())
    snap = next(iter(saved.values()))
    assert snap["adversarial_committee"] == committee
    assert snap["validation_score"] == 80.0


def test_outcome_tracker_writes_committee_resolution_journal(monkeypatch, tmp_path):
    import signal_copy.outcome_tracker as mod

    class FakeChannelTracker:
        def record_trade(self, **kwargs):
            self.last = kwargs

    monkeypatch.setattr(mod, "STORE", tmp_path / "signal_outcomes.json")
    monkeypatch.setattr(mod, "get_tracker", lambda: FakeChannelTracker())
    monkeypatch.setattr("signal_copy.signal_copy_config.COMMITTEE_OUTCOME_JOURNAL", str(tmp_path / "committee_outcomes.jsonl"))

    tracker = SignalOutcomeTracker()
    tracker.track(_signal(), {
        "price": 100.0,
        "validation_score": 80.0,
        "adversarial_committee": {
            "final_vote": "NO",
            "action": "NONE",
            "score": 72.0,
            "no_votes": 2,
            "top_reasons": ["entry_freshness: price already near TP1"],
        },
    }, verdict="VALID", execution_intended=True)

    async def price_fn(symbol):
        return 110.0

    assert asyncio.run(tracker.resolve_open(price_fn)) == 1
    rows = (tmp_path / "committee_outcomes.jsonl").read_text().strip().splitlines()
    row = json.loads(rows[-1])
    assert row["committee_final_vote"] == "NO"
    assert row["exit_reason"] == "TP1"


def test_outcome_tracker_resolves_tp1_from_1m_candle(monkeypatch, tmp_path):
    import signal_copy.outcome_tracker as mod

    class FakeChannelTracker:
        def record_trade(self, **kwargs):
            self.last = kwargs

    fake = FakeChannelTracker()
    monkeypatch.setattr(mod, "STORE", tmp_path / "signal_outcomes.json")
    monkeypatch.setattr(mod, "get_tracker", lambda: fake)

    tracker = SignalOutcomeTracker()
    tracker.track(_signal(), {"price": 100.0}, verdict="VALID", execution_intended=False)
    snap = next(iter(tracker._open.values()))

    async def price_fn(symbol):
        return 100.0

    async def candles_fn(symbol, since_ts):
        return [{"open_time": since_ts + 60, "high": 110.5, "low": 99.5, "close": 109.0}]

    assert asyncio.run(tracker.resolve_open(price_fn, candles_fn)) == 1
    assert fake.last["exit_reason"] == "TP1"
    assert fake.last["pnl_pct"] == 10.0
    assert tracker.open_count() == 0


def test_outcome_tracker_counts_same_candle_tp_sl_as_ambiguous_loss(monkeypatch, tmp_path):
    import signal_copy.outcome_tracker as mod

    class FakeChannelTracker:
        def record_trade(self, **kwargs):
            self.last = kwargs

    fake = FakeChannelTracker()
    monkeypatch.setattr(mod, "STORE", tmp_path / "signal_outcomes.json")
    monkeypatch.setattr(mod, "get_tracker", lambda: fake)

    tracker = SignalOutcomeTracker()
    tracker.track(_signal(), {"price": 100.0}, verdict="VALID", execution_intended=False)

    async def price_fn(symbol):
        return 100.0

    async def candles_fn(symbol, since_ts):
        return [{"open_time": since_ts + 60, "high": 111.0, "low": 94.0, "close": 101.0}]

    assert asyncio.run(tracker.resolve_open(price_fn, candles_fn)) == 1
    assert fake.last["exit_reason"] == "SL_AMBIGUOUS"
    assert fake.last["pnl_pct"] == -5.0


def test_outcome_tracker_skips_invalid_short_tp_sl_geometry(monkeypatch, tmp_path):
    import signal_copy.outcome_tracker as mod

    monkeypatch.setattr(mod, "STORE", tmp_path / "signal_outcomes.json")
    tracker = SignalOutcomeTracker()
    sig = ParsedSignal(
        symbol="DOTUSDT",
        side=SignalSide.SHORT,
        entry_low=1.0,
        entry_high=1.0,
        stop_loss=1.25884,
        take_profits=[6.0, 4.0, 2.0],
        source_name="bad-format",
        source_chat_id=123,
    )

    tracker.track(sig, {"price": 1.0}, verdict="REJECT", execution_intended=False)

    assert tracker.open_count() == 0
