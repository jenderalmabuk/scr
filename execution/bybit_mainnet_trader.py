"""
Bybit Mainnet Trader for SignalCopy with Dynamic Exit Layer.

Real money execution on Bybit USDT Perpetual Futures with:
- Fixed $2 risk per trade
- Dynamic exits: SCRATCH, DAMAGE_REDUCER, PROFIT_LOCK, TRAILING
- Provider SL/TP preservation
- Multi-exchange quote freshness
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Dict, Optional, Any

from execution.bybit_api_client import BybitClient, BybitAPIError
from execution.smart_scratch_exit import calculate_smart_scratch_timeout


logger = logging.getLogger(__name__)


class BybitMainnetTrader:
    """
    Real money trader for Bybit USDT perpetual futures.
    
    Features:
    - Fixed risk per trade (default $2)
    - Dynamic exit layer (scratch/damage/profit/trailing)
    - Position lifecycle management
    - Journal logging
    """
    
    # Dynamic exit epoch marker - trades before this are pre-epoch
    DYNAMIC_EXIT_EPOCH_UTC = "2026-09-06T12:43:00Z"
    
    def __init__(self, journal_path: str = "/app/journal"):
        """Initialize Bybit mainnet trader."""
        self.client = BybitClient()
        self.journal_path = Path(journal_path)
        self.journal_path.mkdir(parents=True, exist_ok=True)
        
        # Configuration from env
        self.capital = float(os.getenv("SIGNALCOPY_CAPITAL", "100"))
        self.risk_per_trade = float(os.getenv("SIGNALCOPY_RISK_PER_TRADE_USD", "2"))
        self.max_leverage = int(os.getenv("SIGNALCOPY_MAX_LEVERAGE", "10"))
        self.max_concurrent = int(os.getenv("SIGNALCOPY_MAX_CONCURRENT", "5"))
        
        # Dynamic exit configuration
        self.dynamic_exit_enabled = self._config_bool("SIGNALCOPY_BYBIT_DYNAMIC_EXIT", default=True)
        
        # Load positions and state
        self.positions: Dict[str, Dict] = {}
        self._load_positions()
        
        logger.info(
            f"[BYBIT_MAINNET] Initialized: capital=${self.capital}, "
            f"risk=${self.risk_per_trade}/trade, max_leverage={self.max_leverage}x, "
            f"max_concurrent={self.max_concurrent}, dynamic_exit={self.dynamic_exit_enabled}"
        )
    
    def _config_bool(self, key: str, default: bool = False) -> bool:
        """Get boolean config from env."""
        val = os.getenv(key, "").lower()
        if not val:
            return default
        return val in ("1", "true", "yes", "on")
    
    async def _get_fresh_open_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Fresh Bybit quote with receipt-time provenance.
        
        Returns dict with price, source, timestamp, freshness flag.
        Gateway uses this for price validation before entry.
        """
        try:
            ticker = self.client.get_ticker(symbol)
            if ticker and "lastPrice" in ticker:
                price = float(ticker["lastPrice"])
                if price > 0:
                    return {
                        "price": price,
                        "price_source": "bybit_usdt_perp",
                        "price_observed_at": datetime.now(timezone.utc).isoformat(),
                        "price_age_sec": 0.0,
                        "price_fresh": True
                    }
        except Exception as e:
            logger.warning(f"[BYBIT_MAINNET] Fresh open price fetch failed {symbol}: {e}")
        return None
    
    async def _get_fresh_close_quote(self, symbol: str) -> Dict[str, float]:
        """
        Get fresh mark price for close/trailing operations.
        
        Returns dict with mark_price for dynamic exit calculations.
        """
        try:
            ticker = self.client.get_ticker(symbol)
            if ticker and "markPrice" in ticker:
                mark_price = float(ticker["markPrice"])
                if mark_price > 0:
                    return {
                        "mark_price": mark_price,
                        "last_price": float(ticker.get("lastPrice", mark_price)),
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    }
        except Exception as e:
            logger.warning(f"[BYBIT_MAINNET] Fresh close quote fetch failed {symbol}: {e}")
        
        # Fallback to last price if mark price not available
        try:
            ticker = self.client.get_ticker(symbol)
            if ticker and "lastPrice" in ticker:
                price = float(ticker["lastPrice"])
                return {
                    "mark_price": price,
                    "last_price": price,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }
        except:
            pass
        
        raise Exception(f"Failed to get fresh close quote for {symbol}")
    
    async def _get_fresh_open_price(self, symbol: str) -> tuple[float, str]:
        """Get fresh open price for entry execution."""
        quote = await self._get_fresh_open_quote(symbol)
        if quote:
            return (float(quote["price"]), str(quote["price_source"]))
        return (0.0, "fresh_price_unavailable")
    
    @staticmethod
    def _lifecycle_price(quote: Dict[str, Any]) -> Optional[float]:
        """Return only fresh multi-exchange lifecycle truth."""
        if not isinstance(quote, dict) or quote.get("price_fresh") is not True:
            return None
        price = float(quote.get("price") or 0)
        return price if price > 0 else None
    
    def _load_positions(self):
        """Load open positions from journal."""
        positions_file = self.journal_path / "open_positions.json"
        if positions_file.exists():
            try:
                data = json.loads(positions_file.read_text())
                # Handle both dict (symbol->position) and list formats
                if isinstance(data, dict):
                    self.positions = {k: v for k, v in data.items() if v.get("exchange") == "bybit"}
                else:
                    self.positions = {p["symbol"]: p for p in data if p.get("exchange") == "bybit"}
                logger.info(f"[BYBIT_MAINNET] Loaded {len(self.positions)} positions from journal")
            except Exception as e:
                logger.error(f"[BYBIT_MAINNET] Failed to load positions: {e}")
                self.positions = {}
    
    def _save_positions(self):
        """Save positions to journal."""
        positions_file = self.journal_path / "open_positions.json"
        try:
            # Load existing positions from other exchanges
            existing = {}
            if positions_file.exists():
                data = json.loads(positions_file.read_text())
                # Handle both dict and list formats
                if isinstance(data, dict):
                    existing = {k: v for k, v in data.items() if v.get("exchange") != "bybit"}
                else:
                    # Convert list to dict
                    existing = {p["symbol"]: p for p in data if p.get("exchange") != "bybit"}
            
            # Merge our positions
            all_positions = {**existing, **self.positions}
            
            # Write atomically
            tmp_file = positions_file.with_suffix(".tmp")
            tmp_file.write_text(json.dumps(all_positions, indent=2))
            tmp_file.replace(positions_file)
            
        except Exception as e:
            logger.error(f"[BYBIT_MAINNET] Failed to save positions: {e}")
    
    def _append_history(self, trade: Dict):
        """Append closed trade to history."""
        history_file = self.journal_path / "trade_history.json"
        try:
            history = []
            if history_file.exists():
                history = json.loads(history_file.read_text())
            
            history.append(trade)
            
            tmp_file = history_file.with_suffix(".tmp")
            tmp_file.write_text(json.dumps(history, indent=2))
            tmp_file.replace(history_file)
            
        except Exception as e:
            logger.error(f"[BYBIT_MAINNET] Failed to append history: {e}")
    
    def _quantize(self, value: float, tick_size: float) -> str:
        """Round value to tick size."""
        if tick_size >= 1:
            precision = 0
        else:
            precision = len(str(tick_size).rstrip('0').split('.')[-1])
        
        d = Decimal(str(value))
        quantized = d.quantize(Decimal(str(tick_size)), rounding=ROUND_DOWN)
        return f"{quantized:.{precision}f}"
    
    async def _fetch_fresh_quote(self, symbol: str) -> Optional[Dict[str, float]]:
        """Fetch fresh multi-exchange quote."""
        try:
            ticker = self.client.get_ticker(symbol)
            
            mark = float(ticker.get("markPrice", 0))
            bid = float(ticker.get("bid1Price", 0))
            ask = float(ticker.get("ask1Price", 0))
            
            if not all([mark, bid, ask]):
                logger.warning(f"[BYBIT_MAINNET] Incomplete ticker for {symbol}")
                return None
            
            return {
                "mark": mark,
                "bid": bid,
                "ask": ask,
                "timestamp": time.time()
            }
        
        except Exception as e:
            logger.error(f"[BYBIT_MAINNET] Failed to fetch quote for {symbol}: {e}")
            return None
    
    def _calculate_position_size(
        self,
        symbol: str,
        entry: float,
        sl: float,
        lot_size: float
    ) -> tuple[str, float]:
        """
        Calculate position size for fixed risk.
        
        Returns: (qty_str, notional_usd)
        """
        # Risk distance in price
        risk_distance = abs(entry - sl)
        
        if risk_distance == 0:
            logger.error(f"[BYBIT_MAINNET] {symbol} SL == entry, cannot size")
            return ("0", 0.0)
        
        # Qty for fixed risk
        qty_for_risk = self.risk_per_trade / risk_distance
        
        # Round down to lot size
        qty_rounded = int(qty_for_risk / lot_size) * lot_size
        
        if qty_rounded <= 0:
            logger.warning(f"[BYBIT_MAINNET] {symbol} qty rounded to 0 (risk_distance={risk_distance:.4f})")
            return ("0", 0.0)
        
        # Quantize
        qty_str = self._quantize(qty_rounded, lot_size)
        notional = qty_rounded * entry
        
        return (qty_str, notional)
    
    async def open_position(
        self,
        symbol: str,
        side: str,  # "LONG" or "SHORT"
        entry_price: float,
        sl_price: float,
        tp_prices: list,
        signal_metadata: Dict = None
    ) -> bool:
        """
        Open new position with fixed risk.
        
        Args:
            symbol: Trading pair (e.g. "BTCUSDT")
            side: "LONG" or "SHORT"
            entry_price: Entry price from signal
            sl_price: Stop loss from signal
            tp_prices: List of TP levels from signal
            signal_metadata: Original signal data
        
        Returns: True if position opened successfully
        """
        try:
            # Check capacity
            if len(self.positions) >= self.max_concurrent:
                logger.warning(
                    f"[BYBIT_MAINNET] Max concurrent reached ({self.max_concurrent}), "
                    f"rejecting {symbol}"
                )
                return False
            
            # Get instrument info
            instrument = self.client.get_instrument_info(symbol)
            lot_size = float(instrument["lotSizeFilter"]["qtyStep"])
            tick_size = float(instrument["priceFilter"]["tickSize"])
            
            # Calculate position size
            qty_str, notional = self._calculate_position_size(symbol, entry_price, sl_price, lot_size)
            
            if qty_str == "0":
                logger.error(f"[BYBIT_MAINNET] {symbol} size calculation failed")
                return False
            
            # Check leverage
            required_margin = notional / self.max_leverage
            logger.info(
                f"[BYBIT_MAINNET] {symbol} {side}: qty={qty_str}, "
                f"notional=${notional:.2f}, margin=${required_margin:.2f}"
            )
            
            # Quantize prices
            entry_str = self._quantize(entry_price, tick_size)
            sl_str = self._quantize(sl_price, tick_size)
            tp1_str = self._quantize(tp_prices[0], tick_size) if tp_prices else None
            
            # Determine order type (market vs pending based on entry zone)
            quote = await self._fetch_fresh_quote(symbol)
            if not quote:
                logger.error(f"[BYBIT_MAINNET] {symbol} no quote available")
                return False
            
            mark = quote["mark"]
            
            # Calculate entry zone (0.1R threshold)
            risk = abs(entry_price - sl_price)
            distance_from_entry = abs(mark - entry_price)
            distance_in_R = distance_from_entry / risk if risk > 0 else 0
            
            # Entry zone logic: < 0.1R = market, >= 0.1R = pending
            if distance_in_R < 0.1:
                order_type = "Market"
                actual_entry = mark  # Will fill at current price
                logger.info(
                    f"[BYBIT_MAINNET] {symbol} inside entry zone "
                    f"({distance_in_R:.2f}R) → MARKET order"
                )
            else:
                order_type = "Limit"
                actual_entry = entry_price  # Pending at signal entry
                logger.info(
                    f"[BYBIT_MAINNET] {symbol} outside entry zone "
                    f"({distance_in_R:.2f}R) → PENDING limit @ {entry_price:.6f}"
                )
            
            # Place order
            bybit_side = "Buy" if side == "LONG" else "Sell"
            
            order_result = self.client.create_order(
                symbol=symbol,
                side=bybit_side,
                order_type=order_type,
                qty=qty_str,
                price=entry_str if order_type == "Limit" else None,
                stop_loss=sl_str,
                take_profit=None,  # Don't set TP - bot will close partial manually via TP ladder
                order_link_id=f"SC_{symbol}_{int(time.time() * 1000)}"
            )
            
            order_id = order_result.get("orderId")
            
            if not order_id:
                logger.error(f"[BYBIT_MAINNET] {symbol} order failed: {order_result}")
                return False
            
            # For pending orders, store in pending tracking (will move to positions when filled)
            if order_type == "Limit":
                logger.info(
                    f"[BYBIT_MAINNET] PENDING {side} {symbol} @ {entry_price:.6f} | "
                    f"order_id={order_id} | Will check fill status in maintenance loop"
                )
                # Note: Bybit will auto-fill and we'll detect via sync_positions()
                # For now, just log - no need to track separately
                return True
            
            # Store position (market orders fill immediately)
            now = datetime.now(timezone.utc)
            
            # Calculate initial risk for blended R tracking
            initial_qty = float(qty_str)
            risk_distance = abs(entry_price - sl_price)
            risk_usd = initial_qty * risk_distance
            
            position = {
                "symbol": symbol,
                "exchange": "bybit",
                "side": side,
                "entry_price": entry_price,
                "sl_price": sl_price,
                "tp_prices": tp_prices,
                "tp_hit": [],  # Track which TPs have been hit
                "qty": qty_str,
                "qty_remaining": float(qty_str),  # Track remaining position size
                "initial_qty": initial_qty,  # Store initial qty for R calculation
                "risk_usd": risk_usd,  # Store initial risk for blended R
                "realized_pnl_usd": 0.0,  # Accumulate PnL from partial closes
                "notional": notional,
                "order_id": order_id,
                "opened_at": now.isoformat(),
                "opened_ts": now.timestamp(),
                "sl_kind": "ORIGINAL",
                "locked_profit": False,
                "metadata": signal_metadata or {}
            }
            
            self.positions[symbol] = position
            self._save_positions()
            
            logger.info(
                f"[BYBIT_MAINNET] OPENED {side} {symbol} @ {entry_price:.6f} | "
                f"qty={qty_str} | SL={sl_price:.6f} | TP1={tp_prices[0]:.6f}"
            )
            
            # Send entry notification (simple format)
            try:
                from notifications.telegram_notifier import send_open_trade
                await send_open_trade({
                    "symbol": symbol,
                    "side": side,
                    "entry_price": entry_price,
                    "entry": entry_price,
                    "sl_price": sl_price,
                    "sl": sl_price,
                    "tp1": tp_prices[0] if tp_prices else 0,
                    "notional": notional,
                    "size_usd": notional,
                })
            except Exception as notif_err:
                logger.warning(f"[BYBIT_MAINNET] Notification failed: {notif_err}")
            
            return True
            
        except Exception as e:
            logger.error(f"[BYBIT_MAINNET] Failed to open {symbol}: {e}")
            return False
    
    async def submit_open(self, timeout_sec: float = 30.0, **params) -> Optional[Dict[str, Any]]:
        """
        Gateway-compatible entry point for opening positions.
        Validates params and delegates to open_position().
        
        Returns dict with execution status for gateway.
        """
        try:
            symbol = str(params.get("symbol", ""))
            side = str(params.get("side", "")).upper()
            sl_price = float(params.get("sl", params.get("sl_price", 0)) or 0)
            
            # Parse TP ladder
            tp_ladder = []
            for i in range(1, 21):
                value = float(params.get(f"tp{i}", 0) or 0)
                if value:
                    tp_ladder.append(value)
            if not tp_ladder and params.get("tp_full"):
                tp_ladder.append(float(params["tp_full"]))
            
            # Validate basic params
            if not symbol or side not in ("LONG", "SHORT"):
                logger.warning(f"[BYBIT_MAINNET] Invalid params symbol={symbol} side={side}")
                return {"ok": False, "executed": False, "code": "INVALID_PARAMS",
                        "reason": "Invalid symbol or side"}
            
            # Check if already open
            if symbol in self.positions:
                logger.info(f"[BYBIT_MAINNET] {symbol} already open — skip dup")
                return {"ok": False, "executed": False, "code": "ALREADY_OPEN",
                        "reason": f"{symbol} already open"}
            
            # Validate price quote
            quote = params.get("execution_price_quote")
            if not isinstance(quote, dict) or quote.get("price_fresh") is not True:
                return {"ok": False, "executed": False, "code": "PRICE_PROVENANCE_REQUIRED",
                        "reason": "Fresh price quote required"}
            
            try:
                observed_at = datetime.fromisoformat(str(quote["price_observed_at"]).replace("Z", "+00:00"))
                age_sec = (datetime.now(timezone.utc) - observed_at.astimezone(timezone.utc)).total_seconds()
                mark = float(quote["price"])
                mark_source = str(quote["price_source"])
            except (KeyError, TypeError, ValueError):
                return {"ok": False, "executed": False, "code": "INVALID_QUOTE",
                        "reason": "Invalid price quote format"}
            
            if mark <= 0 or age_sec > 30.0:
                return {"ok": False, "executed": False, "code": "STALE_GATEWAY_MARK",
                        "reason": f"Stale mark: age={age_sec:.1f}s source={mark_source}"}
            
            requested_entry = float(params.get("entry_price", 0) or 0)
            
            # Check if entry diverges too much from mark
            if requested_entry > 0 and mark > 0 and abs(mark - requested_entry) / requested_entry > 0.05:
                logger.warning(f"[BYBIT_MAINNET] {symbol} stale mark {mark:.6f} vs route {requested_entry:.6f}")
                return {"ok": False, "executed": False, "code": "PRICE_DIVERGENCE",
                        "reason": f"Mark {mark:.6f} vs entry {requested_entry:.6f} >5%"}
            
            # Use requested entry or mark
            entry_price = requested_entry if requested_entry > 0 else mark
            
            # Sanity: check SL hasn't been hit already
            if side == "LONG" and sl_price and mark <= sl_price:
                logger.warning(f"[BYBIT_MAINNET] {symbol} LONG mark {mark:.6f} <= SL {sl_price:.6f}")
                return {"ok": False, "executed": False, "code": "SL_ALREADY_HIT",
                        "reason": f"Mark {mark:.6f} already at/below SL {sl_price:.6f}"}
            if side == "SHORT" and sl_price and mark >= sl_price:
                logger.warning(f"[BYBIT_MAINNET] {symbol} SHORT mark {mark:.6f} >= SL {sl_price:.6f}")
                return {"ok": False, "executed": False, "code": "SL_ALREADY_HIT",
                        "reason": f"Mark {mark:.6f} already at/above SL {sl_price:.6f}"}
            
            # Filter TPs that haven't been hit
            tp_ladder = [tp for tp in tp_ladder
                        if (tp > mark if side == "LONG" else tp < mark)]
            
            if not tp_ladder:
                logger.warning(f"[BYBIT_MAINNET] {symbol} {side} all TPs already passed")
                return {"ok": False, "executed": False, "code": "ALL_TPS_PASSED",
                        "reason": "All take profit levels already passed"}
            
            # Sanity: SL must be on correct side of entry
            if side == "LONG" and sl_price >= entry_price:
                logger.warning(f"[BYBIT_MAINNET] {symbol} LONG SL {sl_price:.6f} >= entry {entry_price:.6f}")
                return {"ok": False, "executed": False, "code": "INVALID_SL",
                        "reason": "SL must be below entry for LONG"}
            if side == "SHORT" and sl_price and sl_price <= entry_price:
                logger.warning(f"[BYBIT_MAINNET] {symbol} SHORT SL {sl_price:.6f} <= entry {entry_price:.6f}")
                return {"ok": False, "executed": False, "code": "INVALID_SL",
                        "reason": "SL must be above entry for SHORT"}
            
            # Attempt to open position
            success = await self.open_position(
                symbol=symbol,
                side=side,
                entry_price=entry_price,
                sl_price=sl_price,
                tp_prices=tp_ladder,
                signal_metadata=params
            )
            
            if success:
                return {
                    "ok": True,
                    "executed": True,
                    "symbol": symbol,
                    "side": side,
                    "entry_price": entry_price,
                    "sl_price": sl_price,
                    "tp_prices": tp_ladder
                }
            else:
                return {"ok": False, "executed": False, "code": "EXECUTION_FAILED",
                        "reason": "Failed to execute order on exchange"}
                
        except Exception as e:
            logger.error(f"[BYBIT_MAINNET] submit_open error: {e}", exc_info=True)
            return {"ok": False, "executed": False, "code": "EXCEPTION",
                    "reason": str(e)}
    
    async def _close(
        self,
        symbol: str,
        reason: str,
        sl_kind: str = None,
        exit_price: float = None
    ):
        """Close full position."""
        pos = self.positions.get(symbol)
        if not pos:
            logger.warning(f"[BYBIT_MAINNET] Cannot close {symbol}: not in positions")
            return
        
        # Use qty_remaining if available (for TP ladder compatibility)
        qty = pos.get("qty_remaining", float(pos.get("qty", 0)))
        await self._close_partial(symbol, reason, exit_price, qty, 0, sl_kind, full_close=True)
    
    async def _close_partial(
        self,
        symbol: str,
        reason: str,
        exit_price: float = None,
        close_qty: float = None,
        qty_remaining: float = None,
        sl_kind: str = None,
        full_close: bool = False
    ):
        """Close partial or full position."""
        pos = self.positions.get(symbol)
        if not pos:
            logger.warning(f"[BYBIT_MAINNET] Cannot close {symbol}: not in positions")
            return
        
        try:
            side = pos["side"]
            entry_price = pos["entry_price"]
            original_qty = float(pos.get("qty", 0))
            
            # Use provided qty or default to remaining/full
            if close_qty is None:
                close_qty = pos.get("qty_remaining", original_qty)
            
            # Get exit price
            if exit_price is None:
                quote = await self._fetch_fresh_quote(symbol)
                if quote:
                    exit_price = quote["mark"]
                else:
                    logger.warning(f"[BYBIT_MAINNET] {symbol} no quote for close, using entry")
                    exit_price = entry_price
            
            # Close on exchange
            bybit_side = "Sell" if side == "LONG" else "Buy"
            
            try:
                # Quantize close qty
                instrument = self.client.get_instrument_info(symbol)
                lot_size = float(instrument["lotSizeFilter"]["qtyStep"])
                close_qty_str = self._quantize(close_qty, lot_size)
                
                # Create order with unique link ID to fetch fill later
                order_link_id = f"SC_CLOSE_{symbol}_{int(time.time() * 1000)}"
                
                order_result = self.client.create_order(
                    symbol=symbol,
                    side=bybit_side,
                    order_type="Market",
                    qty=close_qty_str,
                    reduce_only=True,
                    order_link_id=order_link_id
                )
                
                # Wait briefly for order to fill
                await asyncio.sleep(1.5)
                
                # Fetch actual fill price from order history
                # NOTE: Bybit /v5/order/history GET request fails signature validation
                # when orderLinkId parameter is included (API bug/limitation).
                # Workaround: fetch recent orders by symbol, then filter by orderLinkId
                actual_fill_price = None
                try:
                    order_history = self.client.get_order_history(
                        symbol=symbol,
                        limit=10  # Get recent orders, filter locally
                    )
                    
                    # Find our order by orderLinkId
                    matching_order = None
                    for order in order_history:
                        if order.get("orderLinkId") == order_link_id:
                            matching_order = order
                            break
                    
                    if matching_order:
                        # avgPrice is the actual fill price
                        avg_price_str = matching_order.get("avgPrice", "0")
                        if avg_price_str and avg_price_str != "0" and avg_price_str != "":
                            actual_fill_price = float(avg_price_str)
                            logger.info(
                                f"[BYBIT_MAINNET] {symbol} actual fill: {actual_fill_price:.6f} "
                                f"(expected: {exit_price:.6f})"
                            )
                    else:
                        logger.warning(
                            f"[BYBIT_MAINNET] {symbol} order {order_link_id} not found in recent history"
                        )
                except Exception as fetch_err:
                    logger.warning(
                        f"[BYBIT_MAINNET] {symbol} could not fetch actual fill: {fetch_err}"
                    )
                
                # Use actual fill if available, otherwise use mark price estimate
                if actual_fill_price:
                    exit_price = actual_fill_price
                    
            except Exception as e:
                err_str = str(e)
                # Suppress expected errors
                if "110017" in err_str and "zero position" in err_str:
                    logger.info(f"[BYBIT_MAINNET] {symbol} already closed externally")
                    self.positions.pop(symbol, None)
                    self._save_positions()
                    return
                elif "34040" in err_str and "not modified" in err_str:
                    logger.debug(f"[BYBIT_MAINNET] {symbol} close order not modified")
                    return
                else:
                    logger.error(f"[BYBIT_MAINNET] Failed to close {symbol}: {e}")
                    return
            
            # Calculate PnL for this partial close
            # Use actual fill price (already updated above if fetch succeeded)
            if side == "LONG":
                gross_pnl = (exit_price - entry_price) * close_qty
            else:
                gross_pnl = (entry_price - exit_price) * close_qty
            
            # Estimate trading fees (Bybit taker: ~0.055% per side)
            # Entry fee + Exit fee = ~0.11% total round-trip
            entry_notional = entry_price * close_qty
            exit_notional = exit_price * close_qty
            estimated_entry_fee = entry_notional * 0.00055  # 0.055% taker
            estimated_exit_fee = exit_notional * 0.00055    # 0.055% taker
            total_fees = estimated_entry_fee + estimated_exit_fee
            
            # Net PnL after fees
            pnl_usd = gross_pnl - total_fees
            
            pnl_pct = (pnl_usd / entry_notional) * 100 if entry_notional > 0 else 0
            
            # Update realized PnL for blended R calculation
            if not full_close:
                pos["realized_pnl_usd"] = pos.get("realized_pnl_usd", 0.0) + pnl_usd
                logger.info(
                    f"[BYBIT_MAINNET] {symbol} realized PnL updated: "
                    f"${pos['realized_pnl_usd']:.2f} (this close: ${pnl_usd:.2f})"
                )
            
            # Calculate hold time
            opened_at = datetime.fromisoformat(pos["opened_at"].replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            hold_minutes = (now - opened_at).total_seconds() / 60
            
            # Determine if full close or partial
            is_final = full_close or (qty_remaining is not None and qty_remaining <= original_qty * 0.05)
            
            # Save trade history
            trade = {
                "symbol": symbol,
                "exchange": "bybit",
                "side": side,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "qty": close_qty,
                "pnl_usd": round(pnl_usd, 2),
                "pnl_pct": round(pnl_pct, 2),
                "close_reason": reason,
                "sl_kind": sl_kind or pos.get("sl_kind", "ORIGINAL"),
                "opened_at": pos["opened_at"],
                "closed_at": now.isoformat(),
                "hold_minutes": round(hold_minutes, 1),
                "partial": not is_final,
                "qty_original": original_qty,
                "qty_closed": close_qty,
                "qty_remaining": qty_remaining if not is_final else 0,
                "metadata": pos.get("metadata", {})
            }
            
            self._append_history(trade)
            
            # If final close, remove from positions
            if is_final:
                self.positions.pop(symbol, None)
                logger.info(
                    f"[BYBIT_MAINNET] CLOSED (FINAL) {side} {symbol} @ {exit_price:.4f} | "
                    f"{reason} | PnL ${pnl_usd:+.2f} ({pnl_pct:+.2f}%) | hold {hold_minutes:.0f}m"
                )
            else:
                # Update remaining quantity
                pos["qty_remaining"] = qty_remaining
                logger.info(
                    f"[BYBIT_MAINNET] CLOSED (PARTIAL) {side} {symbol} @ {exit_price:.4f} | "
                    f"{reason} | PnL ${pnl_usd:+.2f} ({pnl_pct:+.2f}%) | "
                    f"Closed: {close_qty:.4f} ({(close_qty/original_qty)*100:.0f}%) | Remaining: {qty_remaining:.4f}"
                )
            
            self._save_positions()
            
            # Send close notification
            try:
                from notifications.telegram_notifier import send_close_trade
                
                # Get current balance for notification
                try:
                    balance = self.client.get_balance()
                except:
                    balance = 0
                
                # Format reason with partial indicator
                notification_reason = reason
                if not is_final:
                    percentage = int((close_qty / original_qty) * 100)
                    notification_reason = f"{reason} ({percentage}% partial)"
                
                await send_close_trade({
                    "symbol": symbol,
                    "side": side,
                    "exit_price": exit_price,
                    "pnl_usd": pnl_usd,
                    "pnl_pct": pnl_pct,
                    "hold_minutes": hold_minutes,
                    "reason": notification_reason,
                    "normalized_reason": notification_reason,
                    "balance_after": balance,
                    "equity": balance,
                })
            except Exception as notif_err:
                logger.exception(f"[BYBIT_MAINNET] Close notification failed: {notif_err}")
        
        except Exception as e:
            err_str = str(e)
            # Suppress expected errors
            if "110017" in err_str and "zero position" in err_str:
                # Position already closed externally - just cleanup local state
                logger.info(f"[BYBIT_MAINNET] {symbol} already closed externally, cleaning up local state")
                self.positions.pop(symbol, None)
                self._save_positions()
            else:
                logger.exception(f"[BYBIT_MAINNET] Close {symbol} failed: {e}")
    
    async def sync_positions(self):
        """Sync local positions with exchange reality."""
        try:
            # Migrate existing positions to add TP ladder fields
            for symbol, pos in self.positions.items():
                if "tp_hit" not in pos:
                    pos["tp_hit"] = []
                if "qty_remaining" not in pos:
                    pos["qty_remaining"] = float(pos.get("qty", 0))
            
            # Save migrated positions
            if any("tp_hit" not in p or "qty_remaining" not in p for p in self.positions.values()):
                self._save_positions()
                logger.info("[BYBIT_MAINNET] Migrated existing positions for TP ladder")
            
            # Fetch exchange positions
            exchange_positions = self.client.get_positions()
            
            # Map exchange positions
            exchange_map = {
                p["symbol"]: p for p in exchange_positions
                if float(p.get("size", 0)) > 0
            }
            
            # Check for missing positions (closed externally)
            for symbol in list(self.positions.keys()):
                if symbol not in exchange_map:
                    pos = self.positions[symbol]
                    logger.info(
                        f"[BYBIT_MAINNET] {symbol} closed externally, "
                        f"sending notification and removing from local state"
                    )
                    
                    # Send close notification for external close
                    try:
                        from notifications.telegram_notifier import send_close_trade
                        
                        # Get entry price and calculate approximate PnL
                        entry_price = float(pos.get("entry_price", 0))
                        side = pos.get("side", "LONG")
                        qty = float(pos.get("qty", 0))
                        
                        # Fetch last known price as exit estimate
                        try:
                            quote = await self._fetch_fresh_quote(symbol)
                            exit_price = quote["mark"] if quote else entry_price
                        except:
                            exit_price = entry_price
                        
                        # Calculate PnL
                        if side == "LONG":
                            pnl_usd = (exit_price - entry_price) * qty
                        else:
                            pnl_usd = (entry_price - exit_price) * qty
                        
                        pnl_pct = (pnl_usd / (entry_price * qty)) * 100 if qty > 0 else 0
                        
                        # Calculate hold time
                        opened_at = datetime.fromisoformat(pos["opened_at"].replace("Z", "+00:00"))
                        now = datetime.now(timezone.utc)
                        hold_minutes = (now - opened_at).total_seconds() / 60
                        
                        # Get balance
                        try:
                            balance = self.client.get_balance()
                        except:
                            balance = 0
                        
                        await send_close_trade({
                            "symbol": symbol,
                            "side": side,
                            "exit_price": exit_price,
                            "pnl_usd": round(pnl_usd, 2),
                            "pnl_pct": round(pnl_pct, 2),
                            "hold_minutes": round(hold_minutes, 1),
                            "reason": "EXTERNAL_CLOSE (TP/SL hit on exchange)",
                            "normalized_reason": "EXTERNAL_CLOSE",
                            "balance_after": balance,
                        })
                        logger.info(f"[BYBIT_MAINNET] {symbol} external close notification sent")
                    except Exception as e:
                        logger.error(f"[BYBIT_MAINNET] {symbol} notification failed: {e}")
                    
                    # Remove from local state (already closed on exchange)
                    self.positions.pop(symbol, None)
            
            # Check for new positions (opened externally)
            for symbol, ex_pos in exchange_map.items():
                if symbol not in self.positions:
                    logger.warning(
                        f"[BYBIT_MAINNET] {symbol} found on exchange but not in local state, "
                        f"importing..."
                    )
                    # Import external position
                    side = "LONG" if ex_pos["side"] == "Buy" else "SHORT"
                    self.positions[symbol] = {
                        "symbol": symbol,
                        "exchange": "bybit",
                        "side": side,
                        "entry_price": float(ex_pos["avgPrice"]),
                        "sl_price": float(ex_pos.get("stopLoss", 0)) or 0,
                        "tp_prices": [float(ex_pos.get("takeProfit", 0))] if ex_pos.get("takeProfit") else [],
                        "qty": ex_pos["size"],
                        "notional": float(ex_pos["positionValue"]),
                        "opened_at": datetime.now(timezone.utc).isoformat(),
                        "opened_ts": time.time(),
                        "sl_kind": "ORIGINAL",
                        "locked_profit": False,
                        "metadata": {"imported": True}
                    }
            
            # Always save to update journal timestamp (for shadow watchdog)
            self._save_positions()
        
        except Exception as e:
            logger.error(f"[BYBIT_MAINNET] Position sync failed: {e}")
    
    async def check_and_execute_exits(self):
        """Check all positions for exit conditions (SL/TP/dynamic)."""
        for symbol in list(self.positions.keys()):
            pos = self.positions[symbol]
            
            # Fetch fresh quote
            quote = await self._fetch_fresh_quote(symbol)
            if not quote:
                continue
            
            mark = quote["mark"]
            
            # Check dynamic exits first
            if self.dynamic_exit_enabled:
                if await self._apply_dynamic_exits(symbol, pos, mark):
                    continue  # Position closed, skip SL/TP check
            
            # Check provider SL
            sl = pos.get("sl_price", 0)
            if sl and self._level_hit(pos, mark, sl, favorable=False):
                await self._close(symbol, "HARD_SL", sl_kind=pos.get("sl_kind", "ORIGINAL"), exit_price=mark)
                continue
            
            # Check TP levels (partial closes: 25% per TP)
            tp_prices = pos.get("tp_prices", [])
            tp_hit = pos.get("tp_hit", [])
            qty_remaining = pos.get("qty_remaining", float(pos.get("qty", 0)))
            
            if tp_prices and qty_remaining > 0:
                for i, tp in enumerate(tp_prices):
                    # Skip already hit TPs
                    if i in tp_hit:
                        continue
                    
                    # Check if TP level hit
                    if self._level_hit(pos, mark, tp, favorable=True):
                        # Calculate partial close size (25% of original)
                        original_qty = float(pos.get("qty", 0))
                        close_qty = original_qty * 0.25
                        
                        # Ensure we don't close more than remaining
                        close_qty = min(close_qty, qty_remaining)
                        
                        # Mark this TP as hit
                        tp_hit.append(i)
                        pos["tp_hit"] = tp_hit
                        
                        # Update remaining quantity
                        qty_remaining -= close_qty
                        pos["qty_remaining"] = qty_remaining
                        
                        # Close partial position
                        await self._close_partial(
                            symbol=symbol,
                            reason=f"TP{i+1}",
                            exit_price=mark,
                            close_qty=close_qty,
                            qty_remaining=qty_remaining
                        )
                        
                        logger.info(
                            f"[BYBIT_MAINNET] {symbol} TP{i+1} @ {mark:.6f} | "
                            f"Closed {close_qty:.4f} (25%) | Remaining: {qty_remaining:.4f}"
                        )
                        
                        # If all TPs hit or remaining too small, close full position
                        if qty_remaining < original_qty * 0.05:  # < 5% remaining
                            await self._close(symbol, f"TP{i+1}_FINAL", exit_price=mark)
                            break
                        
                        # Save updated position
                        self._save_positions()
                        break  # Process one TP per cycle
    
    def _level_hit(self, pos: Dict, mark: float, level: float, favorable: bool) -> bool:
        """Check if price level hit."""
        if level == 0:
            return False
        
        side = pos["side"]
        
        if favorable:
            # TP hit
            if side == "LONG":
                return mark >= level
            else:
                return mark <= level
        else:
            # SL hit
            if side == "LONG":
                return mark <= level
            else:
                return mark >= level
    
    async def _apply_dynamic_exits(self, symbol: str, pos: Dict, mark: float) -> bool:
        """
        Apply dynamic exit layer (SCRATCH/DAMAGE/PROFIT/TRAILING).
        
        Returns True if position was closed.
        """
        # Skip pre-epoch positions
        opened_at_str = pos.get("opened_at", "")
        if opened_at_str < self.DYNAMIC_EXIT_EPOCH_UTC:
            return False
        
        # Parse opened timestamp
        try:
            opened_at = datetime.fromisoformat(opened_at_str.replace("Z", "+00:00"))
        except:
            logger.warning(f"[DYN_EXIT] {symbol} invalid opened_at: {opened_at_str}")
            return False
        
        now = datetime.now(timezone.utc)
        hold_minutes = (now - opened_at).total_seconds() / 60
        
        # Calculate unrealized PnL
        entry = pos["entry_price"]
        qty = float(pos["qty"])
        side = pos["side"]
        
        if side == "LONG":
            unrealized_usd = qty * (mark - entry)
        else:
            unrealized_usd = qty * (entry - mark)
        
        unrealized_pct = (unrealized_usd / (qty * entry)) * 100
        
        # Log heartbeat
        logger.info(
            f"[DYN_EXIT_CHECK] {symbol} {side} | "
            f"opened {hold_minutes:.0f}m ago | pnl {unrealized_pct:+.2f}%"
        )
        
        # LAYER 1: SCRATCH_EXIT (kill zombies)
        if self._config_bool("SCRATCH_EXIT_ENABLED", default=True):
            # Smart scratch: adaptive timeout based on TF, regime, trend
            enable_smart = os.getenv("SMART_SCRATCH_ENABLED", "true").lower() == "true"
            phase1_4h_only = os.getenv("SMART_SCRATCH_PHASE1_4H_ONLY", "false").lower() == "true"  # Phase 2: false
            
            # Channel-specific timeframe defaults
            channel_config = {
                "-1001519663114": "1d",      # jelly
                "-1002053675871": "15m",     # naorist
                "-1002128890109": "1h",      # raydium
                "-1003801941007": "15m",     # Data's Inner Circle 👑
            }
            
            scratch_min = calculate_smart_scratch_timeout(
                position=pos,
                channel_config=channel_config,
                enable_smart=enable_smart,
                phase1_4h_only=phase1_4h_only
            )
            
            scratch_max_abs_pnl = float(os.getenv("SCRATCH_EXIT_MAX_ABS_PNL_PCT", "0.4"))
            
            if hold_minutes >= scratch_min and abs(unrealized_pct) <= scratch_max_abs_pnl:
                logger.info(
                    f"[SCRATCH_EXIT] {symbol} stagnant near BE "
                    f"(hold: {hold_minutes:.0f}min >= {scratch_min:.0f}min, "
                    f"pnl: {unrealized_pct:+.2f}% <= {scratch_max_abs_pnl}%)"
                )
                await self._close(symbol, "SCRATCH_EXIT", sl_kind=pos.get("sl_kind", "ORIGINAL"), exit_price=mark)
                return True
        
        # LAYER 2: DAMAGE_REDUCER (cut chronic losers)
        if self._config_bool("DAMAGE_REDUCER_ENABLED", default=True):
            damage_min = float(os.getenv("DAMAGE_REDUCER_MIN_HOLD_MINUTES", "45"))
            damage_max_loss = float(os.getenv("DAMAGE_REDUCER_MAX_LOSS_PCT", "-2.5"))
            
            if hold_minutes >= damage_min and unrealized_pct <= damage_max_loss:
                logger.info(f"[DAMAGE_REDUCER] {symbol} chronic bleeder")
                await self._close(symbol, "DAMAGE_REDUCER", sl_kind=pos.get("sl_kind", "ORIGINAL"), exit_price=mark)
                return True
        
        # LAYER 3: PROFIT_LOCK (move SL to BE+buffer)
        # GATE: Only lock after TP1 hit to avoid premature locks from volatility spikes
        if self._config_bool("PROFIT_LOCK_ENABLED", default=True):
            if not pos.get("locked_profit", False):
                lock_min = float(os.getenv("PROFIT_LOCK_MIN_MINUTES", "15"))
                lock_buffer = float(os.getenv("PROFIT_LOCK_BUFFER_PCT", "0.3"))
                
                # TP1-gated: only lock after at least one TP hit (confirmed profit)
                tp_hit_count = len(pos.get("tp_hit", []))
                
                if hold_minutes >= lock_min and tp_hit_count >= 1:
                    # Calculate new SL (breakeven + buffer)
                    sl = pos.get("sl_price", 0)
                    risk_distance = abs(entry - sl)
                    buffer_distance = risk_distance * (lock_buffer / 100)
                    
                    if side == "LONG":
                        new_sl = entry + buffer_distance
                        # Safety: don't set too tight
                        if new_sl >= mark * 0.998:
                            new_sl = mark * 0.995
                    else:
                        new_sl = entry - buffer_distance
                        if new_sl <= mark * 1.002:
                            new_sl = mark * 1.005
                    
                    # Update SL on exchange
                    try:
                        result = self.client.set_trading_stop(
                            symbol=symbol,
                            position_idx=0,  # Required for one-way mode
                            stop_loss=self._quantize(new_sl, 0.01)
                        )
                        
                        # Verify API success
                        if not result or result.get('retCode', 0) != 0:
                            raise Exception(f"API returned error: {result}")
                        
                        pos["sl_price"] = new_sl
                        pos["sl_kind"] = "BREAKEVEN"
                        pos["locked_profit"] = True
                        self._save_positions()
                        
                        logger.info(
                            f"[PROFIT_LOCK] {symbol} locked! SL moved {sl:.4f} → {new_sl:.4f}"
                        )
                    except Exception as e:
                        err_str = str(e)
                        
                        # Error 34040 = "not modified" - SL may already be close to target
                        if "34040" in err_str and "not modified" in err_str:
                            # Fetch current SL from exchange to verify
                            try:
                                exchange_positions = self.client.get_positions()
                                exchange_map = {p["symbol"]: p for p in exchange_positions}
                                if symbol in exchange_map:
                                    current_sl = float(exchange_map[symbol].get("stopLoss", 0))
                                    # If SL is already at/above breakeven, consider it locked
                                    if abs(current_sl - new_sl) < risk_distance * 0.1:  # Within 10% of risk
                                        logger.info(
                                            f"[PROFIT_LOCK] {symbol} already locked (SL={current_sl:.4f}, target={new_sl:.4f})"
                                        )
                                        pos["sl_price"] = current_sl
                                        pos["sl_kind"] = "BREAKEVEN"
                                        pos["locked_profit"] = True
                                        self._save_positions()
                                    else:
                                        logger.warning(
                                            f"[PROFIT_LOCK] {symbol} error 34040 but SL not at target (current={current_sl:.4f}, target={new_sl:.4f})"
                                        )
                            except Exception as verify_err:
                                logger.error(f"[PROFIT_LOCK] {symbol} failed to verify SL: {verify_err}")
                        else:
                            logger.error(f"[PROFIT_LOCK] {symbol} failed to update SL: {e}")
        
        # LAYER 4: TRAILING (adaptive trailing after lock)
        # Activate after 1.5R profit, trail 0.6R from peak (shadow proven config)
        # NOTE: Trailing should work independently of PROFIT_LOCK success
        # Trigger: profit >= 1.5R OR TP1+ hit
        for symbol, pos in list(self.positions.items()):
            # Skip if not enough profit/TP yet
            # Allow trailing if: (1) PROFIT_LOCK succeeded, OR (2) TP1+ hit, OR (3) 1.5R+ profit
            tp_hit_count = len(pos.get("tp_hit", []))
            
            # Get fresh mark price for this symbol
            try:
                quote = await self._get_fresh_close_quote(symbol)
                mark = quote["mark_price"]
            except Exception as e:
                logger.warning(f"[TRAILING] {symbol} failed to get mark price: {e}")
                continue
            
            side = pos["side"]
            entry = pos["entry_price"]
            sl = pos.get("sl_price", 0)
            risk_distance = abs(entry - sl) if sl else abs(entry * 0.01)
            
            # Calculate profit in R
            if side == "LONG":
                profit_r = (mark - entry) / risk_distance if risk_distance else 0
            else:
                profit_r = (entry - mark) / risk_distance if risk_distance else 0
            
            # Trailing activation: locked_profit=true OR tp_hit>=1 OR profit>=1.5R
            locked = pos.get("locked_profit", False)
            can_trail = locked or tp_hit_count >= 1 or profit_r >= 1.5
            
            if not can_trail:
                continue
            
            # Track high/low watermark (initialize with current mark if missing)
            if "high_watermark" not in pos:
                pos["high_watermark"] = mark
            if "low_watermark" not in pos:
                pos["low_watermark"] = mark
            
            # Update watermarks
            if side == "LONG":
                pos["high_watermark"] = max(pos["high_watermark"], mark)
                peak = pos["high_watermark"]
            else:
                pos["low_watermark"] = min(pos["low_watermark"], mark)
                peak = pos["low_watermark"]
            
            # Calculate profit in R
            if side == "LONG":
                profit_r = (mark - entry) / risk_distance if risk_distance else 0
            else:
                profit_r = (entry - mark) / risk_distance if risk_distance else 0
            
            # Trailing activation threshold: 1.5R
            if profit_r < 1.5:
                continue
            
            # Calculate trailing SL: peak - 0.6R
            trail_distance = risk_distance * 0.6
            
            if side == "LONG":
                trailing_sl = peak - trail_distance
                # Only move SL up (never down)
                if trailing_sl > sl:
                    new_sl = trailing_sl
                else:
                    continue
            else:
                trailing_sl = peak + trail_distance
                # Only move SL down (never up)
                if trailing_sl < sl:
                    new_sl = trailing_sl
                else:
                    continue
            
            # Update SL on exchange
            try:
                self.client.set_trading_stop(
                    symbol=symbol,
                    stop_loss=self._quantize(new_sl, 0.01)
                )
                
                pos["sl_price"] = new_sl
                pos["sl_kind"] = "TRAILING"
                self._save_positions()
                
                logger.info(
                    f"[TRAILING_LOCK] {symbol} {side} | SL {sl:.4f} → {new_sl:.4f} | "
                    f"peak={peak:.4f} trail_dist={trail_distance:.4f} profit={profit_r:.2f}R"
                )
            except Exception as e:
                err_str = str(e)
                # Suppress expected errors (cosmetic only)
                if "34040" in err_str or "not modified" in err_str:
                    # SL already at correct value - OK
                    continue
                elif "10001" in err_str and "zero position" in err_str:
                    # Position closed externally - will be cleaned up on next sync
                    continue
                else:
                    logger.error(f"[TRAILING_LOCK] {symbol} failed to update SL: {e}")
        
        return False
    
    async def run_maintenance_loop(self):
        """Main maintenance loop - check exits every 3s."""
        logger.info("[BYBIT_MAINNET] Starting maintenance loop")
        
        while True:
            try:
                # Sync with exchange
                await self.sync_positions()
                
                # Check exits
                await self.check_and_execute_exits()
                
                # Sleep
                await asyncio.sleep(3)
            
            except KeyboardInterrupt:
                logger.info("[BYBIT_MAINNET] Shutting down...")
                break
            except Exception as e:
                logger.exception(f"[BYBIT_MAINNET] Maintenance loop error: {e}")
                await asyncio.sleep(5)
