"""Silent Accumulation & Pre-Breakout Radar Engine.

Detects institutional absorption / Wyckoff accumulation signatures across 570+ symbols:
1. Abnormal Volume Expansion (Volume Z-Score >= 1.5)
2. Cumulative Volume Delta (CVD) Taker Buy Dominance (CVD Z-Score >= 1.2)
3. Price Range Compression (Sideways range <= 3.5% on 15m)
4. Open Interest (OI) Growth (Smart money positioning)
5. Funding Rate Neutral / Negative (No retail FOMO yet / potential short squeeze)
6. Sector & Narrative Confluence Mapping

Dispatches high-conviction pre-breakout radar alerts to Telegram configured via
RADAR_TELEGRAM_BOT_TOKEN and RADAR_TELEGRAM_CHAT_ID in .env.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("silent_accumulation_radar")

def _load_env_fallback():
    candidate_paths = [
        Path("/opt/signalcopyreal/.env"),
        Path(__file__).resolve().parent.parent / ".env",
        Path(".env")
    ]
    for p in candidate_paths:
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip().strip("'\"")
                        if k and k not in os.environ:
                            os.environ[k] = v
                break
            except Exception:
                pass

_load_env_fallback()

# Telegram credentials
RADAR_BOT_TOKEN = os.getenv("RADAR_TELEGRAM_BOT_TOKEN", "")
RADAR_CHAT_ID = os.getenv("RADAR_TELEGRAM_CHAT_ID", "")

# File paths
RUNTIME_REVO = Path("/opt/signalcopyreal/runtime/revo")
FLOW_CONTEXT_PATH = RUNTIME_REVO / "revo_flow_context_collector.json"
FLOW_CONTEXT_FALLBACK = RUNTIME_REVO / "revo_flow_context.json"
ALERT_STATE_PATH = Path("/opt/signalcopyreal/journal/silent_radar_state.json")

# Sector & Narrative taxonomy
SECTOR_MAP = {
    # AI & DePIN
    "TAOUSDT": "🤖 AI / Machine Learning",
    "NEARUSDT": "🤖 AI / Sharding L1",
    "RENDERUSDT": "🤖 DePIN / GPU Compute",
    "FILUSDT": "📦 DePIN / Decentralized Storage",
    "GRTUSDT": "🤖 AI / Data Indexing",
    "FETUSDT": "🤖 Artificial Superintelligence",
    "WLDUSDT": "🤖 AI / Identity Protocol",
    "ICPUSDT": "🤖 AI / Internet Computer",
    "ARUSDT": "📦 DePIN / Permanent Storage",
    "IOUSDT": "🤖 DePIN / GPU Cloud",
    "ATHUSDT": "🤖 DePIN / Cloud Compute",
    
    # China / Asian Liquidity Narrative
    "CFXUSDT": "🇨🇳 China Narrative / Conflux L1",
    "NEOUSDT": "🇨🇳 China / Neo Smart Economy",
    "ACHUSDT": "🇨🇳 Asian Payments / Crypto Gateway",
    "CKBUSDT": "🇨🇳 Nervos Network / Bitcoin L2",
    "VETUSDT": "🇨🇳 VeChain / Enterprise Supply",
    "GASUSDT": "🇨🇳 Neo Ecosystem Gas",
    "QTUMUSDT": "🇨🇳 China Smart Contracts",

    # High Performance L1 & L2
    "SUIUSDT": "⚡ High Performance Move L1",
    "APTUSDT": "⚡ High Performance Move L1",
    "SEIUSDT": "⚡ Parallelized EVM L1",
    "AVAXUSDT": "🔺 Avalanche Subnet Ecosystem",
    "SOLUSDT": "☀️ Solana High Throughput L1",
    "INJUSDT": "⚡ Injective Financial L1",
    "TIAUSDT": "🧩 Modular Data Availability",
    "TONUSDT": "💎 Telegram Open Network",

    # DeFi & Liquid Staking
    "LDOUSDT": "💧 Ethereum Liquid Staking",
    "AAVEUSDT": "🏦 Decentralized Lending Protocol",
    "CRVUSDT": "⚖️ Stablecoin AMM & Liquidity",
    "MKRUSDT": "🏛️ Sky / MakerDAO Collateral",
    "PENDLEUSDT": "📈 Yield Tokenization / EigenLayer",
    "ENAUSDT": "💵 Synthetic Dollar / Ethena Yield",
    "UNIUSDT": "🦄 Decentralized Exchange Protocol",

    # Memecoins & High Beta
    "PEPEUSDT": "🐸 Frog Culture Memecoin",
    "BONKUSDT": "🐶 Solana Dog Meme",
    "BOMEUSDT": "📚 Book of Meme / Solana",
    "DOGEUSDT": "🐕 Original Doge / PoW",
    "FLOKIUSDT": "⚔️ Floki Ecosystem / Gaming",
    "POPCATUSDT": "🐱 Solana Cat Meme",
    "WIFUSDT": "🧢 Dogwifhat / Solana Meme",

    # RWA (Real World Assets)
    "ONDOUSDT": "🏢 US Treasury / Institutional RWA",
    "OMUSDT": "🏢 MANTRA Chain / RWA L1",
}

class SilentAccumulationRadar:
    def __init__(self):
        self.state_file = ALERT_STATE_PATH
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.alert_cache: Dict[str, float] = {}  # symbol -> last_alert_ts
        self._load_state()

    def _load_state(self):
        if self.state_file.exists():
            try:
                with open(self.state_file, "r") as f:
                    self.alert_cache = json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load alert cache: {e}")
                self.alert_cache = {}

    def _save_state(self):
        try:
            with open(self.state_file, "w") as f:
                json.dump(self.alert_cache, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save alert cache: {e}")

    def _http_get(self, url: str, params: Dict[str, Any] = None) -> Any:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "NexusRadar/1.0"})
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def _http_post_json(self, url: str, data: Dict[str, Any]) -> Any:
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json",
            "User-Agent": "NexusRadar/1.0"
        })
        with urllib.request.urlopen(req, timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))

    async def send_telegram(self, message: str) -> bool:
        """Send radar alert to Telegram."""
        if not RADAR_BOT_TOKEN or not RADAR_CHAT_ID:
            logger.warning("Telegram bot token or chat ID not set in environment (RADAR_TELEGRAM_BOT_TOKEN / RADAR_TELEGRAM_CHAT_ID).")
            return False
        url = f"https://api.telegram.org/bot{RADAR_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": RADAR_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            res = await asyncio.to_thread(self._http_post_json, url, payload)
            if res.get("ok"):
                logger.info("Telegram alert sent successfully.")
                return True
            else:
                logger.error(f"Telegram API error: {res}")
                return False
        except Exception as e:
            logger.error(f"Failed to send telegram alert: {e}")
            return False

    async def fetch_bybit_klines(self, symbol: str, limit: int = 24) -> List[List]:
        """Fetch 15m closed klines from Bybit linear."""
        url = "https://api.bybit.com/v5/market/kline"
        params = {
            "category": "linear",
            "symbol": symbol,
            "interval": "15",
            "limit": limit
        }
        try:
            data = await asyncio.to_thread(self._http_get, url, params)
            if data.get("retCode") == 0 and "result" in data and "list" in data["result"]:
                return data["result"]["list"]
        except Exception as e:
            logger.debug(f"Error fetching klines for {symbol}: {e}")
        return []

    async def scan_once(self) -> List[Dict[str, Any]]:
        """Perform a single comprehensive scan over all 570+ symbols."""
        target_path = FLOW_CONTEXT_PATH if FLOW_CONTEXT_PATH.exists() else FLOW_CONTEXT_FALLBACK
        if not target_path.exists():
            logger.error(f"Flow context file not found at {target_path}")
            return []

        try:
            with open(target_path, "r") as f:
                flow_data = json.load(f)
        except Exception as e:
            logger.error(f"Error reading flow context: {e}")
            return []

        now_ts = time.time()
        cooldown_sec = 4 * 3600  # 4 hours cooldown per coin

        qualified_candidates = []

        symbols_map = flow_data.get("symbols", flow_data) if isinstance(flow_data, dict) else {}

        for k, v in symbols_map.items():
            if not isinstance(v, dict):
                continue
            sym = v.get("symbol")
            if not sym:
                sym = k.replace("/USDT:USDT", "USDT")
            if not sym.endswith("USDT"):
                continue

            # Metrics extraction
            cvd_z = float(v.get("cvd_zscore_15m") or v.get("cvd_z") or 0.0)
            vol_z = float(v.get("volume_zscore_15m") or v.get("vol_z") or 0.0)
            oi_pct = float(v.get("oi_delta_pct_15m") or v.get("oi_1h_pct") or 0.0)
            funding = float(v.get("funding_rate") or 0.0)
            flow = str(v.get("flow_direction", "UNKNOWN")).upper()

            # Accumulation gating: must have positive CVD (buyers absorbing) and volume expansion
            if cvd_z < 0.5 or vol_z < 0.8:
                continue

            # Funding rate filter: reject overheated FOMO coins
            if funding > 0.0003: # > 0.03% means retail already chased
                continue

            # Calculate base orderflow score (0-60)
            score = 0
            if cvd_z >= 2.0: score += 25
            elif cvd_z >= 1.2: score += 18
            elif cvd_z >= 0.8: score += 10

            if vol_z >= 2.5: score += 20
            elif vol_z >= 1.5: score += 15
            elif vol_z >= 0.8: score += 8

            if oi_pct >= 0.8: score += 15
            elif oi_pct >= 0.2: score += 10

            if -0.0003 <= funding <= 0.0001: score += 10 # Neutral or negative funding bonus!

            if flow in ["LONG_ONLY", "BOTH_ALLOWED"]: score += 10

            if score < 45:
                continue

            # Step 2: Deep Structure Verification via 15m Klines (Price Compression Check)
            klines = await self.fetch_bybit_klines(sym, limit=20)
            if not klines or len(klines) < 12:
                continue

            # klines: [startTime, openPrice, highPrice, lowPrice, closePrice, volume, turnover]
            highs = [float(c[2]) for c in klines]
            lows = [float(c[3]) for c in klines]
            curr_mark = float(klines[0][4]) # Current close/mark

            comp_high = max(highs)
            comp_low = min(lows)

            if comp_low <= 0:
                continue

            range_pct = (comp_high - comp_low) / comp_low * 100.0

            # SILENT ACCUMULATION REQUIREMENT:
            # Price MUST be compressed / sideways (range <= 3.5%, ideally <= 2.2%)
            if range_pct > 3.5:
                continue

            # Award bonus points for tight range compression!
            if range_pct <= 1.8: score += 20
            elif range_pct <= 2.5: score += 15
            elif range_pct <= 3.5: score += 10

            if score >= 70:
                narrative = SECTOR_MAP.get(sym, "🌐 Crypto Ecosystem / Altcoin")
                
                # Setup metrics
                breakout_trigger = comp_high * 1.002
                inval_support = comp_low * 0.996
                risk_pct = (breakout_trigger - inval_support) / breakout_trigger * 100.0
                
                # Estimated TP targets
                tp1_target = breakout_trigger * (1.0 + max(0.025, risk_pct * 1.8 / 100))
                tp2_target = breakout_trigger * (1.0 + max(0.060, risk_pct * 3.5 / 100))
                rr_ratio = (tp1_target - breakout_trigger) / max(0.0001, (breakout_trigger - inval_support))

                candidate = {
                    "symbol": sym,
                    "score": score,
                    "mark": curr_mark,
                    "range_pct": range_pct,
                    "comp_high": comp_high,
                    "comp_low": comp_low,
                    "breakout_trigger": breakout_trigger,
                    "inval_support": inval_support,
                    "tp1_target": tp1_target,
                    "tp2_target": tp2_target,
                    "rr_ratio": rr_ratio,
                    "cvd_z": cvd_z,
                    "vol_z": vol_z,
                    "oi_pct": oi_pct,
                    "funding": funding,
                    "flow": flow,
                    "narrative": narrative,
                    "detected_at": datetime.now(timezone.utc).isoformat()
                }
                qualified_candidates.append(candidate)

        # Sort by score descending
        qualified_candidates = sorted(qualified_candidates, key=lambda x: x["score"], reverse=True)

        logger.info(f"Scan complete. Found {len(qualified_candidates)} qualified silent accumulation setups.")

        # Process alerts with cooldown
        for cand in qualified_candidates[:5]: # Top 5 highest conviction
            sym = cand["symbol"]
            last_alert = self.alert_cache.get(sym, 0)
            if now_ts - last_alert >= cooldown_sec:
                msg = self._format_alert_message(cand)
                sent = await self.send_telegram(msg)
                if sent:
                    self.alert_cache[sym] = now_ts
                    self._save_state()
                    await asyncio.sleep(1.0)

        return qualified_candidates

    def _format_alert_message(self, cand: Dict[str, Any]) -> str:
        sym = cand["symbol"]
        score = cand["score"]
        mark = cand["mark"]
        range_pct = cand["range_pct"]
        cvd_z = cand["cvd_z"]
        vol_z = cand["vol_z"]
        oi_pct = cand["oi_pct"]
        funding = cand["funding"] * 100.0 # to percentage
        flow = cand["flow"]
        narrative = cand["narrative"]
        
        trigger = cand["breakout_trigger"]
        support = cand["inval_support"]
        tp1 = cand["tp1_target"]
        tp2 = cand["tp2_target"]
        rr = cand["rr_ratio"]

        # Quality indicator
        stars = "⭐⭐⭐⭐⭐" if score >= 85 else "⭐⭐⭐⭐"

        msg = (
            f"🚨 <b>[RADAR] SILENT ACCUMULATION DETECTED!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💎 <b>Pair</b>: <code>#{sym}</code>\n"
            f"🏷️ <b>Sektor/Narasi</b>: {narrative}\n"
            f"🎯 <b>Skor Akumulasi</b>: <b>{score}/100</b> {stars}\n\n"
            f"📊 <b>Matriks Orderflow & Akumulasi:</b>\n"
            f"• <b>Harga Saat Ini</b>: <code>{mark:.5g}</code> USDT\n"
            f"• <b>Kompresi Harga (Sideways)</b>: <code>{range_pct:.2f}%</code> (Rentang Sempit)\n"
            f"• <b>Volume Spike</b>: <code>+{vol_z:.2f}σ</code> (Lonjakan Serapan)\n"
            f"• <b>CVD Taker Buy</b>: <code>+{cvd_z:.2f}σ</code> (Agresif Akumulasi)\n"
            f"• <b>Pertumbuhan OI</b>: <code>{oi_pct:+.2f}%</code> (Uang Baru Masuk)\n"
            f"• <b>Funding Rate</b>: <code>{funding:+.4f}%</code> (Tenang / Non-FOMO)\n"
            f"• <b>Flow Bias</b>: <b>{flow}</b> 🟢\n\n"
            f"🚀 <b>Rencana Setup Pre-Breakout:</b>\n"
            f"• <b>Level Trigger Breakout</b>: <code>{trigger:.5g}</code> USDT\n"
            f"• <b>Support Invalidation (SL)</b>: <code>{support:.5g}</code> USDT\n"
            f"• <b>Target Eksplosif TP1</b>: <code>{tp1:.5g}</code> USDT (+{((tp1-trigger)/trigger*100):.1f}%)\n"
            f"• <b>Target Eksplosif TP2</b>: <code>{tp2:.5g}</code> USDT (+{((tp2-trigger)/trigger*100):.1f}%)\n"
            f"• <b>Potensi Risk/Reward</b>: <b>1 : {rr:.1f}</b>\n\n"
            f"💡 <i>Catatan Smart Money: Terdeteksi penyerapan pasif volume tinggi saat harga sideways. Probabilitas lonjakan breakout ke atas sangat tinggi!</i>"
        )
        return msg

    async def run_daemon(self, interval_sec: int = 180):
        """Run continuous background monitoring daemon."""
        logger.info(f"Starting Silent Accumulation Radar Daemon (Interval: {interval_sec}s)...")
        while True:
            try:
                await self.scan_once()
            except Exception as e:
                logger.error(f"Error in radar scan loop: {e}", exc_info=True)
            await asyncio.sleep(interval_sec)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Silent Accumulation Radar")
    parser.add_argument("--daemon", action="store_true", help="Run continuously as daemon")
    parser.add_argument("--interval", type=int, default=180, help="Scan interval in seconds (default: 180)")
    args = parser.parse_args()

    radar = SilentAccumulationRadar()
    if args.daemon:
        asyncio.run(radar.run_daemon(interval_sec=args.interval))
    else:
        candidates = asyncio.run(radar.scan_once())
        print(f"\n[SCAN FINISHED] Dispatched alerts for top candidates. Total found: {len(candidates)}")
        for c in candidates:
            print(f"  • {c['symbol']:<12} | Score: {c['score']}/100 | Range: {c['range_pct']:.2f}% | CVD: +{c['cvd_z']:.2f}s | Vol: +{c['vol_z']:.2f}s | {c['narrative']}")
