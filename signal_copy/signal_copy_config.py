"""
Minimal config for signal_copy pipeline in nexus.

Secrets are read from environment variables so nothing sensitive is hard-coded.
Set them in fusion/.env (already loaded by config.py via python-dotenv) or the
process environment.

Required to actually run:
- Telegram source reading (choose ONE):
    TELETHON path (reads ANY joined group):
        SIGNAL_COPY_TG_API_ID, SIGNAL_COPY_TG_API_HASH
    OR bot path (only chats where the bot is admin):
        SIGNAL_COPY_TG_LISTENER_BOT_TOKEN
- Parser/Validation notifications (VALID/WEAK/REJECT):
    SIGNAL_COPY_PARSER_NOTIFY_BOT_TOKEN, SIGNAL_COPY_PARSER_NOTIFY_CHAT_ID
- Execution/Trades notifications (entry, TP, SL, close):
    SIGNAL_COPY_TRADES_NOTIFY_BOT_TOKEN, SIGNAL_COPY_TRADES_NOTIFY_CHAT_ID
- Discord (optional):
    SIGNAL_COPY_DISCORD_BOT_TOKEN
"""

from __future__ import annotations

import os

try:
    from dotenv import load_dotenv
    load_dotenv()  # idempotent; ensures .env is available regardless of import order
except Exception:
    pass


def _int(name: str, default: int = 0) -> int:
    try:
        v = os.getenv(name, "").strip()
        return int(v) if v else default
    except Exception:
        return default


def _ids(name: str) -> list[int]:
    raw = os.getenv(name, "").strip()
    out = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            pass
    return out


def _bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name, "").strip().lower()
    if not v:
        return default
    return v in {"1", "true", "yes", "y", "on"}


# --- Telegram source (Telethon user-account backend, preferred) ---
TG_API_ID = _int("SIGNAL_COPY_TG_API_ID", 0)
TG_API_HASH = os.getenv("SIGNAL_COPY_TG_API_HASH", "").strip()
TG_SESSION_NAME = os.getenv("SIGNAL_COPY_TG_SESSION", "signal_copy_session").strip()

# --- Telegram source (aiogram bot backend, fallback) ---
TG_LISTENER_BOT_TOKEN = os.getenv("SIGNAL_COPY_TG_LISTENER_BOT_TOKEN", "").strip()

# Allowlist of Telegram chat ids to read signals from (empty = all joined).
TG_SIGNAL_CHANNELS = _ids("SIGNAL_COPY_TG_CHANNELS")
TG_CHANNEL_NAMES: dict[int, str] = {}  # optional id->name mapping for nicer labels

# --- Parser notifications channel (validation reports: VALID/WEAK/REJECT) ---
PARSER_NOTIFY_BOT_TOKEN = os.getenv("SIGNAL_COPY_PARSER_NOTIFY_BOT_TOKEN", "").strip()
PARSER_NOTIFY_CHAT_ID = _int("SIGNAL_COPY_PARSER_NOTIFY_CHAT_ID", 0)

# --- Execution/Trades notifications channel (entry, TP, SL, close) ---
TRADES_NOTIFY_BOT_TOKEN = os.getenv("SIGNAL_COPY_TRADES_NOTIFY_BOT_TOKEN", "").strip()
TRADES_NOTIFY_CHAT_ID = _int("SIGNAL_COPY_TRADES_NOTIFY_CHAT_ID", 0)

# --- Calibration sandbox (Tahap 1/2 testing; READ-ONLY, never executes) ---
# A separate Telegram bot you forward test signals to; it replies with exactly
# what the bot understood (classification + fields + chart-vision read).
CALIB_BOT_TOKEN = os.getenv("SIGNAL_COPY_CALIB_BOT_TOKEN", "").strip()
# Discord channel ids treated as read-only calibration intake (report only).
CALIB_DISCORD_CHANNELS = _ids("SIGNAL_COPY_CALIB_DISCORD_CHANNELS")

