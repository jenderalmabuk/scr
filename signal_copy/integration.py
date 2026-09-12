"""
Integration hook to embed signal_copy inside the main fusion bot.

Call attach_signal_copy() from runtime.startup so the pipeline SHARES the live
trader, risk manager, and AdvancedDataEngine singleton — no duplicate exchange
connections, and signal-copy trades count against the same risk/position state.

Enable via env: SIGNAL_COPY_ENABLED=1 (default off, so the main bot is never
disturbed unless you opt in).
"""

from __future__ import annotations

import os
from typing import Any, Optional

from utils.logger import logger
from . import signal_copy_config as scfg
from .orchestrator import SignalCopyOrchestrator
from .telegram_confirm_bot import TelegramConfirmBot
from .calibration_bot import CalibrationBot
from .signal_schema import SignalSource
from .listeners.telegram_listener import TelegramSignalListener
from .listeners.discord_listener import DiscordSignalListener


def is_enabled() -> bool:
    v = os.getenv("SIGNAL_COPY_ENABLED", "").strip().lower()
    return v in {"1", "true", "yes", "y", "on"}


def attach_signal_copy(
    *,
    task_supervisor: Any,
    trader: Any,
    risk_mgr: Any,
    metrics_provider: Optional[Any] = None,
    notifier: Optional[Any] = None,
) -> Optional[SignalCopyOrchestrator]:
    """
    Wire signal_copy into the running bot and spawn its tasks on the supervisor.
    Returns the orchestrator (or None if disabled / nothing to run).
    """
    if not is_enabled():
        logger.info("[SIGNAL_COPY] disabled (set SIGNAL_COPY_ENABLED=1 to enable)")
        return None

    # Reuse the shared market-data engine unless one is provided.
    if metrics_provider is None:
        try:
            from data.advanced_data import adv_engine
            metrics_provider = adv_engine
        except Exception as exc:
            logger.warning("[SIGNAL_COPY] shared adv_engine unavailable: %s", exc)

    logger.info("[SIGNAL_COPY] attaching | %s", scfg.summary())

    orchestrator = SignalCopyOrchestrator(
        metrics_provider=metrics_provider,
        trader=trader,
        risk_mgr=risk_mgr,
        confirm_bot=None,
        notifier=notifier,
    )

    confirm_bot = None
    if scfg.CONFIRM_BOT_TOKEN and scfg.CONFIRM_CHAT_ID:
        confirm_bot = TelegramConfirmBot(
            bot_token=scfg.CONFIRM_BOT_TOKEN,
            chat_id=scfg.CONFIRM_CHAT_ID,
            confirmations=orchestrator.confirmations,
            on_decision=orchestrator.on_user_decision,
            on_test=orchestrator.inject_test_signal,
            status_provider=orchestrator.status_text,
        )
        orchestrator.confirm_bot = confirm_bot
        try:
            orchestrator.ignore_chat_ids.add(int(scfg.CONFIRM_BOT_TOKEN.split(":")[0]))
        except Exception:
            pass
        if notifier is None:
            orchestrator.notifier = confirm_bot.send_text
    else:
        logger.warning("[SIGNAL_COPY] confirm bot not configured (no token/chat id)")

    orchestrator.start_background()

    async def tg_cb(text, name, chat_id, image=None):
        await orchestrator.handle_incoming_text(text, name, chat_id, source=SignalSource.TELEGRAM, image=image)

    async def dc_cb(text, name, chat_id, image=None):
        await orchestrator.handle_incoming_text(text, name, chat_id, source=SignalSource.DISCORD, image=image)

    spawned = 0

    if (scfg.TG_API_ID and scfg.TG_API_HASH) or scfg.TG_LISTENER_BOT_TOKEN:
        tg_listener = TelegramSignalListener(
            on_message=tg_cb,
            api_id=scfg.TG_API_ID or None,
            api_hash=scfg.TG_API_HASH,
            session_name=scfg.TG_SESSION_NAME,
            bot_token=scfg.TG_LISTENER_BOT_TOKEN,
            channels=scfg.TG_SIGNAL_CHANNELS,
            channel_names=scfg.TG_CHANNEL_NAMES,
        )
        task_supervisor.spawn("signal_copy_tg_listener", tg_listener.start())
        spawned += 1
    else:
        logger.warning("[SIGNAL_COPY] no Telegram source configured")

    if scfg.DISCORD_ENABLED and scfg.discord_token():
        dc_listener = DiscordSignalListener(
            on_message=dc_cb,
            bot_token=scfg.discord_token(),
            channels=scfg.DISCORD_CHANNELS,
            channel_names=scfg.DISCORD_CHANNEL_NAMES,
            selfbot=scfg.DISCORD_SELFBOT,
            debug_guilds=scfg.DISCORD_GUILDS,
            poll_enabled=scfg.DISCORD_POLL_ENABLED,
            poll_interval=scfg.DISCORD_POLL_INTERVAL,
        )
        task_supervisor.spawn("signal_copy_discord_listener", dc_listener.start())
        spawned += 1

    if confirm_bot is not None:
        task_supervisor.spawn("signal_copy_confirm_bot", confirm_bot.start())
        spawned += 1

    if scfg.CALIB_BOT_TOKEN:
        async def _calib_analyze(text, name, image=None):
            return await orchestrator.read_only_report(
                text, name, image, source=SignalSource.TELEGRAM)
        calib_bot = CalibrationBot(scfg.CALIB_BOT_TOKEN, _calib_analyze)
        task_supervisor.spawn("signal_copy_calib_bot", calib_bot.start())
        spawned += 1

    logger.info("[SIGNAL_COPY] attached with %d task(s)", spawned)
    return orchestrator if spawned else None
