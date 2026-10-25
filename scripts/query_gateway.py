import urllib.request
import json

req = urllib.request.Request(
    "http://127.0.0.1:8787/gateway/portfolio",
    headers={"Authorization": "Bearer nexus_gateway_test_2024"}
)
try:
    with urllib.request.urlopen(req, timeout=5) as resp:
        d = json.loads(resp.read().decode())
        print(f"Equity: ${d.get('equity', 0):.2f}")
        print(f"Open positions count: {d.get('open_position_count', 0)}")
        print(f"Open symbols: {d.get('open_symbols', [])}")
        positions = d.get("positions", [])
        if isinstance(positions, dict):
            positions = list(positions.values())
        for p in positions:
            sym = p.get("symbol")
            side = p.get("side")
            entry = p.get("entry_price")
            sl = p.get("sl_price")
            tp = p.get("tp_ladder")
            sl_kind = p.get("sl_kind")
            qty = p.get("qty")
            initial_qty = p.get("initial_qty")
            print(f"  * {sym} {side} | Entry: {entry} | SL: {sl} ({sl_kind}) | TPs: {tp} | Qty: {qty}/{initial_qty}")
except Exception as e:
    print("Error querying gateway:", e)
