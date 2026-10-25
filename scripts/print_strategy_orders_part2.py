with open("/opt/signalcopyreal/scripts/ifvg/strategy_orders.pine", "r", encoding="utf-8") as f:
    lines = f.readlines()

print("".join(lines[120:250]))
