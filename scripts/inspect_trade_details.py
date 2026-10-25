import json
from pathlib import Path

th_path = Path("/opt/signalcopyreal/journal/trade_history.json")
raw = json.loads(th_path.read_text(encoding="utf-8"))
items = raw if isinstance(raw, list) else list(raw.values())

# Find one TP2 and one SL
tp2_trade = next(t for t in items if "TP2" in str(t.get("reason", "")))
sl_trade = next(t for t in items if "SL" in str(t.get("reason", "")))

print("TP2 Trade:")
for k, v in tp2_trade.items():
    if isinstance(v, (int, float, str, bool)):
        print(f"  {k}: {v}")

print("\nSL Trade:")
for k, v in sl_trade.items():
    if isinstance(v, (int, float, str, bool)):
        print(f"  {k}: {v}")
