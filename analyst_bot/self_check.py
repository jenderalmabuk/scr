from analyst_bot.analyzer import format_report, normalize_symbol

assert normalize_symbol("btc/usdt") == "BTCUSDT"
assert normalize_symbol("eth") == "ETHUSDT"
try:
    normalize_symbol("../../etc/passwd")
except ValueError:
    pass
else:
    raise AssertionError("invalid symbol accepted")

sample = {
    "symbol": "BTCUSDT", "exchange": "binance", "price": 100.0, "market_age_s": 12,
    "range_h1_20": [90.0, 110.0], "atr_h1_pct": 1.2,
    "price_stats": {"5m": {"change": 0.1, "qvol": 1000}},
    "oi_changes": {"5m": 0.1, "15m": 0.2, "1h": 2.0, "4h": None}, "oi_value": 1000,
    "cvd_changes": {"5m": None, "15m": None, "1h": None, "4h": None}, "zcvd_15m": -0.2,
    "funding_rate": 0.0001, "funding_zscore": 1.0,
    "flow": {}, "btc": {}, "whales": [], "verdict": "WATCH",
    "reasons": ["statistik OOS detector live belum tervalidasi"],
}
text = format_report(sample)
assert "NO RECENT VERIFIED WHALE ACTIVITY" in text
assert "Belum ada izin SETUP VALIDATED" in text
assert "BUY" not in text and "SELL" not in text
print("analyst_bot self-check: OK")
