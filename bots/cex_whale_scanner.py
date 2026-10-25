#!/usr/bin/env python3
"""cex_whale_scanner.py — Real-time Binance & Bybit CEX Whale & Liquidation Scanner.

Architecture:
1. Realtime Streams:
   - Binance Futures All-Market Liquidation Stream (!forceOrder@arr) via /market endpoint.
   - Binance Futures Top-100 Mega Trades (<symbol>@aggTrade) via /market endpoint.
   - Bybit Linear Public Trades WebSocket.
2. State & Confluence:
   - Writes runtime/whales/latest_cex_whale_{symbol}.json instantly for Signal Copy & Radar confluence.
   - Appends to runtime/whales/cex_whale_events.jsonl.
3. Periodic 30-Minute Intelligence Report (No Spam!):
   - Aggregates all trades & liquidations over the 30-minute window.
   - Ranks Top Accumulation (Net Buy) vs Top Distribution (Net Sell).
   - Summarizes Market Liquidation Pressures (Short Squeeze vs Long Flush).
   - Generates clickable TradingView chart links (https://www.tradingview.com/chart/?symbol=BYBIT:{symbol}.P).
   - Broadcasts report to BOTH Telegram and Discord.
4. Ultra-Mega Instant Alert (High Filter):
   - Only triggers individual instant alert for single extreme trades >= $2,000,000 or single liquidations >= $1,000,000.
"""

from __future__ import annotations

import asyncio
import collections
import html
import json
import logging
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import websockets

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("cex_whale_scanner")

# ---------------- Configuration ----------------
BOT_TOKEN = os.getenv("WHALE_REPORT_BOT_TOKEN", "")
CHAT_ID = os.getenv("WHALE_REPORT_CHAT_ID", "578305627")
DISCORD_WEBHOOK = os.getenv(
    "WHALE_REPORT_DISCORD_WEBHOOK",
    ""
)

# Runtime paths
RUNTIME_DIR = Path(os.getenv("WHALE_RUNTIME_DIR", "/opt/signalcopyreal/runtime/whales"))
if not RUNTIME_DIR.exists():
    RUNTIME_DIR = Path("/home/fusion_omega/fusion_omega_nexus/runtime/whales")
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

# Thresholds in USD
TRADE_TRACK_USD = float(os.getenv("CEX_WHALE_TRADE_USD", "150000"))       # $150K to track in 30m rollup
LIQ_TRACK_USD = float(os.getenv("CEX_WHALE_LIQ_USD", "50000"))           # $50K to track in 30m rollup

# Instant Alert Filters (Strictly Ultra-High to prevent spam)
INSTANT_TRADE_USD = float(os.getenv("CEX_INSTANT_TRADE_USD", "2000000"))  # $2M+ for instant alert
INSTANT_LIQ_USD = float(os.getenv("CEX_INSTANT_LIQ_USD", "1000000"))      # $1M+ for instant liquidation

REPORT_INTERVAL_SEC = int(os.getenv("CEX_REPORT_INTERVAL_SEC", "1800"))   # 1800s = 30 minutes
TOP_SYMBOLS_COUNT = int(os.getenv("CEX_WHALE_TOP_SYMBOLS", "100"))

WIB = timezone(timedelta(hours=7))


# ---------------- Formatting Helpers ----------------

def format_usd(val: float) -> str:
    """Format USD value nicely (e.g. $1.25M, $350K)."""
    val = float(val)
    if val >= 1_000_000_000:
        return f"${val / 1_000_000_000:.2f}B"
    if val >= 1_000_000:
        return f"${val / 1_000_000:.2f}M"
    if val >= 1_000:
        return f"${val / 1_000:.1f}K"
    return f"${val:,.0f}"


def format_price(p: float) -> str:
    """Format crypto price with dynamic decimals."""
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}"
    if p >= 0.001:
        return f"{p:.6f}"
    return f"{p:.8f}"


