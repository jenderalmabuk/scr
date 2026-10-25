import re
from pathlib import Path
import json

env_path = Path("/opt/signalcopyreal/.env")
content = env_path.read_text(encoding="utf-8")

settings = {
    "GATEWAY_TRADER_MODE": "paper",
    "GATEWAY_PAPER_MAINNET": "true",
    "STARTING_BALANCE": "100.0",
    "SIGNALCOPY_CAPITAL": "100.0",
    "SIGNALCOPY_RISK_PER_TRADE_USD": "2.0",
    "SIGNALCOPY_PAPER_DYNAMIC_EXIT": "false",
    "SCRATCH_EXIT_ENABLED": "false",
    "DAMAGE_REDUCER_ENABLED": "false",
}

for key, val in settings.items():
    pattern = rf"^{key}=.*$"
    if re.search(pattern, content, flags=re.MULTILINE):
        content = re.sub(pattern, f"{key}={val}", content, flags=re.MULTILINE)
        print(f"Updated {key}={val}")
    else:
        content += f"\n{key}={val}\n"
        print(f"Appended {key}={val}")

env_path.write_text(content, encoding="utf-8")
print(".env successfully updated")

# Reset paper equity to 100.0
equity_path = Path("/opt/signalcopyreal/journal/paper_equity.json")
equity_path.parent.mkdir(parents=True, exist_ok=True)
equity_payload = {"equity": 100.0, "daily_start_balance": 100.0}
equity_path.write_text(json.dumps(equity_payload, indent=2), encoding="utf-8")
print("Reset paper_equity.json to 100.0")

# Reset open positions to empty dict
pos_path = Path("/opt/signalcopyreal/journal/open_positions.json")
pos_path.parent.mkdir(parents=True, exist_ok=True)
pos_path.write_text("{}", encoding="utf-8")
print("Reset open_positions.json to {}")
