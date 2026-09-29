import json

lifecycle_path = "/opt/signalcopyreal/runtime/state/pending_lifecycle.jsonl"

total_pending = 0
unlocked_market_entries = 0
missed_winners_saved = 0

with open(lifecycle_path) as f:
    for line in f:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if row.get("event") == "PENDING":
                total_pending += 1
                reason = row.get("reason", "")
                price = float(row.get("price") or 0.0)
                boundary = float(row.get("boundary") or 0.0)
                sl = float(row.get("stop_loss") or 0.0)
                low = float(row.get("entry_low") or 0.0)
                high = float(row.get("entry_high") or 0.0)
                side = row.get("side", "")
                
                # Check buffer simulation:
                # 0.4% buffer around entry
                buf = max(abs(boundary - sl) * 0.20 if sl > 0 else 0, boundary * 0.005)
                eff_low = low - buf
                eff_high = high + buf
                
                inside_with_buffer = (eff_low <= price <= eff_high)
                
                # Check chase with max_chase_r=0.50 and min_rr=0.30
                risk = abs(boundary - sl) if sl > 0 else boundary * 0.01
                drift_r = abs(price - boundary) / risk if risk > 0 else 999.0
                chase_ok = (drift_r <= 0.50)
                
                # Would it be MARKET under improved rules?
                unlocked = False
                if reason == "PRICE_OUTSIDE_ENTRY_ZONE":
                    if inside_with_buffer or chase_ok:
                        unlocked = True
                elif reason in ("AUTO_SCORE_MARKET_TO_LIMIT", "FLOW_NO_TRADE_MARKET_BLOCK"):
                    unlocked = True
                    
                if unlocked:
                    unlocked_market_entries += 1
        except Exception:
            pass

print(f"Total historical pending setups: {total_pending}")
print(f"Setups that would be unlocked to instant MARKET execution: {unlocked_market_entries} ({unlocked_market_entries/total_pending*100:.1f}%)")
