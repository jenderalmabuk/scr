import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from collections import Counter

# Let's inspect the exact distribution of score vs 2R outcome
decisions_path = Path("/opt/signalcopyreal/runtime/state/committee_decisions.jsonl")

# We can re-use the audit script logic with score buckets
with open("/opt/signalcopyreal/scripts/audit_telegram_pure_rr.py", "r") as f:
    code = f.read()

# Let's add a score bucket analysis to the end of the script
bucket_code = """
print("==========================================================================")
print("=== ANALISIS SENSITIVITAS SCORE THRESHOLD TERHADAP WIN RATE & EXPECTANCY ===")
print("==========================================================================")

for min_score in [0, 50, 55, 60, 65, 70]:
    subset = [r for r in liquid_signals if (r.get("score") or 0.0) >= min_score and is_adversarial_ok(r)]
    if not subset:
        continue
    n = len(subset)
    r_1r = sum(1 for r in subset if r["hit_1r"])
    r_2r = sum(1 for r in subset if r["hit_2r"])
    r_bep = sum(1 for r in subset if r["outcome"] == "REVERSED_TO_BEP")
    r_sl = sum(1 for r in subset if r["outcome"] == "DIRECT_SL")
    
    net_r = (r_2r * 2.00) + (r_bep * 0.0) - (r_sl * 1.00)
    pf = (r_2r * 2.0) / (r_sl * 1.0) if r_sl > 0 else 999.0
    
    print(f"Threshold Score >= {min_score:2d} | Trades: {n:3d} | Reached 1R: {r_1r/n*100:4.1f}% | Hit 2R: {r_2r/n*100:4.1f}% | SL: {r_sl/n*100:4.1f}% | Net: {net_r:+5.1f}R (${net_r*2.0:+6.1f}) | PF: {pf:.2f}")
"""

with open("/opt/signalcopyreal/scripts/audit_telegram_pure_rr.py", "a") as f:
    f.write("\n" + bucket_code)

print("Appended score sensitivity analysis to script.")
