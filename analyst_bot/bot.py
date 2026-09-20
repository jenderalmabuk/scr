"""Minimal Telegram long-poller for read-only Nexus analyst (stdlib only)."""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request

from analyst_bot.analyzer import normalize_symbol
from analyst_bot.pro_report import report as pro_report
from analyst_bot.ai_analysis import ai_report

TOKEN = os.environ.get("ANALYST_TELEGRAM_BOT_TOKEN", "")
ALLOWED = {int(x) for x in os.environ.get("ANALYST_TELEGRAM_ALLOWED_CHAT_IDS", "").split(",") if x.strip()}
BASE = f"https://api.telegram.org/bot{TOKEN}"


def api(method: str, data: dict | None = None) -> dict:
    encoded = urllib.parse.urlencode(data or {}).encode()
    with urllib.request.urlopen(f"{BASE}/{method}", data=encoded, timeout=40) as response:
        return json.load(response)


def send(chat_id: int, text: str) -> None:
    api("sendMessage", {"chat_id": chat_id, "text": text[:4096]})


def reply(chat_id: int, raw: str, ai: bool = False) -> None:
    try:
        symbol = normalize_symbol(raw)
        text = ai_report(symbol) if ai else pro_report(symbol)
    except Exception as exc:
        text = f"NO TRADE\nAnalisis gagal: {exc}"
    send(chat_id, text)


def main() -> None:
    if not TOKEN:
        raise RuntimeError("ANALYST_TELEGRAM_BOT_TOKEN wajib diisi")
    offset = 0
    while True:
        try:
            result = api("getUpdates", {"offset": offset, "timeout": 30, "allowed_updates": '["message"]'})
            for update in result.get("result", []):
                offset = max(offset, int(update["update_id"]) + 1)
                message = update.get("message") or {}
                chat_id = int((message.get("chat") or {}).get("id", 0))
                text = str(message.get("text") or "").strip()
                if not chat_id or (ALLOWED and chat_id not in ALLOWED):
                    continue
                if text.startswith("/start"):
                    send(chat_id, "Kirim /analyze BTCUSDT atau langsung BTCUSDT. Bot read-only.")
                elif text.startswith("/analyze"):
                    reply(chat_id, text.partition(" ")[2])
                elif text.startswith("/ai"):
                    reply(chat_id, text.partition(" ")[2], ai=True)
                elif text and not text.startswith("/"):
                    reply(chat_id, text)
        except Exception as exc:
            print(f"poll error: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(3)


if __name__ == "__main__":
    main()
