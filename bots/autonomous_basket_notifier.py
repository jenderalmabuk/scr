"""Persistent, deduplicating Telegram notifications for autonomous basket events."""

from __future__ import annotations

import html
import json
import os
import tempfile
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

Sender = Callable[[str, str, str, int], Any]


def _value(value: Any) -> str:
    return str(value)


def _fields(title: str, values: Sequence[tuple[str, Any]]) -> str:
    lines = [title]
    lines.extend(f"{label}: {_value(value)}" for label, value in values)
    return "\n".join(lines)


def format_setups(setups: Sequence[Mapping[str, Any]]) -> str:
    """Format at most five setup candidates."""
    blocks = []
    for setup in setups[:5]:
        blocks.append(_fields("Setup", (
            ("symbol", setup["symbol"]), ("side", setup["side"]),
            ("zone", setup["zone"]), ("trigger", setup["trigger"]),
            ("SL", setup["sl"]), ("TP", setup["tp"]),
            ("expiry", setup["expiry"]),
        )))
    return "\n\n".join(blocks)


def format_setup(setups: Sequence[Mapping[str, Any]]) -> str:
    return format_setups(setups)


def format_rejection(symbol: Any, code: Any, message: Any) -> str:
    return _fields("Rejection", (("symbol", symbol), ("code", code), ("message", message)))


def format_entry(symbol: Any, side: Any, entry: Any, qty: Any, sl: Any, tp: Any,
                 rr: Any, order_link_id: Any, tradingview: Any) -> str:
    return _fields("Entry / Fill", (
        ("symbol", symbol), ("side", side), ("entry", entry), ("qty", qty),
        ("SL", sl), ("TP", tp), ("RR", rr), ("orderLinkId", order_link_id),
        ("TradingView", tradingview),
    ))


def format_fill(*args: Any, **kwargs: Any) -> str:
    return format_entry(*args, **kwargs)


def format_close(reason: Any, pnl: Any, price: Any) -> str:
    return _fields("Close", (("reason", reason), ("PnL", pnl), ("price", price)))


def format_degraded(code: Any, message: Any) -> str:
    return _fields("Autonomous Basket Degraded", (("code", code), ("message", message)))


def _send(token: str, chat_id: str, text: str, timeout: int) -> bool:
    body = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=body, method="POST"
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read())
    return response.status == 200 and payload.get("ok") is True


class TelegramNotifier:
    """Send once, then atomically persist delivered event IDs."""

    def __init__(self, state_dir: os.PathLike[str] | str, token: str | None = None,
                 chat_id: str | None = None, sender: Sender | None = None):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "notifications.json"
        self.token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID")
        self.sender = sender or _send
        self._lock = threading.Lock()
        self._delivered = self._load()

    def _load(self) -> set[str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return {str(item) for item in data} if isinstance(data, list) else set()
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return set()

    def _journal(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".notifications.", dir=self.state_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(sorted(self._delivered), stream, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.state_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def emit(self, event_id: Any, text: str | Mapping[str, Any]) -> bool:
        event_id = str(event_id)
        if not event_id:
            return False
        if isinstance(text, Mapping):
            text = _fields("Event", tuple((str(key), value) for key, value in text.items()))
        safe_text = html.escape(str(text), quote=True)
        with self._lock:
            if event_id in self._delivered:
                return True
            if not self.token or not self.chat_id:
                return False
            try:
                delivered = self.sender(self.token, self.chat_id, safe_text, 15)
            except Exception:
                return False
            if delivered is False:
                return False
            self._delivered.add(event_id)
            try:
                self._journal()
            except OSError:
                self._delivered.discard(event_id)
                return False
            return True
