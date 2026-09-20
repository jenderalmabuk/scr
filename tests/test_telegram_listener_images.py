import asyncio
import sys
import types

from signal_copy.listeners.telegram_listener import TelegramSignalListener


class FakeMessage:
    photo = True
    reply_to_msg_id = None

    async def download_media(self, file=bytes):
        return b"image-bytes"


class FakeEvent:
    chat_id = 123
    raw_text = ""
    message = FakeMessage()


class FakeEvents:
    @staticmethod
    def NewMessage():
        return object()


class FakeTelegramClient:
    def __init__(self, *args, **kwargs):
        self.handlers = []

    def on(self, event):
        def decorator(func):
            self.handlers.append(func)
            return func
        return decorator

    async def start(self):
        return None

    async def run_until_disconnected(self):
        await self.handlers[0](FakeEvent())
        return None


def test_telethon_image_only_message_reaches_callback(monkeypatch):
    fake_module = types.SimpleNamespace(TelegramClient=FakeTelegramClient, events=FakeEvents)
    monkeypatch.setitem(sys.modules, "telethon", fake_module)

    received = []

    async def on_message(text, source_name, source_chat_id=None, image=None):
        received.append((text, source_name, source_chat_id, image))

    listener = TelegramSignalListener(
        on_message=on_message,
        api_id=1,
        api_hash="hash",
        channels=[123],
    )
    ok = asyncio.run(listener._start_telethon())

    assert ok is True
    assert received == [("", "123", 123, b"image-bytes")]
