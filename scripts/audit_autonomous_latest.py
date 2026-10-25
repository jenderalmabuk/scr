import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from collections import Counter
import subprocess

print("===================================================================")
print("=== AUDIT TERBARU BOT AUTONOMOUS (20 Okt 07:00 UTC - 21 Okt 01:00 UTC) ===")
print("===================================================================\n")

# 1. Gateway Portfolio Query
print("--- 1. GATEWAY PORTFOLIO & EQUITY STATUS ---")
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
        print(f"Active Open Positions: {port.get('open_position_count', len(port.get('open_positions', [])))}")
        print(f"Open Symbols: {port.get('open_symbols', [])}")
        print(f"Active Open Risk: ${port.get('active_open_risk_total', 0.0):.4f}")
        print(f"Total Open Risk: ${port.get('total_open_risk', 0.0):.4f}")
        print(f"Recent Intents Count: {len(port.get('recent_intents', []))}")
except Exception as e:
    print("Error querying gateway portfolio:", e)

# 2. Paper Equity State File
print("\n--- 2. PAPER EQUITY STATE FILE ---")
pe_path = Path("/opt/signalcopyreal/journal/paper_equity.json")
if pe_path.exists():
    print("paper_equity.json:", pe_path.read_text().strip())

# 3. Active Open Positions Details
print("\n--- 3. ACTIVE OPEN POSITIONS ---")
op_path = Path("/opt/signalcopyreal/journal/open_positions.json")
if op_path.exists():
    try:
        opos = json.loads(op_path.read_text(encoding="utf-8"))
        print(f"Total Open Positions: {len(opos)}")
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
            print(f"  [{sym}] {side} (tag={tag})")
            print(f"    Entry: {entry} | Mark: {quote} | SL: {sl} | TP1: {tp1} (Hit={tp1_hit})")
            print(f"    Partial PnL: ${partial_pnl:+.4f} | Opened: {opened}")
    except Exception as e:
        print("Error reading open_positions:", e)

# 4. Trades Closed Since Yesterday Afternoon (2026-10-20 06:50 UTC)
AUDIT_CUTOFF = datetime(2026, 10, 20, 6, 50, 0, tzinfo=timezone.utc)
th_path = Path("/opt/signalcopyreal/journal/trade_history.json")

print(f"\n--- 4. TRADES CLOSED SINCE 2026-10-20 06:50 UTC ---")
if th_path.exists():
    try:
        raw = json.loads(th_path.read_text(encoding="utf-8"))
        items = raw if isinstance(raw, list) else list(raw.values())
        period_closed = []
        for t in items:
            tc_str = t.get("timestamp_close") or t.get("closed_at")
            if tc_str:
                try:
                    tc = datetime.fromisoformat(tc_str.replace("Z", "+00:00"))
                    if tc >= AUDIT_CUTOFF:
                        period_closed.append((tc, t))
                except Exception:
                    pass
        period_closed.sort(key=lambda x: x[0])
        print(f"Total trades closed in this period: {len(period_closed)}")
        
        wins = [t for _, t in period_closed if (t.get("pnl_usd") or 0.0) > 0]
        losses = [t for _, t in period_closed if (t.get("pnl_usd") or 0.0) <= 0]
        win_pnl = sum(t.get("pnl_usd", 0.0) or 0.0 for t in wins)
        loss_pnl = sum(t.get("pnl_usd", 0.0) or 0.0 for t in losses)
        net_pnl = win_pnl + loss_pnl

        print(f"Wins: {len(wins)} | Win PnL: ${win_pnl:+.4f}")
        print(f"Losses: {len(losses)} | Loss PnL: ${loss_pnl:+.4f}")
        print(f"Net Closed PnL: ${net_pnl:+.4f} USD")

        print("\nList of Closed Trades:")
        for tc, t in period_closed:
            sym = t.get("symbol")
            side = t.get("side")
            pnl = t.get("pnl_usd", 0.0)
            fee = (t.get("entry_fee_usd", 0.0) or 0.0) + (t.get("exit_fee_usd", 0.0) or 0.0)
            reason = t.get("reason") or t.get("close_reason")
            to = t.get("timestamp_open")
            print(f"  [{tc.strftime('%Y-%m-%d %H:%M:%S')}] {sym} {side} | PnL: ${pnl:+.4f} | Fee: ${fee:.4f} | Reason: {reason} | Opened: {to}")
    except Exception as e:
        print("Error reading trade_history:", e)

# 5. Full All-Time Summary of Autonomous Scanner Since Overhaul (Oct 18 16:22 UTC)
OVERHAUL_CUTOFF = datetime(2026, 10, 18, 16, 22, 0, tzinfo=timezone.utc)
print(f"\n--- 5. ALL-TIME AUTONOMOUS BOT METRICS (Since Overhaul Oct 18 16:22 UTC) ---")
if th_path.exists():
    try:
        raw = json.loads(th_path.read_text(encoding="utf-8"))
        items = raw if isinstance(raw, list) else list(raw.values())
        all_trades = []
        for t in items:
            tc_str = t.get("timestamp_close") or t.get("closed_at")
            if tc_str:
                try:
                    tc = datetime.fromisoformat(tc_str.replace("Z", "+00:00"))
                    if tc >= OVERHAUL_CUTOFF:
                        all_trades.append(t)
                except Exception:
                    pass
        print(f"Total Closed Trades: {len(all_trades)}")
        tp2_trades = [t for t in all_trades if "TP2" in str(t.get("reason", ""))]
        all_wins = [t for t in all_trades if (t.get("pnl_usd") or 0.0) > 0]
        all_losses = [t for t in all_trades if (t.get("pnl_usd") or 0.0) <= 0]
        total_win = sum(t.get("pnl_usd", 0.0) or 0.0 for t in all_wins)
        total_loss = sum(t.get("pnl_usd", 0.0) or 0.0 for t in all_losses)
        print(f"TP2 Hit Count: {len(tp2_trades)}")
        print(f"Total Wins: {len(all_wins)} (${total_win:+.4f})")
        print(f"Total Losses: {len(all_losses)} (${total_loss:+.4f})")
        print(f"Net Realized PnL: ${total_win + total_loss:+.4f} USD")
    except Exception as e:
        print("Error calculating all-time metrics:", e)

# 6. Autonomous Scanner Service Status & Recent Activity
print("\n--- 6. SCANNER SERVICE STATUS & LOGS ---")
try:
    res_svc = subprocess.run(["systemctl", "is-active", "nexus-autonomous-scanner.service"], capture_output=True, text=True)
    print("Service status:", res_svc.stdout.strip())
    res_logs = subprocess.run(["journalctl", "-u", "nexus-autonomous-scanner.service", "--no-pager", "-n", "30"], capture_output=True, text=True)
    print("Recent Logs:")
    print(res_logs.stdout[-2000:] if res_logs.stdout else res_logs.stderr[-2000:])
except Exception as e:
    print("Error getting scanner logs:", e)
