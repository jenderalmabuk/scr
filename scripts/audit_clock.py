#!/usr/bin/env python3
import time
import requests

t0 = time.time() * 1000.0
resp = requests.get("https://api.bybit.com/v5/market/time", timeout=5)
t1 = time.time() * 1000.0

t_host = (t0 + t1) / 2.0
t_exchange = float(resp.json()["time"])
offset_ms = t_host - t_exchange
rtt_ms = t1 - t0

print(f"Host Time:      {t_host:.2f} ms")
print(f"Exchange Time:  {t_exchange:.2f} ms")
print(f"Clock Offset:   {offset_ms:+.2f} ms")
print(f"RTT Latency:    {rtt_ms:.2f} ms")
