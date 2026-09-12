"""
List all Telegram chats your account can see, with their IDs.

Run this ONCE to discover which channels/groups send signals, then copy the IDs
you want into SIGNAL_COPY_TG_CHANNELS in .env (comma separated).

First run will ask for your phone number + the login code Telegram sends you.
This creates a <session>.session file so you won't need to log in again.

Usage:
    python -m signal_copy.list_channels
    python -m signal_copy.list_channels --signals-only   # only chats that look like signal sources
"""

from __future__ import annotations

import argparse
import asyncio

from signal_copy import signal_copy_config as scfg


async def amain(signals_only: bool, limit: int):
    try:
        from telethon import TelegramClient
        from telethon.tl.types import Channel, Chat, User
    except Exception:
        print("ERROR: telethon not installed. Run: pip install telethon")
        return

    if not scfg.TG_API_ID or not scfg.TG_API_HASH:
        print("ERROR: set SIGNAL_COPY_TG_API_ID and SIGNAL_COPY_TG_API_HASH in .env first.")
        return

    client = TelegramClient(scfg.TG_SESSION_NAME, scfg.TG_API_ID, scfg.TG_API_HASH)
    await client.start()  # prompts for phone + code on first run
    print("\nLogged in. Scanning your chats...\n")
    print(f"{'ID':>16}  {'TYPE':<10}  TITLE")
    print("-" * 70)

    rows = []
    async for dialog in client.iter_dialogs(limit=limit or None):
        ent = dialog.entity
        if isinstance(ent, User):
            ctype = "user"
        elif isinstance(ent, Channel):
            ctype = "channel" if ent.broadcast else "supergroup"
        elif isinstance(ent, Chat):
            ctype = "group"
        else:
            ctype = "other"

        title = dialog.name or ""
        if signals_only:
            low = title.lower()
            if not any(k in low for k in ("signal", "vip", "trade", "crypto", "futures",
                                          "call", "pump", "alert", "scalp", "trading")):
                continue
        rows.append((dialog.id, ctype, title))

    for cid, ctype, title in rows:
        print(f"{cid:>16}  {ctype:<10}  {title}")

    print("-" * 70)
    print(f"{len(rows)} chats listed.")
    print("\nNext: copy the IDs of your signal channels into .env, e.g.:")
    print("  SIGNAL_COPY_TG_CHANNELS=-1001234567890,-1009876543210\n")
    await client.disconnect()


def main():
    p = argparse.ArgumentParser(description="List Telegram chats with IDs")
    p.add_argument("--signals-only", action="store_true",
                   help="only show chats whose title hints at signals")
    p.add_argument("--limit", type=int, default=0, help="max chats to scan (0 = all)")
    args = p.parse_args()
    asyncio.run(amain(args.signals_only, args.limit))


if __name__ == "__main__":
    main()
