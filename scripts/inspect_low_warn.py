import json
from pathlib import Path
from collections import Counter

outcomes_path = Path("/opt/signalcopyreal/runtime/state/committee_outcomes.jsonl")
records = []
with outcomes_path.open("r", encoding="utf-8") as f:
    for line in f:
        try:
            records.append(json.loads(line))
        except Exception:
            pass

low_warn = [r for r in records if (r.get("validation_score") or 0) < 60 and r.get("committee_final_vote") == "WARN"]
print(f"Total Low Score + WARN: {len(low_warn)}")

# Inspect top reasons for WARN
warn_reasons = Counter()
channels = Counter()
for r in low_warn:
    channels[r.get("source_chat_id")] += 1
    for reason in r.get("committee_top_reasons", []):
        warn_reasons[reason] += 1

print("\nTop Channels in Low Score + WARN:")
for ch, c in channels.most_common(5):
    print(f"  Channel {ch}: {c}")

print("\nTop WARN reasons:")
for rea, c in warn_reasons.most_common(5):
    print(f"  - {rea}: {c}")

print("\nSample 10 Low Score + WARN trades:")
for r in low_warn[:10]:
    print(f"  {r.get('symbol')} {r.get('side')} (Score: {r.get('validation_score'):.1f}) -> Exit: {r.get('exit_reason')} | PnL: {r.get('pnl_pct'):+.2f}% | Reasons: {r.get('committee_top_reasons')}")
