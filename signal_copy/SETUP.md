# Signal Copy — Setup Guide

## 1. Install dependencies

```bash
pip install telethon discord.py aiogram python-telegram-bot
```

| Library | Used for |
|---------|----------|
| `telethon` | Read ANY Telegram signal group you're a member of (user account). Preferred. |
| `aiogram` | Telegram bot backend (confirm bot + bot-mode listener). |
| `python-telegram-bot` | Plain notifications via fusion's existing transport. |
| `discord.py` | Read Discord signal channels (bot account). |

## 2. Get credentials

### Telegram (read signal groups) — Telethon path (recommended)
1. Go to https://my.telegram.org → API development tools.
2. Create an app, note the `api_id` and `api_hash`.
3. First run will ask for your phone + login code (creates a `.session` file).

This lets the bot read **any group/channel your account has joined** — exactly
what you need to copy public signal groups.

### Telegram confirmation bot (the Ya/Tidak prompts)
1. Talk to @BotFather → `/newbot` → get a bot token.
2. Start a chat with your new bot, then get your chat id (e.g. via @userinfobot).

### Discord (optional)
1. https://discord.com/developers/applications → New Application → Bot → copy token.
2. Enable **Message Content Intent** under Bot settings.
3. Invite the bot to the server containing the signal channels.

## 3. Configure environment (`fusion/.env`)

```ini
# --- Telegram source (Telethon user account) ---
SIGNAL_COPY_TG_API_ID=1234567
SIGNAL_COPY_TG_API_HASH=your_api_hash_here
SIGNAL_COPY_TG_SESSION=signal_copy_session
# Optional allowlist of chat ids (comma separated). Empty = all joined groups.
SIGNAL_COPY_TG_CHANNELS=-1001234567890,-1009876543210

# --- Telegram confirmation bot ---
SIGNAL_COPY_CONFIRM_BOT_TOKEN=123456:ABC-your-bot-token
SIGNAL_COPY_CONFIRM_CHAT_ID=578305627

# --- Discord (optional) ---
SIGNAL_COPY_DISCORD_ENABLED=1
SIGNAL_COPY_DISCORD_BOT_TOKEN=your_discord_bot_token
SIGNAL_COPY_DISCORD_CHANNELS=112233445566778899

# --- Behavior ---
SIGNAL_COPY_RISK_PCT=0.01            # 1% of equity per trade
SIGNAL_COPY_CONFIRM_EXPIRY_SEC=600   # confirmation prompt valid for 10 min
SIGNAL_COPY_AUTO_EXECUTE=0           # 1 = execute VALID signals WITHOUT asking (not recommended)
SIGNAL_COPY_DRY_RUN=0                # 1 = validate + confirm but never place orders
SIGNAL_COPY_NOTIFY_REJECTED=1
SIGNAL_COPY_NOTIFY_WEAK=1
SIGNAL_COPY_DEDUP_WINDOW_SEC=1800
```

## 4. Run

```bash
# safe first run: validates + asks confirmation but never places an order
python run_signal_copy.py --dry-run

# normal run (executes on Bybit/Binance TESTNET because config.TESTNET_MODE=True)
python run_signal_copy.py
```

To go live, set `TESTNET_MODE = False` in `config.py` and provide mainnet API
keys. **Test thoroughly on testnet first.**

## 5. How it behaves

1. A signal group posts a trade call. The listener forwards the text.
2. The parser extracts pair/side/entry/TP/SL/leverage.
3. The validator pulls live OI, CVD, funding, RSI, trend and scores confluence.
4. **REJECT** → you get a message explaining why (no trade).
   **WEAK** → you get a heads-up (no auto-trade).
   **VALID** → you get the full report + **[✅ Ya] [❌ Tidak]** buttons.
5. Tap **Ya** → the bot sizes the trade at 1% equity (respecting the signal's
   stop loss), opens it, and hands it to the engine which manages partial TPs at
   the signal's targets, trails the stop after TP1, and exits on invalidation.
6. Tap **Tidak** (or let it expire) → nothing happens.

## 6. Tuning validation

Edit `signal_copy/validation_config.py`:
- factor weights (`W_*`) to emphasize the signals you trust,
- `VALID_THRESHOLD` / `WEAK_THRESHOLD` to make it stricter/looser,
- hard blocks (`HARD_MAX_SL_DISTANCE_PCT`, `HARD_MIN_RR_RATIO`).

## 7. One-time Telegram login (REQUIRED before first run)

Telethon needs an interactive login the first time (phone number + the code
Telegram sends). This creates `signal_copy_session.session`. Do it once:

```bash
python -m signal_copy.list_channels --signals-only
```

After login the session file is reused, so the bot can run headless/daemonized.
The login MUST be done interactively by you (only you receive the code). If
running on a headless server, do the login in an SSH/tmux shell, or log in
locally and upload the generated `*.session` file to the server directory.

## 8. Two ways to run

**A. Standalone** (separate process, its own trader/data engine):
```bash
python run_signal_copy.py --dry-run   # safe test
python run_signal_copy.py             # executes on testnet
```

**B. Embedded in the main bot** (shares the SAME trader, risk manager, and
market-data engine — no duplicate connections, unified risk/positions):
```ini
# in .env
SIGNAL_COPY_ENABLED=1
```
Then start the main bot as usual (runpaper.py / runlive.py). signal_copy attaches
automatically during startup. Set `SIGNAL_COPY_ENABLED=0` to fully disable it
(the main bot is never affected when off).

## 9. The confirmation prompt

When a signal is VALID you receive (from your confirm bot):
- an **annotated chart image** (15m candles, MA9/MA34, entry zone, SL & TP lines,
  RSI panel, verdict + confluence factor checklist), and
- **[✅ Ya, eksekusi] [❌ Tidak]** buttons.

If chart rendering or kline fetch fails, it falls back to a full text report.
Press **Start** on your confirm bot once so it is allowed to message you.
