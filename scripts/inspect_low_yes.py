import json
from pathlib import Path

outcomes_path = Path("/opt/signalcopyreal/runtime/state/committee_outcomes.jsonl")
decisions_path = Path("/opt/signalcopyreal/runtime/state/committee_decisions.jsonl")

decisions = {}
if decisions_path.exists():
    with decisions_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
                sig_id = row.get("signal_id")
                if sig_id:
                    decisions[sig_id] = row
            except Exception:
                pass

records = []
with outcomes_path.open("r", encoding="utf-8") as f:
    for line in f:
        try:
            records.append(json.loads(line))
        except Exception:
            pass

low_yes = [r for r in records if (r.get("validation_score") or 0) < 60 and r.get("committee_final_vote") == "YES"]
print(f"Total Low Score + Committee YES: {len(low_yes)}\n")

for i, r in enumerate(low_yes, 1):
    sig_id = r.get("signal_id")
    dec = decisions.get(sig_id, {})
    c = dec.get("committee") or {}
    lbb = c.get("legacy_bull_bear") or {}
    votes = [f"{v.get('specialist')}:{v.get('vote')}" for v in c.get("votes", [])]
    print(f"[{i}] {r.get('symbol')} {r.get('side')} (Score: {r.get('validation_score'):.1f})")
    print(f"    Exit: {r.get('exit_reason')} | PnL: {r.get('pnl_pct'):+.2f}%")
    print(f"    Specialists: {', '.join(votes)}")
    print(f"    Legacy BB: {lbb.get('approved')}: {lbb.get('reason')}")
    print()
