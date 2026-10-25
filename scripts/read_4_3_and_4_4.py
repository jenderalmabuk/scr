with open("/opt/signalcopyreal/scripts/IFVG_ARCHITECTURE.md", "r", encoding="utf-8") as f:
    text = f.read()

# Extract 4.3 and 4.4
start = text.find("### 4.3 Swing Structure Detection Algorithm")
end = text.find("## 5. Data Flow")

print(text[start:end])
