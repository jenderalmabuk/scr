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
    
    print(f"Total Trades: {len(trades)}")
    for t in trades[:10]:
        sym = t.get("symbol")
        side = t.get("side")
        pnl = t.get("pnl_usd", 0.0)
        # Check shadow_mfe_r or exit_variants mfe_r
        mfe_r = t.get("shadow_mfe_r") or t.get("scratch_max_favorable_r")
        variants = t.get("shadow_audit", {}).get("exit_variants", {})
        if not mfe_r and variants:
            mfe_r = variants.get("current", {}).get("mfe_r")
        print(f"  {sym:12} {side:5} | PnL: ${pnl:+.4f} | MFE: {mfe_r} | reason: {t.get('reason')}")
