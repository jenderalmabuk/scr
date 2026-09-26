"""Compression Breakout Re-Entry Engine.

Monitors trades that were closed early by de-risking mechanisms (DAMAGE_REDUCER,
SCRATCH_EXIT, etc.). If the token forms a compression/accumulation structure and
then executes a confirmed breakout accompanied by volume/OI surge while the original
invalidation level remains intact, automatically re-enters the trade with a tight
support-based Stop Loss.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("fusion_nexus")

WATCHLIST_FILE = "/app/journal/compression_reentry_watchlist.json"


class CompressionReentryEngine:
    def __init__(self, trader: Any):
        self.trader = trader
        self.watchlist_file = os.getenv("COMPRESSION_WATCHLIST_FILE", WATCHLIST_FILE)
        self.watch_window_hours = float(os.getenv("COMPRESSION_REENTRY_WINDOW_HOURS", "24.0"))
        self.max_compression_range_pct = float(os.getenv("COMPRESSION_MAX_RANGE_PCT", "3.5"))
        self.volume_surge_mult = float(os.getenv("COMPRESSION_VOLUME_SURGE_MULT", "1.25"))
        self.enabled = str(os.getenv("COMPRESSION_REENTRY_ENABLED", "true")).lower() in {"1", "true", "yes"}
        self.watchlist: Dict[str, Dict[str, Any]] = {}
        self._load_watchlist()

    def _load_watchlist(self) -> None:
        """Load persistent watchlist from disk."""
        try:
            if os.path.exists(self.watchlist_file):
                with open(self.watchlist_file, "r") as f:
                    self.watchlist = json.load(f)
                logger.info(f"[REENTRY_ENGINE] Loaded {len(self.watchlist)} candidates from {self.watchlist_file}")
        except Exception as e:
            logger.warning(f"[REENTRY_ENGINE] Failed to load watchlist: {e}")
            self.watchlist = {}

    def _save_watchlist(self) -> None:
        """Save active watchlist to disk."""
        try:
            os.makedirs(os.path.dirname(self.watchlist_file), exist_ok=True)
            with open(self.watchlist_file, "w") as f:
                json.dump(self.watchlist, f, indent=2)
        except Exception as e:
            logger.warning(f"[REENTRY_ENGINE] Failed to save watchlist: {e}")

    async def register_candidate(
        self,
        symbol: str,
        side: str,
        exit_price: float,
        original_entry: float,
        original_sl: float,
        original_qty: float,
        original_tps: List[float],
        metadata: Dict[str, Any],
        reason: str,
    ) -> None:
        """Register a cut trade into the re-entry watch pool."""
        if not self.enabled:
            return

        now_ts = time.time()
        expiry_ts = now_ts + (self.watch_window_hours * 3600.0)

        # Fallback conservative SL if missing
        if not original_sl or original_sl <= 0:
            original_sl = original_entry * 0.95 if side.upper() == "LONG" else original_entry * 1.05

        candidate = {
            "symbol": symbol,
            "side": side.upper(),
            "exit_price": float(exit_price),
            "original_entry": float(original_entry),
            "original_sl": float(original_sl),
            "original_qty": float(original_qty),
            "original_tps": [float(tp) for tp in original_tps if float(tp) > 0],
            "metadata": metadata or {},
            "exit_reason": reason,
            "registered_at": now_ts,
            "expiry_at": expiry_ts,
            "reentry_attempted": False,
        }

        self.watchlist[symbol] = candidate
        self._save_watchlist()
        logger.info(
            f"[REENTRY_WATCH] Registered {symbol} ({side}) for Compression Breakout Re-Entry | "
            f"Exit: {exit_price:.5f} | Orig SL: {original_sl:.5f} | Window: {self.watch_window_hours:.0f}h"
        )

    async def check_reentries(self) -> None:
        """Evaluate active watchlist against current market compression and breakout."""
        if not self.enabled or not self.watchlist:
            return

        now_ts = time.time()
        symbols_to_remove = []

        for symbol, item in list(self.watchlist.items()):
            # 1. Check expiration
            if now_ts > item.get("expiry_at", 0):
                logger.info(f"[REENTRY_EXPIRED] {symbol} watch window expired ({self.watch_window_hours:.0f}h)")
                symbols_to_remove.append(symbol)
                continue

            # 2. Check if already open
            if symbol in self.trader.positions:
                symbols_to_remove.append(symbol)
                continue

            # 3. Fetch fresh quote
            quote = await self.trader._fetch_fresh_quote(symbol)
            if not quote:
                continue

            mark = float(quote.get("mark") or 0.0)
            if mark <= 0:
                continue

            side = item["side"]
            is_long = side == "LONG"
            orig_sl = float(item["original_sl"])

            # 4. Invalidation: Did price violate original SL?
            if is_long and mark <= orig_sl:
                logger.info(f"[REENTRY_DROP] {symbol} violated original SL ({mark:.5f} <= {orig_sl:.5f})")
                symbols_to_remove.append(symbol)
                continue
            elif not is_long and mark >= orig_sl:
                logger.info(f"[REENTRY_DROP] {symbol} violated original SL ({mark:.5f} >= {orig_sl:.5f})")
                symbols_to_remove.append(symbol)
                continue

            # 5. Fetch 15m closed klines to detect compression zone & volume baseline
            try:
                klines = await asyncio.to_thread(
                    self.trader.client.get_klines,
                    symbol=symbol,
                    interval="15",
                    limit=10,
                )
            except Exception as e:
                logger.debug(f"[REENTRY_ENGINE] Could not fetch klines for {symbol}: {e}")
                continue

            if not klines or len(klines) < 4:
                continue

            # klines[0] is active forming candle; klines[1:7] are completed 15m candles
            closed_klines = klines[1:7]
            highs = [float(k[2]) for k in closed_klines if len(k) > 2]
            lows = [float(k[3]) for k in closed_klines if len(k) > 3]
            volumes = [float(k[5]) for k in closed_klines if len(k) > 5]

            if not highs or not lows:
                continue

            p_high = max(highs)
            p_low = min(lows)
            range_pct = (p_high - p_low) / p_low * 100.0 if p_low > 0 else 999.0

            # Active candle metrics
            curr_candle = klines[0]
            curr_open = float(curr_candle[1])
            curr_vol = float(curr_candle[5])
            base_vol = (sum(volumes) / len(volumes)) if volumes else 1.0

            # Breakout condition
            breakout_confirmed = False
            if is_long:
                breakout_confirmed = (mark >= p_high * 1.001) and (mark > curr_open)
            else:
                breakout_confirmed = (mark <= p_low * 0.999) and (mark < curr_open)

            # Volume / Momentum boost
            volume_boost = curr_vol >= base_vol * self.volume_surge_mult

            # Confluence check: Range was in compression (or reasonable consolidate) AND breakout occurs
            if breakout_confirmed and (range_pct <= self.max_compression_range_pct or volume_boost):
                logger.info(
                    f"[REENTRY_TRIGGER] {symbol} {side} Breakout Detected! "
                    f"Mark={mark:.5f} vs Comp High={p_high:.5f} Low={p_low:.5f} (Range: {range_pct:.2f}%) "
                    f"Vol={curr_vol:.1f} vs Base={base_vol:.1f}"
                )

                # Calculate new tight structural SL
                if is_long:
                    new_sl = p_low * 0.997  # Just under compression support
                    risk = (mark - new_sl) / mark
                    # Risk bounds sanity: min 0.8%, max 3.5%
                    if risk < 0.008:
                        new_sl = mark * 0.992
                    elif risk > 0.035:
                        new_sl = mark * 0.965
                else:
                    new_sl = p_high * 1.003
                    risk = (new_sl - mark) / mark
                    if risk < 0.008:
                        new_sl = mark * 1.008
                    elif risk > 0.035:
                        new_sl = mark * 1.035

                tps = item.get("original_tps") or []
                if not tps:
                    dist = abs(mark - new_sl)
                    tps = [mark + dist * 1.5, mark + dist * 2.5] if is_long else [mark - dist * 1.5, mark - dist * 2.5]

                # Execute Re-Entry
                try:
                    res = await self.trader.open_position(
                        symbol=symbol,
                        side=side,
                        entry_price=mark,
                        sl_price=new_sl,
                        tp_prices=tps,
                        signal_metadata={
                            "source": "COMPRESSION_BREAKOUT_REENTRY",
                            "original_entry": item["original_entry"],
                            "cut_reason": item["exit_reason"],
                            "compression_high": p_high,
                            "compression_low": p_low,
                            "compression_range_pct": range_pct,
                        },
                    )

                    if res and res.get("ok"):
                        logger.info(f"[REENTRY_SUCCESS] {symbol} re-entered successfully @ {mark:.5f} with SL {new_sl:.5f}")
                        symbols_to_remove.append(symbol)
                        
                        # Send Telegram Notification
                        try:
                            from notifications.telegram_notifier import send_reentry_trade
                            await send_reentry_trade({
                                "symbol": symbol,
                                "side": side,
                                "entry_price": mark,
                                "sl_price": new_sl,
                                "tp_prices": tps,
                                "compression_range": f"{p_low:.4f} - {p_high:.4f}",
                                "reason": f"COMPRESSION_BREAKOUT (cut: {item['exit_reason']})"
                            })
                        except Exception as tg_err:
                            logger.debug(f"[REENTRY_TG] Optional notify error: {tg_err}")
                    else:
                        logger.warning(f"[REENTRY_FAIL] {symbol} execution failed: {res}")
                except Exception as ex_err:
                    logger.error(f"[REENTRY_ERROR] Error opening re-entry for {symbol}: {ex_err}")

        # Clean up processed or expired symbols
        for sym in symbols_to_remove:
            self.watchlist.pop(sym, None)
        if symbols_to_remove:
            self._save_watchlist()
