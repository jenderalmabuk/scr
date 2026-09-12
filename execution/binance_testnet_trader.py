# execution/binance_testnet_trader.py - Binance Futures Testnet Trader
# Compatible with orchestration/market_loop.py interface (paper_trader API)
from __future__ import annotations

import asyncio
import hashlib
import hmac
import math
import time
import urllib.parse
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx

from signal_copy.execution_config import (
    HARD_MAX_HOLD_MINUTES,
    MAX_HOLD_TRENDING, MAX_HOLD_RANGING, MAX_HOLD_HIGH_VOL, MAX_HOLD_MINUTES,
    PROFIT_LOCK_MIN_MINUTES, PROFIT_LOCK_TRIGGER_PCT, PROFIT_LOCK_BUFFER_PCT,
    TRAIL_SL_PCT, TRAIL_SL_ATR_MULTIPLIER,
    is_coin_allowed,
    ORDER_CONFIRMATION_TIMEOUT,
    SCRATCH_EXIT_ENABLED, SCRATCH_EXIT_RANGING_MINUTES, SCRATCH_EXIT_TRENDING_MINUTES, SCRATCH_EXIT_MAX_ABS_PNL_PCT,
    DAMAGE_REDUCER_ENABLED, DAMAGE_REDUCER_MIN_HOLD_MINUTES, DAMAGE_REDUCER_MAX_LOSS_PCT,
    CLOSE_POSITIONS_ON_SHUTDOWN,
)
# from data.price_validator import update_bybit_price  # unused in nexus
# from telegram_notifier import send_trade_open  # unused in nexus
from utils.logger import logger
from .trade_journal import TradeJournalWriter

BINANCE_TESTNET_BASE = "https://testnet.binancefuture.com"
BINANCE_TESTNET_WS = "wss://stream.binancefuture.com"


