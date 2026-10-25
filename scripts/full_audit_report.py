import json
from datetime import datetime, timezone
from pathlib import Path
from collections import Counter, defaultdict

START_EPOCH = datetime(2026, 10, 17, 13, 44, 0, tzinfo=timezone.utc)
path = Path('/opt/signalcopyreal/runtime/state/signal_lifecycle.jsonl')

signals = defaultdict(dict)
all_events = []

with path.open('r', encoding='utf-8') as f:
    for line in f:
        try:
            row = json.loads(line)
            ts = datetime.fromisoformat(row.get('ts', '').replace('Z', '+00:00'))
            if ts >= START_EPOCH:
                sig_id = row.get('signal_id')
                all_events.append((ts, sig_id, row))
                signals[sig_id]['symbol'] = row.get('symbol', signals[sig_id].get('symbol'))
                signals[sig_id]['side'] = row.get('side', signals[sig_id].get('side'))
                signals[sig_id]['source_chat_id'] = row.get('source_chat_id', signals[sig_id].get('source_chat_id'))
                signals[sig_id]['latest_stage'] = row.get('stage')
                if 'score' in row:
                    signals[sig_id]['score'] = row['score']
                if 'verdict' in row:
                    signals[sig_id]['verdict'] = row['verdict']
                if 'reason' in row and row['reason']:
                    signals[sig_id]['reason'] = row['reason']
                if 'committee' in row and row['committee']:
                    signals[sig_id]['committee'] = row['committee']
        except Exception:
            pass

total_signals = len(signals)
stages = Counter(s.get('latest_stage') for s in signals.values())
verdicts = Counter(s.get('verdict') for s in signals.values())

geom_rejects = [s for s in signals.values() if 'UNFAVORABLE_GEOMETRY' in str(s.get('reason'))]
other_rejects = [s for s in signals.values() if s.get('latest_stage') == 'VALIDATION_REJECTED' and 'UNFAVORABLE_GEOMETRY' not in str(s.get('reason'))]
committee_evals = [s for s in signals.values() if s.get('latest_stage') == 'COMMITTEE_EVALUATED']
duplicates = [s for s in signals.values() if s.get('latest_stage') == 'DUPLICATE']

comm_votes = Counter()
for s in committee_evals:
    c = s.get('committee') or {}
    comm_votes[c.get('final_vote')] += 1

print(f"TOTAL_UNIQUE_SIGNALS={total_signals}")
print(f"GEOMETRY_REJECTS={len(geom_rejects)}")
print(f"OTHER_VALIDATION_REJECTS={len(other_rejects)}")
print(f"DUPLICATES={len(duplicates)}")
print(f"COMMITTEE_EVALUATED={len(committee_evals)}")
print(f"COMMITTEE_VOTES={dict(comm_votes)}")

# Check channel breakdown
channels = Counter(s.get('source_chat_id') for s in signals.values())
print(f"\nTOP_CHANNELS:")
for ch, count in channels.most_common(8):
    print(f"  Channel {ch}: {count} signals")
