"""
Interactive Telegram confirmation bot for validated signals.

Posts the validation report with inline [✅ Ya] / [❌ Tidak] buttons (and also
accepts /ya_<id> /tidak_<id> commands). On a decision it invokes a callback
the orchestrator supplies, which runs execution.

Uses aiogram (already used by whales/telegram_whale_parser.py).
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from utils.logger import logger
from .confirmation import ConfirmationManager, ConfirmState
from .validation_engine import ValidationResult
from .report_formatter import build_validation_report, build_confirmation_prompt, build_chart_caption
from .chart_generator import build_chart

try:
    from aiogram import Bot, Dispatcher, F
    from aiogram.types import (
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        CallbackQuery,
        Message,
    )
    AIOGRAM_AVAILABLE = True
except Exception:  # pragma: no cover
    AIOGRAM_AVAILABLE = False


# decision callback: (token, approved) -> awaitable[str]  (returns a status text)
DecisionCallback = Callable[[str, bool], Awaitable[str]]


class TelegramConfirmBot:
    def __init__(
        self,
        bot_token: str,
        chat_id: int,
        confirmations: ConfirmationManager,
        on_decision: DecisionCallback,
        on_test: Optional[Callable[[], Awaitable[str]]] = None,
        status_provider: Optional[Callable[[], str]] = None,
    ):
        self.bot_token = (bot_token or "").strip()
        self.chat_id = int(chat_id) if chat_id else 0
        self.confirmations: Optional[ConfirmationManager] = confirmations
        self.on_decision: Optional[DecisionCallback] = on_decision
        self.on_test = on_test
        self.status_provider = status_provider
        self.bot: Optional["Bot"] = None
        self.dp: Optional["Dispatcher"] = None
        self.running = False
        self._poll_task: Optional[asyncio.Task] = None

    def _keyboard(self, token: str) -> "InlineKeyboardMarkup":
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Ya, eksekusi", callback_data=f"sc_yes:{token}"),
            InlineKeyboardButton(text="❌ Tidak", callback_data=f"sc_no:{token}"),
        ]])

    async def send_text(self, text: str) -> bool:
        """Send a plain HTML message (used as the orchestrator's notifier)."""
        if not AIOGRAM_AVAILABLE or self.bot is None or not self.chat_id:
            logger.info("[SIGNAL_CONFIRM_BOT][notify] %s", text.replace("\n", " | ")[:300])
            return False
        try:
            await self.bot.send_message(
                chat_id=self.chat_id, text=text,
                parse_mode="HTML", disable_web_page_preview=True,
            )
            return True
        except Exception as exc:
            logger.error("[SIGNAL_CONFIRM_BOT] send_text failed: %s", exc)
            return False

    async def prompt(self, result: ValidationResult) -> bool:
        """Send the analysis (chart photo if possible, else text) + confirm buttons."""
        if not AIOGRAM_AVAILABLE or self.bot is None or not self.chat_id:
            logger.warning("[SIGNAL_CONFIRM_BOT] cannot prompt (bot not ready)")
            return False

        kb = self._keyboard(result.signal.signal_id)

        # Try to attach an annotated chart image first.
        chart_path = None
        try:
            chart_path = await build_chart(result)
        except Exception as exc:
            logger.warning("[SIGNAL_CONFIRM_BOT] chart build failed: %s", exc)

        if chart_path:
            try:
                from aiogram.types import FSInputFile
                photo = FSInputFile(chart_path)
                await self.bot.send_photo(
                    chat_id=self.chat_id, photo=photo,
                    caption=build_chart_caption(result),
                    parse_mode="HTML", reply_markup=kb,
                )
                return True
            except Exception as exc:
                logger.warning("[SIGNAL_CONFIRM_BOT] send_photo failed, falling back to text: %s", exc)
            finally:
                try:
                    import os
                    os.remove(chart_path)
                except Exception:
                    pass

        text = build_validation_report(result) + build_confirmation_prompt(result)
        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=kb,
            )
            return True
        except Exception as exc:
            logger.error("[SIGNAL_CONFIRM_BOT] prompt send failed: %s", exc)
            return False

    async def _decide(self, token: str, approved: bool) -> str:
        pc = await self.confirmations.resolve(token, approved=approved)
        if pc is None:
            return "Sinyal tidak ditemukan (mungkin sudah kedaluwarsa)."
        if pc.state == ConfirmState.EXPIRED:
            return "⌛ Sinyal sudah kedaluwarsa, dibatalkan."
        if pc.state == ConfirmState.REJECTED:
            return "❌ Sinyal ditolak. Tidak ada posisi dibuka."
        if pc.state == ConfirmState.APPROVED:
            # hand off to orchestrator to execute
            try:
                return await self.on_decision(token, True)
            except Exception as exc:
                logger.exception("[SIGNAL_CONFIRM_BOT] decision callback error: %s", exc)
                return f"🔴 Error saat eksekusi: {exc}"
        return f"Status: {pc.state.value}"

    def _register(self) -> None:
        @self.dp.callback_query(F.data.startswith("sc_yes:"))
        async def _yes(cq: "CallbackQuery"):
            token = cq.data.split(":", 1)[1]
            await cq.answer("Memproses…")
            status = await self._decide(token, True)
            try:
                await cq.message.answer(status, parse_mode="HTML")
            except Exception:
                pass

        @self.dp.callback_query(F.data.startswith("sc_no:"))
        async def _no(cq: "CallbackQuery"):
            token = cq.data.split(":", 1)[1]
            await cq.answer("Dibatalkan")
            status = await self._decide(token, False)
            try:
                await cq.message.answer(status, parse_mode="HTML")
            except Exception:
                pass

        @self.dp.message(F.text.startswith("/ya_"))
        async def _cmd_yes(msg: "Message"):
            token = msg.text.split("/ya_", 1)[1].strip()
            status = await self._decide(token, True)
            await msg.answer(status, parse_mode="HTML")

        @self.dp.message(F.text.startswith("/tidak_"))
        async def _cmd_no(msg: "Message"):
            token = msg.text.split("/tidak_", 1)[1].strip()
            status = await self._decide(token, False)
            await msg.answer(status, parse_mode="HTML")

        @self.dp.message(F.text.startswith("/test"))
        async def _cmd_test(msg: "Message"):
            if self.on_test is None:
                await msg.answer("Fitur /test tidak tersedia.")
                return
            await msg.answer("🧪 Menjalankan tes sinyal end-to-end…")
            try:
                res = await self.on_test()
                await msg.answer(res or "Tes selesai.", parse_mode="HTML")
            except Exception as exc:
                await msg.answer(f"Tes gagal: {exc}")

        @self.dp.message(F.text.startswith("/status"))
        async def _cmd_status(msg: "Message"):
            if self.status_provider is None:
                await msg.answer("Status tidak tersedia.")
                return
            try:
                await msg.answer(self.status_provider(), parse_mode="HTML")
            except Exception as exc:
                await msg.answer(f"Gagal ambil status: {exc}")

        @self.dp.message(F.text.startswith("/start"))
        async def _cmd_start(msg: "Message"):
            await msg.answer(
                "👋 Bot Signal-Copy aktif.\n"
                "Perintah:\n"
                "• /test — uji satu sinyal contoh end-to-end\n"
                "• /status — lihat status, posisi, equity\n"
                "Sinyal VALID akan muncul otomatis dengan chart + tombol Ya/Tidak.",
                parse_mode="HTML",
            )

    async def start(self) -> None:
        if not AIOGRAM_AVAILABLE:
            logger.error("[SIGNAL_CONFIRM_BOT] aiogram not installed; confirm bot disabled")
            return
        if not self.bot_token:
            logger.error("[SIGNAL_CONFIRM_BOT] no bot token; confirm bot disabled")
            return
        if self.running:
            return
        self.running = True
        self.bot = Bot(token=self.bot_token)
        self.dp = Dispatcher()
        self._register()
        logger.info("[SIGNAL_CONFIRM_BOT] started (chat_id=%s)", self.chat_id)
        try:
            await self.dp.start_polling(self.bot, allowed_updates=["message", "callback_query"])
        except Exception as exc:
            logger.warning("[SIGNAL_CONFIRM_BOT] polling stopped: %s", exc)
        finally:
            self.running = False

    async def stop(self) -> None:
        self.running = False
        try:
            if self.dp:
                await self.dp.stop_polling()
        except Exception:
            pass
        try:
            if self.bot:
                await self.bot.session.close()
        except Exception:
            pass
