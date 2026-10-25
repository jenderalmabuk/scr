import urllib.request, json
from datetime import datetime, timezone

# 2026-10-17 14:53:53 UTC -> timestamp in ms
ts_ms = int(datetime(2026, 10, 17, 14, 45, 0, tzinfo=timezone.utc).timestamp() * 1000)
url = f"https://api.binance.com/api/v3/klines?symbol=SANDUSDT&interval=15m&startTime={ts_ms}&limit=12"
try:
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    res = urllib.request.urlopen(req)
    data = json.loads(res.read())
    print("SANDUSDT 15m candles after signal (Entry was ~0.3299):")
    for c in data[:10]:
        t = datetime.fromtimestamp(c[0]/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
        o, h, l, cl = float(c[1]), float(c[2]), float(c[3]), float(c[4])
        print(f"  {t} UTC: O={o:.4f} H={h:.4f} L={l:.4f} C={cl:.4f}")
except Exception as e:
    print(f"Error fetching klines: {e}")
