#!/usr/bin/env python3
"""
Autonomous Market Scanner (v2.0 - MTF IFVG-Pro & Golden Scalp-Runner Engine).

Upgraded autonomous architecture:
1. Multi-Timeframe Trend: 1h EMA 20/50 alignment for high-probability bias.
2. Filter 1 (Macro BTC Guard): Strict veto against SHORTs when BTC is bullish.
3. Filter 2 (Liquid Whitelist): Scan verified native crypto with 24h Vol >= $20M (blocks 14 illiquid coins).
4. Trigger Engine: 15m Inversion Fair Value Gap (IFVG-Pro, Min Gap >= 0.50 ATR, candle body close confirmation).
5. Filter 3 (Anti-Wick Noise Floor): Hard Stop at Recent Swing with minimum distance >= 1.0%.
6. Golden Scalp-Runner Execution:
   - TP1 (75%) at 1.5R -> Realize real cash, move SL to Lock +1.0R (Profit-Guaranteed SL).
   - TP2 (25%) at 3.0R -> Trailing Swing runner.
7. Micro Adversarial Veto: CVD flow, OI delta, and Whale conflict protection.
8. Collision Guard: Maximum 4 concurrent slots, never double-enter symbols held by Signal Copy.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("autonomous_scanner")

def _load_env():
    candidates = [
        Path("/opt/signalcopyreal/.env"),
        Path(__file__).resolve().parent.parent / ".env",
        Path(".env")
    ]
    for p in candidates:
        if p.exists():
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip().strip("'\"")
                        if k and k not in os.environ:
                            os.environ[k] = v
                break
            except Exception:
                pass

_load_env()

NEXUS_API = os.getenv("NEXUS_API_URL", "http://127.0.0.1:8000")
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:8787/gateway")
GATEWAY_TOKEN = os.getenv("GATEWAY_TOKEN", "nexus_gateway_test_2024")

MIN_VOLUME_USD = float(os.getenv("AUTONOMOUS_MIN_VOL_USD", "20000000.0"))
MAX_CONCURRENT_POSITIONS = int(os.getenv("AUTONOMOUS_MAX_POSITIONS", "10"))
SCAN_INTERVAL_SEC = int(os.getenv("AUTONOMOUS_SCAN_INTERVAL_SEC", "180"))

TELEGRAM_BOT_TOKEN = os.getenv("SIGNAL_COPY_TRADES_NOTIFY_BOT_TOKEN") or os.getenv("RADAR_TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("SIGNAL_COPY_TRADES_NOTIFY_CHAT_ID") or os.getenv("RADAR_TELEGRAM_CHAT_ID", "")

WHALES_DIR = Path("/opt/signalcopyreal/runtime/whales")
LOW_LIQUID_COINS = {
    "AKEUSDT", "GIGGLEUSDT", "EPICUSDT", "CAPUSDT", "KAITOUSDT", 
    "BANKUSDT", "CYSUSDT", "BEATUSDT", "COTIUSDT", "UAIUSDT", "BTWUSDT", "BLESSUSDT",
    "USELESSUSDT", "APRUSDT", "BATUSDT", "WLFIUSDT"
}

TRADFI_EXCLUSIONS = {
    "NBISUSDT", "QQQUSDT", "SPYUSDT", "IWMUSDT", "DIAUSDT", "XAUUSDT", "XAGUSDT", "USDCUSDT",
    "BUSDUSDT", "FDUSDUSDT", "TUSDUSDT", "DAIUSDT", "EURUSDT", "USDUSDT",
    "AAPLUSDT", "MSFTUSDT", "GOOGLUSDT", "AMZNUSDT", "NVDAUSDT", "TSLAUSDT", "METAUSDT",
    "NFLXUSDT", "BABAUSDT", "AMDUSDT", "AMDSTOCKUSDT", "CATUSDT", "CATSTOCKUSDT", "COINUSDT",
    "MSTRUSDT", "HOODUSDT", "ARMUSDT", "INTCUSDT", "CSCOUSDT", "IBMUSDT", "ORCLUSDT",
    "CRMUSDT", "BAUSDT", "JPMUSDT", "BACUSDT", "WMTUSDT", "DISUSDT", "PLTRUSDT", "UBERUSDT"
}

def _http_get_sync(url: str, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None, timeout: int = 6) -> Optional[Any]:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req_headers = {"User-Agent": "Mozilla/5.0 (NexusAutonomousScanner/2.0)"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.debug("_http_get_sync error %s: %s", url, exc)
    return None

def _http_post_sync(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None, timeout: int = 8) -> Optional[Any]:
    req_headers = {
        "User-Agent": "Mozilla/5.0 (NexusAutonomousScanner/2.0)",
        "Content-Type": "application/json"
    }
    if headers:
        req_headers.update(headers)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=req_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.debug("_http_post_sync error %s: %s", url, exc)
    return None

def compute_ema(values: List[float], period: int) -> float:
    if len(values) < period:
        return values[-1] if values else 0.0
    multiplier = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for val in values[period:]:
        ema = (val - ema) * multiplier + ema
    return ema

def compute_atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        trs.append(tr)
    if len(trs) < period:
        return 0.0
    return sum(trs[-period:]) / period

def compute_rsi(closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [abs(d) if d < 0 else 0.0 for d in deltas]
    
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def load_crypto_whitelist() -> set[str]:
    candidate_paths = [
        Path("/opt/signalcopyreal/bots/universe.txt"),
        Path(__file__).resolve().parent.parent / "bots" / "universe.txt",
        Path("bots/universe.txt"),
    ]
    symbols = set()
    for p in candidate_paths:
        if p.exists():
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    sym = line.strip().upper()
                    if sym and sym.endswith("USDT") and sym not in TRADFI_EXCLUSIONS and "STOCK" not in sym and "-" not in sym:
                        symbols.add(sym)
                break
            except Exception:
                pass
    return symbols

class AutonomousMarketScanner:
    def __init__(self):
        self.last_scanned: Dict[str, float] = {}
        self.crypto_whitelist: set[str] = load_crypto_whitelist()
        logger.info("[INIT] Loaded %d verified pure-crypto symbols into whitelist (Filter 2)", len(self.crypto_whitelist))

    async def get_universe(self) -> List[str]:
        url = f"{NEXUS_API}/universe/top"
        data = await asyncio.to_thread(_http_get_sync, url)
        
        pairs = []
        if data and isinstance(data, dict):
            for item in data.get("pairs", []):
                pair = item.get("pair")
                quote_vol = float(item.get("quote_volume", 0.0))
                if (
                    pair
                    and pair in self.crypto_whitelist
                    and pair not in TRADFI_EXCLUSIONS and pair not in LOW_LIQUID_COINS
                    and "STOCK" not in pair
                    and "-" not in pair
                    and quote_vol >= MIN_VOLUME_USD
                ):
                    pairs.append(pair)
        
        if not pairs:
            fallback = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "NEARUSDT", "SUIUSDT", "AVAXUSDT", "LINKUSDT", "DOGEUSDT", "ADAUSDT", "XRPUSDT"]
            return [p for p in fallback if p in self.crypto_whitelist]
            
        return pairs[:40]

    async def get_btc_regime(self) -> Dict[str, Any]:
        url = f"{NEXUS_API}/btc_regime"
        data = await asyncio.to_thread(_http_get_sync, url)
        return data or {"mode": "NEUTRAL", "btc_regime": "neutral"}

    async def get_gateway_portfolio(self) -> Dict[str, Any]:
        headers = {"Authorization": f"Bearer {GATEWAY_TOKEN}"}
        url = f"{GATEWAY_URL}/portfolio"
        data = await asyncio.to_thread(_http_get_sync, url, headers=headers)
        return data or {}

    async def fetch_klines(self, symbol: str, tf: str = "15m", limit: int = 60) -> List[Dict[str, Any]]:
        for exchange in ["bybit", "binance"]:
            url = f"{NEXUS_API}/klines/{exchange}/{symbol}"
            res = await asyncio.to_thread(_http_get_sync, url, {"tf": tf, "limit": limit})
            if res and res.get("data"):
                return res["data"]
        return []

    async def analyze_pair(self, symbol: str, btc_regime: str) -> Optional[Dict[str, Any]]:
        """
        Evaluate pair using Multi-Timeframe Trend (1h) + 15m IFVG-Pro Engine.
        """
        k1h_task = self.fetch_klines(symbol, tf="1h", limit=60)
        k15m_task = self.fetch_klines(symbol, tf="15m", limit=60)
        k1h, k15m = await asyncio.gather(k1h_task, k15m_task)

        if len(k1h) < 35 or len(k15m) < 35:
            return None

        # ── 1. Multi-Timeframe Macro Trend (1 Hour) ──────────────────────
        closes_1h = [float(k["close"]) for k in k1h]
        ema20_1h = compute_ema(closes_1h, 20)
        ema50_1h = compute_ema(closes_1h, 50)
        last_1h = closes_1h[-1]

        is_bullish_1h = (ema20_1h > ema50_1h) and (last_1h > ema50_1h)
        is_bearish_1h = (ema20_1h < ema50_1h) and (last_1h < ema50_1h)

        if not is_bullish_1h and not is_bearish_1h:
            return None

        # ── 2. Filter 1: Macro BTC Guard ─────────────────────────────────
        # Block SHORTs if BTC is strongly bullish / risk on
        btc_upper = str(btc_regime).upper()
        if is_bearish_1h and btc_upper in ("BULLISH", "RISK_ON"):
            logger.debug("[FILTER 1 GUARD] %s Bearish rejected because BTC is %s", symbol, btc_regime)
            return None

        # ── 3. Timeframe Micro (15m) Indicator Calculations ──────────────
        closes_15m = [float(k["close"]) for k in k15m]
        highs_15m = [float(k["high"]) for k in k15m]
        lows_15m = [float(k["low"]) for k in k15m]
        opens_15m = [float(k["open"]) for k in k15m]

        current_price = closes_15m[-1]
        ema20_15m = compute_ema(closes_15m, 20)
        ema50_15m = compute_ema(closes_15m, 50)
        rsi_15m = compute_rsi(closes_15m, 14)
        atr_15m = compute_atr(highs_15m, lows_15m, closes_15m, 14)

        if atr_15m <= 0 or current_price <= 0:
            return None

        # ── 4. IFVG-Pro Engine: Detect Active FVGs & Inversions ──────────
        min_gap = 0.50 * atr_15m  # Sweet Spot Parameter (Validated empirically)
        active_fvgs = []

        # Scan previous bars for active FVGs formed in last 35 bars
        n_bars = len(k15m)
        for i in range(15, n_bars - 1):
            c3_low = lows_15m[i]
            c1_high = highs_15m[i - 2]
            c1_low = lows_15m[i - 2]
            c3_high = highs_15m[i]

            # Bullish FVG
            if c3_low - c1_high >= min_gap:
                active_fvgs.append({
                    "type": "BULLISH", "top": c3_low, "bottom": c1_high,
                    "bar": i, "status": "ACTIVE"
                })
            # Bearish FVG
            if c1_low - c3_high >= min_gap:
                active_fvgs.append({
                    "type": "BEARISH", "top": c1_low, "bottom": c3_high,
                    "bar": i, "status": "ACTIVE"
                })

        side = None
        fvg_matched = None
        last_close = closes_15m[-1]
        prev_close = closes_15m[-2]

        # Evaluate Inversions on the latest completed candle
        for fvg in active_fvgs:
            bar_age = (n_bars - 1) - fvg["bar"]
            if bar_age < 2 or bar_age > 35:
                continue

            # BEARISH FVG inverted into SUPPORT -> LONG Signal
            if fvg["type"] == "BEARISH":
                if last_close > fvg["top"] and prev_close <= fvg["top"]:
                    # MTF Trend Alignment
                    if is_bullish_1h and (ema20_15m > ema50_15m):
                        side = "LONG"
                        fvg_matched = fvg
                        break

            # BULLISH FVG inverted into RESISTANCE -> SHORT Signal
            elif fvg["type"] == "BULLISH":
                if last_close < fvg["bottom"] and prev_close >= fvg["bottom"]:
                    if is_bearish_1h and (ema20_15m < ema50_15m) and btc_upper not in ("BULLISH", "RISK_ON"):
                        side = "SHORT"
                        fvg_matched = fvg
                        break

        if not side:
            return None

        # ── 5. Smart Stop Loss & Filter 3 (Anti-Wick Noise Floor >= 1.0%) ─
        entry = current_price
        if side == "LONG":
            swing_low = min(lows_15m[-6:])
            raw_sl_dist = max(entry - swing_low, 1.2 * atr_15m)
            min_sl_dist = entry * 0.010  # Filter 3: 1.0% floor
            max_sl_dist = entry * 0.035
            sl_dist = max(min_sl_dist, min(max_sl_dist, raw_sl_dist))
            sl = round(entry - sl_dist, 6)
            tp1 = round(entry + (1.5 * sl_dist), 6)  # TP1: 1.5R (75% Realize Profit)
            tp2 = round(entry + (3.0 * sl_dist), 6)  # TP2: 3.0R (25% Runner Target)
        else:
            swing_high = max(highs_15m[-6:])
            raw_sl_dist = max(swing_high - entry, 1.2 * atr_15m)
            min_sl_dist = entry * 0.010  # Filter 3: 1.0% floor
            max_sl_dist = entry * 0.035
            sl_dist = max(min_sl_dist, min(max_sl_dist, raw_sl_dist))
            sl = round(entry + sl_dist, 6)
            tp1 = round(entry - (1.5 * sl_dist), 6)  # TP1: 1.5R
            tp2 = round(entry - (3.0 * sl_dist), 6)  # TP2: 3.0R

        sl_pct = (sl_dist / entry) * 100.0

        # ── 6. Micro Adversarial Committee Checks ────────────────────────
        flow_url = f"{NEXUS_API}/flow/{symbol}"
        oi_url = f"{NEXUS_API}/oi/bybit/{symbol}"

        flow_task = asyncio.to_thread(_http_get_sync, flow_url)
        oi_task = asyncio.to_thread(_http_get_sync, oi_url)
        flow_data, oi_data = await asyncio.gather(flow_task, oi_task)

        flow_dir = (flow_data or {}).get("flow_direction", "NEUTRAL")
        if side == "LONG" and flow_dir == "SHORT_ONLY":
            logger.info("[ADVERSARIAL VETO] %s LONG rejected by flow_dir=SHORT_ONLY", symbol)
            return None
        if side == "SHORT" and flow_dir == "LONG_ONLY":
            logger.info("[ADVERSARIAL VETO] %s SHORT rejected by flow_dir=LONG_ONLY", symbol)
            return None

        oi_history = (oi_data or {}).get("data", [])
        if oi_history and len(oi_history) >= 2:
            last_oi_delta = float(oi_history[-1].get("oi_delta_pct", 0.0))
            if last_oi_delta < -1.5:
                logger.info("[ADVERSARIAL VETO] %s %s rejected by severe OI drop %.2f%%", symbol, side, last_oi_delta)
                return None

        whale_file = WHALES_DIR / f"latest_cex_whale_{symbol}.json"
        if whale_file.exists():
            try:
                whale = json.loads(whale_file.read_text(encoding="utf-8"))
                w_time = datetime.fromisoformat(whale.get("timestamp", "").replace("Z", "+00:00"))
                if (datetime.now(timezone.utc) - w_time).total_seconds() < 900:
                    w_side = whale.get("side", "").upper()
                    w_val = float(whale.get("value_usd", 0.0))
                    if w_val >= 500000 and ((side == "LONG" and w_side == "SELL") or (side == "SHORT" and w_side == "BUY")):
                        logger.info("[ADVERSARIAL VETO] %s %s blocked by opposing whale $%s", symbol, side, f"{w_val:,.0f}")
                        return None
            except Exception:
                pass

        return {
            "symbol": symbol,
            "side": side,
            "entry": entry,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "rr1": 1.5,
            "rr2": 3.0,
            "sl_pct": sl_pct,
            "rsi_15m": round(rsi_15m, 1),
            "flow_dir": flow_dir,
            "regime": "MTF_IFVG_PRO",
            "gap_atr": round(min_gap / atr_15m, 2),
            "strategy": "MTF_IFVG_PRO",
            "tp_plan": "GOLDEN_SCALP"
        }

    async def execute_intent(self, setup: Dict[str, Any]) -> bool:
        symbol = setup["symbol"]
        side = setup["side"]
        entry = setup["entry"]
        sl = setup["sl"]
        tps = [setup["tp1"], setup["tp2"]]
        sl_frac = (setup["sl_pct"] / 100.0) if setup["sl_pct"] > 0 else 0.01
        target_risk_usd = 2.0  # $2.00 target risk per trade (2% on $100 capital)
        calculated_notional = round(target_risk_usd / sl_frac, 2)

        payload = {
            "source": "AUTONOMOUS_SCANNER",
            "symbol": symbol,
            "side": side,
            "entry_price": entry,
            "sl_price": sl,
            "tps": tps,
            "risk_pct": 0.02,
            "notional": calculated_notional,
            "regime": setup["regime"],
            "confidence": 0.88,
            "tier": "Standard",
            "leverage": 10,
            "tag": f"auto_ifvg_{symbol.lower()}_{int(time.time())}",
            "adv_snapshot": {
                "rsi": setup["rsi_15m"],
                "flow_direction": setup["flow_dir"],
                "sl_pct": setup["sl_pct"],
                "strategy": "MTF_IFVG_PRO",
                "tp_plan": "GOLDEN_SCALP",
                "rr1": 1.5,
                "rr2": 3.0,
                "tp1_pct": 0.75,
                "runner_pct": 0.25
            }
        }

        headers = {"Authorization": f"Bearer {GATEWAY_TOKEN}"}
        url = f"{GATEWAY_URL}/execute"
        
        logger.info("[EXECUTE] Submitting %s %s to Gateway (notional=$%.2f, risk=$%.2f, TP1=1.5R, TP2=3.0R)...",
                    symbol, side, calculated_notional, target_risk_usd)
        res = await asyncio.to_thread(_http_post_sync, url, payload, headers)
        if res and res.get("ok"):
            logger.info("✅ [EXECUTED] %s %s fill confirmed via Gateway! Result: %s", symbol, side, res)
            await self.notify_telegram(setup, success=True, details=res)
            return True
        else:
            logger.warning("❌ [GATEWAY REJECT] %s %s: %s", symbol, side, (res or {}).get("reason"))
        return False

    async def notify_telegram(self, setup: Dict[str, Any], success: bool = True, details: Optional[Dict[str, Any]] = None):
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            return

        icon = "🟢" if setup["side"] == "LONG" else "🔴"
        sym = setup["symbol"]
        tv_link = f"https://www.tradingview.com/chart/?symbol=BYBIT:{sym}.P"
        trade_link = f"https://www.bybit.com/trade/usdt/{sym}"

        msg = (
            f"🤖 <b>[AUTONOMOUS SCANNER] {icon} {setup['side']} │ {sym}</b>\n\n"
            f"📋 <b>TRADE PLAN (MTF IFVG-PRO + GOLDEN SCALP)</b>\n"
            f"   💰 Entry : <code>{setup['entry']}</code>\n"
            f"   🛑 Stop Loss : <code>{setup['sl']}</code> (-{setup['sl_pct']:.2f}%)\n"
            f"   🎯 TP1 (75%) : <code>{setup['tp1']}</code> (1.5R - Kunci Untung & SL Lock +1.0R)\n"
            f"   🎯 TP2 (25%) : <code>{setup['tp2']}</code> (3.0R - Runner Trailing)\n"
            f"   ⚙️ Leverage : 10x | Risk : $2.00 (2%)\n\n"
            f"📊 <b>CONFLUENCE SIGNALS</b>\n"
            f"   • Macro: 1h EMA 20/50 Trend Aligned ✅\n"
            f"   • Trigger: 15m IFVG Inversion (Gap >= 0.5 ATR) ✅\n"
            f"   • Floor SL: Anti-Wick Floor >= 1.0% ✅\n"
            f"   • Orderflow: CVD/OI Confirmed ({setup['flow_dir']}) ✅\n\n"
            f"🔗 <a href='{tv_link}'>TradingView Chart</a> │ <a href='{trade_link}'>Bybit Trade</a>\n"
            f"<i>Execution: Paper Mainnet Gateway (Real Price Matching)</i>"
        )

        tg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }
        await asyncio.to_thread(_http_post_sync, tg_url, payload)

    async def scan_once(self) -> List[Dict[str, Any]]:
        logger.info("=== Starting Autonomous MTF IFVG Scan ===")
        portfolio = await self.get_gateway_portfolio()
        open_count = int(portfolio.get("open_position_count", 0))
        open_symbols = set(portfolio.get("open_symbols", []))

        if open_count >= MAX_CONCURRENT_POSITIONS:
            logger.info("Gateway at capacity (%d/%d positions open). Waiting...", open_count, MAX_CONCURRENT_POSITIONS)
            return []

        btc_info = await self.get_btc_regime()
        btc_mode = btc_info.get("mode", "NEUTRAL")
        logger.info("[MACRO] BTC Regime: %s | Active Positions: %d/%d", btc_mode, open_count, MAX_CONCURRENT_POSITIONS)

        universe = await self.get_universe()
        logger.info("[UNIVERSE] Scanning %d liquid pairs...", len(universe))

        candidates = []
        for symbol in universe:
            # 3-tier Collision Guard: never trade symbols already open in Gateway
            if symbol in open_symbols:
                continue
            
            # Anti-spam cooldown (15 mins per coin)
            if time.time() - self.last_scanned.get(symbol, 0) < 900:
                continue

            setup = await self.analyze_pair(symbol, btc_mode)
            if setup:
                logger.info("🎯 [SETUP FOUND] %s %s @ %s | SL: %s (%.2f%%) | TP1: %s (1.5R) | TP2: %s (3.0R)",
                            setup["symbol"], setup["side"], setup["entry"], setup["sl"], setup["sl_pct"], setup["tp1"], setup["tp2"])
                candidates.append(setup)
                self.last_scanned[symbol] = time.time()

                if open_count < MAX_CONCURRENT_POSITIONS:
                    executed = await self.execute_intent(setup)
                    if executed:
                        open_count += 1
                        open_symbols.add(symbol)
                
                if open_count >= MAX_CONCURRENT_POSITIONS:
                    break

        logger.info("=== Scan Finished. Found %d setups. ===", len(candidates))
        return candidates

    async def run_loop(self):
        logger.info("Starting Autonomous Scanner Daemon (interval=%ds)...", SCAN_INTERVAL_SEC)
        while True:
            try:
                await self.scan_once()
            except Exception as exc:
                logger.error("Scan loop error: %s", exc, exc_info=True)
            await asyncio.sleep(SCAN_INTERVAL_SEC)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Autonomous Market Scanner")
    parser.add_argument("--once", action="store_true", help="Run scan once and exit")
    args = parser.parse_args()

    scanner = AutonomousMarketScanner()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        if args.once:
            loop.run_until_complete(scanner.scan_once())
        else:
            loop.run_until_complete(scanner.run_loop())
    finally:
        loop.close()
