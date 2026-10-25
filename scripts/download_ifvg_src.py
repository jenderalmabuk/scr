import urllib.request
from pathlib import Path

out_dir = Path("/opt/signalcopyreal/scripts/ifvg")
out_dir.mkdir(parents=True, exist_ok=True)

files = [
    "src/IFVG_Indicator.pine",
    "src/IFVG_Strategy.pine",
    "tools/strategy_orders.pine",
    "docs/BACKTEST-CONTEXT.md"
]

for f in files:
    url = f"https://raw.githubusercontent.com/mohed-abbas/IFVG-Pro/main/{f}"
    dest = out_dir / Path(f).name
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        content = urllib.request.urlopen(req).read()
        dest.write_bytes(content)
        print(f"Downloaded {f} -> {dest} ({len(content)} bytes)")
    except Exception as e:
        print(f"Failed {f}: {e}")

print("All files downloaded.")
