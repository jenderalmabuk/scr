import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from collections import Counter

# Let's inspect the effect of Macro Guard on the Telegram signals
with open("/opt/signalcopyreal/scripts/audit_telegram_pure_rr.py", "r") as f:
    text = f.read()

macro_check_code = """
print("==========================================================================")
print("=== PENGARUH FILTER 1 (MACRO GUARD SHORT) PADA SINYAL TELEGRAM ===")
print("==========================================================================")

# Check side distribution of direct SLs in liquid signals
sl_trades = [r for r in liquid_signals if r["outcome"] == "DIRECT_SL"]
sl_sides = Counter(r["side"] for r in sl_trades)
print(f"Direct SL Breakdown: SHORT={sl_sides['SHORT']} ({sl_sides['SHORT']/len(sl_trades)*100:.1f}%), LONG={sl_sides['LONG']} ({sl_sides['LONG']/len(sl_trades)*100:.1f}%)")

hit2r_trades = [r for r in liquid_signals if r["outcome"] == "HIT_2R"]
hit2r_sides = Counter(r["side"] for r in hit2r_trades)
print(f"Hit 2R Breakdown:    SHORT={hit2r_sides['SHORT']} ({hit2r_sides['SHORT']/len(hit2r_trades)*100:.1f}%), LONG={hit2r_sides['LONG']} ({hit2r_sides['LONG']/len(hit2r_trades)*100:.1f}%)")

# Filter out SHORTs (or evaluate LONGs only during BTC Bullish run)
longs_only = [r for r in liquid_signals if r["side"] == "LONG"]
print_group_analysis("SUBGROUP: LONG SIGNALS ONLY (ALIGNED WITH BTC BULLISH MACRO)", longs_only)

shorts_only = [r for r in liquid_signals if r["side"] == "SHORT"]
print_group_analysis("SUBGROUP: SHORT SIGNALS ONLY (COUNTER-TREND)", shorts_only)
"""

with open("/opt/signalcopyreal/scripts/audit_telegram_pure_rr.py", "a") as f:
    f.write("\n" + macro_check_code)

print("Appended Macro Guard analysis.")
