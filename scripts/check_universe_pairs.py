import urllib.request, json

url = "http://127.0.0.1:8000/universe/top"
req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
res = urllib.request.urlopen(req)
data = json.loads(res.read())

pairs = [p['pair'] for p in data.get('pairs', [])]
print(f"Total pairs in universe/top: {len(pairs)}")
print("Sample pairs:", pairs[:30])

if "NBISUSDT" in pairs:
    print("Found NBISUSDT in universe/top")

# Also check Bybit linear instruments to see what is real crypto
bybit_url = "https://api.bybit.com/v5/market/instruments-info?category=linear"
try:
    req2 = urllib.request.Request(bybit_url, headers={'User-Agent': 'Mozilla/5.0'})
    res2 = urllib.request.urlopen(req2, timeout=5)
    d2 = json.loads(res2.read())
    bybit_symbols = [item['symbol'] for item in d2.get('result', {}).get('list', []) if item.get('status') == 'Trading']
    print(f"Total Bybit Linear Trading Symbols: {len(bybit_symbols)}")
    if "NBISUSDT" in bybit_symbols:
        print("NBISUSDT is on Bybit")
    else:
        print("NBISUSDT NOT on Bybit Linear!")
except Exception as e:
    print(f"Error fetching Bybit instruments: {e}")