class BinanceTestnetTrader:
    def __init__(self, api_key: str, api_secret: str, leverage: int = 10, margin_type: str = "CROSSED"):
        self.api_key = api_key
        self.api_secret = api_secret
        self.leverage = leverage
        self.margin_type = margin_type.upper()
        self.base_url = BINANCE_TESTNET_BASE

        self.positions: Dict[str, Dict[str, Any]] = {}
        self.trade_history: List[Dict[str, Any]] = []
        self.instruments_cache: Dict[str, Dict[str, Any]] = {}
        self.current_balance = 750.0
        self.last_balance_check = 0.0
        self.last_export_time = datetime.now(UTC)

        self._client: Optional[httpx.AsyncClient] = None
        self._router_task: Optional[asyncio.Task] = None
        self._reconcile_running = False
        self._journal = TradeJournalWriter()
        self._journal_started = False
        self._order_queue: asyncio.Queue = asyncio.Queue()

        logger.info("✅ Binance Futures Testnet Trader initialized")
        logger.info(f"   Leverage: {self.leverage}x | Margin: {self.margin_type}")

    async def _ensure_journal(self):
        if not self._journal_started:
            await self._journal.start()
            self._journal_started = True

    async def _journal_trade(self, trade: dict):
        self.trade_history.append(trade)
        try:
            await self._ensure_journal()
            await self._journal.write_trade(dict(trade))
        except Exception as exc:
            logger.warning("[JOURNAL] write_trade error: %s", exc)

    # ─── HTTP Client ───────────────────────────────────────────────────────

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    def _sign(self, params: Dict[str, Any]) -> Dict[str, Any]:
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 10000
        query = urllib.parse.urlencode(params)
        signature = hmac.HMAC(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        params["signature"] = signature
        return params

    def _headers(self) -> Dict[str, str]:
        return {"X-MBX-APIKEY": self.api_key}

    async def _request(self, method: str, path: str, params: Dict[str, Any] = None, signed: bool = True) -> Dict[str, Any]:
        client = await self._get_client()
        params = params or {}
        if signed:
            params = self._sign(params)
        url = f"{self.base_url}{path}"
        try:
            if method == "GET":
                resp = await client.get(url, params=params, headers=self._headers())
            elif method == "POST":
                resp = await client.post(url, params=params, headers=self._headers())
            elif method == "DELETE":
                resp = await client.delete(url, params=params, headers=self._headers())
            else:
                resp = await client.request(method, url, params=params, headers=self._headers())
            data = resp.json()
            if resp.status_code >= 400:
                logger.error(f"[BINANCE_API] {method} {path} → {resp.status_code}: {data}")
            return data
        except Exception as e:
            logger.error(f"[BINANCE_API] {method} {path} error: {e}")
            return {"code": -1, "msg": str(e)}

    # ─── Balance ───────────────────────────────────────────────────────────

    async def _update_balance(self) -> bool:
        try:
            data = await self._request("GET", "/fapi/v2/account")
            if isinstance(data, dict):
                # Use totalMarginBalance which includes unrealized PnL
                total_margin = float(data.get("totalMarginBalance", 0))
                available = float(data.get("availableBalance", 0))
                unrealized = float(data.get("totalUnrealizedProfit", 0))
                wallet = float(data.get("totalWalletBalance", 0))
                
                equity = total_margin if total_margin > 0 else wallet
                if equity <= 0.0 and self.current_balance > 0.0:
                    logger.warning(f"⚠️ Balance API returned ${equity:.2f}, keeping ${self.current_balance:.2f}")
                    return False
                self.current_balance = equity
                self.last_balance_check = time.time()
                logger.info(f"💰 Balance: ${wallet:.2f} | Unrealized: ${unrealized:+.2f} | Equity: ${equity:.2f}")
                return True
            
            # Fallback to /fapi/v2/balance endpoint
            data = await self._request("GET", "/fapi/v2/balance")
            if isinstance(data, list):
                for item in data:
                    if item.get("asset") == "USDT":
                        fetched = float(item.get("balance", 0))
                        if fetched <= 0.0 and self.current_balance > 0.0:
                            return False
                        self.current_balance = fetched
                        self.last_balance_check = time.time()
                        logger.info(f"💰 Balance: ${self.current_balance:.2f}")
                        return True
            logger.warning(f"[BINANCE] Balance response unexpected: {str(data)[:200]}")
            return False
        except Exception as e:
            logger.error(f"Balance fetch error: {e}")
            return False

    # ─── Instruments ───────────────────────────────────────────────────────

    async def get_instruments_info(self) -> List[Dict[str, Any]]:
        try:
            data = await self._request("GET", "/fapi/v1/exchangeInfo", signed=False)
            symbols = data.get("symbols", [])
            result = []
            for sym in symbols:
                if sym.get("contractType") != "PERPETUAL":
                    continue
                if sym.get("quoteAsset") != "USDT":
                    continue
                status = "Trading" if sym.get("status") == "TRADING" else sym.get("status")
                info = {
                    "symbol": sym["symbol"],
                    "status": status,
                    "pricePrecision": sym.get("pricePrecision", 4),
                    "quantityPrecision": sym.get("quantityPrecision", 3),
                    "filters": {f["filterType"]: f for f in sym.get("filters", [])},
                }
                self.instruments_cache[sym["symbol"]] = info
                result.append(info)
            logger.info(f"✅ Loaded {len(result)} Binance Futures USDT perpetuals")
            return result
        except Exception as e:
            logger.error(f"get_instruments_info error: {e}")
            return []

    # ─── Price & Qty Helpers ───────────────────────────────────────────────

    def _round_price(self, symbol, price):
        tick, decimals = self._price_tick(symbol)
        if tick > 0:
            steps = (Decimal(str(price)) / Decimal(str(tick))).quantize(Decimal(1), rounding=ROUND_HALF_UP)
            return float(round(steps * Decimal(str(tick)), decimals))
        return round(price, decimals)

    def _round_qty(self, symbol, qty):
        step, decimals = self._qty_step(symbol)
        if step > 0:
            steps = (Decimal(str(qty)) / Decimal(str(step))).quantize(Decimal(1), rounding=ROUND_DOWN)
            return float(round(steps * Decimal(str(step)), decimals))
        return float(Decimal(str(qty)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN))

    @staticmethod
    def _decimals_from_step(step):
        if not step or step <= 0:
            return 0
        return max(0, -int(Decimal(str(step)).normalize().as_tuple().exponent))

    def _qty_step(self, symbol):
        inst = self.instruments_cache.get(symbol)
        if not inst:
            return 0.0, 3
        step = float(inst.get("filters", {}).get("LOT_SIZE", {}).get("stepSize", 0) or 0)
        prec = int(inst.get("quantityPrecision", 3))
        return step, (self._decimals_from_step(step) if step > 0 else prec)

    def _price_tick(self, symbol):
        inst = self.instruments_cache.get(symbol)
        if not inst:
            return 0.0, 4
        tick = float(inst.get("filters", {}).get("PRICE_FILTER", {}).get("tickSize", 0) or 0)
        prec = int(inst.get("pricePrecision", 4))
        return tick, (self._decimals_from_step(tick) if tick > 0 else prec)

    def _fmt_qty(self, symbol, qty):
        _, decimals = self._qty_step(symbol)
        return f"{self._round_qty(symbol, qty):.{decimals}f}"

    def _fmt_price(self, symbol, price):
        _, decimals = self._price_tick(symbol)
        return f"{self._round_price(symbol, price):.{decimals}f}"

    def _get_min_qty(self, symbol: str) -> float:
        inst = self.instruments_cache.get(symbol)
        if not inst:
            return 0.001
        lot_filter = inst.get("filters", {}).get("LOT_SIZE", {})
        return float(lot_filter.get("minQty", 0.001))

    def _get_min_notional(self, symbol: str) -> float:
        inst = self.instruments_cache.get(symbol)
        if not inst:
            return 5.0
        notional_filter = inst.get("filters", {}).get("MIN_NOTIONAL", {})
        return float(notional_filter.get("notional", 5.0))

    async def _get_mark_price(self, symbol: str) -> float:
        try:
            data = await self._request("GET", "/fapi/v1/premiumIndex", params={"symbol": symbol}, signed=False)
            if isinstance(data, dict) and "markPrice" in data:
                return float(data["markPrice"])
        except Exception:
            pass
        return 0.0

    # ─── Position Restore on Startup ───────────────────────────────────────

    async def restore_positions_from_exchange(self) -> int:
        """Restore open positions from exchange after restart.

        Fetches actual positions from /fapi/v2/positionRisk and rebuilds
        local position state. SL/TP algo orders remain active on exchange.
        Returns number of positions restored.
        """
        try:
            data = await self._request("GET", "/fapi/v2/positionRisk")
            if not isinstance(data, list):
                logger.warning("[RESTORE] Failed to fetch positions from exchange")
                return 0

            restored = 0
            for pos_data in data:
                amt = float(pos_data.get("positionAmt", 0))
                if amt == 0:
                    continue

                symbol = pos_data["symbol"]
                if symbol in self.positions:
                    continue  # already tracked

                entry_price = float(pos_data.get("entryPrice", 0))
                mark_price = float(pos_data.get("markPrice", 0))
                if entry_price <= 0:
                    continue

                side = "LONG" if amt > 0 else "SHORT"
                qty = abs(amt)
                notional = qty * entry_price

                # Estimate SL/TP from algo orders on exchange
                sl_price = 0.0
                tp_price = 0.0
                sl_algo_id = None
                tp_algo_id = None
                try:
                    algo_orders = await self._get_open_algo_orders(symbol)
                    for algo in algo_orders:
                        algo_type = str(algo.get("type", "")).upper()
                        trigger = float(algo.get("triggerPrice", 0))
                        algo_id = algo.get("algoId")
                        if algo_type in ("STOP", "STOP_MARKET") and trigger > 0:
                            sl_price = trigger
                            sl_algo_id = int(algo_id) if algo_id else None
                        elif algo_type in ("TAKE_PROFIT", "TAKE_PROFIT_MARKET") and trigger > 0:
                            tp_price = trigger
                            tp_algo_id = int(algo_id) if algo_id else None
                except Exception as e:
                    logger.debug(f"[RESTORE] Could not fetch algo orders for {symbol}: {e}")

                # Fallback SL if not found from algo orders
                if sl_price <= 0:
                    sl_price = entry_price * (0.97 if side == "LONG" else 1.03)

                # Determine regime from position age (rough estimate)
                regime = "RANGING"  # conservative default

                pos = {
                    "symbol": symbol,
                    "side": side,
                    "entry_price": entry_price,
                    "notional": notional,
                    "qty": qty,
                    "tp1": 0.0,  # unknown after restart
                    "tp3": tp_price,
                    "sl": sl_price,
                    "original_sl": sl_price,
                    "sl_kind": "ORIGINAL",
                    "open_time": datetime.now(UTC),  # approximate (actual open time lost)
                    "regime": regime,
                    "score": 0.0,
                    "adv_snapshot": {},
                    "max_hold_minutes": MAX_HOLD_RANGING,
                    "locked_profit": False,
                    "high_watermark": max(entry_price, mark_price),
                    "low_watermark": min(entry_price, mark_price) if mark_price > 0 else entry_price,
                    "order_id": None,
                    "is_vip": False,
                    "actual_risk_amount": abs(entry_price - sl_price) / max(entry_price, 1e-9) * notional,
                    "notification_sent": True,  # don't re-notify
                    "sl_algo_id": sl_algo_id,
                    "tp_algo_id": tp_algo_id,
                    "restored_from_exchange": True,
                }
                self.positions[symbol] = pos
                restored += 1
                logger.info(
                    f"[RESTORE] ♻️ {side} {symbol} @ {entry_price:.6f} | qty={qty} | "
                    f"SL={sl_price:.6f} | TP={tp_price:.6f} | "
                    f"algo_sl={'✅' if sl_algo_id else '❌'} algo_tp={'✅' if tp_algo_id else '❌'}"
                )

            if restored > 0:
                logger.info(f"[RESTORE] ♻️ Restored {restored} positions from exchange")
            else:
                logger.info("[RESTORE] No open positions found on exchange")

            return restored
        except Exception as e:
            logger.error(f"[RESTORE] Error restoring positions: {e}")
            return 0

    # ─── Position Management Interface ─────────────────────────────────────

    def get_open_position_count(self) -> int:
        return len(self.positions)

    def has_open_positions(self) -> bool:
        return len(self.positions) > 0

    def iter_position_items(self):
        for symbol, pos in list(self.positions.items()):
            yield (symbol, pos)

    async def mark_position_open_notified(self, symbol: str, **kwargs) -> None:
        """Called by orchestration after notification is sent."""
        pos = self.positions.get(symbol)
        if pos:
            pos["notification_sent"] = True

    def reset_cycle_counter(self):
        pass

    # ─── Order Router (compatible with paper_trader) ───────────────────────

    async def start_order_router(self):
        self._router_task = asyncio.create_task(self._order_router_loop())
        logger.info("[BINANCE_TESTNET] Order router started")

    def get_router_task(self):
        return self._router_task

    # ─── Lifecycle (Execution Gateway contract) ───────────────────────────
    # The gateway lifespan calls trader.start()/stop() generically. Wire the
    # order router (executes queued opens), the reconciliation loop (detects
    # server-side SL/TP fills -> close notification) and a position monitor
    # (feeds mark prices to check_positions -> bot-side TP ladder scale-outs
    # and their close notifications). Without this, submit_open() times out
    # and no TP/exit notifications are ever sent.
    async def start(self):
        await self.start_order_router()
        self._reconcile_task = asyncio.create_task(self.start_reconciliation_loop(interval_sec=15))
        self._monitor_task = asyncio.create_task(self._position_monitor_loop(interval_sec=5))
        logger.info("[BINANCE_TESTNET] start() — router + reconcile + position-monitor loops up")

    async def stop(self):
        for attr in ("_router_task", "_reconcile_task", "_monitor_task"):
            t = getattr(self, attr, None)
            if t is not None and not t.done():
                t.cancel()
        logger.info("[BINANCE_TESTNET] stop() — loops cancelled")

    async def _position_monitor_loop(self, interval_sec: int = 5):
        """Poll mark prices for open positions and drive check_positions so the
        signal-copy TP ladder scales out (and fires close notifications)."""
        while True:
            try:
                await asyncio.sleep(interval_sec)
                if not self.positions:
                    continue
                price_map: Dict[str, float] = {}
                for sym in list(self.positions.keys()):
                    try:
                        mp = await self._get_mark_price(sym)
                        if mp and mp > 0:
                            price_map[sym] = mp
                    except Exception:
                        continue
                if price_map:
                    await self.check_positions(price_map)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[POSITION_MONITOR] error: {e}")
                await asyncio.sleep(2)

    async def _order_router_loop(self):
        while True:
            try:
                request = await asyncio.wait_for(self._order_queue.get(), timeout=5.0)
                await self._process_order_request(request)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[ORDER_ROUTER] error: {e}")
                await asyncio.sleep(1)

    async def _process_order_request(self, request: Dict[str, Any]):
        """Process a queued order request."""
        symbol = request.get("symbol")
        try:
            result = await self._execute_open(request)
            future = request.get("_future")
            if future and not future.done():
                future.set_result(result)
        except Exception as e:
            logger.error(f"[ORDER_ROUTER] Failed to process {symbol}: {e}")
            future = request.get("_future")
            if future and not future.done():
                future.set_result(None)

    async def submit_open(self, timeout_sec: float = 30.0, **kwargs) -> Optional[Dict[str, Any]]:
        """Submit open request via router queue (compatible with paper_trader)."""
        future = asyncio.get_event_loop().create_future()
        kwargs["_future"] = future
        await self._order_queue.put(kwargs)
        try:
            result = await asyncio.wait_for(future, timeout=timeout_sec)
            return result
        except asyncio.TimeoutError:
            logger.error(f"[SUBMIT_OPEN] Timeout for {kwargs.get('symbol')}")
            return None

    # ─── Core Execution ────────────────────────────────────────────────────

    async def _execute_open(self, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        symbol = params.get("symbol", "")
        side = str(params.get("side", params.get("direction", ""))).upper()
        entry_price = float(params.get("entry_price", 0))
        sl_price = float(params.get("sl", params.get("sl_price", 0)))
        tp1 = float(params.get("tp1", 0))
        tp3 = float(params.get("tp3", params.get("tp_full", 0)))
        notional = float(params.get("notional", params.get("size_usd", 0)))
        regime = str(params.get("regime", "RANGING"))
        adv = params.get("adv_snapshot", params.get("adv", {})) or {}

        if not symbol or side not in ("LONG", "SHORT"):
            logger.warning(f"[BINANCE_OPEN] Invalid params: symbol={symbol} side={side}")
            return None

        if not is_coin_allowed(symbol):
            logger.warning(f"[BINANCE_OPEN] {symbol} not allowed")
            return None

        # Ensure we have this symbol's tick/step/precision specs (refresh once
        # if missing — e.g. a newly-listed coin not present at startup).
        if symbol not in self.instruments_cache:
            try:
                await self.get_instruments_info()
            except Exception as exc:
                logger.warning(f"[BINANCE_OPEN] instrument refresh failed for {symbol}: {exc}")
            if symbol not in self.instruments_cache:
                logger.warning(f"[BINANCE_OPEN] {symbol} has no instrument spec (may not be on this exchange)")

        # Get current mark price
        mark_price = await self._get_mark_price(symbol)
        if mark_price <= 0:
            mark_price = entry_price
        actual_entry = mark_price if mark_price > 0 else entry_price

        # Validate SL
        if sl_price <= 0:
            sl_price = actual_entry * (0.98 if side == "LONG" else 1.02)
        if side == "LONG" and sl_price >= actual_entry:
            sl_price = actual_entry * 0.98
        if side == "SHORT" and sl_price <= actual_entry:
            sl_price = actual_entry * 1.02

        # ─── GEOMETRY GATE: SL Distance & RR Validation ───────────────────
        from signal_copy.execution_config import (
            MAX_SL_DISTANCE_PCT, MIN_SL_DISTANCE_PCT, MIN_RR_RATIO,
            ENTRY_CONFIRMATION_ENABLED, RANGING_TP1_MULTIPLIER, TRENDING_TP1_MULTIPLIER
        )
        # Signal-copy orders are pre-validated by the deep validation engine
        # (best-RR aware) and scaled out across a TP ladder, so the trader's
        # single-TP1 RR gate and RANGING TP1 shrink must NOT re-judge them.
        _is_signal_copy = str(params.get("source", "")).upper() == "SIGNAL_COPY"
        
        # ─── ENTRY CONFIRMATION CHECK (Jun 4 2026 Fix) ───
        # Prevent early entries like IOUSDT where price spiked against us immediately
        if ENTRY_CONFIRMATION_ENABLED and regime == "RANGING" and not _is_signal_copy:
            # For RANGING, require current price to be BEYOND the key level, not just touching
            # This confirms breakdown/breakout has started
            supply_zone = adv.get("supply_zone", adv.get("supply", 0))
            demand_zone = adv.get("demand_zone", adv.get("demand", 0))
            
            if side == "SHORT" and supply_zone > 0:
                # For SHORT, current price should be BELOW supply (breakdown confirmed)
                if actual_entry >= supply_zone * 0.998:  # Within 0.2% of supply
                    logger.warning(f"[ENTRY_CONFIRMATION] REJECT {symbol} SHORT | Entry {actual_entry:.6f} still AT supply {supply_zone:.6f} - wait for breakdown confirmation")
                    return None
            elif side == "LONG" and demand_zone > 0:
                # For LONG, current price should be ABOVE demand (breakout confirmed)
                if actual_entry <= demand_zone * 1.002:  # Within 0.2% of demand
                    logger.warning(f"[ENTRY_CONFIRMATION] REJECT {symbol} LONG | Entry {actual_entry:.6f} still AT demand {demand_zone:.6f} - wait for breakout confirmation")
                    return None
        
        # ─── TP ADJUSTMENT FOR RANGING (Jun 4 2026 Evidence-Based Fix) ───
        # RANGING moves are smaller - reduce TP1 distance to realistic levels
        # Evidence: ARKMUSDT TP1 at -4.17% never hit, price only went -1.03%
        if regime == "RANGING" and tp1 > 0 and not _is_signal_copy:
            original_tp1 = tp1
            tp1_distance = abs(tp1 - actual_entry)
            adjusted_distance = tp1_distance * RANGING_TP1_MULTIPLIER
            
            if side == "SHORT":
                tp1 = actual_entry - adjusted_distance
            else:  # LONG
                tp1 = actual_entry + adjusted_distance
            
            logger.info(f"[TP_ADJUST] {symbol} {regime} | TP1 adjusted from {original_tp1:.6f} to {tp1:.6f} (multiplier={RANGING_TP1_MULTIPLIER})")
        
        sl_distance_pct = abs(actual_entry - sl_price) / actual_entry * 100.0
        _max_sl_cap = 8.0 if _is_signal_copy else MAX_SL_DISTANCE_PCT
        if sl_distance_pct > _max_sl_cap:
            logger.warning(f"[GEOMETRY_GATE] REJECT {symbol} {side} | SL distance {sl_distance_pct:.2f}% > max {_max_sl_cap}%")
            return None
        if sl_distance_pct < MIN_SL_DISTANCE_PCT:
            logger.warning(f"[GEOMETRY_GATE] REJECT {symbol} {side} | SL distance {sl_distance_pct:.2f}% < min {MIN_SL_DISTANCE_PCT}% (noise)")
            return None

        # Check RR ratio (TP1 vs SL) — skip for signal-copy (validated + laddered)
        if not _is_signal_copy and tp1 > 0 and sl_distance_pct > 0:
            tp1_distance_pct = abs(tp1 - actual_entry) / actual_entry * 100.0
            rr_ratio = tp1_distance_pct / sl_distance_pct
            if rr_ratio < MIN_RR_RATIO:
                logger.warning(f"[GEOMETRY_GATE] REJECT {symbol} {side} | RR={rr_ratio:.2f} < min {MIN_RR_RATIO} (TP1={tp1_distance_pct:.2f}% SL={sl_distance_pct:.2f}%)")
                return None

        # Calculate qty
        if notional <= 0:
            notional = self.current_balance * 0.02  # 2% of balance
        qty = self._round_qty(symbol, notional / actual_entry)
        min_qty = self._get_min_qty(symbol)
        if qty < min_qty:
            qty = min_qty

        actual_notional = qty * actual_entry
        if actual_notional < self._get_min_notional(symbol):
            qty = self._round_qty(symbol, self._get_min_notional(symbol) / actual_entry * 1.1)
            actual_notional = qty * actual_entry

        # Check balance
        if time.time() - self.last_balance_check > 10:
            await self._update_balance()
        if self.current_balance < actual_notional / self.leverage * 1.1:
            logger.warning(f"[BINANCE_OPEN] Insufficient margin for {symbol}")
            return None

        # Set leverage and margin type
        await self._set_leverage(symbol)
        await self._set_margin_type(symbol)

        # Place market order
        api_side = "BUY" if side == "LONG" else "SELL"
        order_result = await self._place_market_order(symbol, api_side, qty)
        if not order_result:
            return None

        order_id = order_result.get("orderId")
        avg_price = float(order_result.get("avgPrice", actual_entry))
        filled_qty = float(order_result.get("executedQty", qty))

        if avg_price <= 0:
            avg_price = actual_entry
        if filled_qty <= 0:
            logger.error(f"[BINANCE_OPEN] Zero fill for {symbol}")
            return None

        notional_final = filled_qty * avg_price
        logger.info(f"✅ [BINANCE_TESTNET] FILLED {side} {symbol} @ {avg_price:.6f} | qty={filled_qty} | notional=${notional_final:.2f}")

        # Calculate TP levels
        sl_distance = abs(avg_price - sl_price)
        if side == "LONG":
            tp1_calc = self._round_price(symbol, avg_price + sl_distance * 1.5)
            tp3_calc = self._round_price(symbol, avg_price + sl_distance * 3.0)
        else:
            tp1_calc = self._round_price(symbol, avg_price - sl_distance * 1.5)
            tp3_calc = self._round_price(symbol, avg_price - sl_distance * 3.0)

        if tp1 <= 0:
            tp1 = tp1_calc
        if tp3 <= 0:
            tp3 = tp3_calc

        # Store position first (needed for SL/TP qty lookup)
        pos = {
            "symbol": symbol,
            "side": side,
            "entry_price": avg_price,
            "notional": notional_final,
            "qty": filled_qty,
            "tp1": tp1,
            "tp3": tp3,
            "sl": sl_price,
            "original_sl": sl_price,
            "sl_kind": "ORIGINAL",
            "open_time": datetime.now(UTC),
            "regime": regime,
            "score": float(params.get("score", 0)),
            "adv_snapshot": dict(adv),
            "max_hold_minutes": {"TRENDING": MAX_HOLD_TRENDING, "RANGING": MAX_HOLD_RANGING, "HIGH_VOL": MAX_HOLD_HIGH_VOL}.get(regime, MAX_HOLD_MINUTES),
            "locked_profit": False,
            "high_watermark": avg_price,
            "low_watermark": avg_price,
            "order_id": order_id,
            "is_vip": params.get("is_vip", False),
            "actual_risk_amount": abs(avg_price - sl_price) / max(avg_price, 1e-9) * notional_final,
            "notification_sent": False,
        }
        self.positions[symbol] = pos

        # ─── Multi-target TP ladder (signal-copy) ──────────────────────────
        # If the signal provided multiple targets, scale out an equal slice at
        # EACH target and let the stop trail the remainder. Only set server-side
        # SL here (not a full TP) so the bot manages the laddered scale-out.
        signal_tps = params.get("signal_take_profits") or []
        try:
            signal_tps = [float(t) for t in signal_tps if t and float(t) > 0]
        except Exception:
            signal_tps = []
        # keep only targets on the profit side of entry, ordered nearest->furthest
        signal_tps = [t for t in signal_tps if (t > avg_price if side == "LONG" else t < avg_price)]
        signal_tps = sorted(set(signal_tps), reverse=(side == "SHORT"))

        await self._set_stop_loss(symbol, side, sl_price, qty=filled_qty)

        if len(signal_tps) >= 2:
            n = len(signal_tps)
            frac = round(1.0 / n, 6)
            pos["tp_ladder"] = [{"price": t, "fraction": frac, "hit": False} for t in signal_tps]
            pos["tp_ladder_base_qty"] = filled_qty
            logger.info("🚀 [BINANCE_TESTNET] OPEN %s %s @ %.6f | SL=%.6f | TP ladder=%s (%.0f%% each)",
                        side, symbol, avg_price, sl_price,
                        [round(t, 6) for t in signal_tps], frac * 100)
        else:
            # Single/`no` target -> legacy behavior (partial at tp1, full at tp3).
            await self._set_take_profit(symbol, side, tp3, qty=filled_qty)
            logger.info(f"🚀 [BINANCE_TESTNET] OPEN {side} {symbol} @ {avg_price:.6f} | SL={sl_price:.4f} | TP1={tp1:.4f} TP3={tp3:.4f}")

        # Notification handled by orchestration/notifications.py (notify_open_position)
        return pos

    # ─── API Calls ─────────────────────────────────────────────────────────

    async def _set_leverage(self, symbol: str):
        try:
            await self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": self.leverage})
        except Exception as e:
            logger.debug(f"Set leverage error (ignored): {e}")

    async def _set_margin_type(self, symbol: str):
        try:
            await self._request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": self.margin_type})
        except Exception as e:
            if "No need to change" not in str(e) and "-4046" not in str(e):
                logger.debug(f"Set margin type error (ignored): {e}")

    async def _place_market_order(self, symbol: str, side: str, qty: float) -> Optional[Dict[str, Any]]:
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": self._fmt_qty(symbol, qty),
            "newOrderRespType": "RESULT",
        }
        data = await self._request("POST", "/fapi/v1/order", params)
        if data.get("orderId"):
            return data
        logger.error(f"[BINANCE_ORDER] Failed: {data}")
        return None

    # ─── Algo Order API (Conditional Orders) ─────────────────────────────────
    # Binance migrated conditional orders (STOP, TAKE_PROFIT, etc.) to the
    # Algo Order API as of 2025-12-09. The legacy /fapi/v1/order endpoint
    # returns -4120 for these order types. We now use /fapi/v1/algoOrder.

    async def _place_algo_conditional_order(
        self, symbol: str, side: str, order_type: str,
        trigger_price: float, price: float, qty: float,
    ) -> Dict[str, Any]:
        """Place a conditional order via the Algo Order API (POST /fapi/v1/algoOrder).

        Args:
            symbol: Trading pair (e.g. BTCUSDT)
            side: BUY or SELL (close side)
            order_type: STOP, STOP_MARKET, TAKE_PROFIT, TAKE_PROFIT_MARKET
            trigger_price: Price at which the order triggers
            price: Limit price (for STOP/TAKE_PROFIT). Use 0 for MARKET types.
            qty: Order quantity

        Returns:
            API response dict with algoId on success, or error dict.
        """
        params: Dict[str, Any] = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "triggerPrice": self._fmt_price(symbol, trigger_price),
            "quantity": self._fmt_qty(symbol, qty),
            "workingType": "MARK_PRICE",
            "newOrderRespType": "RESULT",
        }
        # For limit-type orders, include price and timeInForce
        if order_type in ("STOP", "TAKE_PROFIT"):
            params["price"] = self._fmt_price(symbol, price)
            params["timeInForce"] = "GTC"
        # For MARKET types, use reduceOnly (not available in hedge mode)
        if order_type in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
            params["reduceOnly"] = "true"
        else:
            params["reduceOnly"] = "true"

        result = await self._request("POST", "/fapi/v1/algoOrder", params)
        return result

    async def _cancel_algo_order(self, symbol: str, algo_id: int) -> bool:
        """Cancel a single algo order by algoId."""
        try:
            result = await self._request("DELETE", "/fapi/v1/algoOrder", {
                "symbol": symbol,
                "algoId": str(algo_id),
            })
            # Success: code=200 (int or string) or algoId present in response
            code = result.get("code")
            if code in (200, "200") or result.get("algoId") or result.get("msg") == "success":
                return True
            logger.warning(f"[ALGO_CANCEL] Failed for {symbol} algoId={algo_id}: {result}")
            return False
        except Exception as e:
            logger.error(f"[ALGO_CANCEL] Exception for {symbol}: {e}")
            return False

    async def _get_open_algo_orders(self, symbol: str = "") -> List[Dict[str, Any]]:
        """Get open algo orders, optionally filtered by symbol."""
        params: Dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        try:
            result = await self._request("GET", "/fapi/v1/openAlgoOrders", params)
            if isinstance(result, dict) and "orders" in result:
                return result["orders"]
            if isinstance(result, list):
                return result
            return []
        except Exception:
            return []

    async def _set_stop_loss(self, symbol: str, side: str, sl_price: float, qty: float = 0) -> bool:
        """Set SL on exchange via Algo Order API → legacy fallback → bot-monitored."""
        try:
            close_side = "SELL" if side == "LONG" else "BUY"
            rounded_sl = self._round_price(symbol, sl_price)
            actual_qty = qty or self.positions.get(symbol, {}).get("qty", 0)
            actual_qty = self._round_qty(symbol, actual_qty)

            if actual_qty <= 0:
                logger.warning(f"[BINANCE_SL] Cannot set SL for {symbol}: qty=0")
                return False

            # Strategy 1: Algo Order API (STOP with limit price)
            result = await self._place_algo_conditional_order(
                symbol=symbol,
                side=close_side,
                order_type="STOP",
                trigger_price=rounded_sl,
                price=rounded_sl,
                qty=actual_qty,
            )
            if result.get("algoId"):
                algo_id = result["algoId"]
                pos = self.positions.get(symbol)
                if pos:
                    pos["sl_algo_id"] = algo_id
                logger.info(f"[BINANCE_SL] ✅ Algo SL set for {symbol} @ {rounded_sl} (algoId={algo_id})")
                return True

            logger.warning(f"[BINANCE_SL] Algo STOP failed for {symbol}: {result.get('code')}: {result.get('msg', '')}")

            # Strategy 2: Algo Order API (STOP_MARKET — no limit price)
            result2 = await self._place_algo_conditional_order(
                symbol=symbol,
                side=close_side,
                order_type="STOP_MARKET",
                trigger_price=rounded_sl,
                price=0,
                qty=actual_qty,
            )
            if result2.get("algoId"):
                algo_id = result2["algoId"]
                pos = self.positions.get(symbol)
                if pos:
                    pos["sl_algo_id"] = algo_id
                logger.info(f"[BINANCE_SL] ✅ Algo STOP_MARKET set for {symbol} @ {rounded_sl} (algoId={algo_id})")
                return True

            logger.warning(f"[BINANCE_SL] Algo STOP_MARKET failed for {symbol}: {result2.get('code')}: {result2.get('msg', '')}")

            # Strategy 3: Legacy endpoint (STOP order)
            legacy_params = {
                "symbol": symbol,
                "side": close_side,
                "type": "STOP",
                "stopPrice": self._fmt_price(symbol, rounded_sl),
                "price": self._fmt_price(symbol, rounded_sl),
                "quantity": self._fmt_qty(symbol, actual_qty),
                "workingType": "MARK_PRICE",
                "reduceOnly": "true",
                "timeInForce": "GTC",
            }
            legacy_result = await self._request("POST", "/fapi/v1/order", legacy_params)
            if legacy_result.get("orderId"):
                logger.info(f"[BINANCE_SL] ✅ Legacy SL set for {symbol} @ {rounded_sl}")
                return True

            # Strategy 4: Legacy STOP_MARKET
            legacy_params2 = {
                "symbol": symbol,
                "side": close_side,
                "type": "STOP_MARKET",
                "stopPrice": self._fmt_price(symbol, rounded_sl),
                "quantity": self._fmt_qty(symbol, actual_qty),
                "workingType": "MARK_PRICE",
                "reduceOnly": "true",
            }
            legacy_result2 = await self._request("POST", "/fapi/v1/order", legacy_params2)
            if legacy_result2.get("orderId"):
                logger.info(f"[BINANCE_SL] ✅ Legacy STOP_MARKET set for {symbol} @ {rounded_sl}")
                return True

            # All strategies failed — bot will monitor SL
            logger.warning(f"[BINANCE_SL] ⚠️ ALL STRATEGIES FAILED for {symbol} @ {rounded_sl} — relying on bot-monitored SL only")
            return False
        except Exception as e:
            logger.error(f"[BINANCE_SL] Exception for {symbol}: {e}")
            return False

    async def _set_take_profit(self, symbol: str, side: str, tp_price: float, qty: float = 0) -> bool:
        """Set TP on exchange via Algo Order API → legacy fallback → bot-monitored."""
        try:
            close_side = "SELL" if side == "LONG" else "BUY"
            rounded_tp = self._round_price(symbol, tp_price)
            actual_qty = qty or self.positions.get(symbol, {}).get("qty", 0)
            actual_qty = self._round_qty(symbol, actual_qty)

            if actual_qty <= 0:
                logger.warning(f"[BINANCE_TP] Cannot set TP for {symbol}: qty=0")
                return False

            # Strategy 1: Algo Order API (TAKE_PROFIT with limit price)
            result = await self._place_algo_conditional_order(
                symbol=symbol,
                side=close_side,
                order_type="TAKE_PROFIT",
                trigger_price=rounded_tp,
                price=rounded_tp,
                qty=actual_qty,
            )
            if result.get("algoId"):
                algo_id = result["algoId"]
                pos = self.positions.get(symbol)
                if pos:
                    pos["tp_algo_id"] = algo_id
                logger.info(f"[BINANCE_TP] ✅ Algo TP set for {symbol} @ {rounded_tp} (algoId={algo_id})")
                return True

            logger.warning(f"[BINANCE_TP] Algo TAKE_PROFIT failed for {symbol}: {result.get('code')}: {result.get('msg', '')}")

            # Strategy 2: Algo Order API (TAKE_PROFIT_MARKET)
            result2 = await self._place_algo_conditional_order(
                symbol=symbol,
                side=close_side,
                order_type="TAKE_PROFIT_MARKET",
                trigger_price=rounded_tp,
                price=0,
                qty=actual_qty,
            )
            if result2.get("algoId"):
                algo_id = result2["algoId"]
                pos = self.positions.get(symbol)
                if pos:
                    pos["tp_algo_id"] = algo_id
                logger.info(f"[BINANCE_TP] ✅ Algo TAKE_PROFIT_MARKET set for {symbol} @ {rounded_tp} (algoId={algo_id})")
                return True

            logger.warning(f"[BINANCE_TP] Algo TAKE_PROFIT_MARKET failed for {symbol}: {result2.get('code')}: {result2.get('msg', '')}")

            # Strategy 3: Legacy endpoint (TAKE_PROFIT)
            legacy_params = {
                "symbol": symbol,
                "side": close_side,
                "type": "TAKE_PROFIT",
                "stopPrice": self._fmt_price(symbol, rounded_tp),
                "price": self._fmt_price(symbol, rounded_tp),
                "quantity": self._fmt_qty(symbol, actual_qty),
                "workingType": "MARK_PRICE",
                "reduceOnly": "true",
                "timeInForce": "GTC",
            }
            legacy_result = await self._request("POST", "/fapi/v1/order", legacy_params)
            if legacy_result.get("orderId"):
                logger.info(f"[BINANCE_TP] ✅ Legacy TP set for {symbol} @ {rounded_tp}")
                return True

            # Strategy 4: Legacy TAKE_PROFIT_MARKET
            legacy_params2 = {
                "symbol": symbol,
                "side": close_side,
                "type": "TAKE_PROFIT_MARKET",
                "stopPrice": self._fmt_price(symbol, rounded_tp),
                "quantity": self._fmt_qty(symbol, actual_qty),
                "workingType": "MARK_PRICE",
                "reduceOnly": "true",
            }
            legacy_result2 = await self._request("POST", "/fapi/v1/order", legacy_params2)
            if legacy_result2.get("orderId"):
                logger.info(f"[BINANCE_TP] ✅ Legacy TAKE_PROFIT_MARKET set for {symbol} @ {rounded_tp}")
                return True

            # All strategies failed — bot will monitor TP
            logger.warning(f"[BINANCE_TP] ⚠️ ALL STRATEGIES FAILED for {symbol} @ {rounded_tp} — relying on bot-monitored TP only")
            return False
        except Exception as e:
            logger.error(f"[BINANCE_TP] Exception for {symbol}: {e}")
            return False

    # ─── Position Monitoring & Exit ────────────────────────────────────────

    async def check_positions(self, price_map: Dict[str, float]) -> List[Dict[str, Any]]:
        closed = []
        now = datetime.now(UTC)
        for sym, pos in list(self.positions.items()):
            price = price_map.get(sym)
            if not price or price <= 0:
                continue

            side = pos["side"]
            entry = pos["entry_price"]
            sl = pos["sl"]
            original_sl = pos.get("original_sl", pos.get("sl_at_open", sl))
            hold_min = (now - pos["open_time"]).total_seconds() / 60.0
            unrealized = (price - entry) / entry if side == "LONG" else (entry - price) / entry

            # Update watermarks
            if side == "LONG":
                pos["high_watermark"] = max(pos.get("high_watermark", price), price)
            else:
                pos["low_watermark"] = min(pos.get("low_watermark", price), price)
            reason = None
            exit_price = price

            # ─── Multi-target TP ladder (signal-copy) ─────────────────────
            # Scale out an equal slice at each target; trail the remainder.
            ladder = pos.get("tp_ladder")
            if ladder:
                base_qty = pos.get("tp_ladder_base_qty", pos["qty"])
                filled_any = False
                for level in ladder:
                    if level.get("hit"):
                        continue
                    tp = float(level["price"])
                    hit = (side == "LONG" and price >= tp) or (side == "SHORT" and price <= tp)
                    if not hit:
                        continue
                    level["hit"] = True
                    remaining_levels = [l for l in ladder if not l.get("hit")]
                    # Last target (or rounding leftover) -> close all remaining.
                    if not remaining_levels:
                        close_qty = pos["qty"]
                    else:
                        close_qty = self._round_qty(sym, base_qty * float(level["fraction"]))
                        close_qty = min(close_qty, pos["qty"])
                    if close_qty <= 0:
                        continue
                    actual_fill = await self._close_position_market(sym, side, close_qty)
                    fill_price = actual_fill if actual_fill and actual_fill > 0 else tp
                    pos["qty"] = max(pos["qty"] - close_qty, 0.0)
                    if side == "LONG":
                        pnl_pct = (fill_price - entry) / entry
                    else:
                        pnl_pct = (entry - fill_price) / entry
                    notional_part = close_qty * fill_price
                    tp_index = ladder.index(level) + 1
                    is_final = (not remaining_levels) or pos["qty"] <= 0
                    rtag = "TP_FULL" if is_final else f"PARTIAL_TP{tp_index}"
                    trade = {
                        "timestamp_open": pos["open_time"].strftime("%Y-%m-%d %H:%M:%S"),
                        "timestamp_close": now.strftime("%Y-%m-%d %H:%M:%S"),
                        "symbol": sym, "side": side, "entry_price": round(entry, 6),
                        "exit_price": round(fill_price, 6),
                        "notional_usd": round(notional_part, 2),
                        "pnl_pct": round(pnl_pct * 100, 4),
                        "pnl_usd": round(notional_part * pnl_pct, 2),
                        "hold_minutes": round(hold_min, 2),
                        "reason": rtag, "raw_reason": rtag, "normalized_reason": rtag,
                        "regime": pos.get("regime"),
                        "sl_original": round(original_sl, 6),
                        "active_sl_at_exit": round(pos["sl"], 6),
                        "sl_kind_at_exit": pos.get("sl_kind", "ORIGINAL"),
                        "is_partial": not is_final,
                        "partial_fraction": float(level["fraction"]),
                    }
                    await self._journal_trade(trade)
                    closed.append(trade)
                    await self._send_close_notification(trade)
                    logger.info("[TP_LADDER] %s %s | %s fill=%.6f pnl=%.2f%% remaining_qty=%s",
                                sym, side, rtag, fill_price, pnl_pct * 100, pos["qty"])
                    filled_any = True

                    # After the first target: lock profit, move SL to breakeven,
                    # enable trailing for the remainder.
                    if not pos.get("locked_profit"):
                        if side == "LONG":
                            be = max(pos["sl"], entry * 1.001)
                        else:
                            be = min(pos["sl"], entry * 0.999)
                        if be != pos["sl"]:
                            pos["sl"] = be
                            pos["sl_kind"] = "BREAKEVEN"
                            await self._update_server_sl(sym, side, be)
                        pos["locked_profit"] = True

                    if is_final and pos["qty"] <= 0:
                        if pos.get("sl_algo_id"):
                            await self._cancel_algo_order(sym, pos["sl_algo_id"])
                        del self.positions[sym]
                        break

                if sym not in self.positions:
                    continue  # fully closed via ladder
                if filled_any:
                    continue  # one ladder action per cycle; re-evaluate next loop

            # ─── Partial TP1 (close 50% at tp1) ───────────────────────────
            tp1 = pos.get("tp1", 0)
            if not ladder and tp1 > 0 and not pos.get("partial_tp1_done"):
                tp1_hit = (side == "LONG" and price >= tp1) or (side == "SHORT" and price <= tp1)
                if tp1_hit:
                    partial_qty = self._round_qty(sym, pos["qty"] * 0.5)
                    if partial_qty > 0:
                        actual_fill = await self._close_position_market(sym, side, partial_qty)
                        partial_exit_price = actual_fill if actual_fill > 0 else price
                        pos["qty"] -= partial_qty
                        pos["partial_tp1_done"] = True
                        partial_notional = partial_qty * partial_exit_price
                        if side == "LONG":
                            partial_pnl_pct = (partial_exit_price - entry) / entry
                        else:
                            partial_pnl_pct = (entry - partial_exit_price) / entry
                        partial_pnl_usd = partial_notional * partial_pnl_pct

                        # Move SL to breakeven after partial TP
                        if side == "LONG":
                            be_sl = max(pos["sl"], entry * 1.001)
                        else:
                            be_sl = min(pos["sl"], entry * 0.999)
                        if be_sl != pos["sl"]:
                            pos["sl"] = be_sl
                            pos["sl_kind"] = "BREAKEVEN"
                            await self._update_server_sl(sym, side, be_sl)

                        # Update TP algo order for remaining qty
                        tp3 = pos.get("tp3", 0)
                        if tp3 > 0:
                            await self._update_server_tp(sym, side, tp3, pos["qty"])

                        trade = {
                            "timestamp_open": pos["open_time"].strftime("%Y-%m-%d %H:%M:%S"),
                            "timestamp_close": now.strftime("%Y-%m-%d %H:%M:%S"),
                            "symbol": sym, "side": side, "entry_price": round(entry, 6),
                            "exit_price": round(partial_exit_price, 6),
                            "notional_usd": round(partial_notional, 2),
                            "pnl_pct": round(partial_pnl_pct * 100, 4),
                            "pnl_usd": round(partial_pnl_usd, 2),
                            "hold_minutes": round(hold_min, 2),
                            "reason": "PARTIAL_TP1",
                            "raw_reason": "PARTIAL_TP1",
                            "normalized_reason": "PARTIAL_TP1",
                            "regime": pos.get("regime"),
                            "sl_original": round(original_sl, 6),
                            "active_sl_at_exit": round(pos["sl"], 6),
                            "sl_kind_at_exit": pos.get("sl_kind", "ORIGINAL"),
                            "is_partial": True,
                            "partial_fraction": 0.5,
                        }
                        await self._journal_trade(trade)
                        closed.append(trade)
                        await self._send_close_notification(trade)
                        logger.info(f"[PARTIAL_TP1] {sym} {side} | fill={partial_exit_price:.6f} | pnl={partial_pnl_pct*100:.2f}% | remaining_qty={pos['qty']}")
                    continue  # Don't check other exits this cycle after partial

            # ─── TP Full (close remaining at tp3) ──────────────────────────
            tp3 = pos.get("tp3", 0)
            if not ladder and tp3 > 0:
                tp3_hit = (side == "LONG" and price >= tp3) or (side == "SHORT" and price <= tp3)
                if tp3_hit:
                    reason = "TP_FULL"
                    exit_price = price

            # ─── Scratch Exit (no progress after X minutes) ────────────────
            # Prevents zombie trades that bleed slowly. Close near breakeven.
            if reason is None and not pos.get("partial_tp1_done"):
                from signal_copy.execution_config import (
                    SCRATCH_EXIT_ENABLED, SCRATCH_EXIT_RANGING_MINUTES,
                    SCRATCH_EXIT_TRENDING_MINUTES, SCRATCH_EXIT_MAX_ABS_PNL_PCT
                )
                if SCRATCH_EXIT_ENABLED:
                    regime = pos.get("regime", "RANGING")
                    scratch_min = SCRATCH_EXIT_RANGING_MINUTES if regime == "RANGING" else SCRATCH_EXIT_TRENDING_MINUTES
                    if hold_min >= scratch_min and abs(unrealized * 100) < SCRATCH_EXIT_MAX_ABS_PNL_PCT:
                        reason = "SCRATCH_EXIT"
                        exit_price = price

            # ─── Damage Reducer (cut losses early if bleeding badly) ───────
            if reason is None:
                from signal_copy.execution_config import (
                    DAMAGE_REDUCER_ENABLED, DAMAGE_REDUCER_MIN_HOLD_MINUTES,
                    DAMAGE_REDUCER_MAX_LOSS_PCT
                )
                if DAMAGE_REDUCER_ENABLED:
                    if hold_min >= DAMAGE_REDUCER_MIN_HOLD_MINUTES and (unrealized * 100) <= DAMAGE_REDUCER_MAX_LOSS_PCT:
                        reason = "DAMAGE_REDUCER"
                        exit_price = price

            # Profit lock — move SL to breakeven+ after sufficient profit & time.
            # Uses risk-based buffer: BE level = entry + (risk_distance * 0.15)
            # This gives the trade room to breathe while protecting capital.
            if reason is None and hold_min > PROFIT_LOCK_MIN_MINUTES and unrealized > (PROFIT_LOCK_TRIGGER_PCT / 100) and not pos.get("locked_profit"):
                risk_distance = abs(entry - pos.get("original_sl", pos["sl"]))
                # BE buffer = max of (fixed % buffer, 15% of risk distance)
                # This ensures buffer scales with the trade's risk profile
                fixed_buffer = entry * (PROFIT_LOCK_BUFFER_PCT / 100)
                risk_buffer = risk_distance * 0.15
                buffer = max(fixed_buffer, risk_buffer)

                if side == "LONG":
                    new_sl = max(pos["sl"], entry + buffer)
                    # Don't set BE above current price (would immediately trigger)
                    if new_sl >= price * 0.998:
                        new_sl = pos["sl"]  # skip, too tight
                else:
                    new_sl = min(pos["sl"], entry - buffer)
                    if new_sl <= price * 1.002:
                        new_sl = pos["sl"]  # skip, too tight

                if new_sl != pos["sl"]:
                    pos["sl"] = new_sl
                    pos["locked_profit"] = True
                    pos["sl_kind"] = "BREAKEVEN"
                    await self._update_server_sl(sym, side, new_sl)
                    logger.info(f"[PROFIT_LOCK] {sym} | new_sl={new_sl:.6f} | buffer={buffer:.6f} | risk_dist={risk_distance:.6f}")

            # Trailing SL
            if reason is None and pos.get("locked_profit") and hold_min > 10.0:
                old_sl = pos["sl"]
                self._update_trailing_sl(pos, price, sym)
                if pos["sl"] != old_sl:
                    pos["sl_kind"] = "TRAILING"

            # Check SL hit
            if reason is None:
                if side == "LONG" and price <= sl:
                    reason = "SL"
                    exit_price = sl
                elif side == "SHORT" and price >= sl:
                    reason = "SL"
                    exit_price = sl

            # Time exit with profit awareness
            if reason is None and hold_min >= pos.get("max_hold_minutes", HARD_MAX_HOLD_MINUTES):
                if unrealized > 0.005:
                    if not pos.get("time_extended"):
                        pos["time_extended"] = True
                        pos["max_hold_minutes"] = pos.get("max_hold_minutes", HARD_MAX_HOLD_MINUTES) + 30
                        logger.info(f"[TIME_EXTEND] {sym} | unrealized={unrealized*100:.2f}% | extended +30min")
                    else:
                        reason = "TIME_EXIT_EXTENDED"
                else:
                    reason = "TIME_EXIT"

            if reason:
                # Determine SL classification for notification
                sl_kind = pos.get("sl_kind", "ORIGINAL")
                if reason == "SL":
                    if sl_kind == "TRAILING":
                        normalized_reason = "DYNAMIC_SL"
                    elif sl_kind == "BREAKEVEN":
                        normalized_reason = "BREAKEVEN_STOP"
                    elif sl_kind == "DEFENSIVE":
                        normalized_reason = "DEFENSIVE_STOP"
                    else:
                        normalized_reason = "HARD_SL"
                    raw_reason = "SL"
                else:
                    normalized_reason = reason
                    raw_reason = reason

                # Close on exchange — ALWAYS send market close order.
                # Returns actual fill price for accurate PnL calculation.
                actual_fill = await self._close_position_market(sym, side, pos["qty"])

                # Use actual fill price if available, otherwise use detected price
                if actual_fill > 0:
                    exit_price = actual_fill
                # else: keep exit_price as detected (sl level or current price)

                # Recalculate PnL with actual exit price
                if side == "LONG":
                    pnl_pct = (exit_price - entry) / entry
                else:
                    pnl_pct = (entry - exit_price) / entry
                pnl_usd = pos["notional"] * pnl_pct

                # Cancel any remaining algo orders (SL/TP) to prevent ghost triggers
                if pos.get("sl_algo_id"):
                    await self._cancel_algo_order(sym, pos["sl_algo_id"])
                if pos.get("tp_algo_id"):
                    await self._cancel_algo_order(sym, pos["tp_algo_id"])

                trade = {
                    "timestamp_open": pos["open_time"].strftime("%Y-%m-%d %H:%M:%S"),
                    "timestamp_close": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "symbol": sym, "side": side, "entry_price": round(entry, 6),
                    "exit_price": round(exit_price, 6), "notional_usd": round(pos["notional"], 2),
                    "pnl_pct": round(pnl_pct * 100, 4), "pnl_usd": round(pnl_usd, 2),
                    "hold_minutes": round(hold_min, 2), "reason": normalized_reason,
                    "raw_reason": raw_reason,
                    "normalized_reason": normalized_reason,
                    "regime": pos.get("regime"),
                    "sl_original": round(original_sl, 6),
                    "active_sl_at_exit": round(sl, 6),
                    "sl_kind_at_exit": sl_kind,
                }
                await self._journal_trade(trade)
                closed.append(trade)

                # Send rich Telegram notification (same format as paper mode)
                await self._send_close_notification(trade)
                del self.positions[sym]

        return closed

    async def _send_close_notification(self, trade: Dict[str, Any]):
        """Send rich close notification to Telegram (same format as paper mode)."""
        try:
            from notifications.telegram_notifier import send_close_trade
            payload = {
                "symbol": trade.get("symbol", ""),
                "side": trade.get("side", ""),
                "exit_price": trade.get("exit_price", 0),
                "pnl_pct": trade.get("pnl_pct", 0),
                "pnl_usd": trade.get("pnl_usd", 0),
                "hold_minutes": trade.get("hold_minutes", 0),
                "reason": trade.get("normalized_reason", trade.get("reason", "UNKNOWN")),
                "normalized_reason": trade.get("normalized_reason", trade.get("reason", "UNKNOWN")),
                "raw_reason": trade.get("raw_reason", ""),
                "equity": self.current_balance,
                "balance_after": self.current_balance,
                "sl_original": trade.get("sl_original", 0),
                "active_sl_at_exit": trade.get("active_sl_at_exit", 0),
                "sl_kind_at_exit": trade.get("sl_kind_at_exit", "ORIGINAL"),
            }
            await send_close_trade(payload)
        except Exception as e:
            logger.error(f"[TELEGRAM_CLOSE] Failed to send notification for {trade.get('symbol')}: {e}")

    def _update_trailing_sl(self, pos: Dict, price: float, symbol: str):
        atr_pct = pos.get("adv_snapshot", {}).get("atr_pct", 2.5)
        trail_pct = max(TRAIL_SL_PCT / 100.0, (atr_pct / 100.0) * TRAIL_SL_ATR_MULTIPLIER)

        if pos["side"] == "LONG":
            pos["high_watermark"] = max(pos.get("high_watermark", price), price)
            new_sl = max(pos["sl"], pos["high_watermark"] * (1 - trail_pct))
            if new_sl > pos["sl"] and new_sl < price:
                pos["sl"] = new_sl
                asyncio.create_task(self._update_server_sl(symbol, "LONG", new_sl))
                logger.info(f"[TRAILING_SL] {symbol} LONG | new_sl={new_sl:.6f} | trail={trail_pct*100:.2f}%")
        else:
            pos["low_watermark"] = min(pos.get("low_watermark", price), price)
            new_sl = min(pos["sl"], pos["low_watermark"] * (1 + trail_pct))
            if new_sl < pos["sl"] and new_sl > price:
                pos["sl"] = new_sl
                asyncio.create_task(self._update_server_sl(symbol, "SHORT", new_sl))
                logger.info(f"[TRAILING_SL] {symbol} SHORT | new_sl={new_sl:.6f} | trail={trail_pct*100:.2f}%")

    async def _update_server_sl(self, symbol: str, side: str, new_sl: float):
        """Cancel existing SL (algo + legacy) and place new one."""
        try:
            # Cancel algo SL if we have the ID
            pos = self.positions.get(symbol)
            if pos and pos.get("sl_algo_id"):
                await self._cancel_algo_order(symbol, pos["sl_algo_id"])
                pos["sl_algo_id"] = None

            # Also cancel all legacy open orders for this symbol (belt & suspenders)
            await self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})

            # Place new SL via Algo Order API
            await self._set_stop_loss(symbol, side, new_sl)
        except Exception as e:
            logger.error(f"[BINANCE_SL_UPDATE] Failed for {symbol}: {e}")

    async def _update_server_tp(self, symbol: str, side: str, tp_price: float, qty: float):
        """Cancel existing TP and place new one with updated qty (after partial close)."""
        try:
            pos = self.positions.get(symbol)
            if pos and pos.get("tp_algo_id"):
                await self._cancel_algo_order(symbol, pos["tp_algo_id"])
                pos["tp_algo_id"] = None

            # Place new TP with remaining qty
            await self._set_take_profit(symbol, side, tp_price, qty=qty)
        except Exception as e:
            logger.error(f"[BINANCE_TP_UPDATE] Failed for {symbol}: {e}")

    async def _close_position_market(self, symbol: str, side: str, qty: float) -> float:
        """Close position with market order. Returns actual fill price (0 if failed)."""
        try:
            close_side = "SELL" if side == "LONG" else "BUY"
            params = {
                "symbol": symbol,
                "side": close_side,
                "type": "MARKET",
                "quantity": self._fmt_qty(symbol, qty),
                "reduceOnly": "true",
                "newOrderRespType": "RESULT",
            }
            result = await self._request("POST", "/fapi/v1/order", params)
            avg_price = float(result.get("avgPrice", 0))
            filled_qty = float(result.get("executedQty", 0))
            if avg_price > 0 and filled_qty > 0:
                logger.info(f"[BINANCE_CLOSE] {symbol} {side} closed @ {avg_price:.6f} | qty={filled_qty}")
                return avg_price
            elif result.get("orderId"):
                order_id = int(result.get("orderId"))
                try:
                    trades = await self._request("GET", "/fapi/v1/userTrades", {
                        "symbol": symbol,
                        "limit": "20",
                    })
                    fills = [t for t in trades if int(t.get("orderId", -1)) == order_id] if isinstance(trades, list) else []
                    total_qty = sum(float(t.get("qty", 0) or 0) for t in fills)
                    if total_qty > 0:
                        total_quote = sum(float(t.get("price", 0) or 0) * float(t.get("qty", 0) or 0) for t in fills)
                        fill_price = total_quote / total_qty
                        logger.info(f"[BINANCE_CLOSE] {symbol} {side} closed @ {fill_price:.6f} | qty={total_qty} (from userTrades)")
                        return fill_price
                except Exception as exc:
                    logger.warning(f"[BINANCE_CLOSE] userTrades fallback failed for {symbol}: {exc}")
                logger.info(f"[BINANCE_CLOSE] {symbol} {side} closed via market order (no avgPrice/userTrades fill)")
                return 0.0
            else:
                logger.error(f"[BINANCE_CLOSE] {symbol} failed: {result}")
                return 0.0
        except Exception as e:
            logger.error(f"[BINANCE_CLOSE] Failed for {symbol}: {e}")
            return 0.0

    # ─── Reconciliation ───────────────────────────────────────────────────

    async def start_reconciliation_loop(self, interval_sec: int = 30):
        while True:
            try:
                await asyncio.sleep(interval_sec)
                await self._reconcile()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[RECONCILE] error: {e}")
                await asyncio.sleep(10)

    async def _reconcile(self):
        """Sync local state with exchange positions."""
        try:
            data = await self._request("GET", "/fapi/v2/positionRisk")
            if not isinstance(data, list):
                return

            exchange_positions = {}
            for pos in data:
                amt = float(pos.get("positionAmt", 0))
                if amt == 0:
                    continue
                sym = pos["symbol"]
                exchange_positions[sym] = pos
                mark = float(pos.get("markPrice", 0))

            # Remove local positions that no longer exist on exchange (SL/TP hit)
            for sym in list(self.positions.keys()):
                if sym not in exchange_positions:
                    local = self.positions[sym]
                    now = datetime.now(UTC)
                    hold_min = (now - local["open_time"]).total_seconds() / 60.0
                    entry = local["entry_price"]
                    sl = local["sl"]
                    tp3 = local.get("tp3", 0)
                    side = local["side"]
                    original_sl = local.get("original_sl", sl)
                    sl_kind = local.get("sl_kind", "ORIGINAL")

                    # Determine exit price and reason by checking which algo triggered.
                    # Fetch last trade for this symbol to get actual fill price.
                    exit_price = 0.0
                    normalized_reason = "EXCHANGE_CLOSED"
                    try:
                        trades_data = await self._request("GET", "/fapi/v1/userTrades", {
                            "symbol": sym, "limit": "5"
                        })
                        if isinstance(trades_data, list) and trades_data:
                            last_trade = trades_data[-1]
                            exit_price = float(last_trade.get("price", 0))
                    except Exception:
                        pass

                    # If we couldn't get fill price, estimate from SL/TP
                    if exit_price <= 0:
                        exit_price = sl  # fallback

                    # Classify: was it SL or TP that triggered?
                    if tp3 > 0:
                        if side == "LONG" and exit_price >= tp3 * 0.995:
                            normalized_reason = "TP_FULL"
                        elif side == "SHORT" and exit_price <= tp3 * 1.005:
                            normalized_reason = "TP_FULL"
                        elif side == "LONG" and exit_price <= sl * 1.005:
                            normalized_reason = "HARD_SL" if sl_kind == "ORIGINAL" else "DYNAMIC_SL"
                        elif side == "SHORT" and exit_price >= sl * 0.995:
                            normalized_reason = "HARD_SL" if sl_kind == "ORIGINAL" else "DYNAMIC_SL"
                        else:
                            normalized_reason = "EXCHANGE_CLOSED"
                    else:
                        # No TP set, must be SL
                        if side == "LONG" and exit_price <= entry:
                            normalized_reason = "HARD_SL" if sl_kind == "ORIGINAL" else "DYNAMIC_SL"
                        elif side == "SHORT" and exit_price >= entry:
                            normalized_reason = "HARD_SL" if sl_kind == "ORIGINAL" else "DYNAMIC_SL"
                        else:
                            normalized_reason = "EXCHANGE_CLOSED"

                    # Calculate PnL from actual exit price
                    if side == "LONG":
                        pnl_pct = (exit_price - entry) / entry
                    else:
                        pnl_pct = (entry - exit_price) / entry
                    pnl_usd = local["notional"] * pnl_pct

                    logger.info(f"[RECONCILE] {sym} {side} closed on exchange | reason={normalized_reason} | exit={exit_price:.6f} | pnl={pnl_pct*100:.2f}%")

                    trade = {
                        "timestamp_open": local["open_time"].strftime("%Y-%m-%d %H:%M:%S"),
                        "timestamp_close": now.strftime("%Y-%m-%d %H:%M:%S"),
                        "symbol": sym, "side": side,
                        "entry_price": round(entry, 6), "exit_price": round(exit_price, 6),
                        "notional_usd": round(local["notional"], 2),
                        "pnl_pct": round(pnl_pct * 100, 4), "pnl_usd": round(pnl_usd, 2),
                        "hold_minutes": round(hold_min, 2),
                        "reason": normalized_reason,
                        "raw_reason": "EXCHANGE_CLOSED",
                        "normalized_reason": normalized_reason,
                        "regime": local.get("regime"),
                        "sl_original": round(original_sl, 6),
                        "active_sl_at_exit": round(sl, 6),
                        "sl_kind_at_exit": sl_kind,
                    }
                    await self._journal_trade(trade)
                    await self._send_close_notification(trade)

                    # CRITICAL: Cancel remaining algo orders (the other side SL or TP)
                    if local.get("sl_algo_id"):
                        await self._cancel_algo_order(sym, local["sl_algo_id"])
                    if local.get("tp_algo_id"):
                        await self._cancel_algo_order(sym, local["tp_algo_id"])
                    # Also cancel any legacy open orders for this symbol
                    try:
                        await self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": sym})
                    except Exception:
                        pass

                    del self.positions[sym]

            await self._update_balance()
        except Exception as e:
            logger.error(f"[RECONCILE] error: {e}")

    # ─── Cleanup ──────────────────────────────────────────────────────────

    async def _cancel_all_algo_orders(self):
        """Cancel all open algo orders across all symbols."""
        try:
            open_algos = await self._get_open_algo_orders()
            if not open_algos:
                return
            for algo in open_algos:
                algo_id = algo.get("algoId")
                symbol = algo.get("symbol", "")
                if algo_id:
                    await self._cancel_algo_order(symbol, int(algo_id))
            logger.info(f"[BINANCE_SHUTDOWN] Cancelled {len(open_algos)} algo orders")
        except Exception as e:
            logger.error(f"[BINANCE_SHUTDOWN] Error cancelling algo orders: {e}")

    async def close_all_positions(self, reason: str = "MANUAL_SHUTDOWN_FLAG"):
        logger.info("[BINANCE_TESTNET] Closing all positions...")

        # Cancel all algo orders first (SL/TP placed via Algo Order API)
        await self._cancel_all_algo_orders()

        # Close each position via market order + send notification
        now = datetime.now(UTC)
        cumulative_pnl = 0.0
        for sym, pos in list(self.positions.items()):
            side = pos["side"]
            entry = pos["entry_price"]
            actual_fill = await self._close_position_market(sym, side, pos["qty"])
            exit_price = actual_fill if actual_fill > 0 else entry

            # Calculate PnL
            if side == "LONG":
                pnl_pct = (exit_price - entry) / entry
            else:
                pnl_pct = (entry - exit_price) / entry
            pnl_usd = pos["notional"] * pnl_pct
            cumulative_pnl += pnl_usd
            hold_min = (now - pos["open_time"]).total_seconds() / 60.0

            # Update running equity for accurate per-trade reporting
            self.current_balance += pnl_usd

            trade = {
                "timestamp_open": pos["open_time"].strftime("%Y-%m-%d %H:%M:%S"),
                "timestamp_close": now.strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": sym, "side": side, "entry_price": round(entry, 6),
                "exit_price": round(exit_price, 6), "notional_usd": round(pos["notional"], 2),
                "pnl_pct": round(pnl_pct * 100, 4), "pnl_usd": round(pnl_usd, 2),
                "hold_minutes": round(hold_min, 2),
                "reason": reason,
                "raw_reason": reason,
                "normalized_reason": reason,
                "regime": pos.get("regime"),
                "sl_original": round(pos.get("original_sl", pos["sl"]), 6),
                "active_sl_at_exit": round(pos["sl"], 6),
                "sl_kind_at_exit": pos.get("sl_kind", "ORIGINAL"),
            }
            await self._journal_trade(trade)
            await self._send_close_notification(trade)

            # Cancel per-symbol legacy orders
            try:
                await self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": sym})
            except Exception:
                pass

        self.positions.clear()
        # Sync actual balance from exchange
        await self._update_balance()

    async def force_close_all_positions(self, reason: str = "FATAL", **kwargs):
        from signal_copy.execution_config import CLOSE_POSITIONS_ON_SHUTDOWN
        if not CLOSE_POSITIONS_ON_SHUTDOWN and reason == "MANUAL_SHUTDOWN_FLAG":
            logger.info(f"[BINANCE_TESTNET] Keeping {len(self.positions)} positions open (CLOSE_POSITIONS_ON_SHUTDOWN=False)")
            logger.info(f"[BINANCE_TESTNET] Exchange SL/TP remain active as protection during restart")
            for sym, pos in self.positions.items():
                logger.info(f"  ♻️ {pos['side']} {sym} @ {pos['entry_price']:.6f} | SL={pos['sl']:.6f} | algo_sl={'✅' if pos.get('sl_algo_id') else '❌'}")
            return
        # For FATAL or explicit close, always close all
        logger.info(f"[BINANCE_TESTNET] Force close all positions | reason={reason}")
        await self.close_all_positions(reason=reason)

    def export_trade_history_to_excel(self, filename: str = "trade_history_binance_testnet.xlsx"):
        if not self.trade_history:
            return
        try:
            import pandas as pd
            df = pd.DataFrame(self.trade_history)
            df.to_excel(filename, index=False)
            logger.info(f"💾 Trade History exported ({len(self.trade_history)} trades)")
        except Exception as e:
            logger.error(f"Export error: {e}")
