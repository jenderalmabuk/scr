import json

with open("/opt/signalcopyreal/journal/trade_history.json") as f:
    history = json.load(f)

paxg = [t for t in history if t.get("symbol") == "PAXGUSDT"]
if paxg:
    t = paxg[-1]
    for k, v in t.items():
        if not isinstance(v, (dict, list)):
            print(f"{k}: {v}")
        else:
            print(f"{k}: [nested {type(v).__name__}]")
