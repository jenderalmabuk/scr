import json
from datetime import datetime, timezone
from pathlib import Path

OVERHAUL_CUTOFF = datetime(2026, 10, 18, 16, 22, 0, tzinfo=timezone.utc)
th_path = Path("/opt/signalcopyreal/journal/trade_history.json")

if th_path.exists():
    raw = json.loads(th_path.read_text(encoding="utf-8"))
    items = raw if isinstance(raw, list) else list(raw.values())
    trades = []
    for t in items:
        tc_str = t.get("timestamp_close") or t.get("closed_at")
        if tc_str:
            try:
                tc = datetime.fromisoformat(tc_str.replace("Z", "+00:00"))
                if tc >= OVERHAUL_CUTOFF:
                    trades.append(t)
            except Exception:
                pass
    
    print(f"Loaded {len(trades)} trades.")
    if trades:
        t0 = trades[0]
        print("Sample Trade Keys:", list(t0.keys()))
        print("btc_context_snapshot:", t0.get("btc_context_snapshot"))
        print("adv_snapshot:", t0.get("adv_snapshot"))
        print("market_context:", t0.get("market_context"))
