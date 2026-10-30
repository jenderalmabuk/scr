"""ExecutionGateway — ONE RiskManager + ONE trader for every signal source.

This is the "one hand" of the system. It wraps the components that already
exist and are battle-tested in the repo:

    risk/risk_engine.py            -> RiskManager   (unified portfolio limits)
    execution/binance_testnet_trader.py -> trader   (submit_open contract,
                                                     partial TP, trailing, …)

Flow for every OrderIntent, regardless of which engine sent it:

    validate -> risk gate (can_open_new_position) -> size (if needed)
             -> reserve_open_risk -> trader.submit_open -> commit / release

This mirrors exactly what signal_copy/executor.py already does — the gateway
simply makes that path the ONLY path, shared by all engines.
"""

from __future__ import annotations

from pathlib import Path

import hashlib
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from gateway.order_intent import OrderIntent
from execution.manual_signal_tools import fetch_manual_signal_context, normalize_manual_tp_ladder

logger = logging.getLogger("gateway")

REJECTED_INTENTS_PATH = Path(os.getenv("REJECTED_INTENTS_PATH", "/opt/signalcopyreal/journal/rejected_intents.jsonl"))


def _get_active_code_hash() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(Path(__file__).resolve().parent),
            stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        return "head"


def _get_active_config_hash() -> str:
    keys = [
        "SIGNALCOPY_RISK_PER_TRADE_USD", "STARTING_BALANCE", "MAX_ENTRY_DEVIATION_PCT",
        "MIN_ENTRY_SL_DISTANCE_PCT", "SYMBOL_HARD_SL_COOLDOWN_SEC", "GATEWAY_BTC_REGIME_REVALIDATE",
        "GATEWAY_MAX_DIRECTIONAL_POSITIONS", "GATEWAY_MAX_POSITION_LEVERAGE"
    ]
    blob = "|".join(f"{k}={os.getenv(k, '')}" for k in sorted(keys))
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def _classify_reject_layer(reason: str) -> str:
    r = (reason or "").upper()
    if "REGIME_FLIPPED" in r or "BTC IN" in r:
        return "DIRECTIONAL_REGIME_GATE"
    if "COOLDOWN" in r:
        return "COOLDOWN_GUARD"
    if "DIRECTIONAL_EXPOSURE" in r:
        return "CORRELATION_GUARD"
    if "RUNNING POSITIONS" in r:
        return "PORTFOLIO_CAP"
    if "ENTRY_CHASE" in r or "DEVIATION" in r:
        return "EXECUTION_PRICE_GUARD"
    if "TOO_CLOSE_TO_FILL" in r or "INSTANT_STOP" in r:
        return "SL_DISTANCE_GUARD"
    if "PRICE_PROVENANCE" in r or "STALE_GATEWAY_MARK" in r or "NO_MARKET_PRICE" in r:
        return "PROVENANCE_GUARD"
    if "RISK" in r or "CAP" in r or "EQUITY" in r:
        return "RISK_MANAGER"
    return "GATEWAY_SAFETY"


def _compute_event_hash(prev_hash: str, payload_data: Dict[str, Any]) -> str:
    canonical = prev_hash + "|" + json.dumps(payload_data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_rejected_intents_journal(path: Path) -> tuple[bool, int, str]:
    """Verify append-only cryptographic hash chain on journal."""
    if not path.exists():
        return True, 0, "file_not_found_empty"
    prev_hash = "0" * 64
    count = 0
    with path.open("r", encoding="utf-8") as f:
        for idx, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as err:
                return False, count, f"line {idx} json decode error: {err}"
            
            stored_prev = rec.get("previous_event_hash")
            stored_hash = rec.get("event_hash")
            if stored_prev != prev_hash:
                return False, count, f"line {idx} hash-chain break: stored_prev={stored_prev} != expected={prev_hash}"
            
            # Recompute expected hash
            data_copy = {k: v for k, v in rec.items() if k != "event_hash"}
            expected_hash = _compute_event_hash(prev_hash, data_copy)
            if stored_hash != expected_hash:
                return False, count, f"line {idx} tamper detected: stored_hash={stored_hash} != expected={expected_hash}"
            
            prev_hash = stored_hash
            count += 1
    return True, count, "chain_valid"


def trader_rejected(response) -> bool:
    """Normalize bool/dict trader contracts into one rejection decision."""
    return not response or (
        isinstance(response, dict)
        and not response.get("ok", response.get("success", True))
    )


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class GatewayResult:
    ok: bool
    reason: str
    intent_id: str = ""
    symbol: str = ""
    side: str = ""
    notional: float = 0.0
    risk_amount: float = 0.0
    trader_response: Optional[Dict[str, Any]] = field(default=None)
    risk_details: Optional[Dict[str, Any]] = field(default=None)
    position_confirmed: bool = False  # Add field for orchestrator compatibility

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "intent_id": self.intent_id,
            "symbol": self.symbol,
            "side": self.side,
            "notional": self.notional,
            "risk_amount": self.risk_amount,
            "trader_response": self.trader_response,
            "risk_details": self.risk_details,
            "position_confirmed": self.position_confirmed,
        }


