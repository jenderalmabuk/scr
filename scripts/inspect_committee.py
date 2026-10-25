import json
from datetime import datetime, timezone
from pathlib import Path
from collections import Counter

START_EPOCH = datetime(2026, 10, 17, 13, 44, 0, tzinfo=timezone.utc)
path = Path('/opt/signalcopyreal/runtime/state/signal_lifecycle.jsonl')
signals = {}

with path.open('r') as f:
    for line in f:
        try:
            row = json.loads(line)
            ts = datetime.fromisoformat(row.get('ts', '').replace('Z', '+00:00'))
            if ts >= START_EPOCH and row.get('stage') == 'COMMITTEE_EVALUATED':
                signals[row['signal_id']] = row
        except Exception:
            pass

print(f"Total in COMMITTEE_EVALUATED: {len(signals)}")
final_votes = Counter()
legacy_bb_approvals = Counter()

for sig_id, row in signals.items():
    c = row.get('committee') or {}
    bb = c.get('legacy_bull_bear') or {}
    final_votes[c.get('final_vote')] += 1
    legacy_bb_approvals[bb.get('approved')] += 1

print("\nCommittee Final Votes Breakdown:")
for v, count in final_votes.most_common():
    print(f"  {v}: {count}")

print("\nLegacy Bull Bear Approval Breakdown:")
for app, count in legacy_bb_approvals.most_common():
    print(f"  Approved={app}: {count}")

print("\nDetailed list of all 30 signals:")
for sig_id, row in signals.items():
    c = row.get('committee') or {}
    bb = c.get('legacy_bull_bear') or {}
    top_reasons = c.get('top_reasons', [])
    score = row.get('score', 0)
    verdict = row.get('verdict', '')
    print(f"- {row.get('symbol')} {row.get('side')} (Score: {score:.1f}, Verdict: {verdict})")
    print(f"    Final Vote: {c.get('final_vote')} | Legacy BB: {bb.get('approved')} ({bb.get('reason')})")
    print(f"    Top Reasons: {top_reasons}")
