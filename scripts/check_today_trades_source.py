import json
from datetime import datetime, timezone
from pathlib import Path

START_TODAY = datetime(2026, 10, 19, 0, 0, 0, tzinfo=timezone.utc)
trade_path = Path("/opt/signalcopyreal/journal/trade_history.json")

if trade_path.exists():
    raw = json.loads(trade_path.read_text(encoding="utf-8"))
    items = raw if isinstance(raw, list) else list(raw.values())
    for t in items:
        tc = t.get("timestamp_close") or t.get("closed_at")
        if tc:
            try:
                dt = datetime.fromisoformat(tc.replace("Z", "+00:00"))
                if dt >= START_TODAY:
                    to = t.get("timestamp_open")
                    sym = t.get("symbol")
                    side = t.get("side")
                    pnl = t.get("pnl_usd", 0.0)
                    src = t.get("metadata", {}).get("source") or t.get("source", "UNKNOWN")
                    intent_id = t.get("intent_id")
                    print(f"{sym} {side} | Opened: {to} -> Closed: {tc} | PnL: ${pnl:+.4f} | Source: {src} | Intent: {intent_id}")
            except Exception:
                pass
