#!/usr/bin/env python3
"""
Comprehensive audit of paper trading mode since 2026-10-17T13:44:00Z.
Analyzes:
1. All signals received, parsed, rejected, approved.
2. Breakdown of rejections (Geometry Gate, ML Gate, Committee Veto, Validation filters, etc.)
3. Outcome tracking: what would have happened to the rejected signals?
4. Execution intents sent to Gateway and filled positions.
5. Equity and drawdown.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict, Counter

START_EPOCH = datetime(2026, 10, 17, 13, 44, 0, tzinfo=timezone.utc)

def parse_iso(ts_str):
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return None

def audit_lifecycle():
    path = Path("/opt/signalcopyreal/runtime/state/signal_lifecycle.jsonl")
    if not path.exists():
        print("signal_lifecycle.jsonl not found")
        return

    signals = defaultdict(dict)
    
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            ts = parse_iso(row.get("ts", ""))
            if not ts or ts < START_EPOCH:
                continue
            
            sig_id = row.get("signal_id")
            stage = row.get("stage")
            signals[sig_id]["symbol"] = row.get("symbol", signals[sig_id].get("symbol"))
            signals[sig_id]["side"] = row.get("side", signals[sig_id].get("side"))
            signals[sig_id]["source_chat_id"] = row.get("source_chat_id", signals[sig_id].get("source_chat_id"))
            signals[sig_id]["latest_stage"] = stage
            signals[sig_id].setdefault("stages", []).append((ts, stage, row))
            if "score" in row:
                signals[sig_id]["score"] = row["score"]
            if "reason" in row and row["reason"]:
                signals[sig_id]["reason"] = row["reason"]
            if "verdict" in row:
                signals[sig_id]["verdict"] = row["verdict"]

    print(f"=== SIGNAL AUDIT (Since {START_EPOCH.isoformat()}) ===")
    print(f"Total Unique Signals Received: {len(signals)}")
    
    stage_counts = Counter(s.get("latest_stage") for s in signals.values())
    print("\nStage Breakdown:")
    for st, count in stage_counts.most_common():
        print(f"  {st}: {count}")
        
    rejection_reasons = Counter()
    executed_signals = []
    
    for sig_id, data in signals.items():
        reason = data.get("reason", "")
        stage = data.get("latest_stage", "")
        if "REJECT" in stage or data.get("verdict") == "REJECT":
            rejection_reasons[reason] += 1
        elif "EXECUTED" in stage or data.get("position_confirmed") or "INTENT" in stage:
            executed_signals.append((sig_id, data))

    print("\nRejection Reasons Breakdown:")
    for r, count in rejection_reasons.most_common():
        print(f"  - [{count}x] {r}")

    print(f"\nExecuted / Intent Sent Signals: {len(executed_signals)}")
    for sig_id, data in executed_signals:
        print(f"  Signal {sig_id}: {data.get('symbol')} {data.get('side')} - {data.get('latest_stage')}")

    return signals

def audit_committee():
    path = Path("/opt/signalcopyreal/runtime/state/committee_decisions.jsonl")
    if not path.exists():
        return
    decisions = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except Exception:
                continue
            ts = parse_iso(row.get("created_at") or row.get("ts") or "")
            if ts and ts >= START_EPOCH:
                decisions.append(row)
    print(f"\n=== COMMITTEE AUDIT (Since {START_EPOCH.isoformat()}) ===")
    print(f"Total Committee Evaluations: {len(decisions)}")
    votes = Counter(d.get("final_vote") for d in decisions)
    for v, c in votes.most_common():
        print(f"  Final Vote {v}: {c}")

def audit_outcomes():
    # Check outcomes of signals tracked during this period
    path = Path("/opt/signalcopyreal/runtime/state/signal_outcomes.json")
    if not path.exists():
        return
    try:
        outcomes = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    
    print(f"\n=== OUTCOME RESOLUTION AUDIT ===")
    resolved_in_epoch = []
    for k, v in outcomes.items():
        res_time = parse_iso(v.get("resolved_at") or v.get("created_at") or "")
        if res_time and res_time >= START_EPOCH:
            resolved_in_epoch.append((k, v))
            
    print(f"Signals Resolved During Epoch: {len(resolved_in_epoch)}")
    for k, v in resolved_in_epoch[:20]:
        print(f"  {v.get('symbol')} {v.get('side')} ({v.get('source')}): {v.get('status')} PnL: {v.get('pnl_pct')}% (MFE: {v.get('mfe_pct')}%, MAE: {v.get('mae_pct')}%)")

if __name__ == "__main__":
    audit_lifecycle()
    audit_committee()
    audit_outcomes()
