import json
import os
import subprocess
from pathlib import Path

print("=== 1. PATCHING RISK_ENGINE.PY ===")
risk_file = Path("/opt/signalcopyreal/risk/risk_engine.py")
lines = risk_file.read_text(encoding="utf-8").splitlines()

patched_lines = []
replaced = False
for line in lines:
    if "if daily_loss_pct <= -0.018:" in line:
        indent = " " * 8
        patched_lines.append(indent + "raw_stop = float(os.getenv('HARD_DAILY_STOP_PCT', os.getenv('MAX_DAILY_LOSS_PCT', '5.0')))")
        patched_lines.append(indent + "stop_threshold = raw_stop / 100.0 if raw_stop > 1.0 else raw_stop")
        patched_lines.append(indent + "stop_threshold = max(0.05, stop_threshold)")
        patched_lines.append(indent + "if daily_loss_pct <= -stop_threshold:")
        replaced = True
    else:
        patched_lines.append(line)

if replaced:
    risk_file.write_text("\n".join(patched_lines) + "\n", encoding="utf-8")
    print("Successfully patched /opt/signalcopyreal/risk/risk_engine.py with configurable daily stop (min 5.0%)")
else:
    print("Warning: 'if daily_loss_pct <= -0.018:' not found, may have already been replaced.")

print("\n=== 2. RESETTING PAPER_EQUITY.JSON DAILY START BALANCE ===")
equity_file = Path("/opt/signalcopyreal/journal/paper_equity.json")
if equity_file.exists():
    eq_data = json.loads(equity_file.read_text(encoding="utf-8"))
    current_eq = eq_data.get("equity", 100.0)
    eq_data["daily_start_balance"] = current_eq
    equity_file.write_text(json.dumps(eq_data), encoding="utf-8")
    print(f"Updated paper_equity.json: equity={current_eq}, daily_start_balance={current_eq}")

print("\n=== 3. UPDATE DOCKER-COMPOSE.YML VOLUMES ===")
dc_path = Path("/opt/signalcopyreal/docker-compose.yml")
dc_content = dc_path.read_text(encoding="utf-8")
if "- ./risk:/app/risk" not in dc_content:
    dc_content = dc_content.replace(
        "    volumes:\n      - ./journal:/app/journal",
        "    volumes:\n      - ./risk:/app/risk\n      - ./journal:/app/journal"
    )
    dc_path.write_text(dc_content, encoding="utf-8")
    print("Added - ./risk:/app/risk to gateway volumes in docker-compose.yml")
else:
    print("- ./risk:/app/risk already present in docker-compose.yml")

print("\n=== 4. COPYING PATCHED RISK_ENGINE.PY INTO NEXUS_GATEWAY CONTAINER ===")
res_cp = subprocess.run(["docker", "cp", "/opt/signalcopyreal/risk/risk_engine.py", "nexus_gateway:/app/risk/risk_engine.py"], capture_output=True, text=True)
print("Docker cp:", res_cp.stdout, res_cp.stderr)

print("\n=== 5. RESTARTING NEXUS_GATEWAY CONTAINER ===")
res_restart = subprocess.run(["docker", "restart", "nexus_gateway"], capture_output=True, text=True)
print("Docker restart nexus_gateway:", res_restart.stdout, res_restart.stderr)
