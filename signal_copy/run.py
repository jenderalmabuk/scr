#!/usr/bin/env python3
"""
Start the full signal_copy Telegram listener + orchestrator.

Environment (see signal_copy/signal_copy_config.py for details):
  SIGNAL_COPY_TG_API_ID / SIGNAL_COPY_TG_API_HASH  — Telethon user account (reads any joined group)
  SIGNAL_COPY_TG_LISTENER_BOT_TOKEN                — aiogram bot fallback (reads only admin chats)
  SIGNAL_COPY_TG_SESSION                           — Telethon session file name
  SIGNAL_COPY_TG_CHANNELS                          — comma-separated channel IDs (empty = all joined)

  SIGNAL_COPY_PARSER_NOTIFY_BOT_TOKEN              — parser validation reports bot
  SIGNAL_COPY_PARSER_NOTIFY_CHAT_ID                — parser channel chat ID
  SIGNAL_COPY_TRADES_NOTIFY_BOT_TOKEN              — trades execution bot
  SIGNAL_COPY_TRADES_NOTIFY_CHAT_ID                — trades channel chat ID

  SIGNAL_COPY_CONFIRM_BOT_TOKEN                    — confirmation bot (deprecated, kept for compat)
  SIGNAL_COPY_CONFIRM_CHAT_ID                      — confirmation channel

  SIGNAL_COPY_AUTO_EXECUTE                         — skip confirmation (true/false)
  SIGNAL_COPY_DRY_RUN                              — testnet mode (true/false)
  SIGNAL_COPY_LEGACY_VALIDATION                    — permissive thresholds (false recommended)
  SIGNAL_COPY_RISK_PCT                             — risk per trade (default 0.01 = 1%)
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx
import logging

# Load .env via signal_copy_config BEFORE any os.getenv() calls
from signal_copy import signal_copy_config  # noqa: F401

logger = logging.getLogger("fusion_whale_hunter")

# ---------------------------------------------------------------------------
# Bring nexus root onto sys.path early
# ---------------------------------------------------------------------------
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # signal_copy/ -> parent is nexus root
sys.path.insert(0, root)

# suppress noisy telethon logs
logging.getLogger("telethon").setLevel(logging.WARNING)


async def main():
    print("[SIGNAL COPY] Starting...")

    # ------------------------------------------------------------------
    # Metrics bridge — uses nexus FastAPI + Binance fallback
    # ------------------------------------------------------------------
    from nexus.data_bridge import NexusDataBridge
    bridge = NexusDataBridge()
    print("[SIGNAL COPY] Data bridge ready")

    # ------------------------------------------------------------------
    # Components
    # ------------------------------------------------------------------
    trader = None
    risk_mgr = None
    confirm_bot = None

    # Setup notifications: 2 bots (parser + trades)
    from signal_copy.telegram_transport import (
        send_parser_notification,
        send_trades_notification,
        start_telegram_workers,  # dummy
        stop_telegram_workers,   # dummy
    )

    # Start the notification workers
    await start_telegram_workers()

    # Live mode: Risk Manager + Trader (only if NOT dry_run)
    dry_run = os.getenv("SIGNAL_COPY_DRY_RUN", "true").lower() in ("1", "true", "yes")
    if not dry_run:
        # NEW: route through the Execution Gateway — the REAL RiskManager
        # lives inside the gateway so signals + M30/H1 share ONE portfolio.
        from gateway.adapters.signal_copy_adapter import GatewayTraderShim, RemoteRiskStub
        trader = GatewayTraderShim()
        risk_mgr = RemoteRiskStub(trader)
        await risk_mgr.refresh_equity()
        print("[SIGNAL COPY] LIVE mode: Execution Gateway active")
    else:
        from signal_copy.executor import SignalExecutor
        # Stub trader for dry_run
        class DryRunTrader:
            async def submit_open(self, **kw):
                return {"executed": True, "dry": True, "notional": 100.0}
        trader = DryRunTrader()
        print("[SIGNAL COPY] DRY_RUN mode (testnet only)")

    # ------------------------------------------------------------------
    # Orchestrator
    # ------------------------------------------------------------------
    auto_execute = os.getenv("SIGNAL_COPY_AUTO_EXECUTE", "false").lower() in ("1", "true", "yes")

    from signal_copy.orchestrator import SignalCopyOrchestrator
    orch = SignalCopyOrchestrator(
        metrics_provider=bridge,
        trader=trader,
        risk_mgr=risk_mgr,
        confirm_bot=confirm_bot,
        notifier=lambda text: send_parser_notification(text),
        dry_run=dry_run,
        auto_execute=auto_execute,
    )

    # Calibration channel: parse + notify, but NEVER auto-execute
    # FusionXomegabot (-1003988458515) is the test channel
    orch.calib_channels.add(-1003988458515)
    # Also add SMALL test channel if configured
    calib_env = os.getenv("SIGNAL_COPY_CALIBRATION_CHANNELS", "").strip()
    if calib_env:
        for cid in calib_env.split(","):
            try:
                orch.calib_channels.add(int(cid.strip()))
            except ValueError:
                pass

    # Override execution notification to use trades transport
    from signal_copy.telegram_formatter import build_execution_message, build_close_message

    # Store original _execute_token
    orig_execute_token = orch._execute_token

    async def patched_execute_token(signal_id: str) -> str:
        result = await orig_execute_token(signal_id)
        # Send execution result to trades channel
        try:
            # Get the signal and outcome from confirmations
            pc = await orch.confirmations.get(signal_id)
            if pc and pc.result:
                sig = pc.result.signal
                # outcome is not directly returned, but we can reconstruct
                # For now, just send a basic execution message
                from signal_copy.telegram_formatter import build_execution_message
                from signal_copy.validation_engine import ValidationResult
                # This will be handled by the orchestrator's built-in notification
                pass
        except Exception:
            pass
        return result

    orch._execute_token = patched_execute_token

    # Also patch the notification to send execution messages to trades channel
    orig_notify = orch._notify

    async def patched_notify(text: str) -> None:
        await orig_notify(text)
        # If this looks like an execution message, also send to trades channel
        if "EXECUTED" in text or "EXECUTION FAILED" in text or "CLOSE" in text:
            try:
                await send_trades_notification(text)
            except Exception:
                logger.exception("trades notification error")

    orch._notify = patched_notify

    print(f"[SIGNAL COPY] Orchestrator ready (dry_run={dry_run})")

    # ------------------------------------------------------------------
    # Pending Limits Polling — execute limit orders when price reaches zone
    # ------------------------------------------------------------------
    async def _poll_pending_limits():
        while True:
            await asyncio.sleep(15)  # poll every 15 seconds
            try:
                await orch.check_pending_limits()
            except Exception as e:
                logger.exception("Pending limits poll error: %s", e)

    # Start polling task
    asyncio.create_task(_poll_pending_limits())
    print("[SIGNAL COPY] Pending limits polling started (15s interval)")

    # ------------------------------------------------------------------
    # Outcome Resolution — virtual TP1/SL/expiry tracking for channel learning
    # Feeds ChannelPerformanceTracker so EVERY channel (executed or not) builds
    # a fair track record. Fire-and-forget execution never reports exits back,
    # so we resolve the channel's own entry/tp/sl call against live price.
    # ------------------------------------------------------------------
    async def _price_fn(symbol: str) -> float:
        try:
            m = await bridge.get_advanced_metrics(symbol)
            return float((m or {}).get("price") or 0.0)
        except Exception:
            return 0.0

    async def _poll_outcomes():
        from signal_copy.outcome_tracker import get_outcome_tracker
        tracker = get_outcome_tracker()
        while True:
            await asyncio.sleep(60)  # outcomes resolve over hours; 60s is plenty
            try:
                n = await tracker.resolve_open(_price_fn)
                if n:
                    logger.info("[OUTCOME] resolved %d signal(s); %d still open",
                                n, tracker.open_count())
            except Exception as e:
                logger.exception("Outcome poll error: %s", e)

    asyncio.create_task(_poll_outcomes())
    print("[SIGNAL COPY] Outcome resolution polling started (60s interval)")

    # ------------------------------------------------------------------
    # Telegram Listener — Telethon + aiogram fallback
    # ------------------------------------------------------------------
    listener_api_id = int(os.getenv("SIGNAL_COPY_TG_API_ID", os.getenv("TELEGRAM_API_ID", "0")))
    listener_api_hash = os.getenv("SIGNAL_COPY_TG_API_HASH", os.getenv("TELEGRAM_API_HASH", ""))
    listener_bot_token = os.getenv("SIGNAL_COPY_TG_LISTENER_BOT_TOKEN", "")
    listener_session = os.getenv("SIGNAL_COPY_TG_SESSION", "signal_copy_session")
    channels_env = os.getenv("SIGNAL_COPY_TG_CHANNELS", "").strip()
    channels = None
    if channels_env:
        channels = [int(c.strip()) for c in channels_env.split(",") if c.strip()]
    if channels:
        print(f"[SIGNAL COPY] Monitoring channels={channels}")
    else:
        print("[SIGNAL COPY] Starting Telegram listener (channels=ALL joined)...")

    from signal_copy.listeners.telegram_listener import TelegramSignalListener

    # Link the orchestrator's handle_incoming_text as the callback
    # Listener calls: on_message(text, name, chat_id, image)
    async def on_message(text, source_name, source_chat_id=None, image=None):
        try:
            await orch.handle_incoming_text(
                text=text,
                source_name=source_name,
                source_chat_id=source_chat_id,
                image=image,
            )
        except Exception:
            logger.exception("on_message error")

    tg_listener = TelegramSignalListener(
        on_message=on_message,
        api_id=listener_api_id,
        api_hash=listener_api_hash,
        session_name=listener_session,
        bot_token=listener_bot_token,
        channels=channels,
    )

    await tg_listener.start()

    # keep running
    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await tg_listener.stop()
        await stop_telegram_workers()
        print("[SIGNAL COPY] Stopped.")


if __name__ == "__main__":
    asyncio.run(main())