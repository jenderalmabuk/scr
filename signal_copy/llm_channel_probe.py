#!/usr/bin/env python3
"""Read one Telegram channel and compare LLM signal extraction vs regex parser.

Read-only experiment: no gateway, no execution, writes JSONL under journal/llm_channel_probe/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

import httpx

# Load .env once, same convention as signal_copy/run.py.
from signal_copy import signal_copy_config as scfg  # noqa: F401
from signal_copy.signal_parser import parse_signal

_PROMPT = """You extract crypto futures trade signals from Telegram messages.
Return ONLY compact JSON. No markdown.
Schema:
{
  "is_signal": boolean,
  "symbol": "BTCUSDT or null",
  "side": "LONG|SHORT|null",
  "entry_low": number|null,
  "entry_high": number|null,
  "stop_loss": number|null,
  "take_profits": [number],
  "leverage": number|null,
  "entry_type": "market|limit|null",
  "confidence": number,
  "reason": "short reason"
}
Rules:
- is_signal=true only for fresh actionable entries.
- Ignore updates, TP/SL hit, profit screenshots, ads, news, and discussion.
- Preserve numeric prices exactly; do not invent missing SL/TP.
- Normalize symbols to USDT pairs when quote is omitted.
"""


def _json_from_text(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        val = json.loads(text)
        return val if isinstance(val, dict) else None
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        val = json.loads(m.group(0))
        return val if isinstance(val, dict) else None
    except Exception:
        return None


async def llm_extract(text: str, *, base_url: str, api_key: str, model: str, timeout: float) -> dict[str, Any]:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _PROMPT},
            {"role": "user", "content": text[:3500]},
        ],
        "temperature": 0,
        "max_tokens": 700,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    url = base_url.rstrip("/") + "/chat/completions"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    parsed = _json_from_text(content)
    if not parsed:
        return {"is_signal": False, "error": "llm_json_parse_failed", "raw": content[:500]}
    return parsed


def regex_extract(text: str, source_name: str, chat_id: int) -> dict[str, Any] | None:
    sig = parse_signal(text, source_name=source_name, source_chat_id=chat_id)
    return sig.to_dict() if sig else None


async def handle_message(event: Any, *, args: argparse.Namespace, out_path: Path, sem: asyncio.Semaphore) -> None:
    text = event.raw_text or ""
    if not text.strip():
        return
    async with sem:
        msg_id = getattr(event.message, "id", None)
        ts = getattr(event.message, "date", None)
        ts_iso = ts.astimezone(UTC).isoformat() if ts else datetime.now(UTC).isoformat()
        regex = regex_extract(text, args.source_name, args.channel_id)
        try:
            llm = await llm_extract(
                text,
                base_url=args.base_url,
                api_key=args.api_key,
                model=args.model,
                timeout=args.timeout,
            )
            err = None
        except Exception as exc:
            llm = {"is_signal": False}
            err = str(exc)[:500]
        row = {
            "ts": ts_iso,
            "channel_id": args.channel_id,
            "source_name": args.source_name,
            "message_id": msg_id,
            "raw_text": text,
            "regex_signal": regex,
            "llm_signal": llm,
            "llm_error": err,
            "agree_is_signal": bool(regex) == bool(llm.get("is_signal")),
            "created_at": datetime.now(UTC).isoformat(),
        }
        with out_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        print(
            f"[{ts_iso}] msg={msg_id} regex={bool(regex)} llm={bool(llm.get('is_signal'))} "
            f"sym={llm.get('symbol')} side={llm.get('side')} err={err or '-'}",
            flush=True,
        )


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--channel-id", type=int, default=-1001652601224)
    p.add_argument("--source-name", default="llm_probe_-1001652601224")
    p.add_argument("--history", type=int, default=20, help="fetch last N messages then exit; 0 = live listen")
    p.add_argument("--out-dir", default="journal/llm_channel_probe")
    p.add_argument("--model", default=os.getenv("ADVERSARIAL_JUDGE_MODEL") or os.getenv("ADVERSARIAL_MODEL") or "Free-Tiers")
    base_default = os.getenv("NINE_ROUTER_BASE") or os.getenv("SIGNAL_COPY_VISION_OPENAI_BASE_URL") or "http://127.0.0.1:20128/v1"
    # .env is shared with Docker; host.docker.internal works inside containers,
    # but this probe runs on the host by default.
    base_default = base_default.replace("host.docker.internal", "127.0.0.1")
    p.add_argument("--base-url", default=base_default)
    p.add_argument("--api-key", default=os.getenv("NINE_ROUTER_API_KEY") or os.getenv("SIGNAL_COPY_VISION_OPENAI_API_KEY") or "x")
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--concurrency", type=int, default=1)
    args = p.parse_args()

    try:
        from telethon import TelegramClient, events
    except Exception as exc:
        print(f"telethon_missing: {exc}", file=sys.stderr)
        return 2

    api_id = int(os.getenv("SIGNAL_COPY_TG_API_ID") or os.getenv("TELEGRAM_API_ID") or "0")
    api_hash = os.getenv("SIGNAL_COPY_TG_API_HASH") or os.getenv("TELEGRAM_API_HASH") or ""
    session = os.getenv("SIGNAL_COPY_TG_SESSION", "signal_copy_session")
    if not api_id or not api_hash:
        print("missing Telegram API creds", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "history" if args.history else "live"
    out_path = out_dir / f"{args.channel_id}_{suffix}.jsonl"
    sem = asyncio.Semaphore(max(1, args.concurrency))

    client = TelegramClient(session, api_id, api_hash)
    await client.start()
    print(f"LLM probe started channel={args.channel_id} history={args.history} out={out_path}")

    if args.history > 0:
        msgs = await client.get_messages(args.channel_id, limit=args.history)
        for ev_msg in reversed(msgs):
            class E:
                message = ev_msg
                raw_text = ev_msg.raw_text or ""
            await handle_message(E(), args=args, out_path=out_path, sem=sem)
        await client.disconnect()
        return 0

    @client.on(events.NewMessage(chats=[args.channel_id]))
    async def _handler(event: Any) -> None:
        await handle_message(event, args=args, out_path=out_path, sem=sem)

    await client.run_until_disconnected()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