class ExecutionGateway:
    """Single choke-point for order execution.

    Args:
        trader:   any object exposing the fusion async ``submit_open(**kwargs)``
                  contract (BinanceTestnetTrader today; swappable later).
        risk_mgr: risk.risk_engine.RiskManager instance (the ONE portfolio).
        submit_timeout_sec: how long to wait for the trader router queue.
    """

    def __init__(self, trader: Any, risk_mgr: Any, *, submit_timeout_sec: float = 30.0):
        self.trader = trader
        self.risk_mgr = risk_mgr
        self.submit_timeout_sec = float(submit_timeout_sec)
        self.history: List[Dict[str, Any]] = []   # last N results, for /portfolio & debugging
        self._history_max = 200
        self.smart_virtual_book = None
        self._symbol_sl_times: Dict[str, float] = {}
        self._seen_rejected_intent_ids: set = set()
        self._last_reject_event_hash: Optional[str] = None

    # ── public API ───────────────────────────────────────────

    async def execute(self, intent: OrderIntent, *, dry_run: bool = False) -> GatewayResult:
        """Run one intent through the full unified pipeline."""
        res = GatewayResult(ok=False, reason="", intent_id=intent.intent_id,
                            symbol=intent.symbol, side=str(intent.side).upper())

        # 1) validate the contract itself
        err = intent.validate()
        if err:
            res.reason = f"invalid intent: {err}"
            return self._record(intent, res)

        symbol = intent.symbol
        side = str(intent.side).upper()
        entry = float(intent.entry_price)
        sl = float(intent.sl_price)

        if intent.source == "MANUAL" and os.getenv("GATEWAY_ENRICH_MANUAL_SIGNALS", "true").lower() in ("1", "true", "yes"):
            await self._enrich_manual_intent(intent, symbol, side, entry, sl)

        # Independent Arm C paper book observes every valid Signal Copy intent before
        # canonical capacity/risk gates. Failure never changes canonical decision.
        if (intent.source == "SIGNAL_COPY" and
                os.getenv("GATEWAY_PAPER_MAINNET", "false").lower() in ("1", "true", "yes")):
            try:
                if self.smart_virtual_book is None:
                    from execution.smart_virtual_portfolio import SmartVirtualPortfolioBook
                    self.smart_virtual_book = SmartVirtualPortfolioBook(
                        self.trader, equity=float(self.risk_mgr.get_current_equity()))
                    self.trader.smart_virtual_book = self.smart_virtual_book
                await self.smart_virtual_book.capture(intent)
            except Exception as exc:
                logger.warning("[GATEWAY] smart virtual capture failed %s: %s", symbol, exc)

        # Pre-fill Symbol HARD_SL Cooldown Guard (60 min default)
        sl_cooldown_sec = float(os.getenv("SYMBOL_HARD_SL_COOLDOWN_SEC", "3600.0"))
        if sl_cooldown_sec > 0:
            last_sl_time = self._get_symbol_last_sl_time(symbol)
            if last_sl_time > 0:
                elapsed = time.time() - last_sl_time
                if elapsed < sl_cooldown_sec:
                    remaining_m = (sl_cooldown_sec - elapsed) / 60.0
                    res.reason = f"SYMBOL_HARD_SL_COOLDOWN_ACTIVE: {symbol} in cooldown for {remaining_m:.1f}m after stop loss"
                    logger.warning("[GATEWAY] %s", res.reason)
                    return self._record(intent, res)

        # Pre-fill BTC Fast Regime Revalidation Guard for SHORT intents
        if side == "SHORT" and os.getenv("GATEWAY_BTC_REGIME_REVALIDATE", "true").lower() in ("1", "true", "yes"):
            try:
                from signal_copy.btc_regime_gate import BtcRegimeGate
                gate = BtcRegimeGate.get_instance()
                btc_info = await gate.get_btc_regime()
                reg = str(btc_info.get("regime", "")).upper()
                diff = float(btc_info.get("diff_pct", 0.0))
                observed_ts = btc_info.get("timestamp", time.time())
                observed_iso = datetime.fromtimestamp(observed_ts, tz=timezone.utc).isoformat()
                now_iso = datetime.now(timezone.utc).isoformat()
                if reg in ("PUMP", "STRONG_BULLISH") or diff > 0.35:
                    flip_ts = btc_info.get("timestamp", time.time())
                    flip_iso = datetime.fromtimestamp(flip_ts, tz=timezone.utc).isoformat()
                    res.reason = f"REGIME_FLIPPED_BEFORE_FILL: BTC in {reg} regime (+{diff:.2f}% above VWAP)"
                    if intent.adv_snapshot is None:
                        intent.adv_snapshot = {}
                    intent.adv_snapshot.update({
                        "regime_observed_at": observed_iso,
                        "decision_at": now_iso,
                        "regime_flip_at": flip_iso,
                        "order_created_at": getattr(intent, "timestamp", now_iso),
                        "fill_at": None,
                    })
                    logger.warning("[GATEWAY] %s %s blocked: %s (observed_at=%s flip_at=%s)",
                                   symbol, side, res.reason, observed_iso, flip_iso)
                    return self._record(intent, res)
            except Exception as exc:
                logger.debug("[GATEWAY] btc regime revalidation check skipped: %s", exc)

        # 2) Hard cap counts running positions only. Pending setup limits stay unlimited.
        max_running = self.risk_mgr.get_max_running_positions()
        if self.risk_mgr._position_count() >= max_running:
            res.reason = f"max running positions reached ({max_running})"
            return self._record(intent, res)

        # Correlation Guard: Limit directional exposure to prevent Cluster Death
        max_directional = int(os.getenv("GATEWAY_MAX_DIRECTIONAL_POSITIONS", "2"))
        if max_directional > 0 and hasattr(self.trader, "positions"):
            current_positions = getattr(self.trader, "positions", {}) or {}
            same_dir_count = sum(1 for p in current_positions.values() if str(p.get("side", "")).upper() == side)
            if same_dir_count >= max_directional:
                res.reason = f"MAX_DIRECTIONAL_EXPOSURE_{side} (current={same_dir_count}, max={max_directional})"
                return self._record(intent, res)

        # A previously validated pending limit may fill later. Do not apply
        # entry-arrival cooldowns again; running-position cap still applies.
        pending_fill = str(intent.tag).startswith("fusion_quantum_pending_")

        # 3) unified portfolio gate — the whole point of the gateway.
        try:
            gate = self.risk_mgr.check_risk_limits(
                symbol=symbol,
                is_vip=intent.is_vip,
                side=side,
                skip_entry_cooldown=pending_fill,
                skip_cluster_limit=intent.source in ("SIGNAL_COPY", "AUTONOMOUS_SCANNER"),
            )
        except Exception as exc:  # never let a risk-engine bug open an unchecked trade
            res.reason = f"risk check error: {exc}"
            return self._record(intent, res)
        if not gate.get("can_trade", False):
            res.reason = f"blocked by portfolio risk: {gate.get('reason', 'unknown')}"
            return self._record(intent, res)

        # 3) sizing — respect the intent's own SL for risk math
        notional, risk_amount, size_err = self._size(intent, entry, sl, side)
        if size_err:
            res.reason = size_err
            return self._record(intent, res)
        # Fetch fresh execution provenance when the selected trader exposes it.
        price_quote = None
        if callable(getattr(self.trader, "_get_fresh_open_quote", None)):
            try:
                _, price_quote = await self._paper_fill_entry(symbol, side, entry)
            except (TypeError, ValueError) as exc:
                res.reason = f"PRICE_VALIDATION_FAILED: {exc}"
                return self._record(intent, res)

        # Paper HARD_SL economic risk = adverse entry + adverse exit@SL + round-trip fees.
        # Size/reserve on that, not raw signal distance — LAUSDT blew 1% on fees/exit-slip.
        if os.getenv("GATEWAY_PAPER_MAINNET", "false").lower() in ("1", "true", "yes"):
            try:
                notional, risk_amount, price_quote = await self._paper_econ_size(
                    symbol, side, entry, sl, notional, intent.source,
                )
            except (TypeError, ValueError) as exc:
                res.reason = f"PRICE_VALIDATION_FAILED: {exc}"
                return self._record(intent, res)
        res.notional = notional
        res.risk_amount = risk_amount

        # 4) build the trader payload (same keys signal_copy already uses,
        #    so BinanceTestnetTrader needs ZERO changes)
        payload = self._build_payload(intent, entry, sl, side, notional, risk_amount)
        if price_quote is not None:
            payload["execution_price_quote"] = price_quote

        if dry_run:
            res.ok = True
            res.reason = "DRY_RUN (no order placed)"
            logger.info("[GATEWAY] DRY_RUN %s src=%s %s entry=%.6f notional=%.2f sl=%.6f",
                        symbol, intent.source, side, entry, notional, sl)
            return self._record(intent, res)

        # 5) reserve -> submit -> commit/release (identical semantics to signal_copy)
        try:
            reserve = await self.risk_mgr.reserve_open_risk_details(symbol, risk_amount)
        except Exception as exc:
            res.reason = f"RESERVATION_ERROR: {exc}"
            res.risk_details = {"reserved": False, "code": "RESERVATION_ERROR"}
            return self._record(intent, res)
        res.risk_details = reserve
        if not reserve["reserved"]:
            res.reason = f"open-risk reservation blocked: {reserve['code']}"
            return self._record(intent, res)

        try:
            opened = await self.trader.submit_open(timeout_sec=self.submit_timeout_sec, **payload)
        except Exception as exc:
            logger.exception("[GATEWAY] submit_open failed %s: %s", symbol, exc)
            await self._safe_release(symbol)
            res.reason = f"submit_open error: {exc}"
            return self._record(intent, res)

        res.trader_response = opened if isinstance(opened, dict) else {"raw": str(opened)}
        if trader_rejected(opened):
            await self._safe_release(symbol)
            reason_detail = ""
            if isinstance(opened, dict) and opened.get("error"):
                reason_detail = f" | {opened['error']}"
            elif isinstance(opened, dict) and opened.get("reason"):
                reason_detail = f" | {opened['reason']}"
            res.reason = f"trader rejected open: no position created{reason_detail}"
            logger.warning("[GATEWAY] %s %s: %s", symbol, side, res.reason)
            return self._record(intent, res)

        trader_response = opened if isinstance(opened, dict) else {}
        fill_entry = _safe_float(trader_response.get("entry_price", 0.0))
        fill_notional = _safe_float(trader_response.get("notional", 0.0))
        position_confirmed = bool(trader_response.get("position_confirmed")) or (
            fill_entry > 0 and fill_notional > 0
        )
        if not position_confirmed:
            await self._safe_release(symbol)
            res.reason = "trader accepted but no position confirmed"
            res.position_confirmed = False
            logger.warning("[GATEWAY] %s %s: %s response=%s", symbol, side, res.reason, res.trader_response)
            return self._record(intent, res)

        try:
            actual_fill_risk = trader_response.get("max_loss_usd_at_fill", trader_response.get("actual_risk_amount"))
            committed_risk = float(actual_fill_risk) if actual_fill_risk is not None else risk_amount
            await self.risk_mgr.commit_open_trade(symbol, risk_amount=committed_risk, is_vip=intent.is_vip)
        except Exception as exc:
            logger.warning("[GATEWAY] commit_open_trade error %s: %s", symbol, exc)

        res.ok = True
        res.reason = "opened"
        res.position_confirmed = True
        if fill_notional > 0:
            res.notional = fill_notional
        if trader_response.get("actual_risk_amount") is not None:
            res.risk_amount = _safe_float(trader_response.get("actual_risk_amount"))
        logger.info("[GATEWAY] OPENED %s src=%s %s notional=%.2f risk=%.2f intent=%s",
                    symbol, intent.source, side, res.notional, res.risk_amount, intent.intent_id)
        return self._record(intent, res)

    def _get_symbol_last_sl_time(self, symbol: str) -> float:
        """Get timestamp of last HARD_SL exit for symbol from memory or trade history."""
        mem_time = getattr(self, '_symbol_sl_times', {}).get(symbol, 0.0)
        if mem_time > 0:
            return mem_time
        history_path = Path("/opt/signalcopyreal/journal/trade_history.json")
        if not history_path.exists():
            return 0.0
        try:
            import json
            from datetime import datetime, timezone
            with history_path.open("r", encoding="utf-8") as f:
                history = json.load(f)
            for t in reversed(history[-20:]):
                if t.get("symbol") == symbol:
                    reason = str(t.get("normalized_reason") or t.get("exit_reason") or "").upper()
                    if "HARD_SL" in reason or "STOP_LOSS" in reason or "SL" in reason:
                        tc = t.get("timestamp_close") or t.get("exit_time")
                        if tc:
                            dt = datetime.fromisoformat(tc.replace("Z", "+00:00"))
                            return dt.timestamp()
        except Exception:
            pass
        return 0.0

    def portfolio(self) -> Dict[str, Any]:
        """One unified view of the whole book — every engine included."""
        rm = self.risk_mgr
        out: Dict[str, Any] = {}
        for name, fn in (
            ("equity", "get_current_equity"),
            ("daily_pnl_pct", "get_daily_pnl_pct"),
            ("total_exposure_pct", "get_total_exposure_pct"),
            ("reserved_risk_total", "get_reserved_risk_total"),
            ("active_open_risk_total", "get_active_open_risk_total"),
            ("total_open_risk", "get_total_open_risk"),
            ("global_open_risk_budget", "get_parallel_open_risk_budget"),
        ):
            try:
                out[name] = float(getattr(rm, fn)())
            except Exception:
                out[name] = None
        try:
            out["open_position_count"] = int(rm._position_count())
            out["open_symbols"] = sorted(rm._position_symbols())
        except Exception:
            out["open_position_count"] = None
            out["open_symbols"] = []
        try:
            out["daily_loss_limit_hit"] = bool(rm.is_daily_loss_limit_hit())
            out["exposure_limit_exceeded"] = bool(rm.is_exposure_limit_exceeded())
        except Exception:
            pass
        try:
            positions = getattr(rm.trader, "positions", {}) or {}
            out["open_positions"] = [dict(pos) for pos in positions.values()]
            for pos in out["open_positions"]:
                for key, value in list(pos.items()):
                    if hasattr(value, "isoformat"):
                        pos[key] = value.isoformat()
        except Exception:
            out["open_positions"] = []
        out["recent_intents"] = self.history[-20:]
        return out

    # ── internals ────────────────────────────────────────────

    async def _paper_fill_entry(self, symbol: str, side: str, signal_entry: float):
        """Fresh quote used once for sizing and submission."""
        slip = max(0.0, float(os.getenv("PAPER_SLIPPAGE_PCT", "0.0005")))
        getter = getattr(self.trader, "_get_fresh_open_quote", None)
        if not callable(getter):
            raise ValueError("fresh price provenance unavailable")
        quote = await getter(symbol)
        if not isinstance(quote, dict) or quote.get("price_fresh") is not True:
            raise ValueError("fresh market price unavailable")
        base = float(quote.get("price") or 0.0)
        if base <= 0:
            raise ValueError("fresh market price unavailable")
        buy = side == "LONG"
        return base * (1.0 + slip if buy else 1.0 - slip), quote

    @staticmethod
    def _paper_hard_sl_econ(side: str, slipped_entry: float, sl: float, notional: float) -> float:
        """USD loss if HARD_SL hits: entry slip already in price, exit adverse@SL + both fees."""
        fee = max(0.0, float(os.getenv("PAPER_TAKER_FEE_PCT", "0.0005")))
        slip = max(0.0, float(os.getenv("PAPER_SLIPPAGE_PCT", "0.0005")))
        funding_buf = max(0.0, float(os.getenv("PAPER_FUNDING_BUFFER_PCT", "0.0001")))
        qty = float(notional) / max(float(slipped_entry), 1e-9)
        if side == "LONG":
            adverse_exit = float(sl) * (1.0 - slip)
            gross = (adverse_exit - float(slipped_entry)) * qty
        else:
            adverse_exit = float(sl) * (1.0 + slip)
            gross = (float(slipped_entry) - adverse_exit) * qty
        entry_fee = abs(float(notional)) * fee
        exit_fee = abs(adverse_exit * qty) * fee
        funding = abs(float(notional)) * funding_buf
        return max(0.0, -gross + entry_fee + exit_fee + funding)

    async def _paper_econ_size(
        self, symbol: str, side: str, entry: float, sl: float, notional: float, source: str,
    ):
        slipped_entry, quote = await self._paper_fill_entry(symbol, side, entry)
        risk_amount = self._paper_hard_sl_econ(side, slipped_entry, sl, notional)
        strict_risk_cap = max(0.0, _safe_float(os.getenv("SIGNALCOPY_RISK_PER_TRADE_USD"), 2.0))
        if strict_risk_cap > 0 and risk_amount > strict_risk_cap and risk_amount > 0:
            scale = strict_risk_cap / risk_amount
            notional = float(notional) * scale
            risk_amount = self._paper_hard_sl_econ(side, slipped_entry, sl, notional)
        return float(notional), float(risk_amount), quote

    def _size(self, intent: OrderIntent, entry: float, sl: float, side: str):
        """Return (notional, risk_amount, error)."""
        sl_frac = abs(entry - sl) / entry
        if sl_frac <= 0:
            return 0.0, 0.0, "SL distance is zero"

        try:
            equity = float(self.risk_mgr.get_current_equity())
        except Exception as exc:
            return 0.0, 0.0, f"cannot read equity: {exc}"

        risk_cap_usd = max(0.0, _safe_float(os.getenv("SIGNALCOPY_RISK_PER_TRADE_USD"), 2.0))
        if risk_cap_usd <= 0:
            return 0.0, 0.0, "SIGNALCOPY_RISK_PER_TRADE_USD must be greater than zero"

        # Adaptive Sizing during Drawdown (Anti-Deadlock)
        start_balance = float(os.getenv("STARTING_BALANCE", "100.0"))
        if start_balance > 0 and equity < start_balance:
            drawdown_pct = (1.0 - (equity / start_balance)) * 100.0
            if drawdown_pct >= 20.0:
                risk_cap_usd = min(risk_cap_usd, 0.50)
            elif drawdown_pct >= 10.0:
                risk_cap_usd = min(risk_cap_usd, 1.00)

        # Leveraged futures position sizing cap:
        # Allow position leverage up to GATEWAY_MAX_POSITION_LEVERAGE (default 3.5x equity, max $350)
        # instead of clamping to spot 20% equity ($19.40).
        # Net risk fraction calculation incorporating fees, adverse stop slippage, and funding buffer
        fee_rate = max(0.0, _safe_float(os.getenv("GATEWAY_FEE_PCT", os.getenv("PAPER_TAKER_FEE_PCT", "0.0")), 0.0))
        slip_rate = max(0.0, _safe_float(os.getenv("GATEWAY_SLIPPAGE_PCT", os.getenv("PAPER_SLIPPAGE_PCT", "0.0")), 0.0))
        funding_buf_frac = max(0.0, _safe_float(os.getenv("GATEWAY_FUNDING_PCT", os.getenv("PAPER_FUNDING_BUFFER_PCT", "0.0")), 0.0))
        total_risk_frac = sl_frac + (2.0 * fee_rate) + slip_rate + funding_buf_frac

        max_pos_lev = max(1.0, _safe_float(os.getenv("GATEWAY_MAX_POSITION_LEVERAGE"), 3.5))
        max_notional_env = max(10.0, _safe_float(os.getenv("GATEWAY_MAX_NOTIONAL_USD"), 350.0))
        notional_cap = min(equity * max_pos_lev, max_notional_env) if equity > 0 else None
        risk_notional_cap = risk_cap_usd / max(total_risk_frac, 1e-6)
        min_notional = max(0.0, _safe_float(os.getenv("GATEWAY_MIN_NOTIONAL_USD"), 10.0))

        if intent.notional is not None:
            notional = float(intent.notional)
        else:
            if equity <= 0:
                return 0.0, 0.0, "equity is zero"
            risk_budget = min(equity * float(intent.risk_pct), risk_cap_usd)
            notional = risk_budget / max(total_risk_frac, 1e-6)

        notional = min(notional, risk_notional_cap)
        if notional_cap is not None:
            notional = min(notional, notional_cap)

        if min_notional > 0 and notional < min_notional:
            min_risk = min_notional * total_risk_frac
            if min_risk > risk_cap_usd:
                return 0.0, 0.0, (
                    f"min order notional ${min_notional:.2f} would risk ${min_risk:.2f}, "
                    f"above strict ${risk_cap_usd:.2f} cap"
                )
            notional = min_notional

        risk_amount = notional * total_risk_frac
        if risk_amount > risk_cap_usd + 1e-6:
            return 0.0, 0.0, f"sizing exceeds strict ${risk_cap_usd:.2f} risk cap"
        return notional, risk_amount, None

    async def _enrich_manual_intent(self, intent: OrderIntent, symbol: str, side: str, entry: float, sl: float) -> None:
        snapshot = dict(intent.adv_snapshot or {})
        timeframe = str(snapshot.get("signal_timeframe") or snapshot.get("timeframe") or os.getenv("MANUAL_SIGNAL_TIMEFRAME", "15m"))
        metrics = await fetch_manual_signal_context(symbol, side, timeframe=timeframe)
        snapshot.update({k: v for k, v in metrics.items() if v is not None})
        snapshot.setdefault("signal_timeframe", timeframe)
        snapshot.setdefault("manual_context_version", "manual_context_v4")
        snapshot.setdefault("setup_type", os.getenv("MANUAL_DEFAULT_SETUP_TYPE", "EARLY_ENTRY").upper())
        snapshot.setdefault("scratch_exit_profile", "STRUCTURE_HOLD")
        snapshot.setdefault("manual_entry", True)
        snapshot.setdefault("manual_entry_price", entry)
        snapshot.setdefault("manual_stop_loss", sl)
        intent.adv_snapshot = snapshot
        if snapshot.get("regime_label") and not intent.regime:
            intent.regime = str(snapshot["regime_label"])
        score = _safe_float(snapshot.get("validation_score"), 0.0)
        if score > 0:
            intent.confidence = max(float(intent.confidence or 0.0), min(score / 100.0, 1.0))

    def _build_payload(self, intent: OrderIntent, entry: float, sl: float,
                       side: str, notional: float, risk_amount: float) -> Dict[str, Any]:
        tps = [float(t) for t in (intent.tps or [])]
        use_golden = (
            intent.source == "AUTONOMOUS_SCANNER"
            or (intent.source == "SIGNAL_COPY" and os.getenv("SIGNALCOPY_USE_GOLDEN_SCALP", "true").lower() in ("1", "true", "yes"))
            or (intent.adv_snapshot or {}).get("tp_plan") == "GOLDEN_SCALP"
        )
        if use_golden and entry > 0 and sl > 0 and abs(entry - sl) > 0:
            risk = abs(entry - sl)
            prec = max(4, len(str(entry).split(".")[-1]) if "." in str(entry) else 2)
            tp1 = round(entry + (1.5 * risk) if side == "LONG" else entry - (1.5 * risk), prec)
            tp2 = round(entry + (3.0 * risk) if side == "LONG" else entry - (3.0 * risk), prec)
            tps = [tp1, tp2]
            intent.adv_snapshot = dict(intent.adv_snapshot or {})
            intent.adv_snapshot["tp_plan"] = "GOLDEN_SCALP"
            intent.adv_snapshot["rr1"] = 1.5
            intent.adv_snapshot["rr2"] = 3.0
            intent.adv_snapshot["tp1_pct"] = 0.75
            intent.adv_snapshot["runner_pct"] = 0.25
            intent.adv_snapshot["signal_tp_ladder"] = list(tps)
        elif intent.source == "MANUAL" and os.getenv("GATEWAY_NORMALIZE_MANUAL_TPS", "true").lower() in ("1", "true", "yes"):
            tps, tp_plan = normalize_manual_tp_ladder(side, entry, sl, tps)
            intent.adv_snapshot = dict(intent.adv_snapshot or {})
            intent.adv_snapshot["manual_tp_plan"] = tp_plan
            intent.adv_snapshot["signal_tp_ladder"] = list(tps)
        tp_payload = {f"tp{i}": tp for i, tp in enumerate(tps, start=1)}
        tp_full = tps[-1] if tps else 0.0
        payload: Dict[str, Any] = {
            "symbol": intent.symbol,
            "side": side,
            "direction": side,
            "entry_price": entry,
            "sl_price": sl,
            "sl": sl,
            **tp_payload,
            "tp_full": tp_full,
            "tp_plan": "GOLDEN_SCALP" if use_golden else (intent.adv_snapshot or {}).get("tp_plan", ""),
            "notional": notional,
            "size_usd": notional,
            "base_notional": notional,
            "regime": str(intent.regime or "TRENDING"),
            "confidence": float(intent.confidence),
            "actual_risk_amount": risk_amount,
            "risk_amount": risk_amount,
            "planned_risk_amount": risk_amount,
            "source": intent.source,
            "strategy": str((intent.adv_snapshot or {}).get("strategy") or getattr(intent, "strategy", "UNKNOWN") or "UNKNOWN"),
            "intent_id": intent.intent_id,
            "signal_id": str((intent.adv_snapshot or {}).get("signal_id") or intent.tag or intent.intent_id),
            "signal_entry_price": float(intent.entry_price or entry),
            "raw_sl_price": float(intent.sl_price or sl),
            "target_risk_usd": float((intent.adv_snapshot or {}).get("target_risk_usd") or risk_amount or 2.0),
            "tag": intent.tag,
            "adv": dict(intent.adv_snapshot or {}),
            "adv_snapshot": dict(intent.adv_snapshot or {}),
        }
        if intent.leverage:
            payload["leverage"] = int(intent.leverage)
        return payload

    async def _safe_release(self, symbol: str) -> None:
        try:
            await self.risk_mgr.release_open_risk(symbol)
        except Exception:
            pass

    def _record(self, intent: OrderIntent, res: GatewayResult) -> GatewayResult:
        self.history.append({
            "intent": intent.to_dict(),
            "result": {k: v for k, v in res.to_dict().items() if k != "trader_response"},
        })
        if len(self.history) > self._history_max:
            self.history = self.history[-self._history_max:]
        if not res.ok:
            logger.info("[GATEWAY] REJECTED %s src=%s: %s", intent.symbol, intent.source, res.reason)
            self._append_rejected_intent(intent, res)
        return res

    def _get_last_reject_hash_from_disk(self) -> str:
        if not REJECTED_INTENTS_PATH.exists():
            return "0" * 64
        try:
            with REJECTED_INTENTS_PATH.open("r", encoding="utf-8") as f:
                last_line = ""
                for line in f:
                    if line.strip():
                        last_line = line.strip()
                if last_line:
                    rec = json.loads(last_line)
                    return str(rec.get("event_hash") or ("0" * 64))
        except Exception:
            pass
        return "0" * 64

    def _append_rejected_intent(self, intent: OrderIntent, res: GatewayResult) -> None:
        try:
            intent_id = str(intent.intent_id or "")
            if intent_id and intent_id in self._seen_rejected_intent_ids:
                logger.debug("[GATEWAY] duplicate rejected intent_id %s suppressed from journal", intent_id)
                return
            if intent_id:
                self._seen_rejected_intent_ids.add(intent_id)

            REJECTED_INTENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            snapshot = dict(intent.adv_snapshot or {})
            raw_score = _safe_float(snapshot.get("raw_score", snapshot.get("score")), 0.0)
            final_score = _safe_float(snapshot.get("final_score", snapshot.get("score")), 0.0)
            tp_list = []
            tps_raw = getattr(intent, "tps", None) or getattr(intent, "tp_ladder", None) or []
            if tps_raw:
                tp_list = [float(tp) for tp in tps_raw if tp]
            elif getattr(intent, "tp_full", None):
                tp_list = [float(intent.tp_full)]

            now_iso = datetime.now(timezone.utc).isoformat()
            regime_observed = snapshot.get("regime_observed_at") or snapshot.get("price_observed_at") or now_iso
            sig_id = getattr(intent, "signal_id", None) or snapshot.get("signal_id") or getattr(intent, "tag", None) or getattr(intent, "intent_id", "")
            
            # Fetch previous event hash for blockchain-style hash chaining
            if self._last_reject_event_hash is None:
                self._last_reject_event_hash = self._get_last_reject_hash_from_disk()
            prev_hash = self._last_reject_event_hash

            payload = {
                "schema_version": 2,
                "signal_id": str(sig_id or ""),
                "intent_id": intent_id,
                "decision_timestamp": now_iso,
                "reject_layer": _classify_reject_layer(res.reason),
                "primary_reason": str(res.reason or "UNKNOWN"),
                "all_reasons": [str(res.reason or "UNKNOWN")],
                "symbol": str(intent.symbol or ""),
                "side": str(intent.side or ""),
                "entry": float(intent.entry_price or 0.0),
                "sl": float(intent.sl_price or 0.0),
                "tp": tp_list,
                "score_raw": raw_score,
                "score_final": final_score,
                "regime_snapshot": {
                    "regime": str(intent.regime or snapshot.get("regime", "UNKNOWN")),
                    "decision_at": snapshot.get("decision_at") or now_iso,
                    "regime_observed_at": regime_observed,
                    "regime_flip_at": snapshot.get("regime_flip_at"),
                    "order_created_at": snapshot.get("order_created_at") or now_iso,
                },
                "config_hash": _get_active_config_hash(),
                "code_hash": _get_active_code_hash(),
                "previous_event_hash": prev_hash,
            }

            event_hash = _compute_event_hash(prev_hash, payload)
            payload["event_hash"] = event_hash
            self._last_reject_event_hash = event_hash

            # Durable Append with explicit flush and fsync
            with REJECTED_INTENTS_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, default=str, separators=(",", ":")) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except Exception as exc:
            logger.warning("[GATEWAY] failed to append rejected intent: %s", exc)
