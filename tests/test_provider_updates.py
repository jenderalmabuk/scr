from signal_copy.provider_updates import parse_provider_update, UpdateKind

def test_parse_new_tp_from_reply():
    text = "One new tp 1 0.004450\n[REPLY_SYMBOL: ONEUSDT]"
    up = parse_provider_update(text, channel_id=-1001652601224)
    assert up is not None
    assert up.symbol == "ONEUSDT"
    assert up.kind == UpdateKind.UPDATE_TP
    assert up.price == 0.00445
    assert up.tp_index == 1

def test_parse_new_tp_explicit_symbol():
    text = "#BTCUSDT new tp 2: 88500"
    up = parse_provider_update(text, channel_id=-1001652601224)
    assert up is not None
    assert up.symbol == "BTCUSDT"
    assert up.kind == UpdateKind.UPDATE_TP
    assert up.price == 88500.0
    assert up.tp_index == 2

def test_parse_move_sl_price():
    text = "Move SL to 0.0048\n[REPLY_SYMBOL: ONEUSDT]"
    up = parse_provider_update(text, channel_id=-1001652601224)
    assert up is not None
    assert up.symbol == "ONEUSDT"
    assert up.kind == UpdateKind.MOVE_SL_PRICE
    assert up.price == 0.0048

def test_parse_move_sl_be():
    text = "SL to entry\n[REPLY_SYMBOL: ONEUSDT]"
    up = parse_provider_update(text, channel_id=-1001652601224)
    assert up is not None
    assert up.symbol == "ONEUSDT"
    assert up.kind == UpdateKind.MOVE_SL_BE

def test_parse_close_now():
    text = "Close now\n[REPLY_SYMBOL: ONEUSDT]"
    up = parse_provider_update(text, channel_id=-1001652601224)
    assert up is not None
    assert up.symbol == "ONEUSDT"
    assert up.kind == UpdateKind.CLOSE

def test_parse_tp_hit():
    text = "TP 1 HIT\n[REPLY_SYMBOL: ONEUSDT]"
    up = parse_provider_update(text, channel_id=-1001652601224)
    assert up is not None
    assert up.symbol == "ONEUSDT"
    assert up.kind == UpdateKind.TP_HIT
    assert up.tp_index == 1

if __name__ == "__main__":
    test_parse_new_tp_from_reply()
    test_parse_new_tp_explicit_symbol()
    test_parse_move_sl_price()
    test_parse_move_sl_be()
    test_parse_close_now()
    test_parse_tp_hit()
    print("ALL PROVIDER UPDATE TESTS PASSED!")
