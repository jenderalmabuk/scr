#!/usr/bin/env python3

from __future__ import annotations

import asyncio
import os
import re
from typing import Optional

from utils.logger import logger

try:
    from telegram import Bot
    from telegram.error import TelegramError
    BOT_AVAILABLE = True
except ImportError:  # pragma: no cover
    BOT_AVAILABLE = False
    logger.warning("⚠️ python-telegram-bot tidak terinstall. Install: pip install python-telegram-bot")

# Config from environment
PARSER_BOT_TOKEN = os.getenv("SIGNAL_COPY_PARSER_NOTIFY_BOT_TOKEN", "")
PARSER_CHAT_ID = int(os.getenv("SIGNAL_COPY_PARSER_NOTIFY_CHAT_ID", "0"))
TRADES_BOT_TOKEN = os.getenv("SIGNAL_COPY_TRADES_NOTIFY_BOT_TOKEN", "")
TRADES_CHAT_ID = int(os.getenv("SIGNAL_COPY_TRADES_NOTIFY_CHAT_ID", "0"))

_parser_bot: Optional[Bot] = None
_trades_bot: Optional[Bot] = None


def _plain_text(message: str) -> str:
    """Strip only Telegram HTML tags; preserve malformed provider text literally."""
    return re.sub(r"</?(?:a|b|blockquote|code|em|i|pre|s|span|strong|tg-spoiler|u)(?:\s+[^>]*)?>", "", message, flags=re.I)


async def _send_html_then_plain(bot, chat_id: int, message: str) -> bool:
    """Keep expandable HTML when valid; retry malformed provider content as plain text."""
    try:
        await bot.send_message(chat_id=chat_id, text=message, parse_mode="HTML",
                               disable_web_page_preview=True)
        return True
    except Exception as exc:
        logger.warning("⚠️ Telegram HTML send failed, fallback to plain text: %s", exc)
        await bot.send_message(chat_id=chat_id, text=_plain_text(message), parse_mode=None,
                               disable_web_page_preview=True)
        return True


async def _send_with_retry(send_call, attempts: int = 3) -> bool:
    """Retry notification transport only; never retries trade execution."""
    attempts = max(1, int(attempts))
    for attempt in range(1, attempts + 1):
        try:
            await send_call()
            return True
        except Exception as exc:
            if attempt >= attempts:
                logger.error("❌ Telegram send failed after %d attempts: %s", attempts, exc)
                return False
            logger.warning("⚠️ Telegram send attempt %d/%d failed: %s", attempt, attempts, exc)
            await asyncio.sleep(min(2 ** (attempt - 1), 4))
    return False


async def _ensure_bot_ready():
    global _parser_bot, _trades_bot
    if _parser_bot is None and PARSER_BOT_TOKEN:
        _parser_bot = Bot(PARSER_BOT_TOKEN)
        await _parser_bot.initialize()
        me = await _parser_bot.get_me()
        logger.info(f"✅ Parser bot ready: @{me.username} (id={me.id})")
    if _trades_bot is None and TRADES_BOT_TOKEN:
        try:
            _trades_bot = Bot(TRADES_BOT_TOKEN)
            await _trades_bot.initialize()
            me = await _trades_bot.get_me()
            logger.info(f"✅ Trades bot ready: @{me.username} (id={me.id})")
        except TelegramError:
            logger.warning("⚠️ Trades bot token invalid, using parser bot fallback")
            _trades_bot = None


async def send_parser_notification(message: str, chart_path: Optional[str] = None) -> bool:
    """Send validation report via parser bot."""
    if not BOT_AVAILABLE or not PARSER_BOT_TOKEN:
        logger.warning("⚠️ Parser bot not configured")
        return False
    await _ensure_bot_ready()
    try:
        if chart_path:
            try:
                from telegram import InputFile
                with open(chart_path, "rb") as fh:
                    long_report = len(message) > 1024
                    await _parser_bot.send_photo(
                        chat_id=PARSER_CHAT_ID,
                        photo=InputFile(fh, filename=chart_path.rsplit("/", 1)[-1]),
                        caption="📊 Chart validasi" if long_report else message,
                        parse_mode="HTML",
                    )
                    if long_report:
                        return await _send_html_then_plain(_parser_bot, PARSER_CHAT_ID, message)
                    return True
            except Exception as exc:
                logger.warning(f"⚠️ Parser chart send failed, fallback to text: {exc}")
        async def _send_text():
            if not await _send_html_then_plain(_parser_bot, PARSER_CHAT_ID, message):
                raise RuntimeError("Telegram parser notification failed")
        return await _send_with_retry(_send_text)
    except Exception as exc:
        logger.error(f"❌ Parser bot send failed: {exc}")
        return False


async def send_trades_notification(message: str) -> bool:
    """Send trades execution message via trades bot, or fallback to parser bot."""
    if not BOT_AVAILABLE:
        logger.warning("⚠️ Telethon/bots not available")
        return False
    await _ensure_bot_ready()

    target_bot = _trades_bot or _parser_bot
    if not target_bot:
        logger.warning("⚠️ No bot available")
        return False

    wrapped = "🔄 [TRADES] " + message
    try:
        async def _send_text():
            await target_bot.send_message(
            chat_id=TRADES_CHAT_ID if target_bot is _trades_bot else PARSER_CHAT_ID,
            text=wrapped,
            parse_mode="HTML",
            disable_web_page_preview=True,
            )
        return await _send_with_retry(_send_text)
    except Exception as exc:
        logger.error(f"❌ Trades bot send failed: {exc}")
        return False


# Async dummies to maintain compat
async def start_telegram_workers():
    """Start the drivers (noop when using direct Bot(token))."""
    await _ensure_bot_ready()  # Ensure bots ready on start


async def stop_telegram_workers():
    """Graceful shutdown (noop for this driver)."""
    pass


__all__ = ["send_parser_notification", "send_trades_notification", "start_telegram_workers", "stop_telegram_workers"]