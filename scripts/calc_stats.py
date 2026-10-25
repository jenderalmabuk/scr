import json
from datetime import datetime, timezone
from pathlib import Path

START_SCANNER = datetime(2026, 10, 18, 16, 22, 0, tzinfo=timezone.utc)
th_path = Path("/opt/signalcopyreal/journal/trade_history.json")

print("=== STATISTICAL AUDIT OF AUTONOMOUS SCANNER TRADES ===")
if th_path.exists():
    raw = json.loads(th_path.read_text(encoding="utf-8"))
    items = raw if isinstance(raw, list) else list(raw.values())
    trades = []
    for t in items:
        tc_str = t.get("timestamp_close") or t.get("closed_at")
        if tc_str:
            try:
                tc = datetime.fromisoformat(tc_str.replace("Z", "+00:00"))
                if tc >= START_SCANNER:
                    trades.append(t)
            except Exception:
                pass

    print(f"Total trades since system overhaul: {len(trades)}")
    wins = [t for t in trades if (t.get("pnl_usd") or 0.0) > 0]
    losses = [t for t in trades if (t.get("pnl_usd") or 0.0) <= 0]
    
    total_win_pnl = sum(t.get("pnl_usd", 0.0) or 0.0 for t in wins)
    total_loss_pnl = sum(t.get("pnl_usd", 0.0) or 0.0 for t in losses)
    total_fee = sum((t.get("entry_fee_usd", 0.0) or 0.0) + (t.get("exit_fee_usd", 0.0) or 0.0) for t in trades)
    net_pnl = total_win_pnl + total_loss_pnl

    print(f"Wins: {len(wins)} | Total Win PnL: ${total_win_pnl:+.4f} USD")
    print(f"Losses: {len(losses)} | Total Loss PnL: ${total_loss_pnl:+.4f} USD")
    print(f"Total Trading Fees: ${total_fee:.4f} USD")
    print(f"Net Realized PnL: ${net_pnl:+.4f} USD")
    if len(trades) > 0:
        print(f"Win Rate: {len(wins)/len(trades)*100:.1f}%")
    if abs(total_loss_pnl) > 0:
        print(f"Profit Factor: {abs(total_win_pnl)/abs(total_loss_pnl):.2f}")

    print("\nDetailed Breakdown of Trades:")
    for t in trades:
        sym = t.get("symbol")
        side = t.get("side")
        pnl = t.get("pnl_usd", 0.0)
        reason = t.get("reason") or t.get("close_reason")
        tc = t.get("timestamp_close")
        to = t.get("timestamp_open")
        print(f"  {sym} {side} | PnL: ${pnl:+.4f} | Reason: {reason} | Open: {to} -> Close: {tc}")
