path = "/opt/signalcopyreal/signal_copy/validation_engine.py"
with open(path, "r", encoding="utf-8") as f:
    content = f.read()

# 1. Add import os if missing
if "import os" not in content:
    target_imp = "from typing import Any, Dict, List, Optional"
    replace_imp = "from typing import Any, Dict, List, Optional\nimport os"
    content = content.replace(target_imp, replace_imp, 1)

# 2. Add LOW_LIQUID_COINS definition
if "LOW_LIQUID_COINS = {" not in content:
    def_str = """
LOW_LIQUID_COINS = {
    "AKEUSDT", "GIGGLEUSDT", "EPICUSDT", "CAPUSDT", "KAITOUSDT", 
    "BANKUSDT", "CYSUSDT", "BEATUSDT", "COTIUSDT", "UAIUSDT", "BTWUSDT", "BLESSUSDT",
    "USELESSUSDT", "APRUSDT", "BATUSDT", "WLFIUSDT"
}
"""
    target_class = "class Verdict(str, Enum):"
    content = content.replace(target_class, def_str + "\n" + target_class, 1)

with open(path, "w", encoding="utf-8") as f:
    f.write(content)

print("validation_engine.py patched successfully!")
