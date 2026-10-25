import json
from datetime import datetime, timezone
from pathlib import Path
from collections import Counter, defaultdict

START_EPOCH = datetime(2026, 10, 18, 16, 22, 0, tzinfo=timezone.utc)

def parse_iso(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None

print(f"=== COMPREHENSIVE DUAL AUDIT: SIGNAL COPY & AUTONOMOUS SCANNER ===")
print(f"Audit Window: From {START_EPOCH.isoformat()} to NOW\n")

# 1. Closed Trades in trade_history.json
trade_path = Path("/opt/signalcopyreal/journal/trade_history.json")
closed_trades = []
if trade_path.exists():
    try:
        raw = json.loads(trade_path.read_text(encoding="utf-8"))
        items = raw if isinstance(raw, list) else list(raw.values())
        for t in items:
            c_time = parse_iso(t.get("closed_at") or t.get("opened_at"))
            if c_time and c_time >= START_EPOCH:
                closed_trades.append(t)
    except Exception as e:
        print("Error reading trade_history:", e)

print(f"--- 1. CLOSED TRADES IN AUDIT WINDOW: {len(closed_trades)} ---")
for t in closed_trades:
    sym = t.get("symbol")
    side = t.get("side")
    entry = t.get("entry_price")
    exit_p = t.get("exit_price")
    pnl = t.get("pnl_usd") or t.get("net_pnl_usd") or t.get("realized_pnl", 0.0)
    reason = t.get("close_reason") or t.get("reason")
    src = t.get("metadata", {}).get("source") or t.get("source", "UNKNOWN")
    opened = t.get("opened_at")
    closed = t.get("closed_at")
    print(f"  [{src}] {sym} {side} | Entry: {entry} -> Exit: {exit_p} | PnL: ${pnl:+.4f} USD | Reason: {reason} | Opened: {opened} | Closed: {closed}")

# 2. Open Positions
open_path = Path("/opt/signalcopyreal/journal/open_positions.json")
open_pos = {}
if open_path.exists():
    try:
        open_pos = json.loads(open_path.read_text(encoding="utf-8"))
    except Exception as e:
        print("Error reading open_positions:", e)

print(f"\n--- 2. CURRENT OPEN POSITIONS: {len(open_pos)} ---")
for sym, p in open_pos.items():
    side = p.get("side")
    entry = p.get("entry_price")
    mark = p.get("lifecycle_price_quote", {}).get("price")
    sl = p.get("sl_price")
    tp1 = p.get("tp1_price")
    tp1_hit = p.get("tp1_hit")
    partial_pnl = p.get("baseline_partial_net_pnl", 0.0)
    opened = p.get("opened_at")
    tag = p.get("tag", "")
    print(f"  {sym} {side} (tag={tag}) | Entry: {entry} | Mark: {mark} | SL: {sl} | TP1: {tp1} (Hit={tp1_hit}) | Partial PnL: ${partial_pnl:+.4f} USD | Opened: {opened}")

# 3. Paper Equity State
equity_path = Path("/opt/signalcopyreal/journal/paper_equity.json")
if equity_path.exists():
    print(f"\n--- 3. PAPER EQUITY STATE ---")
    print(" ", equity_path.read_text().strip())

# 4. Signal Copy Activity
lifecycle_path = Path("/opt/signalcopyreal/runtime/state/signal_lifecycle.jsonl")
sig_copy_signals = defaultdict(dict)
if lifecycle_path.exists():
    with lifecycle_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
                ts = parse_iso(row.get("ts"))
                if ts and ts >= START_EPOCH:
                    sid = row.get("signal_id")
                    sig_copy_signals[sid]["symbol"] = row.get("symbol", sig_copy_signals[sid].get("symbol"))
                    sig_copy_signals[sid]["side"] = row.get("side", sig_copy_signals[sid].get("side"))
                    sig_copy_signals[sid]["latest_stage"] = row.get("stage")
                    if "score" in row:
                        sig_copy_signals[sid]["score"] = row["score"]
                    if "reason" in row and row["reason"]:
                        sig_copy_signals[sid]["reason"] = row["reason"]
                    if "committee" in row:
                        sig_copy_signals[sid]["committee"] = row["committee"]
            except Exception:
                pass

print(f"\n--- 4. SIGNAL COPY ENGINE ACTIVITY ---")
print(f"  Total Signals Received: {len(sig_copy_signals)}")
sig_stages = Counter(s.get("latest_stage") for s in sig_copy_signals.values())
for st, cnt in sig_stages.most_common():
    print(f"    {st}: {cnt}")

sig_reasons = Counter(s.get("reason") for s in sig_copy_signals.values() if s.get("reason"))
print("  Top Rejection Reasons:")
for r, cnt in sig_reasons.most_common(5):
    print(f"    - [{cnt}x] {r}")

# 5. Check Autonomous Scanner Executions / Setups
print(f"\n--- 5. AUTONOMOUS SCANNER ACTIVITY ---")
# Check journalctl logs for autonomous scanner