# --- Discord source ---
DISCORD_ENABLED = _bool("SIGNAL_COPY_DISCORD_ENABLED", False)
DISCORD_BOT_TOKEN = os.getenv("SIGNAL_COPY_DISCORD_BOT_TOKEN", "").strip()
# Self-bot (user account) mode: reads servers you are a member of. Requires
# discord.py-self and your USER token. NOTE: violates Discord ToS (ban risk).
DISCORD_SELFBOT = _bool("SIGNAL_COPY_DISCORD_SELFBOT", False)
DISCORD_USER_TOKEN = os.getenv("SIGNAL_COPY_DISCORD_USER_TOKEN", "").strip()
DISCORD_CHANNELS = _ids("SIGNAL_COPY_DISCORD_CHANNELS")
DISCORD_CHANNEL_NAMES: dict[int, str] = {}
# Server (guild) ids of interest — used to log the real channel/thread ids of
# incoming messages so the allowlist can be fixed for forum/thread channels,
# and to explicitly subscribe (discord.py-self lazy-guild workaround).
DISCORD_GUILDS = _ids("SIGNAL_COPY_DISCORD_GUILDS")
# REST polling fallback for watched channels (robust against gateway lazy-load).
# Empty -> defaults to ON in selfbot mode, OFF in bot mode.
_dp = os.getenv("SIGNAL_COPY_DISCORD_POLL", "").strip().lower()
DISCORD_POLL_ENABLED = None if not _dp else _dp in {"1", "true", "yes", "y", "on"}
DISCORD_POLL_INTERVAL = float(os.getenv("SIGNAL_COPY_DISCORD_POLL_INTERVAL", "25"))


def discord_token() -> str:
    """Active Discord token: user token in selfbot mode, else bot token."""
    return DISCORD_USER_TOKEN if DISCORD_SELFBOT else DISCORD_BOT_TOKEN


# --- Behavior ---
RISK_PCT = float(os.getenv("SIGNAL_COPY_RISK_PCT", "0.01"))   # 1% of equity per trade
CONFIRM_EXPIRY_SEC = float(os.getenv("SIGNAL_COPY_CONFIRM_EXPIRY_SEC", "3600"))  # 1h scalping limit expiry

# --- Per-channel pending-limit expiry profiles (Opsi A) ---
# Watcher (limit handling) stays global; only the HOLD duration varies by the
# source channel's trade style. Unmapped channels fall back to STANDARD.
EXPIRY_SCALP_SEC = float(os.getenv("SIGNAL_COPY_EXPIRY_SCALP", "2700"))        # 45m
EXPIRY_STANDARD_SEC = float(os.getenv("SIGNAL_COPY_EXPIRY_STANDARD", "10800")) # 3h
EXPIRY_SWING_SEC = float(os.getenv("SIGNAL_COPY_EXPIRY_SWING", "64800"))       # 18h
CHANNELS_SCALP = set(_ids("SIGNAL_COPY_CHANNELS_SCALP"))
CHANNELS_SWING = set(_ids("SIGNAL_COPY_CHANNELS_SWING"))


def expiry_for_channel(chat_id) -> float:
    """Resolve pending-limit expiry (seconds) for a source channel.

    scalp -> EXPIRY_SCALP_SEC, swing -> EXPIRY_SWING_SEC, else STANDARD.
    """
    try:
        cid = int(chat_id) if chat_id is not None else 0
    except (TypeError, ValueError):
        cid = 0
    if cid in CHANNELS_SCALP:
        return EXPIRY_SCALP_SEC
    if cid in CHANNELS_SWING:
        return EXPIRY_SWING_SEC
    return EXPIRY_STANDARD_SEC
