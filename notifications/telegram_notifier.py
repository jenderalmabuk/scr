"""Telegram trade notifications for gateway / testnet execution.

Sends OPEN + CLOSE (TP / SL / trailing / exit) notifications to the trades
Telegram bot. Used by execution/binance_testnet_trader.py's position-manager
loop (``_send_close_notification`` -> ``send_close_trade``).

Credentials come from env (same bot/chat as the signal_copy trades transport,
so entry + exit land in ONE channel):
    SIGNAL_COPY_TRADES_NOTIFY_BOT_TOKEN / SIGNAL_COPY_TRADES_NOTIFY_CHAT_ID
Falls back to TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.

Transport is the raw Telegram Bot API over HTTP (httpx if present, else a
stdlib urllib call in a worker thread) so it works whether or not
python-telegram-bot is installed in the host gateway process.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict

logger = logging.getLogger("fusion_nexus")


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _token() -> str:
    return (
        os.getenv("SIGNAL_COPY_TRADES_NOTIFY_BOT_TOKEN")
        or os.getenv("TELEGRAM_BOT_TOKEN")
        or ""
    )


def _chat_id() -> str:
    return (
        os.getenv("SIGNAL_COPY_TRADES_NOTIFY_CHAT_ID")
        or os.getenv("TELEGRAM_CHAT_ID")
        or ""
    )


async def _send(text: str) -> bool:
    """POST a message to the Telegram Bot API. Never raises."""
    token = _token()
    chat_id = _chat_id()
    if not token or not chat_id:
        logger.warning("[NOTIFY] telegram token/chat not set — skipping notification")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    # Preferred: httpx async (fastapi ecosystem usually has it)
    try:
        import httpx

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=body)
            if resp.status_code == 200:
                return True
            logger.error("[NOTIFY] telegram HTTP %s: %s", resp.status_code, resp.text[:200])
            return False
    except ImportError:
        pass  # fall through to urllib
    except Exception as exc:  # network / telegram error
        logger.error("[NOTIFY] httpx send failed: %s", exc)
        return False

    # Fallback: stdlib urllib in a worker thread (keeps the event loop free)
    def _blocking() -> bool:
        import json
        import urllib.request

        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 (trusted host)
            return resp.status == 200

    try:
        return await asyncio.to_thread(_blocking)
    except Exception as exc:
        logger.error("[NOTIFY] urllib send failed: %s", exc)
        return False


def _fmt_close(p: Dict[str, Any]) -> str:
    symbol = p.get("symbol", "")
    side = str(p.get("side", "")).upper()
    exit_price = _f(p.get("exit_price"))
    pnl_pct = _f(p.get("pnl_pct"))
    pnl_usd = _f(p.get("pnl_usd"))
    hold = _f(p.get("hold_minutes"))
    reason = p.get("normalized_reason") or p.get("reason") or "EXIT"
    balance = _f(p.get("balance_after") if p.get("balance_after") is not None else p.get("equity"))

    win = pnl_usd >= 0
    emoji = "🟢" if win else "🔴"
    sign = "+" if win else ""
    lines = [
        f"{emoji} <b>CLOSE {symbol} {side}</b>",
        f"Exit: <code>{exit_price:g}</code>",
        f"PnL: <b>{sign}{pnl_usd:.2f} USD</b> ({sign}{pnl_pct:.2f}%)",
        f"Hold: {hold:.0f}m | Reason: <b>{reason}</b>",
    ]
    if balance > 0:
        lines.append(f"Balance: <code>{balance:.2f} USD</code>")
    return "\n".join(lines)


def _fmt_open(p: Dict[str, Any]) -> str:
    symbol = p.get("symbol", "")
    side = str(p.get("side", "")).upper()
    entry = _f(p.get("entry_price") if p.get("entry_price") is not None else p.get("entry"))
    notional = _f(p.get("notional") or p.get("size_usd"))
    sl = _f(p.get("sl_price") or p.get("sl"))
    tp1 = _f(p.get("tp1"))
    emoji = "🟢" if side == "LONG" else "🔴"
    lines = [
        f"{emoji} <b>OPEN {symbol} {side}</b>",
        f"Entry: <code>{entry:g}</code>",
    ]
    if notional > 0:
        lines.append(f"Notional: <code>{notional:.2f} USD</code>")
    if sl > 0:
        lines.append(f"SL: <code>{sl:g}</code>")
    if tp1 > 0:
        lines.append(f"TP1: <code>{tp1:g}</code>")
    return "\n".join(lines)


async def send_close_trade(data: Dict[str, Any]) -> bool:
    """Send a rich CLOSE / TP / SL / exit notification to the trades channel."""
    try:
        return await _send(_fmt_close(data))
    except Exception as exc:  # never break the trader loop
        logger.error("[NOTIFY] send_close_trade failed: %s", exc)
        return False


async def send_open_trade(data: Dict[str, Any]) -> bool:
    """Send an OPEN notification (available for callers that want it)."""
    try:
        return await _send(_fmt_open(data))
    except Exception as exc:
        logger.error("[NOTIFY] send_open_trade failed: %s", exc)
        return False


__all__ = ["send_close_trade", "send_open_trade"]
