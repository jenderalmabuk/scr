"""
BTC Regime & Correlation Gate for Signal Copy.

Protects against systemic market cascade / high-beta drag (e.g. altcoins dumping
when BTC experiences a sharp breakdown below 15m VWAP).

Key Principles:
1. Low-Beta / Independent Tokens (PEOPLE, MARSCOIN, Meme/Community tokens) are
   EXEMPT from the BTC gate so their independent breakout profits are preserved.
2. High-Beta Pairs (ETH, SOL, NEAR, ZRO, POL, etc. with r >= 0.50) are BLOCKED
   from entering LONG when BTC is in an active Dump / breakdown regime (BTC < VWAP -0.35%
   or 15m drop < -0.6% or 1h drop < -1.0%).
3. SHORT positions on high-beta pairs are blocked if BTC is actively pumping
   (BTC > VWAP +0.35% or 15m pump > +0.6%).
4. In-memory caching ensures 0ms execution overhead during live signal processing.
"""
from __future__ import annotations

import os
import time
import asyncio
import aiohttp
import numpy as np
from typing import Dict, Any, Optional, List

from utils.logger import logger
from . import validation_config as vc


class BtcRegimeGate:
    _instance: Optional["BtcRegimeGate"] = None

    def __init__(self):
        self._btc_cache: Dict[str, Any] = {}
        self._btc_cache_time: float = 0.0
        self._corr_cache: Dict[str, Any] = {}  # symbol -> (corr_val, timestamp)
        self.cache_ttl: float = 30.0           # 30s cache for BTC klines
        self.corr_ttl: float = 180.0          # 3 minutes cache for correlation

        # Known independent / idiosyncratic / community momentum symbols
        self.low_beta_overrides = {
            "PEOPLEUSDT", "MARSCOINUSDT", "1000PEPEUSDT", "PENGUUSDT",
            "TUTUSDT", "MUBARAKUSDT", "NOMUSDT", "4USDT", "B3USDT",
            "CYSUSDT", "XAIUSDT", "PARTIUSDT", "BROCCOLI714USDT"
        }

    @classmethod
    def get_instance(cls) -> "BtcRegimeGate":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def get_btc_regime(self) -> Dict[str, Any]:
        """Fetch BTC 15m klines and compute rolling 20-bar 15m VWAP + momentum."""
        now = time.time()
        if self._btc_cache and (now - self._btc_cache_time) < self.cache_ttl:
            return self._btc_cache

        url = "https://api.bybit.com/v5/market/kline?category=linear&symbol=BTCUSDT&interval=15&limit=30"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        rows = data.get("result", {}).get("list", [])
                        if len(rows) >= 20:
                            # Index 0 is newest in Bybit API
                            rolling_20 = rows[:20]
                            typical_prices = []
                            volumes = []
                            closes = [float(r[4]) for r in rows]

                            for r in rolling_20:
                                high = float(r[2])
                                low = float(r[3])
                                close = float(r[4])
                                vol = float(r[5])
                                typ = (high + low + close) / 3.0
                                typical_prices.append(typ)
                                volumes.append(vol)

                            total_pv = sum(tp * v for tp, v in zip(typical_prices, volumes))
                            total_v = sum(volumes)
                            vwap = total_pv / total_v if total_v > 0 else closes[0]
                            current_price = closes[0]

                            diff_pct = ((current_price - vwap) / vwap) * 100.0
                            ret_15m = ((closes[0] - closes[1]) / closes[1]) * 100.0 if len(closes) > 1 else 0.0
                            ret_1h = ((closes[0] - closes[4]) / closes[4]) * 100.0 if len(closes) > 4 else 0.0

                            dump_threshold = getattr(vc, "BTC_DUMP_THRESHOLD_PCT", -0.35)
                            pump_threshold = getattr(vc, "BTC_PUMP_THRESHOLD_PCT", 0.35)

                            is_dump = (diff_pct < dump_threshold or ret_15m < -0.60 or ret_1h < -1.00)
                            is_pump = (diff_pct > pump_threshold or ret_15m > 0.60 or ret_1h > 1.00)

                            if diff_pct < -0.6 or ret_15m < -0.8:
                                regime = "DUMP"
                            elif diff_pct < -0.2:
                                regime = "BEARISH"
                            elif diff_pct > 0.6 or ret_15m > 0.8:
                                regime = "PUMP"
                            elif diff_pct > 0.2:
                                regime = "BULLISH"
                            else:
                                regime = "NEUTRAL"

                            self._btc_cache = {
                                "btc_price": current_price,
                                "btc_vwap": vwap,
                                "btc_diff_pct": diff_pct,
                                "btc_ret_15m": ret_15m,
                                "btc_ret_1h": ret_1h,
                                "is_dump": is_dump,
                                "is_pump": is_pump,
                                "regime": regime,
                                "closes": closes[:25],
                                "timestamp": now,
                            }
                            self._btc_cache_time = now
                            return self._btc_cache
            except Exception as e:
                logger.warning("[BTC_GATE] Failed to fetch live BTC klines: %s", e)

        # Fallback if network issue
        return {
            "btc_price": 0.0,
            "btc_vwap": 0.0,
            "btc_diff_pct": 0.0,
            "btc_ret_15m": 0.0,
            "btc_ret_1h": 0.0,
            "is_dump": False,
            "is_pump": False,
            "regime": "NEUTRAL",
            "closes": [],
            "timestamp": now,
        }

    async def compute_correlation(self, symbol: str, btc_closes: List[float]) -> float:
        """Compute rolling 20-bar Pearson correlation between symbol and BTC."""
        now = time.time()
        sym_clean = symbol.upper()
        if sym_clean in self._corr_cache:
            corr_val, t_cached = self._corr_cache[sym_clean]
            if (now - t_cached) < self.corr_ttl:
                return corr_val

        # Check known overrides
        if sym_clean in self.low_beta_overrides:
            corr_val = 0.25
            self._corr_cache[sym_clean] = (corr_val, now)
            return corr_val

        if not btc_closes or len(btc_closes) < 15:
            return 0.65  # default moderate-high correlation

        url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={sym_clean}&interval=15&limit={len(btc_closes)}"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        rows = data.get("result", {}).get("list", [])
                        if len(rows) >= 15:
                            sym_closes = [float(r[4]) for r in rows[:len(btc_closes)]]
                            min_len = min(len(sym_closes), len(btc_closes))

                            # 15m returns
                            sym_c = sym_closes[:min_len]
                            btc_c = btc_closes[:min_len]

                            sym_rets = [(sym_c[i] - sym_c[i+1]) / sym_c[i+1] for i in range(min_len - 1)]
                            btc_rets = [(btc_c[i] - btc_c[i+1]) / btc_c[i+1] for i in range(min_len - 1)]

                            std_s = np.std(sym_rets)
                            std_b = np.std(btc_rets)
                            if std_s > 1e-8 and std_b > 1e-8:
                                r = float(np.corrcoef(sym_rets, btc_rets)[0, 1])
                                if not np.isnan(r):
                                    self._corr_cache[sym_clean] = (r, now)
                                    return r
            except Exception as e:
                logger.debug("[BTC_GATE] Correlation calc for %s error: %s", symbol, e)

        default_r = 0.65
        self._corr_cache[sym_clean] = (default_r, now)
        return default_r

    async def evaluate_gate(self, symbol: str, side: str) -> Dict[str, Any]:
        """Evaluate if the trade passes the Correlation-Gated Smart Dump Gate."""
        enabled = getattr(vc, "BTC_GATE_ENABLED", True)
        if not enabled:
            return {
                "allowed": True,
                "blocked": False,
                "reason": "BTC gate disabled via config",
                "correlation": 0.0,
                "is_low_beta": False,
                "btc_price": 0.0,
                "btc_vwap": 0.0,
                "btc_diff_pct": 0.0,
                "btc_ret_15m": 0.0,
                "btc_regime": "NEUTRAL",
            }

        btc_info = await self.get_btc_regime()
        corr = await self.compute_correlation(symbol, btc_info.get("closes", []))

        is_dump = btc_info.get("is_dump", False)
        is_pump = btc_info.get("is_pump", False)
        diff_pct = btc_info.get("btc_diff_pct", 0.0)
        regime = btc_info.get("regime", "NEUTRAL")

        low_beta_threshold = getattr(vc, "BTC_LOW_BETA_CORR", 0.40)
        high_beta_threshold = getattr(vc, "BTC_HIGH_BETA_CORR", 0.50)

        sym_clean = symbol.upper()
        is_low_beta = (corr < low_beta_threshold) or (sym_clean in self.low_beta_overrides)
        side_norm = side.upper()

        blocked = False
        reason = ""

        if is_low_beta:
            blocked = False
            reason = f"Low-Beta / Independent Momentum (Corr: {corr:+.2f}, Bypass)"
        elif side_norm in ("LONG", "BUY") and is_dump and corr >= high_beta_threshold:
            blocked = True
            reason = f"BTC Dump Risk on High-Beta Pair (BTC vs VWAP: {diff_pct:+.2f}%, Corr: {corr:.2f})"
        elif side_norm in ("SHORT", "SELL") and is_pump and corr >= high_beta_threshold:
            blocked = True
            reason = f"BTC Pump Risk on High-Beta Pair (BTC vs VWAP: {diff_pct:+.2f}%, Corr: {corr:.2f})"
        else:
            blocked = False
            reason = f"Market conditions acceptable (BTC vs VWAP: {diff_pct:+.2f}%, Corr: {corr:.2f})"

        return {
            "allowed": not blocked,
            "blocked": blocked,
            "reason": reason,
            "correlation": corr,
            "is_low_beta": is_low_beta,
            "btc_price": btc_info.get("btc_price", 0.0),
            "btc_vwap": btc_info.get("btc_vwap", 0.0),
            "btc_diff_pct": diff_pct,
            "btc_ret_15m": btc_info.get("btc_ret_15m", 0.0),
            "btc_regime": regime,
        }


def get_btc_regime_gate() -> BtcRegimeGate:
    return BtcRegimeGate.get_instance()
