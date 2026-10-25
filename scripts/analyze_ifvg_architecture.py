import urllib.request
import re

url = "https://raw.githubusercontent.com/mohed-abbas/IFVG-Pro/main/ARCHITECTURE.md"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
content = urllib.request.urlopen(req).read().decode("utf-8")

print(f"Total length of ARCHITECTURE.md: {len(content)} characters\n")

# Extract Table of Contents or Headers
headers = [line for line in content.splitlines() if line.startswith("#")]
print("Headers in ARCHITECTURE.md:")
for h in headers[:45]:
    print("  ", h)

# Save ARCHITECTURE.md locally in scratch for fast reading
with open("/opt/signalcopyreal/scripts/IFVG_ARCHITECTURE.md", "w", encoding="utf-8") as f:
    f.write(content)

print("\nSaved IFVG_ARCHITECTURE.md to VPS /opt/signalcopyreal/scripts/")
