import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from collections import Counter, defaultdict
import subprocess

print("===================================================================")
print("=== DEEP AUDIT: SIGNAL COPY BOTTLENECK & AUTONOMOUS BOT STATUS ===")
print("===================================================================\n")

# --- PART 1: DEEP AUDIT SIGNAL COPY ENGINE ---
print("=========================================")
print("=== 1. SIGNAL COPY PIPELINE ANALYSIS ===")
print("=========================================")

sl_path = Path("/opt/signalcopyreal/runtime/state/signal_lifecycle.jsonl")
START_AUDIT = datetime(2026, 10, 18, 16, 22, 0, tzinfo=timezone.utc)

sigs = defaultdict(dict)
stage_counts = Counter()
rejection_reasons = Counter()
committee_actions = Counter()
specialist_votes = Counter()
specialist_reasons = Counter()

committee_signals = []

if sl_path.exists():
    with sl_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
                ts_str = row.get("ts")
                if not ts_str:
                    continue
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts < START_AUDIT:
                    continue
                
                sid = row.get("signal_id")
                stage = row.get("stage")
                stage_counts[stage] += 1
                
                if sid:
                    sigs[sid]["last_stage"] = stage
                    sigs[sid]["symbol"] = row.get("symbol", sigs[sid].get("symbol"))
                    sigs[sid]["side"] = row.get("side", sigs[sid].get("side"))
                    if row.get("reason"):
                        sigs[sid]["reason"] = row.get("reason")
                        rejection_reasons[row.get("reason")] += 1
                    
                    if stage == "COMMITTEE_EVALUATED":
                        comm = row.get("committee", {})
                        sigs[sid]["committee"] = comm
                        action = comm.get("action")
                        committee_actions[action] += 1
                        committee_signals.append((ts, row))
                        for v in comm.get("votes", []):
                            spec = v.get("specialist")
                            vote = v.get("vote")
                            specialist_votes[f"{spec}:{vote}"] += 1
                            if vote in ("NO", "WARN"):
                                specialist_reasons[f"{spec}: {v.get('reason')}"] += 1
            except Exception:
                pass

print(f"Total Unique Signals Received since {START_AUDIT.strftime('%Y-%m-%d %H:%M UTC')}: {len(sigs)}")
print("\n--- Pipeline Stages Recorded ---")
for st, cnt in stage_counts.most_common():
    print(f"  {st}: {cnt}")

print("\n--- Committee Actions Breakdown ---")
for act, cnt in committee_actions.most_common():
    print(f"  Action '{act}': {cnt}")

print("\n--- Top Direct Validation Rejection Reasons ---")
for r, cnt in rejection_reasons.most_common(8):
    print(f"  [{cnt}x] {r}")

print("\n--- Committee Specialist Negative Votes Breakdown ---")
for k, cnt in specialist_votes.most_common():
    if "NO" in k or "WARN" in k:
        print(f"  [{cnt}x] {k}")

print("\n--- Top Committee Specialist Reasons for Rejection ---")
for r, cnt in specialist_reasons.most_common(10):
    print(f"  [{cnt}x] {r}")

print(f"\n--- Detailed Sample of Last 6 COMMITTEE_EVALUATED Signals ---")
for ts, ev in committee_signals[-6:]:
    sym = ev.get("symbol")
    side = ev.get("side")
    score = ev.get("score")
    comm = ev.get("committee", {})
    action = comm.get("action")
    final_vote = comm.get("final_vote")
    top_reasons = comm.get("top_reasons", [])
    print(f"[{ts.strftime('%m-%d %H:%M:%S')}] {sym} {side} | Score={score:.1f} | FinalVote={final_vote} | Action={action}")
    print(f"   Top Reasons: {top_reasons}")
    for v in comm.get("votes", []):
        if v.get("vote") in ("NO", "WARN"):
            print(f"     -> {v.get('specialist')}: {v.get('vote')} | {v.get('reason')} | evid={v.get('evidence')}")

# Check pending lifecycle or gateway rejected
pl_path = Path("/opt/signalcopyreal/runtime/state/state/pending_lifecycle.jsonl")
if pl_path.exists():
    recent_pl = []
    with pl_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
                ts_str = row.get("ts")
                if ts_str:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if ts >= START_AUDIT:
                        recent_pl.append(row)
            except Exception:
                pass
    print(f"\nPending Lifecycle events since {START_AUDIT.strftime('%Y-%m-%d %H:%M UTC')}: {len(recent_pl)}")
    for ev in recent_pl[-5:]:
        print(f"  [{ev.get('ts')}] {ev.get('event')} {ev.get('symbol')} {ev.get('side')} -> reason={ev.get('reason')}")

