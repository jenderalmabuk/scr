import re
from pathlib import Path

scanner_path = Path("/opt/signalcopyreal/bots/autonomous_market_scanner.py")
content = scanner_path.read_text(encoding="utf-8")

print("=== 1. PATCHING AUTONOMOUS MARKET SCANNER (FILTERS 1, 2, 3) ===")

# 1. Update MIN_VOLUME_USD to 25,000,000.0
content = re.sub(
    r'MIN_VOLUME_USD\s*=\s*float\(os\.getenv\("AUTONOMOUS_MIN_VOL_USD",\s*"[0-9.]+"\)\)',
    'MIN_VOLUME_USD = float(os.getenv("AUTONOMOUS_MIN_VOL_USD", "25000000.0"))',
    content
)

# 2. Add LOW_LIQUID_COINS set and filter in get_universe
low_liquid_def = """LOW_LIQUID_COINS = {
    "AKEUSDT", "GIGGLEUSDT", "EPICUSDT", "CAPUSDT", "KAITOUSDT", 
    "BANKUSDT", "CYSUSDT", "BEATUSDT", "COTIUSDT", "UAIUSDT", "BTWUSDT", "BLESSUSDT",
    "USELESSUSDT", "APRUSDT", "BATUSDT", "WLFIUSDT"
}
"""

if "LOW_LIQUID_COINS" not in content:
    content = content.replace("WHALES_DIR = Path(\"/opt/signalcopyreal/runtime/whales\")", 
                              "WHALES_DIR = Path(\"/opt/signalcopyreal/runtime/whales\")\n" + low_liquid_def)

# Ensure get_universe excludes LOW_LIQUID_COINS
if "and pair not in LOW_LIQUID_COINS" not in content:
    content = content.replace(
        'and pair not in TRADFI_EXCLUSIONS',
        'and pair not in TRADFI_EXCLUSIONS and pair not in LOW_LIQUID_COINS'
    )

# 3. Filter 1: Update SHORT to require btc_regime != "RISK_ON"
content = re.sub(
    r'elif\s+is_bearish_1h:',
    'elif is_bearish_1h and btc_regime != "RISK_ON":',
    content
)

# 4. Filter 3: Minimum SL Distance >= 1.0% (0.010) and 1.5 * atr_15m
content = content.replace("min_sl_dist = entry * 0.008", "min_sl_dist = entry * 0.010")
content = content.replace("1.2 * atr_15m", "1.5 * atr_15m")

scanner_path.write_text(content, encoding="utf-8")
print("Successfully patched /opt/signalcopyreal/bots/autonomous_market_scanner.py")
