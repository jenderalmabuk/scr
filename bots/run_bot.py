"""Nexus bot runner — drop-in replacement for clean_core/run_*.sh.

Loads config from config.yaml, patches fetch_recent to use Nexus API,
then runs the same engine loop.

Usage:
    python run_bot.py --bot m30_imbalance
    python run_bot.py --bot h1_imbalance --dry  # dry run, no orders
"""

import json
import os
import sys
import yaml

# --- 1. Patch fetch_recent BEFORE importing engine ---
# Replace backtest.data.fetch_recent with nexus_data.fetch_recent
import backtest.data
import bots.nexus_data as nexus_data
backtest.data.fetch_recent = nexus_data.fetch_recent

# --- 2. Load config ---
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")
with open(CONFIG_PATH) as f:
    config = yaml.safe_load(f)

# --- 3. Parse bot name from env ---
BOT_NAME = os.environ.get("BOT_NAME", "m30_imbalance")
cfg = config.get(BOT_NAME, {})
if not cfg:
    print(f"ERROR: unknown bot '{BOT_NAME}'. Available: {list(config.keys())}")
    sys.exit(1)

MIN_UNIVERSE = int(os.environ.get("MIN_UNIVERSE", "10"))


def _tg_alert(text: str) -> None:
    """Best-effort Telegram alert (runner-level, before engine import)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAMBOTTOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    try:
        import requests
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": int(chat), "text": text}, timeout=10)
    except Exception:
        pass


def _normalize_pair(p: str) -> str:
    """Freqtrade 'BTC/USDT:USDT' -> engine 'BTCUSDT'."""
    p = p.strip()
    if not p:
        return ""
    if ":" in p:
        p = p.split(":", 1)[0]
    return p.replace("/", "").upper()


def _load_universe_fallback() -> list:
    """Load universe from scanner runtime files when universe.txt is empty.

    Path override via env UNIVERSE_SOURCE; otherwise tries
    runtime/revo/freqtrade_pairlist.json then pair_universe_all.json.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = []
    env_src = os.environ.get("UNIVERSE_SOURCE")
    if env_src:
        candidates.append(env_src)
    candidates += [
        os.path.join(repo_root, "runtime", "revo", "canonical_universe.json"),
        os.path.join(repo_root, "runtime", "revo", "freqtrade_pairlist.json"),
        os.path.join(repo_root, "runtime", "revo", "pair_universe_all.json"),
    ]
    for path in candidates:
        try:
            with open(path) as fh:
                data = json.load(fh)
        except Exception:
            continue
        pairs = data.get("pairs", data) if isinstance(data, dict) else data
        symbols = []
        for item in pairs:
            if isinstance(item, str):
                sym = _normalize_pair(item)
            elif isinstance(item, dict):
                sym = item.get("symbol") or _normalize_pair(item.get("pair", ""))
            else:
                continue
            if sym and sym not in symbols:
                symbols.append(sym)
        if symbols:
            print(f"[nexus-runner] Universe loaded from fallback {path} (n={len(symbols)})")
            return symbols
    return []


# --- 4. Build CLI args from config ---
cli_args = [a for a in sys.argv[1:] if a != "--dry"]  # engine is dry by default; no --dry arg exists
cli_has_symbols = "--symbols" in cli_args
args = []

symbols_from_file = []
if "symbols_file" in cfg and not cli_has_symbols:
    try:
        with open(os.path.join(os.path.dirname(__file__), cfg["symbols_file"])) as f:
            symbols_from_file = f.read().split()
    except FileNotFoundError:
        print(f"[nexus-runner] WARNING: symbols_file {cfg['symbols_file']} not found")

if not symbols_from_file and not cli_has_symbols:
    print("[nexus-runner] WARNING: symbols_file is empty — falling back to scanner universe")
    symbols_from_file = _load_universe_fallback()

if not cli_has_symbols and len(symbols_from_file) < MIN_UNIVERSE:
    msg = (f"UNIVERSE EMPTY/TOO SMALL (n={len(symbols_from_file)}) — bot '{BOT_NAME}' "
           f"needs >= {MIN_UNIVERSE} symbols. Check bots/universe.txt or UNIVERSE_SOURCE.")
    print(f"[nexus-runner] WARNING: {msg}")
    _tg_alert(f"⚠️ {msg}")

for key, val in cfg.items():
    if key == "symbols_file":
        continue
    if isinstance(val, bool):
        if val:
            args.append(f"--{key.replace('_', '-')}")
    else:
        args.extend([f"--{key.replace('_', '-')}", str(val)])

if symbols_from_file:
    args.extend(["--symbols"] + symbols_from_file)

args.extend(cli_args)  # CLI overrides/extra flags go last
args = [str(a) for a in args]

print(f"[nexus-runner] Bot: {BOT_NAME}")
print(f"[nexus-runner] Universe size: {len(symbols_from_file) if symbols_from_file else 'CLI-provided'}")
print(f"[nexus-runner] Args: {args[:40]}{' ...' if len(args) > 40 else ''}")

# --- 5. Import engine and run ---
# Engine uses argparse — pass our built args
sys.argv = ["engine.py"] + args

from clean_core.engine import main
main()