AUTO_EXECUTE_WITHOUT_CONFIRM = _bool("SIGNAL_COPY_AUTO_EXECUTE", False)  # if True, skip yes/no
DRY_RUN = _bool("SIGNAL_COPY_DRY_RUN", False)   # validate + confirm but never place orders
NOTIFY_REJECTED = _bool("SIGNAL_COPY_NOTIFY_REJECTED", True)  # tell user about rejects too
NOTIFY_WEAK = _bool("SIGNAL_COPY_NOTIFY_WEAK", True)
# Tahap 1: push the detailed per-message "read report" to Telegram (calibration).
PARSE_REPORT = _bool("SIGNAL_COPY_PARSE_REPORT", False)
# Surface detected whale-accumulation narratives (no trading yet).
NOTIFY_ACCUM = _bool("SIGNAL_COPY_NOTIFY_ACCUM", False)

# Legacy validation mode (permissive, mirrors old bot behavior)
LEGACY_VALIDATION = _bool("SIGNAL_COPY_LEGACY_VALIDATION", False)

# Dedup window: ignore identical signals (same symbol+side+entry) within N seconds.
DEDUP_WINDOW_SEC = float(os.getenv("SIGNAL_COPY_DEDUP_WINDOW_SEC", "1800"))

# --- Vision (Tahap 2): read the chart/outlook image attached to a signal ---
VISION_ENABLED = _bool("SIGNAL_COPY_VISION_ENABLED", False)
# Per-channel vision routing: only these channels use the chart-vision path
# (build a signal from an image, or enrich a parsed signal with chart data).
# Empty set = legacy behavior (vision on ALL image-bearing channels when the
# global flag is on). Parser-only channels must be EXCLUDED here.
CHANNELS_VISION = set(_ids("SIGNAL_COPY_CHANNELS_VISION"))


def vision_enabled_for_channel(chat_id) -> bool:
    """Whether the chart-vision path is allowed for a given source channel.

    Global flag off -> always False. Global flag on + empty allowlist ->
    legacy (all channels). Global flag on + non-empty allowlist -> only listed.
    """
    if not VISION_ENABLED:
        return False
    if not CHANNELS_VISION:
        return True
    try:
        cid = int(chat_id) if chat_id is not None else 0
    except (TypeError, ValueError):
        cid = 0
    return cid in CHANNELS_VISION
# "openai" (OpenAI-compatible: local proxy / OpenRouter / Google Gemini openai-endpoint / OpenAI / similar)
# or "n8n" (webhook to your n8n flow).
VISION_BACKEND = os.getenv("SIGNAL_COPY_VISION_BACKEND", "openai").strip().lower()
VISION_TIMEOUT_SEC = float(os.getenv("SIGNAL_COPY_VISION_TIMEOUT_SEC", "300"))
N8N_WEBHOOK_URL = os.getenv("SIGNAL_COPY_N8N_WEBHOOK_URL", "").strip()
# OpenAI-compatible cloud/local backend.
# Default: local proxy at http://127.0.0.1:20128/v1 with models gc/gemini-2.5-pro or groq/openai/gpt-oss-120b
VISION_OPENAI_BASE_URL = os.getenv("SIGNAL_COPY_VISION_OPENAI_BASE_URL", "http://127.0.0.1:20128/v1").strip()
VISION_OPENAI_API_KEY = os.getenv("SIGNAL_COPY_VISION_OPENAI_API_KEY", "sk-0b1153f1a8ae386c-rvqg8m-99aa5f44").strip()
VISION_OPENAI_MODEL = os.getenv("SIGNAL_COPY_VISION_OPENAI_MODEL", "gc/gemini-2.5-pro").strip()

# --- Adversarial gate (Tahap 3): bull/bear debate before entry ---
ADVERSARIAL_ENABLED = _bool("SIGNAL_COPY_ADVERSARIAL_ENABLED", True)
# Gate mode: how the LLM bull/bear verdict affects a VALID signal.
#   "hard" = legacy: judge NO -> REJECT (blocks even high-conviction signals).
#   "soft" = judge NO downgrades to WEAK only if score < SOFT_FLOOR; high-score
#            signals ride through with the verdict attached as an advisory note.
#   "off"  = advisory only: never changes the verdict, just annotates the report.
# Advisory by default: LLM enriches report but never vetoes deterministic VALID.
ADVERSARIAL_MODE = os.getenv("SIGNAL_COPY_ADVERSARIAL_MODE", "off").strip().lower()
# Deterministic validation score at/above which the LLM can NEVER block.
ADVERSARIAL_SOFT_FLOOR = float(os.getenv("SIGNAL_COPY_ADVERSARIAL_SOFT_FLOOR", "90"))

