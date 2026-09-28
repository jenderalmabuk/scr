"""Adaptive Exhaustion & Structure Take-Profit Engine.

Identifies volume exhaustion spikes (climax buying), ATR volatility blow-offs,
and major HTF resistance/orderblock walls when an active trade is in solid profit.
Executes an adaptive profit harvest (closing 50% for bot trades, 25% for manual trades)
at the peak and locks the remaining position to Breakeven + buffer, preventing
profitable trades from giving back all their gains into a sudden dump.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("fusion_nexus")


class ExhaustionHarvestEngine:
    def __init__(self, trader: Any):
        self.trader = trader
        self.enabled = str(os.getenv("EXHAUSTION_HARVEST_ENABLED", "true")).lower() in {"1", "true", "yes"}
        self.min_pnl_pct = float(os.getenv("EXHAUSTION_MIN_PNL_PCT", "1.8"))
        self.min_mfe_r = float(os.getenv("EXHAUSTION_MIN_MFE_R", "0.50"))
        self.harvest_fraction = float(os.getenv("EXHAUSTION_HARVEST_FRACTION", "0.50"))
        self.vol_spike_mult = float(os.getenv("EXHAUSTION_VOL_SPIKE_MULT", "2.5"))
        self.atr_expansion_mult = float(os.getenv("EXHAUSTION_ATR_EXPANSION_MULT", "2.0"))
        self.res_proximity_pct = float(os.getenv("EXHAUSTION_RESISTANCE_PROXIMITY_PCT", "0.50"))
        self.majors = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
        self.min_pnl_majors_pct = float(os.getenv("EXHAUSTION_MIN_PNL_MAJORS_PCT", "1.5"))

    async def check_exhaustion_harvest(self, symbol: str, pos: Dict[str, Any], mark: float) -> bool:
        """
        Evaluate if an active winning position has reached an exhaustion climax or HTF wall.
        Returns True if partial profit was harvested.
        """
        if not self.enabled:
            return False

        # Guard: Only harvest once per trade lifetime
        if pos.get("exhaustion_harvested", False):
            return False

        # Guard: If TP1 was already executed by standard ladder, skip early harvest
        if len(pos.get("tp_hit", [])) >= 1:
            return False

        entry = float(pos.get("entry_price") or 0.0)
        if entry <= 0:
            return False

        side = str(pos.get("side") or "").upper()
        is_long = side == "LONG"
        current_pnl_pct = (mark - entry) / entry * 100.0 if is_long else (entry - mark) / entry * 100.0

        risk_dist = self.trader._get_risk_distance(pos)
        current_r = 0.0
        if risk_dist > 0:
            current_r = (mark - entry) / risk_dist if is_long else (entry - mark) / risk_dist

        # Must be in meaningful profit territory (adaptive: 1.5% for Majors like SOL/BTC/ETH, 1.8% for Altcoins)
        target_min_pnl = self.min_pnl_majors_pct if symbol in self.majors else self.min_pnl_pct
        if current_pnl_pct < target_min_pnl and current_r < self.min_mfe_r:
            return False

        # Fetch recent 15m klines for volume, ATR, and resistance analysis
        try:
            klines = await asyncio.to_thread(
                self.trader.client.get_klines,
                symbol=symbol,
                interval="15",
                limit=50,
            )
        except Exception as e:
            logger.debug(f"[EXHAUSTION] Failed to fetch klines for {symbol}: {e}")
            return False

        if not klines or len(klines) < 15:
            return False

        curr_candle = klines[0]
        closed_candles = klines[1:]

        try:
            c_open = float(curr_candle[1])
            c_high = float(curr_candle[2])
            c_low = float(curr_candle[3])
            c_close = float(curr_candle[4])
            curr_vol = float(curr_candle[5])
        except (IndexError, ValueError):
            return False

        c_range = max(c_high - c_low, 1e-8)
        if is_long:
            upper_wick = c_high - max(c_open, c_close)
            wick_ratio = upper_wick / c_range
        else:
            lower_wick = min(c_open, c_close) - c_low
            wick_ratio = lower_wick / c_range

        # 1. Volume Spike Exhaustion
        vol_baseline = (sum(float(k[5]) for k in closed_candles[:30]) / min(30, len(closed_candles))) if closed_candles else 1.0
        vol_ratio = curr_vol / vol_baseline if vol_baseline > 0 else 1.0
        has_vol_exhaustion = (vol_ratio >= self.vol_spike_mult) and (wick_ratio >= 0.28)

        # 2. ATR Volatility Blow-Off
        atr_14 = self.trader._calc_atr_from_klines(closed_candles, period=14)
        atr_ratio = c_range / atr_14 if atr_14 > 0 else 1.0
        has_atr_blowoff = (atr_ratio >= self.atr_expansion_mult) and (wick_ratio >= 0.35)

        # 3. HTF Resistance / Prior Swing High Proximity
        prior_highs = [float(k[2]) for k in closed_candles[:40] if len(k) > 2]
        prior_lows = [float(k[3]) for k in closed_candles[:40] if len(k) > 3]

        has_res_wall = False
        res_price = 0.0
        if is_long and prior_highs:
            res_price = max(prior_highs)
            dist_pct = abs(mark - res_price) / res_price * 100.0
            if dist_pct <= self.res_proximity_pct and mark <= res_price * 1.002:
                has_res_wall = True
        elif not is_long and prior_lows:
            res_price = min(prior_lows)
            dist_pct = abs(mark - res_price) / res_price * 100.0
            if dist_pct <= self.res_proximity_pct and mark >= res_price * 0.998:
                has_res_wall = True

        if not (has_vol_exhaustion or has_atr_blowoff or has_res_wall):
            return False

        # Identify triggers for audit & notification
        triggers = []
        if has_vol_exhaustion:
            triggers.append(f"VolSpike {vol_ratio:.1f}x")
        if has_atr_blowoff:
            triggers.append(f"ATR {atr_ratio:.1f}x")
        if has_res_wall:
            triggers.append(f"HTF_Wall {res_price:g}")
        reason_label = " + ".join(triggers)

        logger.info(
            f"[EXHAUSTION_DETECTED] {symbol} {side} @ {mark:.5f} | "
            f"Gain={current_pnl_pct:+.2f}% ({current_r:+.2f}R) | Triggers: {reason_label} | "
            f"Wick={wick_ratio:.0%}"
        )

        qty_remaining = float(pos.get("qty_remaining", pos.get("qty", 0.0)))
        original_qty = float(pos.get("qty", 0.0))
        if qty_remaining <= 0 or original_qty <= 0:
            return False

        # Check if position is manual trade
        is_manual = bool(
            pos.get("is_manual")
            or (pos.get("metadata") or {}).get("imported")
            or (pos.get("metadata") or {}).get("manual_imported_position")
            or (pos.get("metadata") or {}).get("adv_snapshot", {}).get("manual_imported_position")
            or (pos.get("metadata") or {}).get("adv_snapshot", {}).get("manual_context_enriched")
        )
        effective_fraction = float(os.getenv("MANUAL_TP1_FRACTION", "0.25")) if is_manual else self.harvest_fraction

        close_qty = qty_remaining * effective_fraction
        next_qty_remaining = max(0.0, qty_remaining - close_qty)

        # 1. Execute partial close at peak
        harvest_reason = f"EXHAUSTION_HARVEST_{triggers[0].split()[0]}"
        await self.trader._close_partial(
            symbol=symbol,
            reason=harvest_reason,
            exit_price=mark,
            close_qty=close_qty,
            qty_remaining=next_qty_remaining,
            sl_kind="EXHAUSTION_HARVEST",
            full_close=next_qty_remaining <= original_qty * 0.05,
        )

        # 2. Lock remaining position to Breakeven + buffer (+0.3%)
        try:
            buffer_dist = risk_dist * 0.05 if risk_dist > 0 else entry * 0.003
            if is_long:
                new_sl = entry + buffer_dist
                if new_sl >= mark * 0.998:
                    new_sl = mark * 0.995
            else:
                new_sl = entry - buffer_dist
                if new_sl <= mark * 1.002:
                    new_sl = mark * 1.005

            instrument = await asyncio.to_thread(self.trader.client.get_instrument_info, symbol)
            tick_size = float(instrument["priceFilter"]["tickSize"])
            await asyncio.to_thread(
                self.trader.client.set_trading_stop,
                symbol=symbol,
                position_idx=0,
                stop_loss=self.trader._quantize(new_sl, tick_size),
            )
            pos["sl_price"] = new_sl
            pos["sl_kind"] = "EXHAUSTION_BEP_PROTECTED"
            pos["locked_profit"] = True
            logger.info(f"[EXHAUSTION_LOCK] {symbol} remaining SL moved to BEP+buffer {new_sl:.5f}")
        except Exception as lock_err:
            logger.warning(f"[EXHAUSTION_LOCK] Could not move SL to BEP for {symbol}: {lock_err}")

        # Mark as harvested to prevent re-triggering on same position
        pos["exhaustion_harvested"] = True
        pos["qty_remaining"] = next_qty_remaining
        self.trader._save_positions()

        # 3. Send rich Telegram notification
        try:
            from notifications.telegram_notifier import send_exhaustion_harvest_trade
            await send_exhaustion_harvest_trade({
                "symbol": symbol,
                "side": side,
                "exit_price": mark,
                "pnl_pct": current_pnl_pct,
                "harvest_pct": int(effective_fraction * 100),
                "reason": reason_label,
                "new_sl": pos.get("sl_price", entry),
                "is_manual": is_manual,
            })
        except Exception as tg_err:
            logger.debug(f"[EXHAUSTION_TG] Optional notify error: {tg_err}")

        return True
