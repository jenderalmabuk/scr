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
from execution.smart_scratch_exit import calculate_smart_scratch_timeout, evaluate_damage_reducer_gate, evaluate_scratch_exit_gate, update_scratch_excursion
from execution.manual_signal_tools import fetch_manual_signal_context, normalize_manual_tp_ladder


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
        self._position_lock = asyncio.Lock()
        self._kline_cache: Dict[str, Dict[str, Any]] = {}
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
            ticker = await asyncio.to_thread(self.client.get_ticker, symbol)
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
    
    def _append_scratch_audit(self, symbol: str, pos: Dict, decision: Dict[str, Any], mark: float, *, layer: str = "SCRATCH_EXIT"):
        """Append compact dynamic-exit decisions with audit-ready top-level fields."""
        audit_worthy = (
            decision.get("should_exit")
            or decision.get("should_partial_exit")
            or (decision.get("time_due") and decision.get("near_breakeven"))
            or (decision.get("time_due") and (decision.get("pct_due") or decision.get("mfe_giveback") or decision.get("local_bottom_guard")))
        )
        if not audit_worthy:
            return
        bucket = int(float(decision.get("hold_minutes", 0)) // 15)
        key = f"{layer}:{decision.get('action')}:{decision.get('reason')}:{bucket}"
        if pos.get("scratch_audit_last_key") == key and not (decision.get("should_exit") or decision.get("should_partial_exit")):
            return
        pos["scratch_audit_last_key"] = key
        audit_file = Path(os.getenv("SCRATCH_EXIT_AUDIT_JOURNAL", str(self.journal_path / "scratch_exit_audit.jsonl")))
        manual_profile = decision.get("manual_structure_profile") or decision.get("manual_profile") or {}
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "exchange": "bybit",
            "side": pos.get("side"),
            "entry_price": pos.get("entry_price"),
            "sl_price": pos.get("sl_price"),
            "mark": mark,
            "layer": layer,
            "action": decision.get("action"),
            "reason": decision.get("reason"),
            "decision_reason": decision.get("reason"),
            "close_reason": layer if decision.get("should_exit") else None,
            "hold_minutes": decision.get("hold_minutes"),
            "current_r": decision.get("current_r"),
            "max_favorable_r": decision.get("max_favorable_r"),
            "max_adverse_r": decision.get("max_adverse_r"),
            "tp1_progress": decision.get("tp1_progress"),
            "tp_hit_count": decision.get("tp_hit_count"),
            "manual_profile": manual_profile,
            "setup_context": decision.get("setup_context") or {},
            "decision": decision,
        }
        try:
            audit_file.parent.mkdir(parents=True, exist_ok=True)
            with audit_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, default=str, separators=(",", ":")) + "\n")
        except Exception as e:
            logger.warning(f"[SCRATCH_AUDIT] {symbol} write failed: {e}")
    
    async def _enrich_imported_position(self, symbol: str, pos: Dict[str, Any]) -> bool:
        """Give manually imported exchange positions parser-like context."""
        if os.getenv("BYBIT_ENRICH_IMPORTED_POSITIONS", "true").lower() not in ("1", "true", "yes"):
            return False
        metadata = pos.get("metadata") if isinstance(pos.get("metadata"), dict) else {}
        existing_context = metadata.get("adv_snapshot") if isinstance(metadata.get("adv_snapshot"), dict) else {}
        has_enrichment_error = any(
            key in existing_context
            for key in ("manual_context_error", "manual_metrics_error", "manual_mtf_error", "manual_tv_error")
        )
        if (
            metadata.get("manual_context_enriched")
            and metadata.get("manual_context_version") == "manual_context_v4"
            and existing_context.get("mtf_alignment")
            and existing_context.get("tradingview")
            and not has_enrichment_error
        ):
            return False

        side = str(pos.get("side") or "").upper()
        entry = float(pos.get("entry_price") or 0)
        sl = float(pos.get("sl_price") or 0)
        tps = pos.get("tp_prices") or []
        ladder, tp_plan = normalize_manual_tp_ladder(side, entry, sl, tps)
        if ladder:
            pos["tp_prices"] = ladder

        timeframe = str(metadata.get("signal_timeframe") or metadata.get("timeframe") or os.getenv("MANUAL_SIGNAL_TIMEFRAME", "15m"))
        try:
            context = await fetch_manual_signal_context(symbol, side, timeframe=timeframe)
        except Exception as exc:
            context = {"manual_context_error": str(exc)[:160], "signal_timeframe": timeframe}
        setup_type = str(metadata.get("setup_type") or context.get("setup_type") or os.getenv("MANUAL_DEFAULT_SETUP_TYPE", "EARLY_ENTRY")).upper()
        context.setdefault("signal_timeframe", timeframe)
        context.setdefault("setup_type", setup_type)
        context.setdefault("scratch_exit_profile", "STRUCTURE_HOLD")
        context["manual_imported_position"] = True
        context["manual_tp_plan"] = tp_plan
        context["signal_tp_ladder"] = list(pos.get("tp_prices") or [])
        metadata.update({
            "imported": True,
            "manual_context_enriched": True,
            "manual_context_version": "manual_context_v4",
            "manual_context_enriched_at": datetime.now(timezone.utc).isoformat(),
            "manual_timeframe": timeframe,
            "setup_type": setup_type,
            "scratch_exit_profile": "STRUCTURE_HOLD",
            "adv_snapshot": context,
        })
        if context.get("regime_label"):
            metadata.setdefault("regime", context.get("regime_label"))
        pos["metadata"] = metadata
        logger.info(
            "[BYBIT_MAINNET] enriched imported position %s tf=%s mtf=%s tv=%s tp_plan=%s",
            symbol, timeframe,
            (context.get("mtf_alignment") or {}).get("score"),
            (context.get("tradingview") or {}).get("score"),
            tp_plan.get("source"),
        )
        return True
    
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
        Fallback fixed-risk size. Gateway-provided notional is preferred.
        
        Returns: (qty_str, notional_usd)
        """
        risk_distance = abs(entry - sl)
        
        if risk_distance == 0:
            logger.error(f"[BYBIT_MAINNET] {symbol} SL == entry, cannot size")
            return ("0", 0.0)
        
        qty_for_risk = self.risk_per_trade / risk_distance
        qty_rounded = int(qty_for_risk / lot_size) * lot_size
        
        if qty_rounded <= 0:
            logger.warning(f"[BYBIT_MAINNET] {symbol} qty rounded to 0 (risk_distance={risk_distance:.4f})")
            return ("0", 0.0)
        
        qty_str = self._quantize(qty_rounded, lot_size)
        notional = float(qty_str) * entry
        
        return (qty_str, notional)

    def _qty_from_notional(self, symbol: str, notional: float, entry: float, lot_size: float) -> tuple[str, float]:
        if notional <= 0 or entry <= 0:
            return ("0", 0.0)
        qty_raw = notional / entry
        qty_rounded = int(qty_raw / lot_size) * lot_size
        if qty_rounded <= 0:
            logger.warning(f"[BYBIT_MAINNET] {symbol} qty rounded to 0 (notional={notional:.4f}, entry={entry:.8g})")
            return ("0", 0.0)
        qty_str = self._quantize(qty_rounded, lot_size)
        return (qty_str, float(qty_str) * entry)
    
    async def open_position(
        self,
        symbol: str,
        side: str,  # "LONG" or "SHORT"
        entry_price: float,
        sl_price: float,
        tp_prices: list,
        signal_metadata: Dict = None,
        requested_notional: float = 0.0,
        requested_risk_amount: float = 0.0,
    ) -> Dict[str, Any]:
        """Open a confirmed market position using gateway-sized notional."""
        try:
            if len(self.positions) >= self.max_concurrent:
                logger.warning(
                    f"[BYBIT_MAINNET] Max concurrent reached ({self.max_concurrent}), "
                    f"rejecting {symbol}"
                )
                return {"ok": False, "executed": False, "position_confirmed": False,
                        "code": "MAX_CONCURRENT", "reason": "max concurrent positions reached"}
            
            instrument = self.client.get_instrument_info(symbol)
            lot_size = float(instrument["lotSizeFilter"]["qtyStep"])
            tick_size = float(instrument["priceFilter"]["tickSize"])
            sl_str = self._quantize(sl_price, tick_size)
            
            quote = await self._fetch_fresh_quote(symbol)
            if not quote:
                logger.error(f"[BYBIT_MAINNET] {symbol} no quote available")
                return {"ok": False, "executed": False, "position_confirmed": False,
                        "code": "NO_MARKET_PRICE", "reason": "no quote available"}
            
            mark = float(quote["mark"])
            risk = abs(entry_price - sl_price)
            is_long = (str(side).upper() == "LONG")
            in_profit = (mark > entry_price) if is_long else (mark < entry_price)
            in_discount = (mark < entry_price) if is_long else (mark > entry_price)
            distance_from_entry = abs(mark - entry_price)
            distance_in_R = distance_from_entry / risk if risk > 0 else 0.0

            max_chase_r = float(os.getenv("SIGNAL_COPY_MAX_CHASE_R", "0.35"))
            max_discount_r = float(os.getenv("SIGNAL_COPY_MAX_DISCOUNT_R", "0.60"))

            # Disallow execution if price has chased too far into profit or dipped too deep towards SL
            if in_profit and distance_in_R > max_chase_r:
                logger.info(
                    f"[BYBIT_MAINNET] {symbol} chased too far in profit "
                    f"({distance_in_R:.2f}R > {max_chase_r:.2f}R) — reject"
                )
                return {"ok": False, "executed": False, "position_confirmed": False,
                        "code": "CHASE_TOO_FAR", "reason": f"price ran {distance_in_R:.2f}R in profit"}

            if in_discount and distance_in_R > max_discount_r:
                logger.info(
                    f"[BYBIT_MAINNET] {symbol} discount too deep near SL "
                    f"({distance_in_R:.2f}R > {max_discount_r:.2f}R) — reject"
                )
                return {"ok": False, "executed": False, "position_confirmed": False,
                        "code": "DISCOUNT_TOO_DEEP", "reason": f"price dipped {distance_in_R:.2f}R towards SL"}

            actual_entry = mark
            if requested_notional > 0:
                qty_str, notional = self._qty_from_notional(symbol, requested_notional, actual_entry, lot_size)
            else:
                qty_str, notional = self._calculate_position_size(symbol, actual_entry, sl_price, lot_size)
            
            if qty_str == "0" or notional <= 0:
                logger.error(f"[BYBIT_MAINNET] {symbol} size calculation failed")
                return {"ok": False, "executed": False, "position_confirmed": False,
                        "code": "INVALID_SIZE", "reason": "size calculation failed"}
            
            qty_float = float(qty_str)
            actual_risk_amount = qty_float * abs(actual_entry - sl_price)
            if requested_risk_amount > 0 and actual_risk_amount > requested_risk_amount * 1.02:
                logger.warning(
                    f"[BYBIT_MAINNET] {symbol} rounded risk ${actual_risk_amount:.4f} "
                    f"> requested ${requested_risk_amount:.4f}"
                )
                return {"ok": False, "executed": False, "position_confirmed": False,
                        "code": "RISK_EXCEEDS_REQUEST", "reason": "rounded risk exceeds requested risk"}
            
            required_margin = notional / self.max_leverage
            logger.info(
                f"[BYBIT_MAINNET] {symbol} {side}: qty={qty_str}, "
                f"notional=${notional:.2f}, margin=${required_margin:.2f}, entry_zone={distance_in_R:.2f}R"
            )
            
            bybit_side = "Buy" if side == "LONG" else "Sell"
            order_result = self.client.create_order(
                symbol=symbol,
                side=bybit_side,
                order_type="Market",
                qty=qty_str,
                price=None,
                stop_loss=sl_str,
                take_profit=None,
                order_link_id=f"SC_{symbol}_{int(time.time() * 1000)}"
            )
            order_id = order_result.get("orderId")
            if not order_id:
                logger.error(f"[BYBIT_MAINNET] {symbol} order failed: {order_result}")
                return {"ok": False, "executed": False, "position_confirmed": False,
                        "code": "ORDER_REJECTED", "reason": "exchange did not return order id"}
            
            exchange_position = None
            for _ in range(10):
                try:
                    positions = self.client.get_positions(symbol)
                    exchange_position = next(
                        (p for p in positions if p.get("symbol") == symbol and float(p.get("size", 0) or 0) > 0),
                        None,
                    )
                    if exchange_position:
                        break
                except Exception as exc:
                    logger.warning(f"[BYBIT_MAINNET] {symbol} position confirm check failed: {exc}")
                await asyncio.sleep(0.5)
            if not exchange_position:
                return {"ok": False, "executed": True, "position_confirmed": False,
                        "code": "POSITION_NOT_CONFIRMED", "reason": "market order accepted but position not confirmed",
                        "order_id": order_id}

            actual_qty = float(exchange_position.get("size") or qty_str)
            actual_entry = float(exchange_position.get("avgPrice") or actual_entry)
            notional = float(exchange_position.get("positionValue") or (actual_qty * actual_entry))
            actual_risk_amount = actual_qty * abs(actual_entry - sl_price)
            now = datetime.now(timezone.utc)
            position = {
                "symbol": symbol,
                "exchange": "bybit",
                "side": side,
                "entry_price": actual_entry,
                "sl_price": sl_price,
                "original_sl_price": sl_price,
                "provider_sl_original": sl_price,
                "initial_risk_distance": abs(actual_entry - sl_price),
                "tp_prices": tp_prices,
                "tp_hit": [],
                "qty": str(actual_qty),
                "qty_remaining": actual_qty,
                "initial_qty": actual_qty,
                "risk_usd": actual_risk_amount,
                "realized_pnl_usd": 0.0,
                "notional": notional,
                "order_id": order_id,
                "opened_at": now.isoformat(),
                "opened_ts": now.timestamp(),
                "sl_kind": "ORIGINAL",
                "locked_profit": False,
                "scratch_high_watermark": actual_entry,
                "scratch_low_watermark": actual_entry,
                "scratch_max_favorable_r": 0.0,
                "scratch_max_adverse_r": 0.0,
                "metadata": signal_metadata or {}
            }
            self.positions[symbol] = position
            self._save_positions()
            
            logger.info(
                f"[BYBIT_MAINNET] OPENED {side} {symbol} @ {actual_entry:.6f} | "
                f"qty={actual_qty:g} | SL={sl_price:.6f} | TP1={(tp_prices[0] if tp_prices else 0):.6f}"
            )
            
            try:
                from notifications.telegram_notifier import send_open_trade
                await send_open_trade({
                    "symbol": symbol,
                    "side": side,
                    "entry_price": actual_entry,
                    "entry": actual_entry,
                    "sl_price": sl_price,
                    "sl": sl_price,
                    "tp1": tp_prices[0] if tp_prices else 0,
                    "notional": notional,
                    "size_usd": notional,
                })
            except Exception as notif_err:
                logger.warning(f"[BYBIT_MAINNET] Notification failed: {type(notif_err).__name__}: {notif_err}")
            
            return {
                "ok": True,
                "executed": True,
                "position_confirmed": True,
                "symbol": symbol,
                "side": side,
                "entry_price": actual_entry,
                "sl_price": sl_price,
                "tp_prices": tp_prices,
                "qty": actual_qty,
                "notional": notional,
                "actual_risk_amount": actual_risk_amount,
                "max_loss_usd_at_fill": actual_risk_amount,
                "order_id": order_id,
            }
            
        except Exception as e:
            logger.error(f"[BYBIT_MAINNET] Failed to open {symbol}: {e}", exc_info=True)
            return {"ok": False, "executed": False, "position_confirmed": False,
                    "code": "EXCEPTION", "reason": str(e)}
    
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
            
            requested_notional = float(params.get("notional", params.get("size_usd", 0)) or 0)
            requested_risk_amount = float(params.get("actual_risk_amount", params.get("risk_amount", 0)) or 0)
            result = await self.open_position(
                symbol=symbol,
                side=side,
                entry_price=entry_price,
                sl_price=sl_price,
                tp_prices=tp_ladder,
                signal_metadata=params,
                requested_notional=requested_notional,
                requested_risk_amount=requested_risk_amount,
            )
            return result
                
        except Exception as e:
            logger.error(f"[BYBIT_MAINNET] submit_open error: {e}", exc_info=True)
            return {"ok": False, "executed": False, "code": "EXCEPTION",
                    "reason": str(e)}
    
    async def update_stop(self, symbol: str, new_sl: float) -> Dict[str, Any]:
        """Update or remove stop loss on Bybit position (called by gateway position_action)."""
        async with self._position_lock:
            pos = self.positions.get(symbol)
            if not pos:
                return {"ok": False, "code": "POSITION_NOT_FOUND"}
            
            old_sl = float(pos.get("sl_price") or 0.0)
            side = str(pos.get("side", "LONG")).upper()
            
            # If new_sl <= 0: Remove/suspend stop loss from Bybit
            if new_sl <= 0.0:
                try:
                    await asyncio.to_thread(
                        self.client.set_trading_stop,
                        symbol=symbol,
                        position_idx=0,
                        stop_loss="",
                    )
                    pos["sl_price"] = 0.0
                    pos["sl_suspended"] = True
                    self._save_positions()
                    logger.info(f"[BYBIT_MAINNET] {symbol} stop loss removed/suspended by provider update")
                    return {"ok": True, "code": "STOP_REMOVED", "old_sl": old_sl, "new_sl": 0.0}
                except Exception as exc:
                    logger.error(f"[BYBIT_MAINNET] {symbol} failed to remove stop loss: {exc}")
                    return {"ok": False, "code": f"BYBIT_ERROR: {exc}"}

            # Safety check: prevent moving SL farther away (widening risk), but allow trailing/profit lock
            if old_sl > 0.0 and not pos.get("sl_suspended"):
                if side == "LONG" and new_sl < old_sl:
                    return {"ok": False, "code": "STOP_WIDENS_RISK"}
                if side == "SHORT" and new_sl > old_sl:
                    return {"ok": False, "code": "STOP_WIDENS_RISK"}

            try:
                instrument = await asyncio.to_thread(self.client.get_instrument_info, symbol)
                tick_size = float(instrument["priceFilter"]["tickSize"])
                quantized_sl = self._quantize(new_sl, tick_size)
                await asyncio.to_thread(
                    self.client.set_trading_stop,
                    symbol=symbol,
                    position_idx=0,
                    stop_loss=quantized_sl,
                )
                pos["sl_price"] = float(new_sl)
                pos["sl_suspended"] = False
                pos["sl_kind"] = "PROVIDER_UPDATE"
                self._save_positions()
                logger.info(f"[BYBIT_MAINNET] {symbol} stop loss updated to {quantized_sl} (was {old_sl})")
                return {"ok": True, "code": "STOP_UPDATED", "old_sl": old_sl, "new_sl": float(new_sl)}
            except Exception as exc:
                logger.error(f"[BYBIT_MAINNET] {symbol} failed to update stop loss: {exc}")
                return {"ok": False, "code": f"BYBIT_ERROR: {exc}"}

    async def close_position(self, symbol: str, reason: str = "PROVIDER_CLOSE") -> Dict[str, Any]:
        """Close full position on Bybit (called by gateway position_action)."""
        async with self._position_lock:
            pos = self.positions.get(symbol)
            if not pos:
                return {"ok": False, "code": "POSITION_NOT_FOUND"}
            try:
                mark_dict = await self._get_fresh_close_quote(symbol)
                mark = mark_dict.get("mark_price", 0.0)
            except Exception:
                mark = 0.0
            try:
                await self._close(symbol, reason=reason, exit_price=mark)
                logger.info(f"[BYBIT_MAINNET] {symbol} position closed by {reason} at {mark}")
                return {"ok": True, "code": "POSITION_CLOSED", "reason": reason, "exit_price": mark}
            except Exception as exc:
                logger.error(f"[BYBIT_MAINNET] {symbol} close_position failed: {exc}")
                return {"ok": False, "code": f"CLOSE_FAILED: {exc}"}

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
                
                # Create order with unique link ID to fetch fill + Bybit closed PnL later
                order_link_id = f"SC_CLOSE_{symbol}_{int(time.time() * 1000)}"
                close_order_id = None
                bybit_closed_pnl = None
                bybit_closed_pnl_row = None
                
                order_result = self.client.create_order(
                    symbol=symbol,
                    side=bybit_side,
                    order_type="Market",
                    qty=close_qty_str,
                    reduce_only=True,
                    order_link_id=order_link_id
                )
                close_order_id = order_result.get("orderId") if isinstance(order_result, dict) else None
                
                # Wait briefly for order to fill and closed-PnL ledger to update
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
                        close_order_id = matching_order.get("orderId") or close_order_id
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

                # Bybit UI uses the exchange closed-PnL ledger. Prefer it over
                # bot-side estimates so Telegram/journal match Bybit after fees,
                # funding and exchange rounding.
                try:
                    for attempt in range(3):
                        pnl_rows = self.client.get_closed_pnl(symbol=symbol, limit=50)
                        if close_order_id:
                            matches = [r for r in pnl_rows if str(r.get("orderId") or "") == str(close_order_id)]
                        else:
                            target_qty = float(close_qty_str)
                            matches = [
                                r for r in pnl_rows
                                if abs(float(r.get("closedSize") or r.get("qty") or 0) - target_qty) <= max(target_qty * 1e-6, 1e-12)
                            ]
                        if matches:
                            bybit_closed_pnl_row = matches[0]
                            bybit_closed_pnl = float(bybit_closed_pnl_row.get("closedPnl"))
                            avg_exit = bybit_closed_pnl_row.get("avgExitPrice")
                            if avg_exit not in (None, "", "0"):
                                exit_price = float(avg_exit)
                            logger.info(
                                f"[BYBIT_MAINNET] {symbol} Bybit closedPnL: "
                                f"${bybit_closed_pnl:+.4f} orderId={close_order_id or '-'}"
                            )
                            break
                        if attempt < 2:
                            await asyncio.sleep(1.0)
                except Exception as pnl_err:
                    logger.warning(f"[BYBIT_MAINNET] {symbol} could not fetch Bybit closed PnL: {pnl_err}")
                    
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
            
            # Calculate PnL for this partial close. Bybit's closedPnl is the
            # source of truth when available; fallback remains for rare API lag.
            if side == "LONG":
                gross_pnl = (exit_price - entry_price) * close_qty
            else:
                gross_pnl = (entry_price - exit_price) * close_qty
            
            entry_notional = entry_price * close_qty
            exit_notional = exit_price * close_qty
            estimated_entry_fee = entry_notional * 0.00055
            estimated_exit_fee = exit_notional * 0.00055
            total_fees = estimated_entry_fee + estimated_exit_fee
            estimated_pnl_usd = gross_pnl - total_fees
            pnl_source = "bybit_closed_pnl" if bybit_closed_pnl is not None else "bot_estimate"
            pnl_usd = bybit_closed_pnl if bybit_closed_pnl is not None else estimated_pnl_usd
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
                "pnl_source": pnl_source,
                "bybit_closed_pnl_usd": round(bybit_closed_pnl, 8) if bybit_closed_pnl is not None else None,
                "estimated_pnl_usd": round(estimated_pnl_usd, 8),
                "gross_pnl_usd": round(gross_pnl, 8),
                "estimated_fee_usd": round(total_fees, 8),
                "close_order_id": close_order_id,
                "close_order_link_id": order_link_id,
                "bybit_closed_pnl_row": bybit_closed_pnl_row,
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
                    "pnl_source": pnl_source,
                    "bybit_closed_pnl_usd": bybit_closed_pnl,
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
            def _ex_float(value, default: float = 0.0) -> float:
                try:
                    if value in (None, "", "None"):
                        return default
                    return float(value)
                except (TypeError, ValueError):
                    return default

            # Migrate existing positions to add TP ladder and scratch fields
            migrated = False
            for symbol, pos in self.positions.items():
                if "tp_hit" not in pos:
                    pos["tp_hit"] = []
                    migrated = True
                if "qty_remaining" not in pos:
                    pos["qty_remaining"] = float(pos.get("qty", 0))
                    migrated = True
                entry = float(pos.get("entry_price", 0) or 0)
                if entry > 0 and "scratch_high_watermark" not in pos:
                    pos["scratch_high_watermark"] = entry
                    pos["scratch_low_watermark"] = entry
                    pos["scratch_max_favorable_r"] = 0.0
                    pos["scratch_max_adverse_r"] = 0.0
                    migrated = True
                metadata = pos.get("metadata") if isinstance(pos.get("metadata"), dict) else {}
                if metadata.get("imported") is True:
                    migrated = await self._enrich_imported_position(symbol, pos) or migrated
            
            # Save migrated positions
            if migrated:
                self._save_positions()
                logger.info("[BYBIT_MAINNET] Migrated existing positions for TP ladder/scratch tracking")
            
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
                        
                        # Prefer Bybit's closed-PnL ledger for externally closed
                        # positions; fallback to mark estimate only if unavailable.
                        bybit_closed_pnl = None
                        bybit_closed_pnl_row = None
                        try:
                            pnl_rows = self.client.get_closed_pnl(symbol=symbol, limit=20)
                            matches = [
                                r for r in pnl_rows
                                if abs(float(r.get("closedSize") or r.get("qty") or 0) - qty) <= max(qty * 1e-6, 1e-12)
                            ]
                            if matches:
                                bybit_closed_pnl_row = matches[0]
                                bybit_closed_pnl = float(bybit_closed_pnl_row.get("closedPnl"))
                                avg_exit = bybit_closed_pnl_row.get("avgExitPrice")
                                exit_price = float(avg_exit) if avg_exit not in (None, "", "0") else entry_price
                            else:
                                raise ValueError("no matching closed-pnl row")
                        except Exception:
                            try:
                                quote = await self._fetch_fresh_quote(symbol)
                                exit_price = quote["mark"] if quote else entry_price
                            except Exception:
                                exit_price = entry_price
                        
                        if side == "LONG":
                            estimated_pnl_usd = (exit_price - entry_price) * qty
                        else:
                            estimated_pnl_usd = (entry_price - exit_price) * qty
                        pnl_usd = bybit_closed_pnl if bybit_closed_pnl is not None else estimated_pnl_usd
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
                        
                        self._append_history({
                            "symbol": symbol,
                            "exchange": "bybit",
                            "side": side,
                            "entry_price": entry_price,
                            "exit_price": exit_price,
                            "qty": qty,
                            "pnl_usd": round(pnl_usd, 2),
                            "pnl_pct": round(pnl_pct, 2),
                            "pnl_source": "bybit_closed_pnl" if bybit_closed_pnl is not None else "bot_estimate",
                            "bybit_closed_pnl_usd": round(bybit_closed_pnl, 8) if bybit_closed_pnl is not None else None,
                            "bybit_closed_pnl_row": bybit_closed_pnl_row,
                            "close_order_id": bybit_closed_pnl_row.get("orderId") if isinstance(bybit_closed_pnl_row, dict) else None,
                            "close_reason": "EXTERNAL_CLOSE",
                            "sl_kind": pos.get("sl_kind", "ORIGINAL"),
                            "opened_at": pos.get("opened_at"),
                            "closed_at": now.isoformat(),
                            "hold_minutes": round(hold_minutes, 1),
                            "partial": False,
                            "qty_original": qty,
                            "qty_closed": qty,
                            "qty_remaining": 0,
                            "metadata": pos.get("metadata", {}),
                        })
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
                        logger.info(f"[BYBIT_MAINNET] {symbol} external close history/notification sent")
                    except Exception as e:
                        logger.error(f"[BYBIT_MAINNET] {symbol} notification failed: {e}")
                    
                    # Remove from local state (already closed on exchange)
                    self.positions.pop(symbol, None)
            
            # Update existing positions with exchange-side truth. Dynamic exits,
            # risk budget, and journal must follow actual Bybit size/SL after
            # partial closes, profit locks, or manual exchange-side changes.
            for symbol, ex_pos in exchange_map.items():
                if symbol in self.positions:
                    pos = self.positions[symbol]
                    entry = float(pos.get("entry_price") or _ex_float(ex_pos.get("avgPrice"), 0.0) or 0.0)
                    exchange_size = _ex_float(ex_pos.get("size"), 0.0)
                    exchange_notional = _ex_float(ex_pos.get("positionValue"), 0.0)
                    exchange_sl = _ex_float(ex_pos.get("stopLoss"), 0.0)
                    exchange_tp = _ex_float(ex_pos.get("takeProfit"), 0.0)
                    side = str(pos.get("side") or ("LONG" if ex_pos.get("side") == "Buy" else "SHORT")).upper()

                    if exchange_size > 0 and abs(_ex_float(pos.get("qty_remaining"), exchange_size) - exchange_size) > max(exchange_size * 1e-6, 1e-12):
                        pos["qty_remaining"] = exchange_size
                        migrated = True
                    if exchange_notional > 0 and abs(_ex_float(pos.get("notional"), exchange_notional) - exchange_notional) > 1e-8:
                        pos["notional"] = exchange_notional
                        migrated = True
                    if exchange_sl > 0 and abs(_ex_float(pos.get("sl_price"), 0.0) - exchange_sl) > max(exchange_sl * 1e-8, 1e-12):
                        pos["sl_price"] = exchange_sl
                        if entry > 0 and ((side == "LONG" and exchange_sl >= entry) or (side == "SHORT" and exchange_sl <= entry)):
                            pos["locked_profit"] = True
                            if str(pos.get("sl_kind") or "").upper() not in {"TRAILING", "BREAKEVEN"}:
                                pos["sl_kind"] = "BREAKEVEN"
                        migrated = True
                    elif exchange_sl > 0 and entry > 0 and ((side == "LONG" and exchange_sl >= entry) or (side == "SHORT" and exchange_sl <= entry)) and not pos.get("locked_profit"):
                        pos["locked_profit"] = True
                        if str(pos.get("sl_kind") or "").upper() not in {"TRAILING", "BREAKEVEN"}:
                            pos["sl_kind"] = "BREAKEVEN"
                        migrated = True
                    if exchange_tp > 0 and not pos.get("tp_prices"):
                        pos["tp_prices"] = [exchange_tp]
                        migrated = True

            # Check for new positions (opened externally)
            for symbol, ex_pos in exchange_map.items():
                if symbol not in self.positions:
                    logger.warning(
                        f"[BYBIT_MAINNET] {symbol} found on exchange but not in local state, "
                        f"importing..."
                    )
                    # Import external position
                    side = "LONG" if ex_pos["side"] == "Buy" else "SHORT"
                    imported_pos = {
                        "symbol": symbol,
                        "exchange": "bybit",
                        "side": side,
                        "entry_price": _ex_float(ex_pos.get("avgPrice"), 0.0),
                        "sl_price": _ex_float(ex_pos.get("stopLoss"), 0.0),
                        "tp_prices": [_ex_float(ex_pos.get("takeProfit"), 0.0)] if _ex_float(ex_pos.get("takeProfit"), 0.0) > 0 else [],
                        "qty": ex_pos["size"],
                        "notional": _ex_float(ex_pos.get("positionValue"), 0.0),
                        "opened_at": datetime.now(timezone.utc).isoformat(),
                        "opened_ts": time.time(),
                        "sl_kind": "ORIGINAL",
                        "locked_profit": False,
                        "scratch_high_watermark": float(ex_pos["avgPrice"]),
                        "scratch_low_watermark": float(ex_pos["avgPrice"]),
                        "scratch_max_favorable_r": 0.0,
                        "scratch_max_adverse_r": 0.0,
                        "metadata": {"imported": True}
                    }
                    await self._enrich_imported_position(symbol, imported_pos)
                    self.positions[symbol] = imported_pos
            
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
            update_scratch_excursion(pos, mark)
            
            # Check provider SL before dynamic exits so canonical hard-stop ownership is preserved.
            sl = pos.get("sl_price", 0)
            if sl and self._level_hit(pos, mark, sl, favorable=False):
                await self._close(symbol, "HARD_SL", sl_kind=pos.get("sl_kind", "ORIGINAL"), exit_price=mark)
                continue
            
            # Check TP levels before scratch/damage. This prevents full SCRATCH_EXIT at
            # breakeven when price already qualifies for a provider TP partial.
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
                        original_qty = float(pos.get("qty", 0))
                        total_tps = max(1, len(tp_prices))
                        
                        # Dynamic TP slicing: slice fraction based on total TPs provided
                        if total_tps == 1:
                            target_fraction = 1.0
                        elif total_tps == 2:
                            target_fraction = 0.50
                        elif total_tps == 3:
                            target_fraction = 0.40 if i == 0 else 0.30
                        else:
                            target_fraction = 1.0 / total_tps
                        
                        # Target quantity for this TP
                        target_qty = original_qty * target_fraction
                        
                        # Is this the final TP or is remaining dust?
                        is_last_tp = (i == total_tps - 1) or ((qty_remaining - target_qty) <= original_qty * 0.05)
                        
                        if is_last_tp:
                            close_qty = qty_remaining
                            qty_remaining = 0.0
                        else:
                            close_qty = min(target_qty, qty_remaining)
                            qty_remaining -= close_qty
                        
                        # Mark this TP as hit
                        tp_hit.append(i)
                        pos["tp_hit"] = tp_hit
                        pos["qty_remaining"] = qty_remaining
                        
                        reason_str = f"TP{i+1}_FINAL" if is_last_tp else f"TP{i+1}"
                        
                        # Close partial or full position
                        await self._close_partial(
                            symbol=symbol,
                            reason=reason_str,
                            exit_price=mark,
                            close_qty=close_qty,
                            qty_remaining=qty_remaining,
                            full_close=is_last_tp
                        )
                        
                        pct_str = f"{(close_qty/original_qty)*100:.0f}%" if original_qty > 0 else "?"
                        logger.info(
                            f"[BYBIT_MAINNET] {symbol} {reason_str} @ {mark:.6f} | "
                            f"Closed {close_qty:.4f} ({pct_str}) | Remaining: {qty_remaining:.4f} "
                            f"(total TPs: {total_tps})"
                        )
                        
                        if not is_last_tp and symbol in self.positions:
                            self._save_positions()
                        break  # Process one TP per cycle
            
            if symbol not in self.positions:
                continue
            pos = self.positions[symbol]
            
            # Dynamic exits run after provider SL/TP checks. SCRATCH_EXIT still has
            # its own TP/progress guard; DAMAGE_REDUCER remains protective for chronic losers.
            if self.dynamic_exit_enabled:
                if await self._apply_dynamic_exits(symbol, pos, mark):
                    continue
    
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
    
    def _get_risk_distance(self, pos: Dict) -> float:
        """Resolve the initial/original risk distance for R-multiple calculations.
        Avoids collapsing to 0.0 when sl_price is moved to breakeven."""
        entry = float(pos.get("entry_price") or 0.0)
        initial_rd = float(pos.get("initial_risk_distance") or 0.0)
        if initial_rd > 0:
            return initial_rd

        orig_sl = float(pos.get("provider_sl_original") or pos.get("original_sl_price") or 0.0)
        if orig_sl > 0 and abs(entry - orig_sl) > 0:
            return abs(entry - orig_sl)

        meta = pos.get("metadata") or {}
        adv = meta.get("adv_snapshot") or {}
        tp_plan = adv.get("manual_tp_plan") or {}
        plan_rd = float(tp_plan.get("risk_distance") or 0.0)
        if plan_rd > 0:
            return plan_rd

        tp_prices = pos.get("tp_prices") or []
        if tp_prices and len(tp_prices) > 0:
            tp1 = float(tp_prices[0])
            if abs(tp1 - entry) > 0:
                return abs(tp1 - entry)

        sl = float(pos.get("sl_price") or 0.0)
        if sl > 0 and abs(entry - sl) > 0.000001:
            return abs(entry - sl)

        return abs(entry * 0.01) if entry > 0 else 1.0

    async def _get_recent_swing_level(self, symbol: str, side: str, limit: int = 12) -> Optional[float]:
        """
        Identify recent structural swing level from 15m closed candles.
        
        For LONG: finds the lowest low among recent closed 15m candles and applies
        a -0.3% anti-hunt buffer.
        For SHORT: finds the highest high among recent closed 15m candles and applies
        a +0.3% anti-hunt buffer.
        
        Caches kline results for 30s to avoid redundant API load.
        """
        now = time.time()
        cached = self._kline_cache.get(symbol)
        if cached and (now - cached.get("ts", 0) < 30.0):
            klines = cached.get("klines", [])
        else:
            try:
                klines = await asyncio.to_thread(
                    self.client.get_klines,
                    symbol=symbol,
                    interval="15",
                    limit=limit
                )
                if klines:
                    self._kline_cache[symbol] = {"ts": now, "klines": klines}
            except Exception as e:
                logger.warning(f"[HYBRID_TRAIL] Failed to fetch klines for {symbol}: {e}")
                if cached:
                    klines = cached.get("klines", [])
                else:
                    return None
        
        if not klines or len(klines) < 2:
            return None
        
        # klines[0] is current open candle; klines[1:] are closed candles
        closed_klines = klines[1:min(len(klines), limit)]
        if not closed_klines:
            return None
        
        try:
            buffer_pct = float(os.getenv("HYBRID_TRAIL_SWING_BUFFER_PCT", "0.3")) / 100.0
            if side == "LONG":
                window_lows = [float(k[3]) for k in closed_klines[:8] if len(k) > 3]
                if not window_lows:
                    return None
                recent_swing_low = min(window_lows)
                return recent_swing_low * (1.0 - buffer_pct)
            else:
                window_highs = [float(k[2]) for k in closed_klines[:8] if len(k) > 2]
                if not window_highs:
                    return None
                recent_swing_high = max(window_highs)
                return recent_swing_high * (1.0 + buffer_pct)
        except Exception as e:
            logger.warning(f"[HYBRID_TRAIL] Error parsing klines for {symbol}: {e}")
            return None

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
        scratch_excursion = update_scratch_excursion(pos, mark)
        
        # Log heartbeat
        logger.info(
            f"[DYN_EXIT_CHECK] {symbol} {side} | "
            f"opened {hold_minutes:.0f}m ago | pnl {unrealized_pct:+.2f}% | "
            f"mfe={scratch_excursion.get('max_favorable_r', 0):.2f}R"
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
                "-1001161683441": "4h",      # Global Crypto Research (GCR)
            }
            
            scratch_min = calculate_smart_scratch_timeout(
                position=pos,
                channel_config=channel_config,
                enable_smart=enable_smart,
                phase1_4h_only=phase1_4h_only
            )
            
            scratch_max_abs_pnl = float(os.getenv("SCRATCH_EXIT_MAX_ABS_PNL_PCT", "0.4"))
            decision = evaluate_scratch_exit_gate(
                position=pos,
                mark=mark,
                hold_minutes=hold_minutes,
                scratch_timeout_min=scratch_min,
                unrealized_pct=unrealized_pct,
                max_abs_pnl_pct=scratch_max_abs_pnl,
            )
            self._append_scratch_audit(symbol, pos, decision, mark)
            
            if decision["should_exit"]:
                logger.info(
                    f"[SCRATCH_EXIT] {symbol} stagnant near BE "
                    f"(hold: {hold_minutes:.0f}min >= {scratch_min:.0f}min, "
                    f"pnl: {unrealized_pct:+.2f}% <= {scratch_max_abs_pnl}%, "
                    f"mfe={decision.get('max_favorable_r', 0):.2f}R)"
                )
                pos["dynamic_exit_layer"] = {"layer": "SCRATCH_EXIT", **decision, "at": now.isoformat(), "mark": mark}
                await self._close(symbol, "SCRATCH_EXIT", sl_kind=pos.get("sl_kind", "ORIGINAL"), exit_price=mark)
                return True
            if decision["time_due"] and decision["near_breakeven"]:
                logger.info(
                    f"[SCRATCH_WAIT] {symbol} {decision['reason']} | "
                    f"hold={hold_minutes:.0f}m pnl={unrealized_pct:+.2f}% "
                    f"mfe={decision.get('max_favorable_r', 0):.2f}R tp1_progress={decision.get('tp1_progress', 0):.0%}"
                )
        
        # LAYER 2: DAMAGE_REDUCER (cut confirmed losers before hard SL)
        if self._config_bool("DAMAGE_REDUCER_ENABLED", default=True):
            damage_min = float(os.getenv("DAMAGE_REDUCER_MIN_HOLD_MINUTES", "45"))
            damage_max_loss = float(os.getenv("DAMAGE_REDUCER_MAX_LOSS_PCT", "-2.5"))
            decision = evaluate_damage_reducer_gate(
                position=pos,
                mark=mark,
                hold_minutes=hold_minutes,
                unrealized_pct=unrealized_pct,
                damage_min_hold_min=damage_min,
                damage_max_loss_pct=damage_max_loss,
            )
            self._append_scratch_audit(symbol, pos, decision, mark, layer="DAMAGE_REDUCER")
            if decision["should_exit"]:
                logger.info(
                    f"[DAMAGE_REDUCER] {symbol} {decision['reason']} | "
                    f"hold={hold_minutes:.0f}m pnl={unrealized_pct:+.2f}% "
                    f"r={decision.get('current_r', 0):+.2f} evidence={decision.get('reversal_evidence', [])}"
                )
                pos["dynamic_exit_layer"] = {"layer": "DAMAGE_REDUCER", **decision, "at": now.isoformat(), "mark": mark}
                await self._close(symbol, "DAMAGE_REDUCER", sl_kind=pos.get("sl_kind", "ORIGINAL"), exit_price=mark)
                return True
            if decision.get("should_partial_exit"):
                stage_key = str(decision.get("stage_key") or decision.get("reason") or "DAMAGE_PARTIAL")
                done_stages = pos.setdefault("damage_partial_stages", [])
                if stage_key not in done_stages:
                    qty_remaining = float(pos.get("qty_remaining", pos.get("qty", 0)) or 0)
                    original_qty = float(pos.get("qty", 0) or 0)
                    close_fraction = float(decision.get("close_fraction") or 0.5)
                    close_qty = qty_remaining * close_fraction
                    next_qty_remaining = max(0.0, qty_remaining - close_qty)
                    if qty_remaining > 0 and close_qty > 0:
                        done_stages.append(stage_key)
                        logger.info(
                            f"[DAMAGE_REDUCER_PARTIAL] {symbol} {decision['reason']} | "
                            f"close={close_fraction:.0%} hold={hold_minutes:.0f}m pnl={unrealized_pct:+.2f}% "
                            f"r={decision.get('current_r', 0):+.2f} mfe={decision.get('max_favorable_r', 0):+.2f}R "
                            f"evidence={decision.get('reversal_evidence', [])}"
                        )
                        pos["dynamic_exit_layer"] = {"layer": "DAMAGE_REDUCER_PARTIAL", **decision, "at": now.isoformat(), "mark": mark}
                        await self._close_partial(
                            symbol,
                            f"DAMAGE_REDUCER_{decision['reason']}",
                            exit_price=mark,
                            close_qty=close_qty,
                            qty_remaining=next_qty_remaining,
                            sl_kind=pos.get("sl_kind", "ORIGINAL"),
                            full_close=next_qty_remaining <= original_qty * 0.05,
                        )
                        return True
            if decision["time_due"] and (decision["pct_due"] or decision.get("current_r", 0) <= decision.get("normal_min_adverse_r", -0.45) or decision.get("mfe_giveback") or decision.get("local_bottom_guard")):
                bucket = int(hold_minutes // 15)
                wait_key = f"{decision['reason']}:{bucket}"
                if pos.get("damage_wait_last_key") != wait_key:
                    pos["damage_wait_last_key"] = wait_key
                    logger.info(
                        f"[DAMAGE_WAIT] {symbol} {decision['reason']} | "
                        f"hold={hold_minutes:.0f}m pnl={unrealized_pct:+.2f}% "
                        f"r={decision.get('current_r', 0):+.2f} evidence={decision.get('reversal_evidence', [])}"
                    )
        
        # LAYER 3: PROFIT_LOCK (move SL to BE+buffer)
        # GATE: Only lock after TP1 hit to avoid premature locks from volatility spikes
        if self._config_bool("PROFIT_LOCK_ENABLED", default=True):
            if not pos.get("locked_profit", False):
                lock_min = float(os.getenv("PROFIT_LOCK_MIN_MINUTES", "15"))
                lock_buffer = float(os.getenv("PROFIT_LOCK_BUFFER_PCT", "0.3"))
                
                # TP1-gated: only lock after at least one TP hit (confirmed profit)
                tp_hit_count = len(pos.get("tp_hit", []))
                
                # Trigger instantly once at least one TP is confirmed
                if tp_hit_count >= 1:
                    # Calculate new SL (breakeven + buffer)
                    sl = pos.get("sl_price", 0)
                    risk_distance = self._get_risk_distance(pos)
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
                        instrument = self.client.get_instrument_info(symbol)
                        tick_size = float(instrument["priceFilter"]["tickSize"])
                        result = self.client.set_trading_stop(
                            symbol=symbol,
                            position_idx=0,  # Required for one-way mode
                            stop_loss=self._quantize(new_sl, tick_size)
                        )
                        
                        # BybitClient already raises when retCode != 0. The
                        # trading-stop endpoint often returns an empty result
                        # body ({}) on success, so do not treat {} as failure.
                        
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
                                    current_sl = float(exchange_map[symbol].get("stopLoss") or 0)
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
        
        # LAYER 4: HYBRID STEP-LOCK RATCHET & 15M SWING-LOW TRAILING ENGINE
        # Synthesizes structural invalidation (recent 15m closed candle swing)
        # with milestone step-lock floor (locking TP1/TP2/TP3 levels).
        tp_hit_count = len(pos.get("tp_hit", []))
        entry = float(pos.get("entry_price") or 0)
        sl = float(pos.get("sl_price") or 0)
        risk_distance = self._get_risk_distance(pos)
        
        if side == "LONG":
            profit_r = (mark - entry) / risk_distance if risk_distance else 0
        else:
            profit_r = (entry - mark) / risk_distance if risk_distance else 0
        
        locked = pos.get("locked_profit", False)
        can_trail = locked or tp_hit_count >= 1 or profit_r >= 1.5
        if not can_trail:
            return False
        
        if "high_watermark" not in pos:
            pos["high_watermark"] = mark
        if "low_watermark" not in pos:
            pos["low_watermark"] = mark
        
        if side == "LONG":
            pos["high_watermark"] = max(float(pos["high_watermark"]), mark)
            peak = float(pos["high_watermark"])
        else:
            pos["low_watermark"] = min(float(pos["low_watermark"]), mark)
            peak = float(pos["low_watermark"])
        
        # Don't activate trailing ratchet until at least 1.5R profit or TP1 hit
        if profit_r < 1.5 and tp_hit_count < 1:
            return False
        
        # 1. Milestone Step-Lock Floor Calculation
        tp_prices = pos.get("tp_prices") or []
        def _get_tp_val(idx: int, default_r: float) -> float:
            if len(tp_prices) > idx:
                try:
                    val = float(tp_prices[idx])
                    if val > 0:
                        return val
                except (ValueError, TypeError):
                    pass
            if side == "LONG":
                return entry + (risk_distance * default_r)
            else:
                return entry - (risk_distance * default_r)

        tp1 = _get_tp_val(0, 1.0)
        tp2 = _get_tp_val(1, 2.0)
        tp3 = _get_tp_val(2, 3.0)
        
        be_buffer = risk_distance * 0.05
        step_floor = sl
        
        if side == "LONG":
            be_level = entry + be_buffer
            if tp_hit_count >= 3 or profit_r >= 3.0:
                # After TP3 or 3.0R: Floor is locked at TP2
                step_floor = max(step_floor, tp2)
            elif tp_hit_count >= 2 or profit_r >= 2.0:
                # After TP2 or 2.0R: Floor is locked at TP1
                step_floor = max(step_floor, tp1)
            elif tp_hit_count >= 1 or locked or profit_r >= 1.5:
                # After TP1 or 1.5R: Floor is locked at Breakeven + buffer
                step_floor = max(step_floor, be_level)
        else:
            be_level = entry - be_buffer
            if tp_hit_count >= 3 or profit_r >= 3.0:
                step_floor = min(step_floor, tp2) if step_floor > 0 else tp2
            elif tp_hit_count >= 2 or profit_r >= 2.0:
                step_floor = min(step_floor, tp1) if step_floor > 0 else tp1
            elif tp_hit_count >= 1 or locked or profit_r >= 1.5:
                step_floor = min(step_floor, be_level) if step_floor > 0 else be_level

        # 2. Query 15m Structural Swing Level Invalidation
        swing_level = await self._get_recent_swing_level(symbol, side)
        
        # 3. Hybrid Synthesis: Combine Structure + Step Floor
        if side == "LONG":
            candidate_sl = step_floor
            if swing_level is not None and swing_level > sl:
                # Market formed a higher structural low above current SL
                # Follow swing low, guaranteed not to fall below milestone floor
                candidate_sl = max(step_floor, swing_level)
            
            # Anti-choke breathing room: never put SL within 0.4% of mark price
            max_allowed_sl = mark * 0.996
            target_sl = min(candidate_sl, max_allowed_sl)
            
            # Only ratchet forward
            if target_sl <= sl:
                return False
            
            # Minimum increment check (at least 0.05R or 0.1% of mark)
            min_increment = max(risk_distance * 0.05, mark * 0.001)
            if (target_sl - sl) < min_increment:
                return False
        else:
            candidate_sl = step_floor if step_floor > 0 else sl
            if swing_level is not None and (sl == 0 or swing_level < sl):
                candidate_sl = min(step_floor, swing_level) if step_floor > 0 else swing_level
            
            # Anti-choke breathing room: never put SL within 0.4% of mark price
            min_allowed_sl = mark * 1.004
            target_sl = max(candidate_sl, min_allowed_sl)
            
            # Only ratchet downward
            if sl > 0 and target_sl >= sl:
                return False
            
            min_increment = max(risk_distance * 0.05, mark * 0.001)
            if sl > 0 and (sl - target_sl) < min_increment:
                return False
        
        # 4. Update Trading Stop on Bybit Exchange
        try:
            instrument = await asyncio.to_thread(self.client.get_instrument_info, symbol)
            tick_size = float(instrument["priceFilter"]["tickSize"])
            quantized_sl = self._quantize(target_sl, tick_size)
            
            await asyncio.to_thread(
                self.client.set_trading_stop,
                symbol=symbol,
                position_idx=0,
                stop_loss=quantized_sl
            )
            
            old_sl = sl
            pos["sl_price"] = target_sl
            pos["sl_kind"] = "HYBRID_STEP_SWING"
            pos["locked_profit"] = True
            self._save_positions()
            
            logger.info(
                f"[HYBRID_TRAIL] {symbol} {side} ratchet: SL {old_sl:.4f} → {target_sl:.4f} "
                f"(quantized={quantized_sl}) | step_floor={step_floor:.4f}, "
                f"swing_lvl={f'{swing_level:.4f}' if swing_level else 'N/A'}, mark={mark:.4f}, profit={profit_r:.2f}R"
            )
        except Exception as e:
            err_str = str(e)
            if "34040" in err_str or "not modified" in err_str:
                return False
            if "10001" in err_str and "zero position" in err_str:
                return False
            logger.error(f"[HYBRID_TRAIL] {symbol} failed to update SL: {e}")
        
        return False
    
    async def run_maintenance_loop(self):
        """Main maintenance loop - check exits every 3s."""
        logger.info("[BYBIT_MAINNET] Starting maintenance loop")
        
        while True:
            try:
                async with self._position_lock:
                    # Sync with exchange
                    await self.sync_positions()
                    
                    # Check exits
                    await self.check_and_execute_exits()
                
                # Sleep OUTSIDE the lock so submit_open can acquire it
                await asyncio.sleep(3)
            
            except KeyboardInterrupt:
                logger.info("[BYBIT_MAINNET] Shutting down...")
                break
            except Exception as e:
                logger.exception(f"[BYBIT_MAINNET] Maintenance loop error: {e}")
                await asyncio.sleep(5)
