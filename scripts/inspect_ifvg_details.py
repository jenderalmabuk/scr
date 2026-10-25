import re
from pathlib import Path

ind_path = Path("/opt/signalcopyreal/scripts/ifvg/IFVG_Indicator.pine")
orders_path = Path("/opt/signalcopyreal/scripts/ifvg/strategy_orders.pine")

ind_text = ind_path.read_text(encoding="utf-8")
orders_text = orders_path.read_text(encoding="utf-8")

print(f"IFVG_Indicator.pine: {len(ind_text.splitlines())} lines")
print(f"strategy_orders.pine: {len(orders_text.splitlines())} lines\n")

# Find section titles in IFVG_Indicator.pine
sections = re.findall(r"//\s*={5,}\s*\n//\s*(SECTION\s*\d+[^:\n]*:[^\n]*)", ind_text, re.IGNORECASE)
if not sections:
    sections = [l for l in ind_text.splitlines() if "SECTION" in l and "//" in l]

print("Sections in IFVG_Indicator.pine:")
for s in sections[:25]:
    print("  ", s.strip())

# Find type definitions
types = re.findall(r"type\s+([A-Za-z0-9_]+)\s*\n([^}]+)", ind_text)
print(f"\nFound {len(types)} custom types in IFVG_Indicator.pine:")
for t_name, t_body in types[:5]:
    fields = [f.strip() for f in t_body.splitlines() if f.strip() and not f.strip().startswith("//")]
    print(f"  Type {t_name} with {len(fields)} fields: {', '.join(fields[:6])}...")

# Check strategy_orders.pine logic: How orders are placed and managed
print("\n--- Strategy Orders Logic Overview ---")
order_calls = [l.strip() for l in orders_text.splitlines() if "strategy.entry" in l or "strategy.exit" in l or "strategy.close" in l]
print("Order calls in strategy_orders.pine:")
for oc in order_calls:
    print("  ", oc)