# Check Signal Copy config in docker container
print("\n--- Signal Copy Container Config & Environment ---")
try:
    res = subprocess.run(["docker", "exec", "nexus_signal_copy", "env"], capture_output=True, text=True)
    for line in res.stdout.splitlines():
        if any(k in line for k in ["COMMITTEE", "GEOMETRY", "RR", "GATEWAY", "SCORE", "ML", "VETO", "PAPER"]):
            print(f"  {line}")
except Exception as e:
    print("Error checking signal copy env:", e)

# --- PART 2: AUTONOMOUS BOT STATUS ---
print("\n=========================================")
print("=== 2. AUTONOMOUS BOT & GATEWAY AUDIT ===")
print("=========================================")

# 1. Gateway Portfolio
try:
    req = urllib.request.Request(
        "http://127.0.0.1:8787/gateway/portfolio",
        headers={"Authorization": "Bearer nexus_gateway_test_2024"}
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        port = json.loads(resp.read().decode())
        print(f"Current Equity: ${port.get('equity'):.4f} USDT")
        print(f"Daily PnL %: {port.get('daily_pnl_pct')}%")
        print(f"Daily Loss Limit Hit: {port.get('daily_loss_limit_hit')}")
        print(f"Trading Enabled: {port.get('trading_enabled', True)}")
        print(f"Open Positions Count: {len(port.get('open_positions', []))}")
        print(f"Open Symbols: {port.get('open_symbols', [])}")
        print(f"Active Open Risk: ${port.get('active_open_risk_total', 0.0):.4f}")
except Exception as e:
    print("Error querying gateway portfolio:", e)

# 2. Open Positions
op_path = Path("/opt/signalcopyreal/journal/open_positions.json")
if op_path.exists():
    try:
        opos = json.loads(op_path.read_text(encoding="utf-8"))
        print(f"\nActive Open Positions: {len(opos)}")
        for sym, p in opos.items():
            side = p.get("side")
            entry = p.get("entry_price")
            quote = p.get("lifecycle_price_quote", {}).get("price")
            sl = p.get("sl_price")
            tp1 = p.get("tp1_price")
            tp1_hit = p.get("tp1_hit")
            tag = p.get("tag")
            opened = p.get("opened_at")
            partial_pnl = p.get("baseline_partial_net_pnl", 0.0)
            print(f"  {sym} {side} (tag={tag})")
            print(f"    Entry: {entry} | Mark: {quote} | SL: {sl} | TP1: {tp1} (Hit={tp1_hit})")
            print(f"    Partial PnL Realized: ${partial_pnl:+.4f} | Opened: {opened}")
    except Exception as e:
        print("Error reading open_positions:", e)

# 3. Closed Trades in History since 01:00 UTC today
print("\n--- Trades Closed Today (2026-10-20 UTC) ---")
TODAY_START = datetime(2026, 10, 20, 0, 0, 0, tzinfo=timezone.utc)
th_path = Path("/opt/signalcopyreal/journal/trade_history.json")
if th_path.exists():
    try:
        raw = json.loads(th_path.read_text(encoding="utf-8"))
        items = raw if isinstance(raw, list) else list(raw.values())
        today_closed = []
        for t in items:
            tc_str = t.get("timestamp_close") or t.get("closed_at")
            if tc_str:
                try:
                    tc = datetime.fromisoformat(tc_str.replace("Z", "+00:00"))
                    if tc >= TODAY_START:
                        today_closed.append((tc, t))
                except Exception:
                    pass
        today_closed.sort(key=lambda x: x[0])
        print(f"Total trades closed today: {len(today_closed)}")
        for tc, t in today_closed:
            sym = t.get("symbol")
            side = t.get("side")
            pnl = t.get("pnl_usd", 0.0) or t.get("net_pnl_usd", 0.0) or 0.0
            fee = (t.get("entry_fee_usd", 0.0) or 0.0) + (t.get("exit_fee_usd", 0.0) or 0.0)
            reason = t.get("reason") or t.get("close_reason")
            print(f"  [{tc.strftime('%H:%M:%S')}] {sym} {side} | PnL: ${pnl:+.4f} | Fee: ${fee:.4f} | Reason: {reason}")
    except Exception as e:
        print("Error reading trade_history:", e)

# 4. Autonomous Scanner Recent Activity
print("\n--- Autonomous Scanner Service Status & Last Logs ---")
try:
    res_svc = subprocess.run(["systemctl", "is-active", "nexus-autonomous-scanner.service"], capture_output=True, text=True)
    print("Service status:", res_svc.stdout.strip())
    res_logs = subprocess.run(["journalctl", "-u", "nexus-autonomous-scanner.service", "--no-pager", "-n", "25"], capture_output=True, text=True)
    print("Recent Scanner Logs:")
    print(res_logs.stdout[-1500:] if res_logs.stdout else res_logs.stderr[-1500:])
except Exception as e:
    print("Error getting scanner service logs:", e)
