import json
from collections import defaultdict
from datetime import datetime, timezone

lifecycle_path = "/opt/signalcopyreal/runtime/state/pending_lifecycle.jsonl"

pending_setups = {}
missed_winners = []
reasons_count = defaultdict(int)

with open(lifecycle_path) as f:
    for line in f:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            sig_id = row.get("signal_id")
            event = row.get("event")
            
            if event == "PENDING":
                pending_setups[sig_id] = row
                reasons_count[row.get("reason", "UNKNOWN")] += 1
            elif event in ("EXPIRED", "CANCELLED", "REJECTED"):
                if sig_id in pending_setups:
                    p = pending_setups[sig_id]
                    side = p.get("side", "")
                    tp_list = p.get("take_profits", [])
                    tp1 = float(tp_list[0]) if tp_list else 0.0
                    
                    path_h = float(row.get("path_high") or 0.0)
                    path_l = float(row.get("path_low") or 999999.0)
                    
                    # Check if price reached TP1 while waiting
                    hit_tp = False
                    if tp1 > 0:
                        if side == "LONG" and path_h >= tp1:
                            hit_tp = True
                        elif side == "SHORT" and path_l <= tp1:
                            hit_tp = True
                            
                    if hit_tp:
                        missed_winners.append({
                            "symbol": p.get("symbol"),
                            "side": side,
                            "entry_req": p.get("boundary") or p.get("active_entry"),
                            "price_at_sig": p.get("price"),
                            "tp1": tp1,
                            "path_extreme": path_h if side == "LONG" else path_l,
                            "reason": p.get("reason"),
                            "ts": row.get("ts")
                        })
        except Exception:
            pass

print("="*90)
print(f"AUDIT PENDING SETUPS DARI LOG (pending_lifecycle.jsonl):")
print("="*90)
print(f"Total Sinyal yang dijadikan PENDING LIMIT : {len(pending_setups)}")
print(f"Sinyal Pending yang TERBANG CAPAI TP1 TANPA TERISI : {len(missed_winners)}")
print("\nDistribusi Alasan Mengapa Order Dijadikan LIMIT PENDING bukannya MARKET:")
for r, c in sorted(reasons_count.items(), key=lambda x: x[1], reverse=True):
    print(f"  • {r:30}: {c:3} sinyal")

print("\n" + "="*90)
print(f"CONTOH SINYAL PENDING YANG TERBANG KE TARGET (TERLEWATKAN):")
print("="*90)
print(f"{'Waktu':19} | {'Symbol':12} | {'Side':5} | {'Harga Sinyal':12} | {'Limit Tunggu':12} | {'TP1':10} | {'Puncak Harga':12} | {'Alasan Pending'}")
print("-" * 105)
for m in missed_winners[-20:]:
    print(f"{m['ts'][:19]:19} | {m['symbol']:12} | {m['side']:5} | {str(m['price_at_sig']):12} | {str(m['entry_req']):12} | {str(m['tp1']):10} | {str(m['path_extreme']):12} | {m['reason']}")

print("="*90)
