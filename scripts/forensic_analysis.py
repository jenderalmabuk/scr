import json
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

OVERHAUL_CUTOFF = datetime(2026, 10, 18, 16, 22, 0, tzinfo=timezone.utc)
th_path = Path("/opt/signalcopyreal/journal/trade_history.json")

print("===================================================================")
print("=== FORENSIC ANALYSIS: WHY IS EQUITY DOWN -$3.09 ACROSS 39 TRADES? ===")
print("===================================================================\n")

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

    print(f"Total Analyzed Trades: {len(trades)}")
    
    # 1. By Direction (LONG vs SHORT)
    by_side = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0, "pnl": 0.0, "fees": 0.0})
    for t in trades:
        s = t.get("side", "UNKNOWN")
        pnl = t.get("pnl_usd", 0.0) or t.get("net_pnl_usd", 0.0) or 0.0
        fee = (t.get("entry_fee_usd", 0.0) or 0.0) + (t.get("exit_fee_usd", 0.0) or 0.0)
        by_side[s]["count"] += 1
        by_side[s]["pnl"] += pnl
        by_side[s]["fees"] += fee
        if pnl > 0:
            by_side[s]["wins"] += 1
        else:
            by_side[s]["losses"] += 1

    print("\n--- 1. PERFORMANCE BY DIRECTION ---")
    for s, data in by_side.items():
        wr = (data["wins"] / data["count"]) * 100 if data["count"] > 0 else 0
        print(f"  {s}: {data['count']} trades | Wins: {data['wins']} | Losses: {data['losses']} | WinRate: {wr:.1f}% | Net PnL: ${data['pnl']:+.4f} | Fees: ${data['fees']:.4f}")

    # 2. By Symbol Performance
    by_sym = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0, "pnl": 0.0})
    for t in trades:
        sym = t.get("symbol", "UNKNOWN")
        pnl = t.get("pnl_usd", 0.0) or t.get("net_pnl_usd", 0.0) or 0.0
        by_sym[sym]["count"] += 1
        by_sym[sym]["pnl"] += pnl
        if pnl > 0:
            by_sym[sym]["wins"] += 1
        else:
            by_sym[sym]["losses"] += 1

    print("\n--- 2. TOP PROFITABLE SYMBOLS ---")
    sorted_syms = sorted(by_sym.items(), key=lambda x: x[1]["pnl"], reverse=True)
    for sym, d in sorted_syms[:7]:
        print(f"  {sym:12} | PnL: ${d['pnl']:+.4f} | {d['wins']}W / {d['losses']}L")

    print("\n--- 3. TOP LOSING SYMBOLS ---")
    for sym, d in sorted_syms[-8:]:
        print(f"  {sym:12} | PnL: ${d['pnl']:+.4f} | {d['wins']}W / {d['losses']}L")

    # 4. Stop Loss Distance & Duration Analysis
    print("\n--- 4. DURATION & SL DISTANCE OF LOSSES ---")
    durations_min = []
    sl_distances_pct = []
    for t in trades:
        pnl = t.get("pnl_usd", 0.0) or t.get("net_pnl_usd", 0.0) or 0.0
        if pnl <= 0:
            hm = t.get("hold_minutes")
            if hm is not None:
                durations_min.append(float(hm))
            sl_pct = t.get("sl_pct") or t.get("adv_snapshot", {}).get("sl_pct")
            if sl_pct is not None:
                sl_distances_pct.append(float(sl_pct))

    if durations_min:
        print(f"  Average Hold Time of Losses: {sum(durations_min)/len(durations_min):.1f} minutes")
        print(f"  Median Hold Time: {sorted(durations_min)[len(durations_min)//2]:.1f} minutes")
        under_30m = [m for m in durations_min if m < 30]
        print(f"  Losses stopped out in < 30 min: {len(under_30m)} of {len(durations_min)} ({len(under_30m)/len(durations_min)*100:.1f}%)")

    if sl_distances_pct:
        print(f"  Average SL Distance: {sum(sl_distances_pct)/len(sl_distances_pct):.2f}%")
        print(f"  Min SL Distance: {min(sl_distances_pct):.2f}% | Max: {max(sl_distances_pct):.2f}%")

    # 5. Fee Drag
    total_gross = sum(t.get("gross_pnl_usd", t.get("pnl_usd", 0.0)) for t in trades)
    total_fee = sum((t.get("entry_fee_usd", 0.0) or 0.0) + (t.get("exit_fee_usd", 0.0) or 0.0) for t in trades)
    print("\n--- 5. FEE DRAG ANALYSIS ---")
    print(f"  Gross PnL (Before Fees): ${total_gross:+.4f} USD")
    print(f"  Total Exchange Fees: ${total_fee:.4f} USD")
    print(f"  Net Realized PnL: ${total_gross - total_fee:+.4f} USD")
    print(f"  Fees consumed {abs(total_fee/total_gross)*100:.1f}% of gross movement" if total_gross != 0 else "")