def tv_link(symbol: str) -> str:
    """Generate TradingView chart link formatted as requested."""
    clean_sym = symbol.upper().replace("/", "").replace("-", "")
    if clean_sym.endswith(".P"):
        clean_sym = clean_sym[:-2]
    if clean_sym.endswith("USDT"):
        return f"https://www.tradingview.com/chart/?symbol=BYBIT:{clean_sym}.P"
    return f"https://www.tradingview.com/chart/?symbol=BYBIT:{clean_sym}USDT.P"


# ---------------- Telegram & Discord Dispatcher ----------------

def send_telegram(text: str) -> bool:
    """Send formatted HTML alert to Telegram."""
    if not BOT_TOKEN or not CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=payload, headers={"User-Agent": "cex-whale-scanner/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()).get("ok", False)
    except Exception as exc:
        logger.warning("Telegram send failed: %s", exc)
        return False


def send_discord(text: str) -> bool:
    """Send alert to Discord webhook if configured, converting HTML to Markdown."""
    if not DISCORD_WEBHOOK:
        return False
    md = re.sub(r'<b>(.*?)</b>', r'**\1**', text)
    md = re.sub(r'<i>(.*?)</i>', r'*\1*', md)
    md = re.sub(r'<a href="(.*?)">(.*?)</a>', r'[\2](\1)', md)
    md = html.unescape(md)

    data = json.dumps({"content": md[:2000]}).encode("utf-8")
    try:
        req = urllib.request.Request(DISCORD_WEBHOOK, data=data, headers={
            "Content-Type": "application/json",
            "User-Agent": "cex-whale-scanner/1.0",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 204
    except Exception as exc:
        logger.warning("Discord send failed: %s", exc)
        return False


def notify(text: str):
    """Broadcast alert to BOTH Telegram and Discord."""
    tg_ok = send_telegram(text)
    dc_ok = send_discord(text)
    logger.info("Broadcast sent: tg=%s, dc=%s", tg_ok, dc_ok)


# ---------------- State & Event Recording ----------------

def record_whale_event(event: Dict[str, Any]):
    """Save latest event file atomically + append to jsonl."""
    symbol = event["symbol"]
    event["timestamp_utc"] = datetime.now(timezone.utc).isoformat()

    # 1. Append to jsonl
    jsonl_path = RUNTIME_DIR / "cex_whale_events.jsonl"
    try:
        with open(jsonl_path, "a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception as exc:
        logger.error("Failed to write to jsonl: %s", exc)

    # 2. Atomic write latest per symbol for engine confluence
    latest_path = RUNTIME_DIR / f"latest_cex_whale_{symbol}.json"
    tmp_path = latest_path.with_suffix(".tmp")
    try:
        with open(tmp_path, "w") as f:
            json.dump(event, f, indent=2)
        os.replace(tmp_path, latest_path)
    except Exception as exc:
        logger.error("Failed to write latest whale json for %s: %s", symbol, exc)


# ---------------- 30-Minute Rolling Aggregator ----------------

class Rolling30mAggregator:
    """Maintains a rolling 30-minute window of CEX trades and liquidations."""

    def __init__(self, window_sec: int = REPORT_INTERVAL_SEC):
        self.window_sec = window_sec
        self.trades: collections.deque = collections.deque()
        self.liquidations: collections.deque = collections.deque()
        self.last_report_ts = time.time()

    def add_trade(self, symbol: str, val_usd: float, is_buy: bool, price: float, exchange: str):
        now = time.time()
        self.trades.append({
            "time": now,
            "symbol": symbol.upper(),
            "val_usd": val_usd,
            "is_buy": is_buy,
            "price": price,
            "exchange": exchange,
        })
        self._prune(now)

    def add_liquidation(self, symbol: str, val_usd: float, is_short_liq: bool, price: float):
        now = time.time()
        self.liquidations.append({
            "time": now,
            "symbol": symbol.upper(),
            "val_usd": val_usd,
            "is_short_liq": is_short_liq,
            "price": price,
        })
        self._prune(now)

    def _prune(self, now: float):
        cutoff = now - self.window_sec
        while self.trades and self.trades[0]["time"] < cutoff:
            self.trades.popleft()
        while self.liquidations and self.liquidations[0]["time"] < cutoff:
            self.liquidations.popleft()

    def build_report(self) -> Optional[str]:
        now = time.time()
        self._prune(now)

        if not self.trades and not self.liquidations:
            return None

        now_wib = datetime.now(WIB)
        start_wib = now_wib - timedelta(seconds=self.window_sec)

        # 1. Aggregate Trades per symbol
        sym_stats = collections.defaultdict(lambda: {
            "buy_vol": 0.0,
            "sell_vol": 0.0,
            "buy_count": 0,
            "sell_count": 0,
            "last_price": 0.0,
        })

        tot_trade_vol = 0.0
        for t in self.trades:
            s = t["symbol"]
            val = t["val_usd"]
            tot_trade_vol += val
            sym_stats[s]["last_price"] = t["price"]
            if t["is_buy"]:
                sym_stats[s]["buy_vol"] += val
                sym_stats[s]["buy_count"] += 1
            else:
                sym_stats[s]["sell_vol"] += val
                sym_stats[s]["sell_count"] += 1

        # 2. Aggregate Liquidations per symbol
        tot_short_liq = 0.0
        tot_long_liq = 0.0
        liq_stats = collections.defaultdict(lambda: {"short_liq": 0.0, "long_liq": 0.0})

        for l in self.liquidations:
            s = l["symbol"]
            val = l["val_usd"]
            if l["is_short_liq"]:
                tot_short_liq += val
                liq_stats[s]["short_liq"] += val
            else:
                tot_long_liq += val
                liq_stats[s]["long_liq"] += val

        # 3. Sort Accumulation (Net Buy) vs Distribution (Net Sell)
        scored_tokens = []
        for s, d in sym_stats.items():
            net = d["buy_vol"] - d["sell_vol"]
            tot = d["buy_vol"] + d["sell_vol"]
            buy_ratio = (d["buy_vol"] / tot * 100) if tot > 0 else 0
            scored_tokens.append({
                "symbol": s,
                "net": net,
                "buy_vol": d["buy_vol"],
                "sell_vol": d["sell_vol"],
                "tot_vol": tot,
                "buy_ratio": buy_ratio,
                "price": d["last_price"],
            })

        accums = sorted([x for x in scored_tokens if x["net"] > 0], key=lambda x: x["net"], reverse=True)[:5]
        dists = sorted([x for x in scored_tokens if x["net"] < 0], key=lambda x: x["net"])[:5]

        # 4. Top Liquidated symbols
        top_liqs = sorted(
            liq_stats.items(),
            key=lambda item: item[1]["short_liq"] + item[1]["long_liq"],
            reverse=True
        )[:4]

        # 5. Build HTML Report
        lines = [
            f"🐋 <b>CEX WHALE ORDERFLOW & LIQUIDATION REPORT (30m)</b>",
            f"🕐 {start_wib.strftime('%H:%M')} — {now_wib.strftime('%H:%M')} WIB · Binance & Bybit",
            f"📊 <b>Total Volume:</b> {format_usd(tot_trade_vol)} · {len(self.trades)} Order Paus",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]

        # Accumulation Section
        if accums:
            lines.append("🚀 <b>TOP AKUMULASI PAUS (Net Buy Dominan)</b>")
            for a in accums:
                fire = " 🔥" if a["buy_ratio"] >= 80 else ""
                lines.append(
                    f" • <b>{a['symbol']}</b>: +{format_usd(a['net'])} "
                    f"(Buy: {format_usd(a['buy_vol'])} | Sell: {format_usd(a['sell_vol'])}) — {a['buy_ratio']:.0f}% Buy{fire}"
                )
            lines.append("")

        # Distribution Section
        if dists:
            lines.append("🔴 <b>TOP DISTRIBUSI PAUS (Net Sell Dominan)</b>")
            for d in dists:
                warning = " ⚠️" if (100 - d["buy_ratio"]) >= 80 else ""
                lines.append(
                    f" • <b>{d['symbol']}</b>: −{format_usd(abs(d['net']))} "
                    f"(Sell: {format_usd(d['sell_vol'])} | Buy: {format_usd(d['buy_vol'])}) — {(100 - d['buy_ratio']):.0f}% Sell{warning}"
                )
            lines.append("")

        # Liquidation Section
        lines.append("💥 <b>RADAR LIKUIDASI PASAR</b>")
        lines.append(f" • <b>Short Squeezed:</b> {format_usd(tot_short_liq)} (Tekanan Beli Paksa)")
        lines.append(f" • <b>Long Flushed:</b> {format_usd(tot_long_liq)} (Tekanan Jual Paksa)")
        if top_liqs:
            liq_details = []
            for s, lq in top_liqs:
                parts = []
                if lq["short_liq"] > 0:
                    parts.append(f"Short {format_usd(lq['short_liq'])}")
                if lq["long_liq"] > 0:
                    parts.append(f"Long {format_usd(lq['long_liq'])}")
                liq_details.append(f"{s} ({'/'.join(parts)})")
            lines.append(f" • <b>Koin Terdampak:</b> {', '.join(liq_details)}")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")

        # TradingView Links for top coins
        highlight_coins = []
        for x in accums[:2]:
            highlight_coins.append(x["symbol"])
        for x in dists[:2]:
            if x["symbol"] not in highlight_coins:
                highlight_coins.append(x["symbol"])

        if highlight_coins:
            lines.append("📈 <b>Chart TradingView:</b>")
            for sym in highlight_coins:
                clean_name = sym.replace("USDT", "")
                lines.append(f" • <a href=\"{tv_link(sym)}\">{clean_name} ({sym})</a>")

        return "\n".join(lines)


aggregator = Rolling30mAggregator(window_sec=REPORT_INTERVAL_SEC)


# ---------------- Binance Top Symbols Fetcher ----------------

def fetch_binance_top_symbols(limit: int = TOP_SYMBOLS_COUNT) -> List[str]:
    """Fetch top USDT-M futures pairs sorted by 24h quote volume from Binance."""
    url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "cex-whale-scanner/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        usdt_pairs = [
            item for item in data
            if item.get("symbol", "").endswith("USDT")
        ]
        usdt_pairs.sort(key=lambda x: float(x.get("quoteVolume", 0) or 0), reverse=True)
        top = [item["symbol"].lower() for item in usdt_pairs[:limit]]
        logger.info("Fetched top %d Binance futures symbols. Top 5: %s", len(top), top[:5])
        return top
    except Exception as exc:
        logger.warning("Failed to fetch Binance top symbols (%s), using curated defaults", exc)
        return [
            "btcusdt", "ethusdt", "solusdt", "suiusdt", "dogeusdt", "xrpusdt", "aptusdt",
            "nearusdt", "pepeusdt", "wifusdt", "bchusdt", "linkusdt", "avaxusdt", "adausdt",
            "shibusdt", "fetusdt", "taousdt", "tonusdt", "bnbusdt", "trxusdt", "arbusdt",
            "opusdt", "injusdt", "seiusdt", "flokiusdt", "omgtusdt", "renderusdt", "filusdt"
        ]


# ---------------- Binance All-Market Liquidation Worker ----------------

async def binance_liquidation_worker():
    """Worker monitoring Binance Futures All-Market Liquidation Stream (!forceOrder@arr)."""
    ws_url = "wss://fstream.binance.com/market/ws/!forceOrder@arr"
    logger.info("Starting Binance Liquidation Worker (!forceOrder@arr)...")

    while True:
        try:
            async with websockets.connect(ws_url, ping_interval=20, max_size=10_485_760) as ws:
                logger.info("Connected to Binance Liquidation Stream")
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    order = data.get("o", {})
                    if not order:
                        continue

                    symbol = order.get("s", "")
                    side = order.get("S", "")  # SELL (Long liquidated) or BUY (Short liquidated)
                    qty = float(order.get("q", 0) or 0)
                    price = float(order.get("p", 0) or 0)
                    val_usd = qty * price

                    if val_usd < LIQ_TRACK_USD:
                        continue

                    is_short_liq = (side == "BUY")
                    bias = "BULLISH" if is_short_liq else "BEARISH"
                    action_title = "SHORT LIQUIDATED (Squeeze)" if is_short_liq else "LONG LIQUIDATED (Flush)"

                    # Feed 30-min aggregator
                    aggregator.add_liquidation(symbol, val_usd, is_short_liq, price)

                    # Record state for trading bots
                    event = {
                        "exchange": "BINANCE",
                        "type": "LIQUIDATION",
                        "symbol": symbol,
                        "bias": bias,
                        "value_usd": round(val_usd, 2),
                        "price": price,
                        "side": side,
                        "details": f"{symbol} {action_title} {format_usd(val_usd)} @ {format_price(price)}",
                    }
                    record_whale_event(event)

                    # Trigger instant alert ONLY if extreme mega liquidation
                    if val_usd >= INSTANT_LIQ_USD:
                        chart_url = tv_link(symbol)
                        emoji = "💥🚀" if is_short_liq else "🔴📉"
                        alert_text = (
                            f"{emoji} <b>ULTRA-MEGA LIQUIDATION ALERT</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"⚡ <b>{symbol}</b> — <b>{action_title}</b>\n"
                            f"💰 <b>Nilai Likuidasi:</b> {format_usd(val_usd)}\n"
                            f"💲 <b>Harga:</b> ${format_price(price)}\n"
                            f"🏛️ <b>Bursa:</b> Binance Futures\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"🔗 <a href=\"{chart_url}\">📈 Buka Chart di TradingView</a>"
                        )
                        notify(alert_text)

        except Exception as exc:
            logger.error("Binance Liquidation Worker error: %s. Reconnecting in 5s...", exc)
            await asyncio.sleep(5)


# ---------------- Binance Top Trades Worker ----------------

async def binance_trades_worker(symbols_subset: List[str], worker_id: int):
    """Worker monitoring Binance Futures aggregated trades for a subset of symbols."""
    streams = "/".join(f"{s}@aggTrade" for s in symbols_subset)
    ws_url = f"wss://fstream.binance.com/market/stream?streams={streams}"
    logger.info("Starting Binance Trades Worker #%d (%d symbols)...", worker_id, len(symbols_subset))

    while True:
        try:
            async with websockets.connect(ws_url, ping_interval=20, max_size=10_485_760) as ws:
                logger.info("Binance Trades Worker #%d connected to stream", worker_id)
                while True:
                    msg = await ws.recv()
                    payload = json.loads(msg)
                    data = payload.get("data", {})
                    if data.get("e") != "aggTrade":
                        continue

                    symbol = data.get("s", "")
                    price = float(data.get("p", 0) or 0)
                    qty = float(data.get("q", 0) or 0)
                    val_usd = price * qty

                    if val_usd < TRADE_TRACK_USD:
                        continue

                    # m == True: buyer is maker -> Aggressive Taker Sell (Market Sell)
                    # m == False: seller is maker -> Aggressive Taker Buy (Market Buy)
                    is_buyer_taker = not data.get("m", False)
                    bias = "BULLISH" if is_buyer_taker else "BEARISH"
                    action_title = "MARKET BUY" if is_buyer_taker else "MARKET SELL"

                    # Feed 30-min aggregator
                    aggregator.add_trade(symbol, val_usd, is_buyer_taker, price, "BINANCE")

                    # Record state for trading bots
                    event = {
                        "exchange": "BINANCE",
                        "type": "WHALE_TRADE",
                        "symbol": symbol,
                        "bias": bias,
                        "value_usd": round(val_usd, 2),
                        "price": price,
                        "is_taker_buy": is_buyer_taker,
                        "details": f"{symbol} {action_title} {format_usd(val_usd)} @ {format_price(price)}",
                    }
                    record_whale_event(event)

                    # Trigger instant alert ONLY if extreme mega order (>= $2M)
                    if val_usd >= INSTANT_TRADE_USD:
                        chart_url = tv_link(symbol)
                        emoji = "🟢🐋" if is_buyer_taker else "🔴🐋"
                        alert_text = (
                            f"{emoji} <b>ULTRA-MEGA WHALE ORDER (≥$2M)</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"💎 <b>{symbol}</b> — <b>{action_title}</b>\n"
                            f"💰 <b>Volume:</b> {format_usd(val_usd)}\n"
                            f"💲 <b>Harga:</b> ${format_price(price)}\n"
                            f"⚡ <b>Eksekusi:</b> {'Aggressive Market Buy' if is_buyer_taker else 'Aggressive Market Sell'}\n"
                            f"🏛️ <b>Bursa:</b> Binance Futures\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"🔗 <a href=\"{chart_url}\">📈 Buka Chart di TradingView</a>"
                        )
                        notify(alert_text)

        except Exception as exc:
            logger.error("Binance Trades Worker #%d error: %s. Reconnecting in 5s...", worker_id, exc)
            await asyncio.sleep(5)


# ---------------- Bybit Linear Trades Worker ----------------

async def bybit_trades_worker(symbols: List[str]):
    """Worker monitoring Bybit Linear public trades for top coins."""
    ws_url = "wss://stream.bybit.com/v5/public/linear"
    logger.info("Starting Bybit Linear Trades Worker (%d symbols)...", len(symbols))

    chunk_size = 10
    chunks = [symbols[i:i + chunk_size] for i in range(0, len(symbols), chunk_size)]

    while True:
        try:
            async with websockets.connect(ws_url, ping_interval=20, max_size=10_485_760) as ws:
                logger.info("Connected to Bybit Linear WebSocket")

                for ch in chunks:
                    args = [f"publicTrade.{s.upper()}" for s in ch]
                    sub_msg = {"op": "subscribe", "args": args}
                    await ws.send(json.dumps(sub_msg))
                    await asyncio.sleep(0.1)

                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    topic = data.get("topic", "")
                    if not topic.startswith("publicTrade."):
                        continue

                    trades = data.get("data", [])
                    for t in trades:
                        symbol = t.get("s", "")
                        side = t.get("S", "")  # Buy or Sell
                        price = float(t.get("p", 0) or 0)
                        qty = float(t.get("v", 0) or 0)
                        val_usd = price * qty

                        if val_usd < TRADE_TRACK_USD:
                            continue

                        is_buy = (side.upper() == "BUY")
                        bias = "BULLISH" if is_buy else "BEARISH"
                        action_title = "BYBIT WHALE BUY" if is_buy else "BYBIT WHALE SELL"

                        # Feed 30-min aggregator
                        aggregator.add_trade(symbol, val_usd, is_buy, price, "BYBIT")

                        # Record state for trading bots
                        event = {
                            "exchange": "BYBIT",
                            "type": "WHALE_TRADE",
                            "symbol": symbol,
                            "bias": bias,
                            "value_usd": round(val_usd, 2),
                            "price": price,
                            "is_taker_buy": is_buy,
                            "details": f"{symbol} {action_title} {format_usd(val_usd)} @ {format_price(price)}",
                        }
                        record_whale_event(event)

                        # Trigger instant alert ONLY if extreme mega order (>= $2M)
                        if val_usd >= INSTANT_TRADE_USD:
                            chart_url = tv_link(symbol)
                            emoji = "🟢🐋" if is_buy else "🔴🐋"
                            alert_text = (
                                f"{emoji} <b>ULTRA-MEGA WHALE ORDER (≥$2M)</b>\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"💎 <b>{symbol}</b> — <b>{action_title}</b>\n"
                                f"💰 <b>Volume:</b> {format_usd(val_usd)}\n"
                                f"💲 <b>Harga:</b> ${format_price(price)}\n"
                                f"⚡ <b>Eksekusi:</b> {'Bybit Aggressive Buy' if is_buy else 'Bybit Aggressive Sell'}\n"
                                f"🏛️ <b>Bursa:</b> Bybit Linear\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"🔗 <a href=\"{chart_url}\">📈 Buka Chart di TradingView</a>"
                            )
                            notify(alert_text)

        except Exception as exc:
            logger.error("Bybit Trades Worker error: %s. Reconnecting in 5s...", exc)
            await asyncio.sleep(5)


# ---------------- 30-Minute Report Scheduler Worker ----------------

async def report_scheduler_worker():
    """Worker that broadcasts the 30-minute rollup report to Telegram and Discord."""
    logger.info("Starting 30-minute report scheduler (interval: %ds)...", REPORT_INTERVAL_SEC)
    
    # Broadcast startup greeting
    startup_msg = (
        f"🐋 <b>CEX Whale Scanner Engine Aktif</b>\n"
        f"• <b>Laporan Berkala:</b> Setiap {REPORT_INTERVAL_SEC // 60} Menit (Telegram + Discord)\n"
        f"• <b>Format Link:</b> TradingView Bybit (.P)\n"
        f"• <b>Alert Instan:</b> HANYA jika Order Raksasa ≥ $2.0M atau Likuidasi ≥ $1.0M\n"
        f"• <b>Integrasi Sinyal:</b> Grade A+ (Whale Backed) otomatis aktif."
    )
    notify(startup_msg)

    while True:
        try:
            await asyncio.sleep(30)
            elapsed = time.time() - aggregator.last_report_ts
            if elapsed >= REPORT_INTERVAL_SEC:
                report_html = aggregator.build_report()
                if report_html:
                    logger.info("Broadcasting 30-minute CEX Whale Report...")
                    notify(report_html)
                else:
                    logger.info("30-minute window quiet; no report needed.")
                aggregator.last_report_ts = time.time()
        except Exception as exc:
            logger.error("Error in report scheduler: %s", exc)
            await asyncio.sleep(10)


# ---------------- Main Orchestrator ----------------

async def main():
    logger.info("==========================================================")
    logger.info("  CEX WHALE SCANNER ENGINE (30m Rollup + Discord & TG)    ")
    logger.info("  Tracking: Trade >= $150K | Liq >= $50K                  ")
    logger.info("  Instant Alerts: Trade >= $2.0M | Liq >= $1.0M           ")
    logger.info("==========================================================")

    # 1. Fetch top symbols for Binance trades
    top_symbols = fetch_binance_top_symbols(limit=TOP_SYMBOLS_COUNT)

    # Split into 2 workers for Binance streams
    mid = len(top_symbols) // 2
    binance_group1 = top_symbols[:mid]
    binance_group2 = top_symbols[mid:]

    # Bybit top 30 symbols
    bybit_symbols = top_symbols[:30]

    # 2. Launch background coroutines
    tasks = [
        asyncio.create_task(binance_liquidation_worker()),
        asyncio.create_task(binance_trades_worker(binance_group1, worker_id=1)),
        asyncio.create_task(binance_trades_worker(binance_group2, worker_id=2)),
        asyncio.create_task(bybit_trades_worker(bybit_symbols)),
        asyncio.create_task(report_scheduler_worker()),
    ]

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Scanner terminated by user.")
