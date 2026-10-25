import json
from pathlib import Path

trade_path = Path("/opt/signalcopyreal/journal/trade_history.json")
data = json.loads(trade_path.read_text(encoding="utf-8"))
sample = data[-1] if isinstance(data, list) else list(data.values())[-1]
print("metadata content:", json.dumps(sample.get("metadata", {}), indent=2))
