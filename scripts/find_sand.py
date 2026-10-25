import json
from datetime import datetime, timezone
from pathlib import Path

START_EPOCH = datetime(2026, 10, 17, 13, 44, 0, tzinfo=timezone.utc)
path = Path('/opt/signalcopyreal/runtime/state/signal_lifecycle.jsonl')

target_sig = None
for line in path.open():
    try:
        row = json.loads(line)
        ts = datetime.fromisoformat(row.get('ts', '').replace('Z', '+00:00'))
        if ts >= START_EPOCH and row.get('symbol') == 'SANDUSDT' and row.get('stage') == 'COMMITTEE_EVALUATED':
            c = row.get('committee', {})
            if c.get('final_vote') == 'YES':
                target_sig = row['signal_id']
                print(f"Found target signal: {target_sig} at {ts}")
                break
    except Exception:
        pass

if target_sig:
    print(f"\nAll lifecycle events for {target_sig}:")
    for line in path.open():
        try:
            row = json.loads(line)
            if row.get('signal_id') == target_sig:
                print(f"  [{row.get('ts')}] Stage: {row.get('stage')} | ExecutionIntended: {row.get('execution_intended')} | Reason: {row.get('reason')} | GatewayAccepted: {row.get('gateway_accepted')}")
        except Exception:
            pass
