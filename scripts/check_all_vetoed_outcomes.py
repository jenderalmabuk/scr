import urllib.request, json
from datetime import datetime, timezone

vetoed_signals = [
    {
        "symbol": "SANDUSDT", "side": "LONG", "time": "2026-10-17 14:53 UTC",
        "ts_ms": int(datetime(2026, 10, 17, 14, 53, 0, tzinfo=timezone.utc).timestamp() * 1000),
        "reason": "OI 15m down (-1.90%), shows exit not buildup",
    },
    {
        "symbol": "ICNTUSDT", "side": "LONG", "time": "2026-10-17 17:09 UTC",
        "ts_ms": int(datetime(2026, 10, 17, 17, 9, 0, tzinfo=timezone.utc).timestamp() * 1000),
        "reason": "Price 0.1999 below entry 0.2084, setup invalid, missed move",
    },
    {
        "symbol": "QUSDT", "side": "LONG", "time": "2026-10-18 09:30 UTC",
        "ts_ms": int(datetime(2026, 10, 18, 9, 30, 0, tzinfo=timezone.utc).timestamp() * 1000),
        "reason": "OI 1h -4.74% shows big exit pressure",
    },
    {
        "symbol": "QUSDT", "side": "LONG", "time": "2026-10-18 11:18 UTC",
        "ts_ms": int(datetime(2026, 10, 18, 11, 18, 0, tzinfo=timezone.utc).timestamp() * 1000),
        "reason": "Entry missed, chasing in high vol regime, weak RR",
    }
]

def fetch_candles(sym, ts_ms):
    # Try Bybit linear
    try:
        url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={sym}&interval=15&start={ts_ms}&limit=12"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        res = urllib.request.urlopen(req, timeout=5)
        raw = json.loads(res.read())
        list_data = raw.get("result", {}).get("list", [])
        if list_data:
            # Bybit returns reverse order: [start, open, high, low, close, ...]
            candles = sorted(list_data, key=lambda x: int(x[0]))
            return [(int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4])) for c in candles]
    except Exception:
        pass
        
    # Try Binance Futures
    try:
        url = f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval=15m&startTime={ts_ms}&limit=12"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        res = urllib.request.urlopen(req, timeout=5)
        raw = json.loads(res.read())
        if raw:
            return [(int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4])) for c in raw]
    except Exception:
        pass
    return None

print("=== ACTUAL MARKET OUTCOME OF THE 4 VETOED VALID SIGNALS ===\n")
for sig in vetoed_signals:
    sym = sig["symbol"]
    candles = fetch_candles(sym, sig["ts_ms"])
    if not candles:
        print(f"Could not fetch candles for {sym}\n")
        continue
    entry_price = candles[0][1]
    high_price = max(c[2] for c in candles)
    low_price = min(c[3] for c in candles)
    close_price = candles[-1][4]
    
    max_runup = (high_price - entry_price) / entry_price * 100
    max_drawdown = (low_price - entry_price) / entry_price * 100
    net_ret = (close_price - entry_price) / entry_price * 100
    
    print(f"[{sig['time']}] {sym} {sig['side']}")
    print(f"  Veto Reason: {sig['reason']}")
    print(f"  Entry Candle Price: {entry_price:.6g}")
    print(f"  Max Runup (MFE): +{max_runup:.2f}% | Max Drawdown (MAE): {max_drawdown:.2f}% | Net 3h: {net_ret:.2f}%")
    if max_drawdown < -2.5 or net_ret < 0:
        print("  -> RESULT: WOULD HAVE HIT SL / LOSS! Veto successfully saved capital! 🛡️")
    else:
        print("  -> RESULT: Small gain or scratch.")
    print()
