import json
from pathlib import Path
from datetime import datetime, timezone
import subprocess

print("=== 1. LAST 3 TRADES IN TRADE_HISTORY.JSON ===")
trade_path = Path("/opt/signalcopyreal/journal/trade_history.json")
if trade_path.exists():
    try:
        raw = json.loads(trade_path.read_text(encoding="utf-8"))
        items = raw if isinstance(raw, list) else list(raw.values())
        print(f"Total trades: {len(items)}")
        for t in items[-3:]:
            print(json.dumps(t, indent=2))
    except Exception as e:
        print("Error reading trade_history:", e)

print("\n=== 2. OPEN POSITIONS IN OPEN_POSITIONS.JSON ===")
open_path = Path("/opt/signalcopyreal/journal/open_positions.json")
if open_path.exists():
    try:
        pos = json.loads(open_path.read_text(encoding="utf-8"))
        print(json.dumps(pos, indent=2))
    except Exception as e:
        print("Error reading open_positions:", e)

print("\n=== 3. GATEWAY RISK STATE FROM GATEWAY LOGS ===")
res = subprocess.run(
    ["docker", "logs", "--tail", "60", "nexus_gateway"],
    capture_output=True, text=True
)
print("Nexus Gateway Recent Logs:")
print(res.stdout[-1500:] if res.stdout else res.stderr[-1500:])

print("\n=== 4. AUTONOMOUS SCANNER SERVICE STATUS & LOGS ===")
res_svc = subprocess.run(
    ["journalctl", "-u", "nexus-autonomous-scanner.service", "--no-pager", "-n", "30"],
    capture_output=True, text=True
)
print(res_svc.stdout)

print("\n=== 5. SIGNAL COPY COMMITTEE EVALUATED DETAILS ===")
START_EPOCH = datetime(2026, 10, 18, 16, 22, 0, tzinfo=timezone.utc)
lifecycle_path = Path("/opt/signalcopyreal/runtime/state/signal_lifecycle.jsonl")
if lifecycle_path.exists():
    committee_evals = []
    with lifecycle_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
                ts_str = row.get("ts")
                if ts_str:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if ts >= START_EPOCH and row.get("stage") == "COMMITTEE_EVALUATED":
                        committee_evals.append(row)
            except Exception:
                pass
    print(f"Total COMMITTEE_EVALUATED events since {START_EPOCH.isoformat()}: {len(committee_evals)}")
    for ev in committee_evals[-5:]:
        sym = ev.get("symbol")
        score = ev.get("score")
        comm = ev.get("committee", {})
        ts = ev.get("ts")
        print(f"  [{ts}] {sym}: score={score}, committee={comm}")
