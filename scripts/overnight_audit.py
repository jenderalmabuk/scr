import json
from datetime import datetime, timezone
from pathlib import Path
import urllib.request
import subprocess

print("=============================================================")
print("=== COMPREHENSIVE OVERNIGHT AUDIT (2026-10-19 to 2026-10-20) ===")
print("=============================================================\n")

# 1. Gateway Portfolio Query
print("--- 1. GATEWAY PORTFOLIO & EQUITY STATUS ---")
try:
    req = urllib.request.Request(
        "http://127.0.0.1:8787/gateway/portfolio",
        headers={"Authorization": "Bearer nexus_gateway_test_2024"}
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        port = json.loads(resp.read().decode())
        print(f"Equity: ${port.get('equity'):.4f} USDT")
        print(f"Daily PnL %: {port.get('daily_pnl_pct')}%")
        print(f"Daily Loss Limit Hit: {port.get('daily_loss_limit_hit')}")
        print(f"Trading Enabled: {port.get('trading_enabled', True)}")
        print(f"Active Open Positions Count: {port.get('open_position_count', len(port.get('open_positions', [])))}")
        print(f"Open Symbols: {port.get('open_symbols', [])}")
        print(f"Active Open Risk: ${port.get('active_open_risk_total', 0.0):.4f}")
        print(f"Total Open Risk: ${port.get('total_open_risk', 0.0):.4f}")
        print(f"Recent Intents Count: {len(port.get('recent_intents', []))}")
        if port.get('recent_intents'):
            print("Recent Intents Sample:")
            for item in port.get('recent_intents')[-5:]:
                intent = item.get("intent", {})
                res = item.get("result", {})
                print(f"  [{intent.get('source')}] {intent.get('symbol')} {intent.get('side')} -> ok={res.get('ok')} | reason={res.get('reason')}")
except Exception as e:
    print("Error querying gateway portfolio:", e)

# 2. Paper Equity File
print("\n--- 2. PAPER EQUITY STATE FILE ---")
pe_path = Path("/opt/signalcopyreal/journal/paper_equity.json")
if pe_path.exists():
    print("paper_equity.json:", pe_path.read_text().strip())

# 3. Open Positions
print("\n--- 3. OPEN POSITIONS DETAILS ---")
op_path = Path("/opt/signalcopyreal/journal/open_positions.json")
if op_path.exists():
    try:
        opos = json.loads(op_path.read_text(encoding="utf-8"))
        print(f"Total open: {len(opos)}")
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
            print(f"  {sym} {side} (tag={tag}) | Entry: {entry} | Mark: {quote} | SL: {sl} | TP1: {tp1} (Hit={tp1_hit}) | Partial PnL: ${partial_pnl:+.4f} | Opened: {opened}")
    except Exception as e:
        print("Error reading open_positions:", e)

# 4. Closed Trades in History
print("\n--- 4. TRADES CLOSED SINCE 2026-10-19 14:00 UTC ---")
AUDIT_CUTOFF = datetime(2026, 10, 19, 14, 0, 0, tzinfo=timezone.utc)
th_path = Path("/opt/signalcopyreal/journal/trade_history.json")
if th_path.exists():
    try:
        raw = json.loads(th_path.read_text(encoding="utf-8"))
        items = raw if isinstance(raw, list) else list(raw.values())
        recent_closed = []
        for t in items:
            tc_str = t.get("timestamp_close") or t.get("closed_at")
            if tc_str:
                try:
                    tc = datetime.fromisoformat(tc_str.replace("Z", "+00:00"))
                    if tc >= AUDIT_CUTOFF:
                        recent_closed.append((tc, t))
                except Exception:
                    pass
        recent_closed.sort(key=lambda x: x[0])
        print(f"Total closed trades since {AUDIT_CUTOFF.strftime('%Y-%m-%d %H:%M:%S UTC')}: {len(recent_closed)}")
        total_pnl = 0.0
        for tc, t in recent_closed:
            sym = t.get("symbol")
            side = t.get("side")
            pnl = t.get("pnl_usd") if t.get("pnl_usd") is not None else t.get("net_pnl_usd", 0.0)
            fee = (t.get("entry_fee_usd", 0.0) or 0.0) + (t.get("exit_fee_usd", 0.0) or 0.0)
            reason = t.get("reason") or t.get("close_reason")
            tag = t.get("tag") or t.get("metadata", {}).get("tag", "no_tag")
            total_pnl += (pnl or 0.0)
            print(f"  [{tc.strftime('%Y-%m-%d %H:%M:%S')}] {sym} {side} | PnL: ${pnl:+.4f} | Fee: ${fee:.4f} | Reason: {reason} | Tag: {tag}")
        print(f"Total Net PnL of Recent Closed: ${total_pnl:+.4f} USD")
    except Exception as e:
        print("Error reading trade_history:", e)

# 5. Autonomous Scanner Service Status & Activity
print("\n--- 5. AUTONOMOUS SCANNER ACTIVITY ---")
try:
    res_svc = subprocess.run(["systemctl", "is-active", "nexus-autonomous-scanner.service"], capture_output=True, text=True)
    print("Service status:", res_svc.stdout.strip())
    res_logs = subprocess.run(["journalctl", "-u", "nexus-autonomous-scanner.service", "--no-pager", "-n", "30"], capture_output=True, text=True)
    print("Recent logs:")
    print(res_logs.stdout[-2000:] if res_logs.stdout else res_logs.stderr[-2000:])
except Exception as e:
    print("Error getting scanner service logs:", e)

# 6. Signal Copy Telegram Activity
print("\n--- 6. SIGNAL COPY (TELEGRAM) ACTIVITY ---")
sl_path = Path("/opt/signalcopyreal/runtime/state/signal_lifecycle.jsonl")
if sl_path.exists():
    try:
        from collections import Counter
        sigs = {}
        with sl_path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                    ts_str = row.get("ts")
                    if ts_str:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if ts >= AUDIT_CUTOFF:
                            sid = row.get("signal_id")
                            if sid:
                                sigs.setdefault(sid, {})
                                sigs[sid]["stage"] = row.get("stage", sigs[sid].get("stage"))
                                if row.get("reason"):
                                    sigs[sid]["reason"] = row.get("reason")
                                if "score" in row:
                                    sigs[sid]["score"] = row["score"]
                except Exception:
                    pass
        print(f"Total Telegram signals received since {AUDIT_CUTOFF.strftime('%Y-%m-%d %H:%M:%S UTC')}: {len(sigs)}")
        stages = Counter(s.get("stage") for s in sigs.values())
        for st, c in stages.most_common():
            print(f"  {st}: {c}")
        reasons = Counter(s.get("reason") for s in sigs.values() if s.get("reason"))
        print("Top rejection reasons:")
        for r, c in reasons.most_common(5):
            print(f"  - [{c}x] {r}")
    except Exception as e:
        print("Error reading signal_lifecycle:", e)
