from signal_copy.signal_parser import parse_signal
from signal_copy.signal_schema import SignalSide


def test_bbb_channel_signal_multiline_entry_and_emoji_sl():
    raw = """🔰 PAIR: CRV/ USDT 💹

🔴Position: SHORT🚀

☑️ Entry Zone:
💠 0.3509
💠 0.3534


🌐 Leverage: Cross • 50X ⚡🔥

🎯 Take Profit Targets:
🥇 0.3455
🥈 0.3433
🥉 0.3398

❌ Stop Loss: 🔴0.3563

💎 VIP Signals: @BBB_Support_Team✨"""

    sig = parse_signal(raw, source_name="Boom_Boom_Billionaires", source_chat_id=-1001756316676)
    assert sig is not None
    assert sig.symbol == "CRVUSDT"
    assert sig.side == SignalSide.SHORT
    assert sig.entry_low == 0.3509
    assert sig.entry_high == 0.3534
    assert sig.stop_loss == 0.3563
    assert sig.take_profits == [0.3455, 0.3433, 0.3398]
    assert sig.leverage == 50.0
    assert sig.level_anomalies == []
