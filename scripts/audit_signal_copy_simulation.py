import json
from datetime import datetime, timezone
from pathlib import Path
from collections import Counter

START_TS = datetime(2026, 10, 18, 16, 22, 0, tzinfo=timezone.utc).timestamp()
outcome_path = Path("/opt/signalcopyreal/runtime/state/committee_outcomes.jsonl")
lifecycle_path = Path("/opt/signalcopyreal/runtime/state/signal_lifecycle.jsonl")

print("==========================================================================")
print("=== AUDIT SIGNAL COPY JIKA DITERAPKAN FILTER 1+2+3 & PENGELOLAAN TP/SL ===")
print("==========================================================================\n")

LOW_LIQUID_COINS = {
    "AKEUSDT", "GIGGLEUSDT", "EPICUSDT", "CAPUSDT", "KAITOUSDT", 
    "BANKUSDT", "CYSUSDT", "BEATUSDT", "COTIUSDT", "UAIUSDT", "BTWUSDT", "BLESSUSDT",
    "USELESSUSDT", "APRUSDT", "BATUSDT", "WLFIUSDT"
}

# 1. Evaluate Telegram signals from committee_outcomes
raw_outcomes = []
if outcome_path.exists():
    with outcome_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
                if row.get("ts", 0) >= START_TS:
                    raw_outcomes.append(row)
            except Exception:
                pass

print(f"Total Telegram Signals Tracked in Outcomes since Oct 18: {len(raw_outcomes)}")

# Test Filter 2 (Liquid coins only) on Telegram signals
liquid_tg = [r for r in raw_outcomes if r.get("symbol") not in LOW_LIQUID_COINS]
print(f"Signals remaining after Filter 2 (Liquid Whitelist): {len(liquid_tg)} of {len(raw_outcomes)}")

# Compare performance of Liquid Telegram Signals
def analyze_outcomes(label, data):
    print(f"\n--- {label} ({len(data)} signals) ---")
    if not data:
        return
    reasons = Counter(r.get("exit_reason") for r in data)
    for r, cnt in reasons.most_common():
        print(f"  {r}: {cnt} ({cnt/len(data)*100:.1f}%)")
    
    # Model 1 (Old: 50% @ 1R, 50% @ 2R)
    # TP1 hit gives +0.5R, SL gives -1.0R, Expire gives ~0.0R
    # Model 2 (Proposed: 1R->BEP, 75% @ 2R, 25% Trailing)
    # If hit TP1 then reverse -> 0.0R (BEP)
    # If hit TP2 -> +1.5R (75%) + +0.55R (25% trailing) = +2.05R
    # If SL -> -1.0R
    m1_r = 0.0
    m2_r = 0.0
    for r in data:
        reason = r.get("exit_reason")
        if reason == "TP1":
            # In old model, TP1 locked +0.5R
            m1_r += 0.50
            # In new model, 1R moves to BEP. Did it reach 2R?
            # Check pnl_pct or exit_price
            pnl_pct = abs(float(r.get("pnl_pct", 0.0)))
            if pnl_pct >= 2.0:
                m2_r += 2.05 # Reached 2R
            else:
                m2_r += 0.0 # Stopped at BEP
        elif reason == "SL":
            m1_r -= 1.0
            m2_r -= 1.0
        elif reason == "EXPIRE":
            pnl = float(r.get("pnl_pct", 0.0))
            m1_r += (pnl / 2.0)
            m2_r += (pnl / 2.0)
            
    print(f"  Model 1 Expectancy (R): {m1_r:+.2f}R (Total ${m1_r * 2.0:+.2f} USD on $2 risk)")
    print(f"  Model 2 Expectancy (R): {m2_r:+.2f}R (Total ${m2_r * 2.0:+.2f} USD on $2 risk)")

analyze_outcomes("Telegram Signals - Baseline (Semua Koin)", raw_outcomes)
analyze_outcomes("Telegram Signals - Dengan Filter Koin Likuid Saja", liquid_tg)
