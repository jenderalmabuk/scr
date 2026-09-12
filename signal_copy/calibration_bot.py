"""
Calibration sandbox bot (separate Telegram bot, READ-ONLY).

Forward or paste a signal (text, or image + caption) to this bot and it replies
with exactly what the pipeline understood: classification, parsed fields, and —
if an image is attached and vision is enabled — the chart-vision read. It never
executes trades; it is purely for calibrating the parser/classifier/vision.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from utils.logger import logger

try:
    from aiogram import Bot, Dispatcher, F
    from aiogram.types import Message
    AIOGRAM_AVAILABLE = True
except Exception:  # pragma: no cover
    AIOGRAM_AVAILABLE = False

# analyzer(text, source_name, image_bytes|None) -> awaitable[str (HTML report)]
Analyzer = Callable[..., Awaitable[str]]


class CalibrationBot:
    def __init__(self, bot_token: str, analyzer: Analyzer):
        self.bot_token = (bot_token or "").strip()
        self.analyzer = analyzer
        self.bot: Optional["Bot"] = None
        self.dp: Optional["Dispatcher"] = None
        self.running = False

    async def _download_photo(self, msg) -> Optional[bytes]:
        if not getattr(msg, "photo", None):
            return None
        try:
            ph = msg.photo[-1]  # largest size
            f = await self.bot.get_file(ph.file_id)
            buf = await self.bot.download_file(f.file_path)
            data = buf.read() if hasattr(buf, "read") else bytes(buf)
            return data or None
        except Exception as exc:
            logger.warning("[CALIB_BOT] photo download failed: %s", exc)
            return None

    def _register(self) -> None:
        @self.dp.message(F.text.startswith("/start"))
        async def _start(msg: "Message"):
            await msg.answer(
                "🧪 <b>Sandbox kalibrasi aktif</b>.\n"
                "Forward / tempel pesan sinyal ke sini (teks, atau gambar chart + caption).\n"
                "Saya balas APA yang dibaca bot: klasifikasi, field (pair/arah/entry/SL/TP/"
                "timeframe), dan hasil baca chart bila ada gambar.\n"
                "<i>READ-ONLY — tidak membuka posisi.</i>",
                parse_mode="HTML",
            )

        @self.dp.message()
        async def _any(msg: "Message"):
            try:
                text = msg.text or msg.caption or ""
                image = await self._download_photo(msg)
                if not text.strip() and image is None:
                    await msg.answer("Kirim teks sinyal atau gambar chart (boleh dengan caption).")
                    return
                if image is not None:
                    from signal_copy import signal_copy_config as scfg
                    est = "5-10 detik" if getattr(scfg, "VISION_BACKEND", "ollama") == "openai" else "20-60 detik"
                    await msg.answer(f"🔎 Membaca chart… (~{est})")
                rep = await self.analyzer(text, "calib:telegram", image)
                await msg.answer(rep, parse_mode="HTML")
            except Exception as exc:
                logger.exception("[CALIB_BOT] handler error: %s", exc)
                try:
                    await msg.answer(f"Error: {exc}")
                except Exception:
                    pass

    async def start(self) -> None:
        if not AIOGRAM_AVAILABLE:
            logger.error("[CALIB_BOT] aiogram not installed; calibration bot disabled")
            return
        if not self.bot_token:
            logger.info("[CALIB_BOT] no token; calibration bot disabled")
            return
        if self.running:
            return
        self.running = True
        self.bot = Bot(token=self.bot_token)
        self.dp = Dispatcher()
        self._register()
        logger.info("[CALIB_BOT] started (calibration sandbox)")
        try:
            await self.dp.start_polling(self.bot, allowed_updates=["message"])
        except Exception as exc:
            logger.warning("[CALIB_BOT] polling stopped: %s", exc)
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
