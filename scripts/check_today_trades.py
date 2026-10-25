import json
from datetime import datetime, timezone
from pathlib import Path

START_TODAY = datetime(2026, 10, 19, 0, 0, 0, tzinfo=timezone.utc)
trade_path = Path("/opt/signalcopyreal/journal/trade_history.json")

print(f"=== ALL TRADES CLOSED ON 2026-10-19 ===")
if trade_path.exists():
    raw = json.loads(trade_path.read_text(encoding="utf-8"))
    items = raw if isinstance(raw, list) else list(raw.values())
    today_trades = []
    for t in items:
        tc = t.get("timestamp_close") or t.get("closed_at")
        if tc:
            try:
                dt = datetime.fromisoformat(tc.replace("Z", "+00:00"))
                if dt >= START_TODAY:
                    today_trades.append((dt, t))
            except Exception:
                pass
    today_trades.sort(key=lambda x: x[0])
    total_pnl = 0.0
    for dt, t in today_trades:
        sym = t.get("symbol")
        side = t.get("side")
        pnl = t.get("pnl_usd", 0.0) or t.get("net_pnl_usd", 0.0) or 0.0
        fee = (t.get("entry_fee_usd", 0.0) or 0.0) + (t.get("exit_fee_usd", 0.0) or 0.0)
        reason = t.get("reason") or t.get("close_reason")
        tag = t.get("tag") or t.get("metadata", {}).get("tag", "no_tag")
        total_pnl += pnl
        print(f"[{dt.strftime('%H:%M:%S')}] {sym} {side} | PnL: ${pnl:+.4f} | Fees: ${fee:.4f} | Reason: {reason} | Tag: {tag}")
    print(f"\nTotal Closed PnL Today: ${total_pnl:+.4f} across {len(today_trades)} trades")
