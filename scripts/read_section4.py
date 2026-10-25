with open("/opt/signalcopyreal/scripts/IFVG_ARCHITECTURE.md", "r", encoding="utf-8") as f:
    text = f.read()

# Extract Section 4: Core Algorithms
sec4_start = text.find("## 4. Core Algorithms")
sec6_start = text.find("## 6. Memory Management")

if sec4_start != -1 and sec6_start != -1:
    print(text[sec4_start:sec6_start])
else:
    print("Section 4 not found!")
