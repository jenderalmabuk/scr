import json
from pathlib import Path
from collections import Counter
from datetime import datetime, timezone

START_TS = datetime(2026, 10, 18, 16, 22, 0, tzinfo=timezone.utc).timestamp()
outcome_path = Path("/opt/signalcopyreal/runtime/state/committee_outcomes.jsonl")

if outcome_path.exists():
    outcomes = []
    with outcome_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
                if row.get("ts", 0) >= START_TS:
                    outcomes.append(row)
            except Exception:
                pass
    
    print(f"Total signals tracked in committee_outcomes SINCE OCT 18: {len(outcomes)}")
    reasons = Counter(r.get("exit_reason") for r in outcomes)
    print("Outcomes breakdown:")
    for r, cnt in reasons.most_common():
        print(f"  {r}: {cnt} ({cnt/len(outcomes)*100:.1f}%)")
    
    pnls = [r.get("pnl_pct", 0.0) for r in outcomes if r.get("pnl_pct") is not None]
    if pnls:
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        print(f"\nWinning outcomes: {len(wins)} | Losing outcomes: {len(losses)}")
        print(f"Average PnL % of rejected signals: {sum(pnls)/len(pnls):.2f}%")
        print(f"Total Cumulative PnL %: {sum(pnls):.2f}%")