# --- Entry style: regime-aware market vs pending-limit (chase control) ---
# Drift = how far current price sits from the signal entry, measured in R
# (units of entry->SL distance). Fresh: fill at MARKET now. Lagging: chase at
# MARKET only in trending regimes. Too far: hold as pending limit, wait pullback.
ENTRY_DRIFT_FRESH_R = float(os.getenv("SIGNAL_COPY_ENTRY_DRIFT_FRESH_R", "0.25"))
ENTRY_DRIFT_MAX_R = float(os.getenv("SIGNAL_COPY_ENTRY_DRIFT_MAX_R", "0.50"))
# Master switch for the drift-band pending-limit HOLD on MARKET-typed signals.
# Default OFF = original behavior: market signals fill immediately at market
# (no parking as a pending limit waiting for a pullback). Set
# SIGNAL_COPY_ENTRY_DRIFT_HOLD=true to re-enable the regime-aware chase/wait.
ENTRY_DRIFT_HOLD_ENABLED = _bool("SIGNAL_COPY_ENTRY_DRIFT_HOLD", False)
# Regimes (comma list, upper) where a lagging entry may still chase at market.
ENTRY_CHASE_REGIMES = {
    r.strip().upper()
    for r in os.getenv("SIGNAL_COPY_ENTRY_CHASE_REGIMES", "TRENDING,STRONG_TREND").split(",")
    if r.strip()
}
# When a pending limit fills on pullback, re-run validation; skip entry if the
# setup is no longer valid (price/flow moved against the thesis while waiting).
ENTRY_REVALIDATE_ON_FILL = _bool("SIGNAL_COPY_ENTRY_REVALIDATE_ON_FILL", True)
# Parse-but-don't-execute at max open positions. When True (default), a signal
# is still parsed, validated, and reported even when the book is full, but the
# EXECUTION is skipped with a clear notice (the signal is never dropped). The
# gateway also enforces MAX_OPEN_POS_GLOBAL as a hard backstop. Set
# SIGNAL_COPY_GATE_ON_MAX_POS=false to rely on the gateway backstop only.
GATE_EXEC_ON_MAX_POS = _bool("SIGNAL_COPY_GATE_ON_MAX_POS", True)

# --- VIP Fast Lane / Silent Accumulation (Tahap 4) ---
VIP_FAST_LANE_ENABLED = _bool("SIGNAL_COPY_VIP_FAST_LANE_ENABLED", False)

# --- SMC Engine (OB + FVG) ---
SMC_ENABLED = _bool("SIGNAL_COPY_SMC_ENABLED", False)


def summary() -> str:
    return (
        f"signal_copy config: "
        f"tg_telethon={'on' if (TG_API_ID and TG_API_HASH) else 'off'} "
        f"tg_bot={'on' if TG_LISTENER_BOT_TOKEN else 'off'} "
        f"parser_notify={'on' if PARSER_NOTIFY_BOT_TOKEN and PARSER_NOTIFY_CHAT_ID else 'off'} "
        f"trades_notify={'on' if TRADES_NOTIFY_BOT_TOKEN and TRADES_NOTIFY_CHAT_ID else 'off'} "
        f"discord={'on' if DISCORD_ENABLED and discord_token() else 'off'}"
        f"{'(selfbot)' if DISCORD_SELFBOT else ''} "
        f"risk={RISK_PCT*100:.2f}% dry_run={DRY_RUN} auto={AUTO_EXECUTE_WITHOUT_CONFIRM}"
    )