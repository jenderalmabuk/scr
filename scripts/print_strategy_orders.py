with open("/opt/signalcopyreal/scripts/ifvg/strategy_orders.pine", "r", encoding="utf-8") as f:
    text = f.read()

# Let's inspect inputs and order execution logic
print(text[:2500])
print("\n... [MIDDLE] ...\n")
print(text[2500:5000])
