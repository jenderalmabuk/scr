from signal_copy.normalizer import normalize_signal
from signal_copy.signal_parser import parse_signal
from signal_copy.signal_schema import SignalSide


def _parse(text):
    sig = parse_signal(text, source_name="test", source_chat_id=-1001935772577)
    assert sig is not None
    normalize_signal(sig, {"price": sig.entry_mid, "atr_pct": 2.0})
    return sig


def test_indexed_entry_signal_matches_provider_levels():
    sig = _parse("""#ONEUSDT 15m
STATUS : SHORT 🚀
👉 ENTRY1 : 0.001974 👉 ENTRY2 : 0.0020924
🎯 TP1 : 0.0019345 (💰 2%)
🎯 TP2 : 0.001895 (💰 4%)
🎯 TP3 : 0.0018556 (💰 6%)
⛔️ SL: 0.00215166
LEVERAGE: Cross 20x""")

    assert sig.symbol == "ONEUSDT"
    assert sig.side == SignalSide.SHORT
    assert sig.entry_low == 0.001974
    assert sig.entry_high == 0.0020924
    assert sig.take_profits == [0.0019345, 0.001895, 0.0018556]
    assert sig.stop_loss == 0.00215166
    assert sig.leverage == 20.0
    assert sig.timeframe == "15m"
    assert sig.level_anomalies == []


def test_indexed_entry_ignores_profit_percentages_as_tp_values():
    sig = _parse("""#UNIUSDT 15m
STATUS : SHORT 🚀
👉 ENTRY1 : 9.155 👉 ENTRY2 : 9.704
🎯 TP1 : 8.972 (💰 2%)
🎯 TP2 : 8.789 (💰 4%)
🎯 TP3 : 8.606 (💰 6%)
⛔️ SL: 9.97895
LEVERAGE: Cross 20x""")

    assert sig.take_profits == [8.972, 8.789, 8.606]


def test_open_entry2_new_tp_update_uses_entry2_price_and_infers_short():
    sig = _parse("""#OPUSDT OPEN ENTRY2 0.12268
NEW TP
🎯 TP1 : 0.11683 (💰 2.00%)
🎯 TP2 : 0.11444 (💰 4.00%)
🎯 TP3 : 0.11206 (💰 6.00%)
[REPLY_SYMBOL: OPUSDT]""")

    assert sig.symbol == "OPUSDT"
    assert sig.side == SignalSide.SHORT
    assert sig.entry_low == 0.12268
    assert sig.entry_high == 0.12268
    assert sig.take_profits == [0.11683, 0.11444, 0.11206]
    assert sig.tp_source == "signal"
    assert sig.sl_source == "improvised_atr"
    assert "MALFORMED_PROVIDER_LEVELS" not in sig.level_anomalies


def test_comma_separated_micro_entry_zone_keeps_decimals():
    sig = _parse("""📊#ONE/USDT–LONG–10x - 30x 🚀

📍 𝐄𝐧𝐭𝐫𝐲 𝐙𝐨𝐧𝐞:

➥ 0.00184, 0.00182

🎯𝐓𝐚𝐤𝐞 𝐏𝐫𝐨𝐟𝐢𝐭:

✅️ TP1  0.00190
✅️ TP2  0.00199
✅️ TP3  0.00230

🛑 𝐒𝐭𝐨𝐩 𝐋𝐨𝐬𝐬: - 0.00168""")

    assert sig.symbol == "ONEUSDT"
    assert sig.side == SignalSide.LONG
    assert sig.entry_low == 0.00182
    assert sig.entry_high == 0.00184
    assert sig.take_profits == [0.0019, 0.00199, 0.0023]
    assert sig.stop_loss == 0.00168


def test_base_side_quote_symbol_form():
    sig = _parse("""✅Entry : $0.19206-0.1940

(SYN SHORT /USDT 5-15X🆘)

🏹 Target 1 $0.1900
🏹Target 2. $0.1880
🏹Target 3  $0.1860
🏹Target 4  $0.1840
🏹Target 5  $0.1820
🏹Target 6  $0.18000

🚫Stop loss : 0.20164""")

    assert sig.symbol == "SYNUSDT"
    assert sig.side == SignalSide.SHORT
    assert sig.entry_low == 0.19206
    assert sig.entry_high == 0.194


def test_single_line_entry_range_before_side_and_targets_uses_full_range():
    sig = _parse("""✅Entry 0.79065 - 0.78206

(🆘LONG BTWUSDT 5-20x🆘)

🏹 Target 1 $0.79875
🏹Target 2. $0.80606
🏹Target 3 $0.812067
🚫Stop loss -0.749256""")

    assert sig.symbol == "BTWUSDT"
    assert sig.side == SignalSide.LONG
    assert sig.entry_low == 0.78206
    assert sig.entry_high == 0.79065


def test_numbered_entry_and_target_ordinals_are_not_prices():
    radar = _parse("""🌟 VIP Signal ✅ Long
Pair: #KNC/USDT
📊 Entry Price: 1) 0.13060 2) 0.12668
📈 Targets: 1) 0.13135 2) 0.13407 3) 0.13680 4) 0.13953
Stop Loss: 0.12237
Leverage: 10x-20x""")
    musk = _parse("""#Free #Futures_signal
🔴 SHORT #IOTA/USDT
Entry : 1) 0.046780 2) 0.048183
Targets : 1) 0.046506 2) 0.045518 3) 0.044531 4) 0.043543
🛑 Stop : 0.049727
Leverage : 10x (isolated)""")

    assert radar.entry_low == 0.12668
    assert radar.entry_high == 0.1306
    assert radar.take_profits[:4] == [0.13135, 0.13407, 0.1368, 0.13953]
    assert musk.entry_low == 0.04678
    assert musk.entry_high == 0.048183
    assert musk.take_profits[:4] == [0.046506, 0.045518, 0.044531, 0.043543]


def test_pipe_and_underscore_entry_zones_use_full_range():
    rapid = _parse("""⚡ RAPID SCALPERS SIGNAL ⚡

🪙 Pair: USELESSUSDT
🧭 Direction: LONG
⚙️ Leverage: Cross 10x
🎯 Entry: 0,236080 | 0,233770 | 0,231280
💰 Take Profits: 0,238441 | 0,241039 | 0,244703
🛑 Stop Loss: 0,230046""")
    ghost = _parse("""💥THE GHOST CRYPTO 💥

#FLUIDUSDT — BUY (LONG)
🟢 Leverage: Cross 50x

⚡️Entry : 1.300_1.215
💵Take Profits:
TP1: 1.332
TP2: 1.378
TP3: 1.445
TP4: 1.559

⛔️Stop Loss: 1.260""")

    assert rapid.entry_low == 0.23128
    assert rapid.entry_high == 0.23608
    assert ghost.entry_low == 1.215
    assert ghost.entry_high == 1.3
