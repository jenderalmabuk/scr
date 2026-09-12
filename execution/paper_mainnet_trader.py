"""Paper Execution Engine (Mainnet-Priced)

Drop-in replacement for BinanceTestnetTrader in the gateway. Instead of routing
orders to Binance TESTNET (whose orderbook/mark price diverge wildly from the
real market and cause fake instant stop-outs), this fills and manages positions
against REAL mainnet prices (via Nexus FastAPI /klines/binance). No real orders
are placed — PnL is a faithful paper simulation of what mainnet would have done.

Contract compatibility with gateway/service.py + run_gateway.py:
- async submit_open(timeout_sec=..., **payload) -> dict|None   (truthy == opened)
- async start()  / async stop()                                (lifecycle loops)

payload keys (same as testnet trader): symbol, side, entry_price, sl / sl_price,
tp1, tp3 / tp_full, notional / size_usd, leverage, regime, adv_snapshot.

Paper fills include configurable adverse slippage and taker fees. This remains a
simulation; exchange-native protective orders are required before real money.
"""
from __future__ import annotations
import asyncio, json, logging, os
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, Optional
import aiohttp
from signal_copy.fresh_quote import fetch_fresh_quote
from execution.smart_scratch_exit import calculate_smart_scratch_timeout

# Trailing stop execution config
TRAILING_EXECUTION_ENABLED = os.getenv("TRAILING_EXECUTION_ENABLED", "false").lower() in ("1", "true", "yes")
TRAILING_MIN_HOLD_MINUTES = int(os.getenv("TRAILING_MIN_HOLD_MINUTES", "0"))

PAPER_STATE_PATH = Path(os.getenv("FQ_PAPER_POSITION_STATE", "runtime/fusion_quantum/journal/open_positions.json"))
PAPER_EQUITY_PATH = Path(os.getenv("FQ_PAPER_EQUITY_STATE", "runtime/fusion_quantum/journal/paper_equity.json"))
SHADOW_TRAILING_PATH = Path(os.getenv("SHADOW_TRAILING_JOURNAL", "journal/shadow_trailing.jsonl"))
DYNAMIC_SL_EXPERIMENT_EPOCH = "signalcopy_dynamic_sl_2026q3"
DYNAMIC_SL_CONFIG_VERSION = "dynamic_sl_shadow_v1"
DYNAMIC_SL_VARIANT_STATUS = {
    "current": "candidate", "regime_hybrid": "candidate",
    "frontload_40": "diagnostic_only", "frontload_50": "diagnostic_only",
    "mfe_giveback": "rejected", "economic_be": "rejected",
}


def persist_equity(equity: float, daily_start_balance: Optional[float] = None) -> None:
    """Keep realized paper equity and current UTC-day baseline across restarts."""
    PAPER_EQUITY_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"equity": float(equity)}
    if daily_start_balance is not None:
        payload["daily_start_balance"] = float(daily_start_balance)
    tmp = PAPER_EQUITY_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, PAPER_EQUITY_PATH)


def load_equity_state() -> Optional[Dict[str, float]]:
    try:
        raw = json.loads(PAPER_EQUITY_PATH.read_text())
        state = {"equity": float(raw["equity"])}
        if raw.get("daily_start_balance") is not None:
            state["daily_start_balance"] = float(raw["daily_start_balance"])
        return state
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def load_equity():
    state = load_equity_state()
    return state["equity"] if state else None

def _jsonable(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def _persist_positions(positions):
    PAPER_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PAPER_STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(positions, default=_jsonable, separators=(",", ":")))
    os.replace(tmp, PAPER_STATE_PATH)


_LEGACY_SHADOW_TRAILING_FIELDS = {
    "shadow_trailing_active": "active",
    "shadow_activated_at": "activated_at",
    "shadow_high_watermark": "high_watermark",
    "shadow_low_watermark": "low_watermark",
    "shadow_stop": "stop",
    "shadow_exit_price": "exit_price",
    "shadow_exit_at": "exit_at",
    "shadow_remaining_net_pnl": "remaining_net_pnl",
    "shadow_exit_logged": "exit_logged",
    "locked_profit": "locked_profit",
}


def _migrate_shadow_trailing(pos: Dict[str, Any]) -> None:
    """Move legacy shadow-only roots into namespace; provider lifecycle stays immutable."""
    shadow = pos.get("shadow_trailing")
    legacy_present = any(key in pos for key in _LEGACY_SHADOW_TRAILING_FIELDS)
    if not legacy_present:
        return
    if not isinstance(shadow, dict):
        shadow = {}
        pos["shadow_trailing"] = shadow
    for old, new in _LEGACY_SHADOW_TRAILING_FIELDS.items():
        if old in pos:
            shadow.setdefault(new, pos.pop(old))


def _load_positions():
    def restore(value, key=""):
        if isinstance(value, dict):
            return {name: restore(item, name) for name, item in value.items()}
        if isinstance(value, list):
            return [restore(item) for item in value]
        if isinstance(value, str) and (key.endswith("_at") or key == "timestamp"):
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                pass
        return value
    try:
        positions = restore(json.loads(PAPER_STATE_PATH.read_text()))
        for pos in positions.values():
            _migrate_shadow_trailing(pos)
        return positions
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return {}


def _append_shadow_result(row: Dict[str, Any]) -> None:
    """Append one immutable baseline-vs-shadow result at actual position close."""
    SHADOW_TRAILING_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SHADOW_TRAILING_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, default=_jsonable, separators=(",", ":")) + "\n")

UTC = timezone.utc
logger = logging.getLogger("gateway.paper")

# ── Dynamic exit layer (ported from FusionXomegabot binance_testnet_trader) ──
# Defaults mirror signal_copy/execution_config.py; read fresh each call so env
# changes and test patch.dict overrides take effect without module reload.
def _dyn_env_bool(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).lower() in ("1", "true", "yes")


def _dyn_env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return float(default)


def _dyn_env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return int(default)


class PaperMainnetTrader:
    """Paper trader using mainnet prices for accurate edge validation."""
    
    # Max age thresholds (hours)
    MAX_POSITION_AGE = {
        'pre_tp1': 72,   # 3 days - dead signal threshold
        'post_tp1': 168, # 7 days - runner exhausted threshold
    }

    def __init__(self, nexus_api: str = "http://fastapi:8000", poll_interval: float = 3.0):
        self.nexus_api = nexus_api.rstrip("/")
        self.poll_interval = float(poll_interval)
        self.positions: Dict[str, Dict[str, Any]] = _load_positions()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._journal = None
        self.risk_mgr: Any = None  # set by run_gateway wiring; enables realized-PnL equity sync

    # ── lifecycle ────────────────────────────────────────────────
    async def start(self):
        if self._running:
            return
        from execution.trade_journal import TradeJournalWriter
        self._running = True
        self._session = aiohttp.ClientSession()
        self._journal = TradeJournalWriter()
        await self._journal.start()
        self._task = asyncio.create_task(self._management_loop(), name="paper_mgmt")
        logger.info("[PAPER] started — mainnet-priced fills (poll=%.1fs)", self.poll_interval)

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._journal:
            await self._journal.shutdown()
        if self._session:
            await self._session.close()
        logger.info("[PAPER] stopped")

    # ── helpers ──────────────────────────────────────────────────
    async def _get_mark_price(self, symbol: str) -> float:
        """Last 1m close from mainnet Binance (via Nexus FastAPI)."""
        if self._session is None:
            return 0.0
        try:
            url = f"{self.nexus_api}/klines/binance/{symbol}?tf=1m&limit=1"
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as r:
                if r.status == 200:
                    data = await r.json()
                    # Nexus returns {"exchange":"binance","symbol":"...","data":[...]}
                    if isinstance(data, dict) and "data" in data:
                        bars = data["data"]
                        if isinstance(bars, list) and bars:
                            return float(bars[-1]["close"])
                    # fallback: old format (direct list)
                    elif isinstance(data, list) and data:
                        return float(data[-1]["close"])
        except Exception as e:
            logger.warning("[PAPER] mark_price fetch fail %s: %s", symbol, e)
        return 0.0

    async def _get_fresh_open_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Fresh Binance Futures quote with receipt-time provenance."""
        if self._session is not None:
            try:
                url = "https://fapi.binance.com/fapi/v1/ticker/price"
                async with self._session.get(url, params={"symbol": symbol},
                                             timeout=aiohttp.ClientTimeout(total=3)) as response:
                    if response.status == 200:
                        price = float((await response.json()).get("price", 0) or 0)
                        if price > 0:
                            return {"price": price, "price_source": "binance_futures_rest",
                                    "price_observed_at": datetime.now(timezone.utc).isoformat(),
                                    "price_age_sec": 0.0, "price_fresh": True}
            except Exception as exc:
                logger.warning("[PAPER] fresh open price fetch fail %s: %s", symbol, exc)
        return None

    async def _get_fresh_open_price(self, symbol: str) -> tuple[float, str]:
        quote = await self._get_fresh_open_quote(symbol)
        return ((float(quote["price"]), str(quote["price_source"]))
                if quote else (0.0, "fresh_price_unavailable"))

    @staticmethod
    def _lifecycle_price(quote: Dict[str, Any]) -> Optional[float]:
        """Return only fresh multi-exchange lifecycle truth."""
        if not isinstance(quote, dict) or quote.get("price_fresh") is not True:
            return None
        price = float(quote.get("price") or 0)
        return price if price > 0 else None

    @staticmethod
    def _level_hit(pos: Dict[str, Any], price: float, level: float, *, favorable: bool) -> bool:
        side = str(pos.get("side", "")).upper()
        return ((price >= level) if side == "LONG" else (price <= level)) if favorable else \
               ((price <= level) if side == "LONG" else (price >= level))

    @classmethod
    def _confirmed_sl_price(cls, pos: Dict[str, Any], candidate: float,
                            quote: Dict[str, Any]) -> Optional[float]:
        """Confirm SL against fresh exchange truth; candidate retained for audit only."""
        price = cls._lifecycle_price(quote)
        sl = float(pos.get("sl_price") or 0)
        return price if price is not None and sl > 0 and cls._level_hit(pos, price, sl, favorable=False) else None

    async def _get_completed_bars(self, symbol: str, *, tf: str = "1m", limit: int = 6) -> list:
        if self._session is None:
            return []
        try:
            url = f"{self.nexus_api}/klines/binance/{symbol}?tf={tf}&limit={limit}"
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as response:
                data = await response.json() if response.status == 200 else {}
                return data.get("data", []) if isinstance(data, dict) else data
        except Exception:
            return []

    @staticmethod
    def _score_completed_bars(bars: list, side: str, *, entry: float, sl: float,
                              mfe_r: float = 0.0) -> Optional[Dict[str, Any]]:
        """Score independent reversal evidence from closed 1m bars only."""
        if len(bars) < 5:
            return None
        rows = bars[-5:-1]  # latest API row is still-forming 1m candle
        try:
            closes = [float(row["close"]) for row in rows]
            volumes = [float(row.get("volume", 0)) for row in rows]
            stamp = rows[-1].get("open_time") or rows[-1].get("timestamp") or rows[-1].get("time")
            opened = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            closed = opened + timedelta(seconds=60)
        except (KeyError, TypeError, ValueError):
            return None
        adverse = (closes[-1] < closes[-2] < closes[-3] if side == "LONG"
                   else closes[-1] > closes[-2] > closes[-3])
        categories = []
        if adverse:
            categories.extend(("structure", "momentum"))
        if volumes[-1] > max(volumes[:-1] or [0]) and adverse:
            categories.append("volume")
        risk = abs(float(entry) - float(sl)) or abs(float(entry)) * 0.01
        profit_r = ((closes[-1] - entry) if side == "LONG" else (entry - closes[-1])) / risk
        if float(mfe_r) - profit_r >= 0.5:
            categories.append("progress")
        return {"completed": True, "closed_at": closed.isoformat(), "timeframe_seconds": 60,
                "conflict_categories": len(categories), "categories": categories,
                "profit_r": profit_r}

    @staticmethod
    def _adverse_fill(price: float, side: str, *, opening: bool) -> float:
        slip = max(0.0, float(os.getenv("PAPER_SLIPPAGE_PCT", "0.0005")))
        buy = (side == "LONG" and opening) or (side == "SHORT" and not opening)
        return float(price) * (1.0 + slip if buy else 1.0 - slip)

    @staticmethod
    def _fee(notional: float) -> float:
        rate = max(0.0, float(os.getenv("PAPER_TAKER_FEE_PCT", "0.0005")))
        return abs(float(notional)) * rate

    def _apply_equity_delta(self, delta: float) -> Optional[float]:
        if self.risk_mgr is None or not hasattr(self.risk_mgr, "sync_balance"):
            return None
        equity_after = float(self.risk_mgr.get_current_equity()) + float(delta)
        self.risk_mgr.sync_balance(equity_after)
        persist_equity(equity_after, getattr(self.risk_mgr, "daily_start_balance", None))
        return equity_after

    @staticmethod
    def _shadow_enabled() -> bool:
        return os.getenv("SHADOW_TRAILING_ENABLED", "true").lower() in ("1", "true", "yes")

    def _shadow_trail_pct(self, pos: Dict[str, Any]) -> float:
        fixed = max(0.0, float(os.getenv("SHADOW_TRAIL_MIN_PCT", "0.01")))
        atr = float((pos.get("adv_snapshot") or {}).get("atr_pct", 0) or 0) / 100.0
        atr_mult = max(0.0, float(os.getenv("SHADOW_TRAIL_ATR_MULTIPLIER", "1.0")))
        return max(fixed, atr * atr_mult)

    @staticmethod
    def _build_smart_exit_plan(pos: Dict[str, Any], bars: list, *, equity: float,
                               risk_pct: float = 0.01,
                               structure_mode: str = "FIVE_BAR",
                               opened_at: Any = None) -> Dict[str, Any]:
        """Build independent 15m structure+ATR shadow plan from closed candles only."""
        if len(bars) < 15:
            return {"candidate_available": False,
                    "reason": "INSUFFICIENT_COMPLETED_15M_BARS"}
        closed = bars[:-1]  # Nexus latest row may still be forming.
        try:
            rows = [{key: float(row[key]) for key in ("high", "low", "close")}
                    for row in closed]
            entry = float(pos["entry_price"])
            side = str(pos["side"]).upper()
        except (KeyError, TypeError, ValueError):
            return {"candidate_available": False, "reason": "INVALID_15M_BARS"}
        if side not in ("LONG", "SHORT") or entry <= 0:
            return {"candidate_available": False, "reason": "INVALID_POSITION"}
        true_ranges = []
        for index, row in enumerate(rows):
            previous = rows[index - 1]["close"] if index else row["close"]
            true_ranges.append(max(row["high"] - row["low"],
                                   abs(row["high"] - previous),
                                   abs(row["low"] - previous)))
        atr = sum(true_ranges[-14:]) / 14.0
        if atr <= 0:
            return {"candidate_available": False, "reason": "INVALID_ATR"}
        buffer_mult = max(0.0, float(os.getenv("SMART_SHADOW_SL_ATR_MULT", "0.5")))
        lookback = max(2, int(os.getenv("SMART_SHADOW_SWING_LOOKBACK", "5")))
        structure_type, sweep_index, pivot_index = "FIVE_BAR", None, None
        if structure_mode == "LIQUIDITY_GRAB":
            kind = "low" if side == "LONG" else "high"
            pivots = []
            for index in range(2, len(rows) - 2):
                value = rows[index][kind]
                neighbours = [rows[j][kind] for j in range(index - 2, index + 3) if j != index]
                if (value < min(neighbours) if kind == "low" else value > max(neighbours)):
                    pivots.append(index)
            grab = None
            for sweep in range(14, len(rows)):
                sweep_atr = sum(true_ranges[sweep - 13:sweep + 1]) / 14.0
                for pivot in reversed([p for p in pivots if p + 2 < sweep]):
                    level, row = rows[pivot][kind], rows[sweep]
                    reclaimed = (row["low"] <= level - 0.1 * sweep_atr and row["close"] > level
                                 if side == "LONG" else
                                 row["high"] >= level + 0.1 * sweep_atr and row["close"] < level)
                    if reclaimed:
                        grab = (sweep, pivot, sweep_atr)
                        break
                if grab and grab[0] == sweep:
                    continue
            if grab is None:
                return {"candidate_available": False, "reason": "NO_CONFIRMED_LIQUIDITY_GRAB"}
            sweep_index, pivot_index, atr = grab
            structure_type = "LIQUIDITY_GRAB"
            sl = (min(rows[sweep_index]["low"], rows[pivot_index]["low"]) - atr * buffer_mult
                  if side == "LONG" else
                  max(rows[sweep_index]["high"], rows[pivot_index]["high"]) + atr * buffer_mult)
        else:
            structure = rows[-lookback:]
            sl = (min(row["low"] for row in structure) - atr * buffer_mult if side == "LONG"
                  else max(row["high"] for row in structure) + atr * buffer_mult)
        if sl <= 0 or (side == "LONG" and sl >= entry) or (side == "SHORT" and sl <= entry):
            return {"candidate_available": False, "reason": "STRUCTURE_SL_WRONG_SIDE"}
        risk_per_unit = abs(entry - sl)
        budget = max(0.0, float(equity) * max(0.0, float(risk_pct)))
        fee_rate = max(0.0, float(os.getenv("PAPER_TAKER_FEE_PCT", "0.0005")))
        slip = max(0.0, float(os.getenv("PAPER_SLIPPAGE_PCT", "0.0005")))
        adverse_sl = sl * (1.0 - slip if side == "LONG" else 1.0 + slip)
        economic_risk_per_unit = abs(entry - adverse_sl) + entry * fee_rate + adverse_sl * fee_rate
        notional_cap = max(0.0, float(equity) *
                           max(0.0, float(os.getenv("MAX_TOTAL_EXPOSURE_PCT", "200"))) / 100.0)
        qty = min(budget / economic_risk_per_unit, notional_cap / entry)
        if qty <= 0:
            return {"candidate_available": False, "reason": "INVALID_SMART_RISK_SIZE"}
        tp1 = entry + risk_per_unit if side == "LONG" else entry - risk_per_unit
        return {"candidate_available": True, "status": "OPEN", "timeframe": "15m",
                "atr_period": 14, "atr": atr, "sl_atr_mult": buffer_mult,
                "swing_lookback": lookback, "sl_price": sl, "active_stop": sl,
                "risk_per_unit": risk_per_unit, "risk_budget_usd": budget,
                "economic_risk_usd": economic_risk_per_unit * qty,
                "qty": qty, "notional_usd": qty * entry,
                "notional_cap_usd": notional_cap, "entry_price": entry, "side": side,
                "tp1_price": tp1, "tp1_r": 1.0, "tp1_hit": False,
                "trail_atr_mult": max(0.0, float(os.getenv("SMART_SHADOW_TRAIL_ATR_MULT", "1.5"))),
                "high_watermark": entry, "low_watermark": entry,
                "last_candle_id": closed[-1].get("open_time") or closed[-1].get("timestamp") or closed[-1].get("time"),
                "opened_at": (opened_at.isoformat() if isinstance(opened_at, datetime) else opened_at),
                "exit_reason": None, "exit_price": None, "observations": [],
                "structure_type": structure_type, "sweep_index": sweep_index,
                "pivot_index": pivot_index,
                "config_version": ("smart_exit_liquidity_grab_v1"
                                   if structure_type == "LIQUIDITY_GRAB" else
                                   "smart_exit_15m_atr_v1")}

    @staticmethod
    def _build_liquidity_grab_exit_plan(pos: Dict[str, Any], bars: list, *, equity: float,
                                        risk_pct: float = 0.01, opened_at: Any = None) -> Dict[str, Any]:
        return PaperMainnetTrader._build_smart_exit_plan(
            pos, bars, equity=equity, risk_pct=risk_pct,
            structure_mode="LIQUIDITY_GRAB", opened_at=opened_at)

    @staticmethod
    def _build_smart_exit_router_plan(pos: Dict[str, Any], bars: list, *, equity: float,
                                      risk_pct: float = 0.01,
                                      opened_at: Any = None) -> Dict[str, Any]:
        """Freeze deterministic entry-time routing; never inspect future outcome."""
        features = deepcopy(pos.get("adv_snapshot") or {})
        liquidity = PaperMainnetTrader._build_liquidity_grab_exit_plan(
            pos, bars, equity=equity, risk_pct=risk_pct, opened_at=opened_at)
        side = str(pos.get("side", "")).upper()
        regime = str(features.get("regime_label") or features.get("regime") or "").upper()
        mtf = features.get("mtf_alignment")
        mtf_score = float(mtf.get("score", 0) or 0) if isinstance(mtf, dict) else 0.0
        mtf_trend = str(mtf.get("entry_trend", "")).upper() if isinstance(mtf, dict) else str(mtf).upper()
        aligned = ((side == "LONG" and mtf_trend in ("UP", "BULLISH")) or
                   (side == "SHORT" and mtf_trend in ("DOWN", "BEARISH")))
        flow = str(features.get("flow_direction") or features.get("cvd_trend") or "").upper()
        flow_aligned = side in flow or (side == "LONG" and flow == "BULLISH") or \
            (side == "SHORT" and flow == "BEARISH")
        trend_score = (2 if regime == "TRENDING" else 0) + \
            (2 if aligned or mtf_score >= 70 else 0) + (1 if flow_aligned else 0)
        scores = {"LIQUIDITY_GRAB": 10 if liquidity.get("candidate_available") else 0,
                  "WIDE_TREND": trend_score, "DEFENSIVE_ATR": 0}
        if liquidity.get("candidate_available"):
            plan, family, selected = liquidity, "LIQUIDITY_GRAB", "smart_exit_liquidity_grab_v1"
        elif trend_score >= 5:
            plan = PaperMainnetTrader._build_smart_exit_plan(
                pos, bars, equity=equity, risk_pct=risk_pct, opened_at=opened_at)
            if not plan.get("candidate_available"):
                return {"candidate_available": False, "reason": plan.get("reason"),
                        "feature_snapshot": features, "scores": scores,
                        "config_version": "smart_exit_router_v1"}
            plan["tp1_r"] = 1.5
            risk = float(plan["risk_per_unit"])
            plan["tp1_price"] = (float(plan["entry_price"]) + 1.5 * risk if side == "LONG"
                                 else float(plan["entry_price"]) - 1.5 * risk)
            plan["trail_atr_mult"] = 2.0
            family, selected = "WIDE_TREND", "smart_exit_wide_trend_v1"
        else:
            return {"candidate_available": False,
                    "reason": "INSUFFICIENT_ROUTER_EVIDENCE",
                    "feature_snapshot": features, "scores": scores,
                    "config_version": "smart_exit_router_v1"}
        plan.update(setup_family=family, feature_snapshot=features, scores=scores,
                    selected_plan=selected, config_version="smart_exit_router_v1")
        return plan

    @staticmethod
    def _advance_smart_exit_plan(plan: Dict[str, Any], candle: Dict[str, Any]) -> None:
        """Advance smart shadow with pessimistic stop-first same-bar ordering."""
        if not plan.get("candidate_available") or plan.get("status") != "OPEN":
            return
        try:
            high, low, close = (float(candle[key]) for key in ("high", "low", "close"))
        except (KeyError, TypeError, ValueError):
            return
        side, stop = plan["side"], float(plan["active_stop"])
        stop_hit = low <= stop if side == "LONG" else high >= stop
        if stop_hit:
            plan.update(status="CLOSED", exit_reason="SMART_TRAIL" if plan["tp1_hit"] else "SMART_SL",
                        exit_price=stop, exited_at=candle.get("closed_at"))
        else:
            tp1 = float(plan["tp1_price"])
            if not plan["tp1_hit"] and (high >= tp1 if side == "LONG" else low <= tp1):
                plan["tp1_hit"] = True
                plan["tp1_hit_at"] = candle.get("closed_at")
            plan["high_watermark"] = max(float(plan["high_watermark"]), high)
            plan["low_watermark"] = min(float(plan["low_watermark"]), low)
            if plan["tp1_hit"]:
                distance = float(plan["atr"]) * float(plan["trail_atr_mult"])
                candidate = (float(plan["high_watermark"]) - distance if side == "LONG"
                             else float(plan["low_watermark"]) + distance)
                plan["active_stop"] = (max(stop, candidate) if side == "LONG"
                                       else min(stop, candidate))
        plan["observations"].append({"closed_at": candle.get("closed_at"),
                                     "high": high, "low": low, "close": close,
                                     "active_stop": plan["active_stop"],
                                     "tp1_hit": plan["tp1_hit"], "status": plan["status"]})

    @classmethod
    def _update_smart_exit_shadow(cls, pos: Dict[str, Any], bars: list,
                                  key: str = "smart_exit_shadow") -> None:
        plan = pos.get(key)
        if not isinstance(plan, dict) or not plan.get("candidate_available") or len(bars) < 2:
            return
        row = bars[-2]  # latest completed 15m candle; final row may still form.
        stamp = row.get("open_time") or row.get("timestamp") or row.get("time")
        if stamp is None or stamp == plan.get("last_candle_id"):
            return
        try:
            opened = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            candle = {key: float(row[key]) for key in ("open", "high", "low", "close")}
        except (KeyError, TypeError, ValueError):
            return
        candle["closed_at"] = (opened + timedelta(minutes=15)).isoformat()
        plan_opened = plan.get("opened_at")
        if plan_opened:
            try:
                if datetime.fromisoformat(str(candle["closed_at"]).replace("Z", "+00:00")) <= \
                        datetime.fromisoformat(str(plan_opened).replace("Z", "+00:00")):
                    plan["last_candle_id"] = stamp
                    return
            except (TypeError, ValueError):
                return
        plan["last_candle_id"] = stamp
        cls._advance_smart_exit_plan(plan, candle)

    @classmethod
    def _update_smart_exit_shadows(cls, pos: Dict[str, Any], bars: list) -> None:
        cls._update_smart_exit_shadow(pos, bars, "smart_exit_shadow")
        cls._update_smart_exit_shadow(pos, bars, "smart_exit_liquidity_shadow")
        cls._update_smart_exit_shadow(pos, bars, "smart_exit_router_shadow")

    @classmethod
    def _finalize_smart_exit_shadow(cls, pos: Dict[str, Any], *, provider_exit: float,
                                    key: str = "smart_exit_shadow") -> Dict[str, Any]:
        plan = pos.get(key) or {}
        if not plan.get("candidate_available"):
            return {"candidate_available": False, "reason": plan.get("reason", "UNAVAILABLE")}
        trigger = float(plan.get("exit_price") or provider_exit)
        side, qty, entry = plan["side"], float(plan["qty"]), float(plan["entry_price"])
        fill = cls._adverse_fill(trigger, side, opening=False)
        gross = (fill - entry) * qty if side == "LONG" else (entry - fill) * qty
        entry_fee = cls._fee(entry * qty)
        exit_fee = cls._fee(fill * qty)
        return {"candidate_available": True, "triggered": plan.get("status") == "CLOSED",
                "exit_reason": plan.get("exit_reason") or "PROVIDER_TERMINAL",
                "trigger_price": trigger, "fill_price": fill, "qty": qty,
                "gross_pnl_usd": gross, "entry_fee_usd": entry_fee,
                "exit_fee_usd": exit_fee, "net_pnl_usd": gross - entry_fee - exit_fee,
                "tp1_hit": bool(plan.get("tp1_hit")),
                "config_version": plan.get("config_version")}
    
    def _normalize_exit_reason(self, raw_reason: str, sl_kind: str) -> str:
        """Normalize exit reason based on SL kind (FusionXomega pattern)."""
        if "SL" in raw_reason.upper() or "STOP" in raw_reason.upper():
            if sl_kind == "TRAILING":
                return "DYNAMIC_SL"
            elif sl_kind == "BREAKEVEN":
                return "BREAKEVEN_STOP"
            else:
                return "HARD_SL"
        return raw_reason

    def _record_stale_conflict_shadow(self, pos: Dict[str, Any], mark: float) -> bool:
        """Record stale/conflict candidate; never close or mutate provider lifecycle."""
        if os.getenv("STALE_SHADOW_ENABLED", "true").lower() not in ("1", "true", "yes"):
            return False
        if pos.get("tp1_hit") is True or pos.get("shadow_stale_candidate"):
            return False
        opened = pos.get("opened_at")
        if not isinstance(opened, datetime):
            return False
        age_h = (datetime.now(UTC) - opened).total_seconds() / 3600.0
        conflicts = int((pos.get("adv_snapshot") or {}).get("stale_conflict_categories", 0) or 0)
        if age_h < 18.0 or conflicts < 3:
            return False
        pos["shadow_stale_candidate"] = True
        pos["shadow_stale_mark"] = float(mark)
        pos["shadow_stale_at"] = datetime.now(UTC)
        logger.info("[SHADOW_STALE] WOULD_EXIT %s %s @ %.6g age=%.1fh conflicts=%d",
                    pos.get("symbol"), pos.get("side"), mark, age_h, conflicts)
        return True

    def _shadow_variant_snapshot(self, pos: Dict[str, Any], mark: float,
                                 conflict_count: int = 0) -> Dict[str, Any]:
        """Persist parallel counterfactual inputs; never mutate actual lifecycle."""
        entry, sl = float(pos["entry_price"]), float(pos["sl_price"])
        risk = abs(entry - sl) or entry * 0.01
        side = pos["side"]
        profit_r = ((mark - entry) if side == "LONG" else (entry - mark)) / risk
        peak = max(float(pos.get("shadow_mfe_r", profit_r)), profit_r)
        pos["shadow_mfe_r"] = peak
        giveback = peak - profit_r
        
        # NEW: Reversal risk size reduction for pilot channel
        bad_signal = pos.get("bad_signal_shadow") or {}
        reversal_risk = bad_signal.get("reversal_risk", False)
        source_chat_id = (pos.get("adv_snapshot") or {}).get("source_chat_id")
        pilot_channel = (source_chat_id == -1001652601224)
        
        variants = {
            "current": {"trail_atr_mult": 1.0},
            "conflict3": {"would_exit": conflict_count >= 3},
            "wide_continuation": {"trail_atr_mult": 2.0, "would_exit": conflict_count >= 3},
            "mfe_giveback": {"would_exit": conflict_count >= 3 and giveback >= 0.5,
                             "profit_r": profit_r, "mfe_r": peak, "giveback_r": giveback},
            "reversal_reduced_size": {
                "size_mult": 0.5 if (reversal_risk and pilot_channel) else 1.0,
                "reversal_risk": reversal_risk,
                "pilot_channel": pilot_channel,
                "enabled": reversal_risk and pilot_channel,
            },
        }
        pos["shadow_variants"] = variants
        return variants

    def _smart_sl_tp_snapshot(self, pos: Dict[str, Any], mark: float,
                              conflict_count: int = 0,
                              confirmed_bars: int = 0) -> Dict[str, Any]:
        """Record smart SL/TP counterfactuals; never mutate actual qty, SL, or TPs."""
        entry = float(pos.get("entry_price") or 0)
        sl = float(pos.get("sl_price") or 0)
        risk = abs(entry - sl)
        side = str(pos.get("side", "LONG")).upper()
        profit_r = ((mark - entry) if side == "LONG" else (entry - mark)) / max(risk, 1e-12)
        pre_tp1 = pos.get("tp1_hit") is not True
        conflict_exit = pre_tp1 and profit_r <= -0.5 and conflict_count >= 3 and confirmed_bars >= 2
        result = {
            "pre_tp1_conflict": {"would_exit": conflict_exit, "profit_r": profit_r,
                                 "conflict_count": conflict_count, "confirmed_bars": confirmed_bars},
            "frontload_40": {"tp_allocations": [0.40, 0.25, 0.35],
                "experiment_epoch": DYNAMIC_SL_EXPERIMENT_EPOCH,
                "config_version": DYNAMIC_SL_CONFIG_VERSION,
                "variant_status": DYNAMIC_SL_VARIANT_STATUS["frontload_40"]},
            "frontload_50": {"tp_allocations": [0.50, 0.25, 0.25],
                "experiment_epoch": DYNAMIC_SL_EXPERIMENT_EPOCH,
                "config_version": DYNAMIC_SL_CONFIG_VERSION,
                "variant_status": DYNAMIC_SL_VARIANT_STATUS["frontload_50"]},
            "hybrid": {"would_exit": conflict_exit, "pre_tp1_profit_r": profit_r,
                       "post_tp1_giveback_r": 0.5, "continuation_atr_mult": 2.0},
        }
        pos["smart_sl_tp_shadow"] = result
        return result

    @staticmethod
    def _fresh_conflicts(candle: Optional[Dict[str, Any]]) -> Optional[int]:
        """Use only fresh, explicitly completed runtime candle evidence."""
        if not isinstance(candle, dict) or candle.get("completed") is not True:
            return None
        try:
            closed = datetime.fromisoformat(str(candle["closed_at"]).replace("Z", "+00:00"))
            age = (datetime.now(UTC) - closed.astimezone(UTC)).total_seconds()
            max_age = max(1.0, float(candle["timeframe_seconds"])) * 2.0
            return int(candle["conflict_categories"]) if 0 <= age <= max_age else None
        except (KeyError, TypeError, ValueError):
            return None

    def _shadow_fill(self, pos: Dict[str, Any], trigger: float, qty: float) -> Dict[str, float]:
        fill = self._adverse_fill(trigger, pos["side"], opening=False)
        adverse = abs(fill - trigger) * qty
        fee = self._fee(fill * qty)
        gross = ((fill - pos["entry_price"]) * qty if pos["side"] == "LONG"
                 else (pos["entry_price"] - fill) * qty)
        return {"fill_price": fill, "adverse_slippage_usd": adverse,
                "fees_usd": fee, "remaining_qty_net_pnl": gross - fee}

    def _update_audit_shadows(self, pos: Dict[str, Any], mark: float,
                              completed_candle: Optional[Dict[str, Any]] = None) -> None:
        """Advance post-TP1 counterfactual exits without touching provider fields."""
        if pos.get("tp1_hit") is not True:
            return
        audit = pos.setdefault("shadow_audit", {})
        audit.update(experiment_epoch=DYNAMIC_SL_EXPERIMENT_EPOCH,
                     config_version=DYNAMIC_SL_CONFIG_VERSION)
        states = audit.setdefault("exit_variants", {})
        side, entry = pos["side"], float(pos["entry_price"])
        qty = float(pos.get("qty", 0))
        risk = abs(entry - float(pos.get("sl_price", entry))) or entry * 0.01
        profit_r = ((mark - entry) if side == "LONG" else (entry - mark)) / risk
        conflicts = self._fresh_conflicts(completed_candle)
        if conflicts is None:
            pos.pop("shadow_conflict_candle", None)
            pos.pop("shadow_conflict_last_candle", None)
            pos["shadow_conflict_confirmed_bars"] = 0
        now = datetime.now(UTC)
        for name in ("current", "conflict3", "wide_continuation", "mfe_giveback", "economic_be", "regime_hybrid"):
            state = states.setdefault(name, {"high_watermark": mark, "low_watermark": mark,
                "hypothetical_stop": None, "triggered_at": None, "trigger_price": None,
                "fill_price": None, "adverse_slippage_usd": 0.0, "fees_usd": 0.0,
                "remaining_qty_net_pnl": None, "candidate_available": name != "conflict3",
                "experiment_epoch": DYNAMIC_SL_EXPERIMENT_EPOCH,
                "config_version": DYNAMIC_SL_CONFIG_VERSION,
                "variant_status": DYNAMIC_SL_VARIANT_STATUS.get(name, "diagnostic_only")})
            if state["triggered_at"] is not None:
                continue
            state["high_watermark"] = max(float(state["high_watermark"]), mark)
            state["low_watermark"] = min(float(state["low_watermark"]), mark)
            peak_r = max(float(state.get("mfe_r", profit_r)), profit_r)
            state["mfe_r"] = peak_r
            trail = self._shadow_trail_pct(pos) * (2.0 if name == "wide_continuation" else 1.0)
            buffer_pct = float(os.getenv("SHADOW_ECONOMIC_BE_BUFFER_PCT", "0.002"))
            economic_be = entry * (1 + buffer_pct if side == "LONG" else 1 - buffer_pct)
            if name == "regime_hybrid":
                snapshot = pos.get("adv_snapshot") or {}
                regime = str(snapshot.get("regime_label") or "UNKNOWN").upper()
                rsi = snapshot.get("rsi")
                move = snapshot.get("price_change_15m_pct")
                available = rsi is not None and move is not None and regime != "UNKNOWN"
                directional_rsi = (float(rsi) if side == "LONG" else 100.0 - float(rsi)) if available else 0.0
                directional_move = (float(move) if side == "LONG" else -float(move)) if available else 0.0
                extended = available and (directional_rsi >= 70.0 or directional_move >= 5.0)
                defensive = available and (regime == "RANGING" or extended or (conflicts is not None and conflicts >= 3))
                healthy_trend = available and regime == "TRENDING" and not defensive
                state["candidate_available"] = available
                state["setup_family"] = ("EXTENDED_HIGH_VOL" if extended and regime == "HIGH_VOL"
                                         else "RANGE_MEAN_REVERSION" if regime == "RANGING"
                                         else "TREND_CONTINUATION" if healthy_trend
                                         else "DEFENSIVE" if defensive else "UNKNOWN")
                state["route"] = "ECONOMIC_BE" if defensive else "WIDE_CONTINUATION" if healthy_trend else "PROVIDER_FALLBACK"
                trail *= 2.0 if healthy_trend else 1.0
                stop = economic_be if defensive else ((state["high_watermark"] * (1 - trail) if side == "LONG"
                                                       else state["low_watermark"] * (1 + trail)) if healthy_trend else None)
            elif name == "economic_be":
                stop = economic_be
            else:
                stop = (state["high_watermark"] * (1 - trail) if side == "LONG"
                        else state["low_watermark"] * (1 + trail))
            state["hypothetical_stop"] = stop
            trail_hit = stop is not None and ((side == "LONG" and mark <= stop) or (side == "SHORT" and mark >= stop))
            if name in ("economic_be", "regime_hybrid"):
                hit = trail_hit
            elif name == "conflict3":
                state["candidate_available"] = conflicts is not None
                if conflicts is None:
                    state.pop("conflict_categories", None)
                    state.pop("categories", None)
                else:
                    state["conflict_categories"] = conflicts
                    state["categories"] = list(completed_candle.get("categories") or [])
                hit = conflicts is not None and conflicts >= 3 and profit_r <= -0.5
            elif name == "mfe_giveback":
                hit = peak_r > 0 and peak_r - profit_r >= 0.5
            else:
                hit = pos.get("tp1_hit") is True and trail_hit
            if hit:
                trigger = mark if name == "conflict3" else stop
                state.update({"triggered_at": now, "trigger_price": trigger,
                              **self._shadow_fill(pos, trigger, qty)})

    def _record_shadow_tp(self, pos: Dict[str, Any], index: int, trigger: float) -> None:
        """Book hypothetical TP slices in independent allocation ledgers."""
        audit = pos.setdefault("shadow_audit", {})
        ledgers = audit.setdefault("tp_ledgers", {})
        count, initial = max(1, len(pos.get("tp_ladder") or [])), float(pos.get("initial_qty", 0))
        allocations = {"provider": [1.0 / count] * count,
                       "frontload_40": [0.40, 0.25, 0.35],
                       "frontload_50": [0.50, 0.25, 0.25]}
        for name, weights in allocations.items():
            ledger = ledgers.setdefault(name, {"remaining_qty": initial,
                "realized_net_pnl": 0.0, "fills": [],
                "experiment_epoch": DYNAMIC_SL_EXPERIMENT_EPOCH,
                "config_version": DYNAMIC_SL_CONFIG_VERSION,
                "variant_status": DYNAMIC_SL_VARIANT_STATUS.get(name, "diagnostic_only")})
            if index >= len(weights) or any(fill["tp_index"] == index for fill in ledger["fills"]):
                continue
            qty = min(ledger["remaining_qty"], initial * weights[index])
            result = self._shadow_fill(pos, trigger, qty)
            ledger["remaining_qty"] -= qty
            ledger["realized_net_pnl"] += result["remaining_qty_net_pnl"]
            ledger["fills"].append({"tp_index": index, "trigger_price": trigger, "qty": qty,
                                    "timestamp": datetime.now(UTC), **result})

    def _update_shadow_trailing(self, pos: Dict[str, Any], mark: float) -> None:
        """Shadow trailing: journaling + optional live execution (FusionXomega pattern)."""
        _migrate_shadow_trailing(pos)
        shadow = pos.setdefault("shadow_trailing", {})
        if not self._shadow_enabled() or shadow.get("exit_price"):
            return
        # Require explicit TP1 event. Never infer eligibility from restored
        # legacy next_tp_index because that contaminated prior shadow results.
        if pos.get("tp1_hit") is not True:
            return
        
        side = pos["side"]
        trail = self._shadow_trail_pct(pos)
        
        # Calculate hold time
        now = datetime.now(UTC)
        opened_at = pos.get("opened_at", now)
        hold_minutes = (now - opened_at).total_seconds() / 60.0
        
        # Keep counterfactual state namespaced so shadow-only mode cannot be
        # mistaken for canonical execution state by position consumers.
        if not shadow.get("locked_profit"):
            shadow["locked_profit"] = True

        if not shadow.get("active"):
            shadow.update(active=True, activated_at=datetime.now(UTC),
                          high_watermark=mark, low_watermark=mark)
        shadow["high_watermark"] = max(float(shadow.get("high_watermark", mark)), mark)
        shadow["low_watermark"] = min(float(shadow.get("low_watermark", mark)), mark)

        if side == "LONG":
            candidate = shadow["high_watermark"] * (1.0 - trail)
            shadow["stop"] = max(float(shadow.get("stop", 0) or 0), candidate)
            hit = mark <= shadow["stop"]
        else:
            candidate = shadow["low_watermark"] * (1.0 + trail)
            old = float(shadow.get("stop", 0) or 0)
            shadow["stop"] = min(old, candidate) if old > 0 else candidate
            hit = mark >= shadow["stop"]
        
        # Shadow journaling (always track counterfactual)
        if hit and not shadow.get("exit_logged"):
            actual_exit = self._adverse_fill(shadow["stop"], side, opening=False)
            qty = float(pos.get("qty", 0))
            gross = ((actual_exit - pos["entry_price"]) * qty if side == "LONG"
                     else (pos["entry_price"] - actual_exit) * qty)
            fee = self._fee(actual_exit * qty)
            shadow.update(exit_price=actual_exit, exit_at=datetime.now(UTC),
                          remaining_net_pnl=gross - fee, exit_logged=True)
            logger.info("[SHADOW_TRAIL] WOULD_EXIT %s %s @ %.6g stop=%.6g qty=%.8g",
                        pos["symbol"], side, actual_exit, shadow["stop"], qty)
        
        # SignalCopy experiments are permanently shadow-only. Provider SL/TP
        # remain canonical even if a stale environment enables live trailing.

    @staticmethod
    def _classify_bad_signal(side: str, fill: float, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """Deterministic entry-quality research labels; missing data is unavailable."""
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        categories, unavailable = [], []
        rsi, move = snapshot.get("rsi"), snapshot.get("price_change_15m_pct")
        if rsi is None:
            unavailable.append("RSI_UNAVAILABLE")
        if move is None:
            unavailable.append("PRICE_CHANGE_15M_UNAVAILABLE")
        if rsi is not None and move is not None and (
              (side == "LONG" and (float(rsi) >= 70 or float(move) >= 5)) or
              (side == "SHORT" and (float(rsi) <= 30 or float(move) <= -5))):
            # SMART EXEMPTIONS: Allow breakouts/momentum
            # Exempt if: large move (>10%) OR volume spike (>1.5σ) OR MTF aligned
            vol_z = snapshot.get("volume_zscore_15m")
            mtf_raw = snapshot.get("mtf_alignment", "")
            # Handle dict or string: extract label if dict, use as-is if string
            mtf = mtf_raw.get("label", "") if isinstance(mtf_raw, dict) else str(mtf_raw)
            mtf = mtf.upper() if mtf else ""
            abs_move = abs(float(move) if move else 0)
            
            # Check exemptions
            is_large_move = abs_move > 10  # Big breakout (e.g. BLESSUSDT +40%)
            is_volume_spike = vol_z and float(vol_z) > 1.5  # Strong volume
            is_mtf_aligned = (side == "LONG" and mtf in ["BULLISH", "TRENDING"]) or \
                           (side == "SHORT" and mtf in ["BEARISH", "TRENDING_DOWN"])
            
            # Only block if NO exemptions apply
            if not (is_large_move or is_volume_spike or is_mtf_aligned):
                categories.append("DIRECTIONAL_OVEREXTENSION")
        low = snapshot.get("entry_low", snapshot.get("signal_entry_low"))
        high = snapshot.get("entry_high", snapshot.get("signal_entry_high"))
        if low is None or high is None:
            unavailable.append("provider_entry_zone")
        elif not min(float(low), float(high)) <= fill <= max(float(low), float(high)):
            categories.append("ENTRY_OUTSIDE_PROVIDER_ZONE")
        flow = str(snapshot.get("flow_direction") or "").upper()
        if not flow:
            status = str(snapshot.get("flow_lookup_status") or "UNKNOWN")
            unavailable.append(f"FLOW_DIRECTION_UNAVAILABLE:{status}")
        elif (side == "LONG" and flow in ("SHORT", "SHORT_ONLY", "BEARISH")) or (side == "SHORT" and flow in ("LONG", "LONG_ONLY", "BULLISH")):
            categories.append("FLOW_CONFLICT")
        
        # NEW: Reversal risk detection
        reversal_risk = False
        if rsi is not None and move is not None:
            if side == "LONG":
                reversal_risk = (float(rsi) > 70 or float(move) > 5.0)
            else:
                reversal_risk = (float(rsi) < 30 or float(move) < -5.0)
        
        return {"classified_at": datetime.now(UTC), "categories": categories,
                "unavailable": unavailable, "evidence": {"side": side, "fill": fill,
                "rsi": rsi, "price_change_15m_pct": move, "entry_low": low,
                "entry_high": high, "flow_direction": flow or None},
                "reversal_risk": reversal_risk}

    @classmethod
    def _ensure_bad_signal_classification(cls, pos: Dict[str, Any]) -> None:
        snapshot = pos.get("adv_snapshot") or {}
        current = pos.get("bad_signal_shadow") or {}
        zone_was_unavailable = "provider_entry_zone" in current.get("unavailable", [])
        zone_is_available = snapshot.get("signal_entry_low") is not None and snapshot.get("signal_entry_high") is not None
        if not current or (zone_was_unavailable and zone_is_available):
            pos["bad_signal_shadow"] = cls._classify_bad_signal(
                str(pos.get("side", "LONG")), float(pos.get("entry_price") or 0), snapshot
            )

    @staticmethod
    def _classify_bad_signal_outcome(classification: Dict[str, Any], baseline_net_pnl: float,
                                     best_shadow_net_pnl: float) -> Dict[str, Any]:
        delta = float(best_shadow_net_pnl) - float(baseline_net_pnl)
        labelled = bool(classification.get("categories"))
        verdict = ("AVOIDED_LOSS" if labelled and baseline_net_pnl < 0 and delta > 0
                   else "SACRIFICED_WINNER" if labelled and baseline_net_pnl > 0 and delta < 0
                   else "NEUTRAL" if labelled else "UNCLASSIFIED")
        return {"verdict": verdict, "baseline_net_pnl": baseline_net_pnl,
                "best_shadow_net_pnl": best_shadow_net_pnl, "delta_usd": delta}

    # ── open ─────────────────────────────────────────────────────
    async def submit_open(self, timeout_sec: float = 30.0, **params) -> Optional[Dict[str, Any]]:
        del timeout_sec
        symbol = str(params.get("symbol", ""))
        side = str(params.get("side", "")).upper()
        sl_price = float(params.get("sl", params.get("sl_price", 0)) or 0)
        tp_ladder = []
        for i in range(1, 21):
            value = float(params.get(f"tp{i}", 0) or 0)
            if value:
                tp_ladder.append(value)
        if not tp_ladder and params.get("tp_full"):
            tp_ladder.append(float(params["tp_full"]))
        tp1 = tp_ladder[0] if tp_ladder else 0.0
        notional = float(params.get("notional", params.get("size_usd", 0)) or 0)
        leverage = int(params["leverage"]) if params.get("leverage") else 1
        regime = str(params.get("regime", "TRENDING"))

        if not symbol or side not in ("LONG", "SHORT"):
            logger.warning("[PAPER] invalid params symbol=%s side=%s", symbol, side)
            return None
        if symbol in self.positions:
            logger.info("[PAPER] %s already open — skip dup", symbol)
            return None

        requested_entry = float(params.get("entry_price", 0) or 0)
        quote = params.get("execution_price_quote")
        if not isinstance(quote, dict) or quote.get("price_fresh") is not True:
            return {"ok": False, "executed": False, "code": "PRICE_PROVENANCE_REQUIRED",
                    "reason": "PRICE_PROVENANCE_REQUIRED"}
        try:
            observed_at = datetime.fromisoformat(str(quote["price_observed_at"]).replace("Z", "+00:00"))
            age_sec = (datetime.now(timezone.utc) - observed_at.astimezone(timezone.utc)).total_seconds()
            mark = float(quote["price"])
            mark_source = str(quote["price_source"])
        except (KeyError, TypeError, ValueError):
            mark, age_sec, mark_source = 0.0, float("inf"), "invalid_quote"
        if mark <= 0 or age_sec > 30.0:
            return {"ok": False, "executed": False, "code": "STALE_GATEWAY_MARK",
                    "reason": f"STALE_GATEWAY_MARK: age={age_sec:.3f} source={mark_source}"}
        # SignalCopy already resolves a fresh live price. Reject a materially
        # divergent exchange mark instead of treating stale cache as market truth.
        if requested_entry > 0 and mark > 0 and abs(mark - requested_entry) / requested_entry > 0.05:
            logger.warning("[PAPER] %s stale mark %.6g vs fresh route %.6g — reject", symbol, mark, requested_entry)
            return {"ok": False, "executed": False, "code": "STALE_GATEWAY_MARK",
                    "reason": f"STALE_GATEWAY_MARK: gateway={mark:.8g} route={requested_entry:.8g} source={mark_source}",
                    "price_source": mark_source}
        if mark <= 0:
            logger.warning("[PAPER] no mainnet price for %s — reject", symbol)
            return {"ok": False, "executed": False, "code": "NO_MARKET_PRICE",
                    "reason": "NO_MARKET_PRICE"}
        raw_fill = requested_entry if str(params.get("tag", "")).startswith("fusion_quantum") and requested_entry > 0 else mark
        fill = self._adverse_fill(raw_fill, side, opening=True)

        # Limit already invalidated before gateway open: no synthetic perfect fill.
        if side == "LONG" and sl_price and mark <= sl_price:
            logger.warning("[PAPER] %s LONG mark %.6g <= SL %.6g — reject gap-through fill", symbol, mark, sl_price)
            return None
        if side == "SHORT" and sl_price and mark >= sl_price:
            logger.warning("[PAPER] %s SHORT mark %.6g >= SL %.6g — reject gap-through fill", symbol, mark, sl_price)
            return None
        tp_ladder = [tp for tp in tp_ladder
                     if (tp > mark if side == "LONG" else tp < mark)]
        if not tp_ladder:
            logger.warning("[PAPER] %s %s all provider targets already passed — reject stale fill",
                           symbol, side)
            return None
        tp1 = tp_ladder[0]

        # Sanity guard: reject if signal SL is on the wrong side of the fill
        # (protects against fake fills that would instant-stop).
        if side == "LONG" and sl_price >= fill:
            logger.warning("[PAPER] %s LONG SL %.6g >= fill %.6g — reject (instant-stop guard)", symbol, sl_price, fill)
            return None
        if side == "SHORT" and sl_price and sl_price <= fill:
            logger.warning("[PAPER] %s SHORT SL %.6g <= fill %.6g — reject (instant-stop guard)", symbol, sl_price, fill)
            return None


        # Final defense at actual fill. Gateway sizing may race price movement;
        # preserve provider SL and reduce quantity instead of widening stop.
        qty = notional / fill if fill > 0 else 0
        adverse_sl_fill = self._adverse_fill(sl_price, side, opening=False)
        stop_gross_loss = abs(fill - adverse_sl_fill) * qty
        max_loss_at_fill = stop_gross_loss + self._fee(fill * qty) + self._fee(adverse_sl_fill * qty)
        if self.risk_mgr is not None and hasattr(self.risk_mgr, "get_current_equity"):
            max_trade_loss = max(0.0, float(self.risk_mgr.get_current_equity()) * 0.01)
            if max_loss_at_fill > max_trade_loss and max_loss_at_fill > 0:
                scale = max_trade_loss / max_loss_at_fill
                notional *= scale
                qty = notional / fill
                max_loss_at_fill = (
                    abs(fill - adverse_sl_fill) * qty
                    + self._fee(fill * qty)
                    + self._fee(adverse_sl_fill * qty)
                )
        if qty <= 0 or max_loss_at_fill <= 0:
            return {"ok": False, "executed": False, "code": "INVALID_ACTUAL_FILL_RISK",
                    "reason": "INVALID_ACTUAL_FILL_RISK"}
        entry_fee = self._fee(fill * qty)
        # Observability: capture the signal-copy enrichment snapshot (metrics,
        # score, confidence) at open so the trade journal isn't blank at close.
        # This is the metrics dict the executor forwards as `adv_snapshot`
        # (price/cvd/oi/funding/rsi/vol + mtf/tv/vision). It does NOT contain
        # Nexus-scanner SMC structure fields — those stay UNKNOWN by design.
        _adv_snapshot = dict(params.get("adv_snapshot") or params.get("adv") or {})
        for key in ("entry_low", "entry_high"):
            if params.get(key) is not None and key not in _adv_snapshot:
                _adv_snapshot[key] = params[key]
        bad_signal_shadow = self._classify_bad_signal(side, fill, _adv_snapshot)
        smart_bars = await self._get_completed_bars(symbol, tf="15m", limit=21)
        smart_equity = (float(self.risk_mgr.get_current_equity())
                        if self.risk_mgr is not None and hasattr(self.risk_mgr, "get_current_equity")
                        else float(os.getenv("STARTING_BALANCE", "1000")))
        smart_seed = {"symbol": symbol, "side": side, "entry_price": fill,
                      "adv_snapshot": _adv_snapshot}
        opened_at = datetime.now(UTC)
        smart_plan = self._build_smart_exit_plan(
            smart_seed, smart_bars, equity=smart_equity,
            risk_pct=float(os.getenv("SIGNAL_COPY_RISK_PCT", "0.01")), opened_at=opened_at)
        smart_plan["experiment_epoch"] = datetime.now(UTC).isoformat()
        smart_plan["collection_mode"] = "shadow_only"
        liquidity_plan = self._build_liquidity_grab_exit_plan(
            smart_seed, smart_bars, equity=smart_equity,
            risk_pct=float(os.getenv("SIGNAL_COPY_RISK_PCT", "0.01")), opened_at=opened_at)
        liquidity_plan["experiment_epoch"] = datetime.now(UTC).isoformat()
        liquidity_plan["collection_mode"] = "shadow_only"
        router_plan = self._build_smart_exit_router_plan(
            smart_seed, smart_bars, equity=smart_equity,
            risk_pct=float(os.getenv("SIGNAL_COPY_RISK_PCT", "0.01")), opened_at=opened_at)
        router_plan["experiment_epoch"] = datetime.now(UTC).isoformat()
        router_plan["collection_mode"] = "shadow_only"
        
        self.positions[symbol] = {
            "symbol": symbol, "side": side, "entry_price": fill,
            "sl_price": sl_price, "provider_sl_original": sl_price,
            "tp1_price": tp1, "tp_ladder": tp_ladder,
            "next_tp_index": 0, "initial_qty": qty,
            "qty": qty, "notional": notional, "leverage": leverage, "regime": regime,
            "opened_at": opened_at, "status": "OPEN", "tp1_hit": False,
            "raw_entry_price": raw_fill, "entry_fee": entry_fee,
            "fill_ledger": [{"timestamp": datetime.now(UTC).isoformat(), "kind": "ENTRY",
                             "entry_price": fill, "qty": qty, "notional_usd": notional,
                             "fee_usd": entry_fee, "actual_fill_risk_usd": max_loss_at_fill}],
            "max_loss_usd_at_fill": max_loss_at_fill,
            "adv_snapshot": _adv_snapshot, "bad_signal_shadow": bad_signal_shadow,
            "smart_exit_shadow": smart_plan,
            "smart_exit_liquidity_shadow": liquidity_plan,
            "smart_exit_router_shadow": router_plan,
            "score": float(params.get("score", 0) or 0),
            "confidence": float(params.get("confidence", 0) or 0),
            "tag": str(params.get("tag", "")),
        }
        _persist_positions(self.positions)
        equity_after_entry = self._apply_equity_delta(-entry_fee)
        logger.info("[PAPER] OPEN %s %s @ %.6g (%s) | SL %.6g TP1 %.6g | $%.0f | fee %.4f",
                    side, symbol, fill, "limit" if raw_fill == requested_entry else "mainnet mark", sl_price, tp1, notional, entry_fee)
        return {"success": True, "ok": True, "symbol": symbol, "side": side,
                "entry_price": fill, "raw_entry_price": raw_fill, "qty": qty,
                "notional": notional, "entry_fee": entry_fee,
                "max_loss_usd_at_fill": max_loss_at_fill, "equity": equity_after_entry}

    # ── management ───────────────────────────────────────────────
    async def _management_loop(self):
        while self._running:
            try:
                await asyncio.sleep(self.poll_interval)
                for symbol in list(self.positions.keys()):
                    pos = self.positions.get(symbol)
                    if not pos or pos.get("status", "OPEN") != "OPEN":
                        continue
                    
                    quote = await fetch_fresh_quote(symbol)
                    mark = self._lifecycle_price(quote)
                    if mark is None:
                        logger.warning("[PAPER] %s lifecycle quote unavailable source=%s",
                                       symbol, quote.get("price_source"))
                        continue
                    pos["lifecycle_price_quote"] = quote

                    # Max-age, SL, TP, trailing, and notifications share this quote.
                    await self._check_aged_position(symbol, pos, mark)
                    pos = self.positions.get(symbol)
                    if not pos:
                        continue

                    # Dynamic exit layer (paper-only, env-gated). Runs BEFORE the
                    # provider SL check so defensive exits fire first — same order
                    # as FusionXomegabot. Returns True if it closed the position.
                    if await self._apply_dynamic_exits(symbol, pos, mark):
                        continue

                    sl = float(pos.get("sl_price") or 0)
                    if sl and self._level_hit(pos, mark, sl, favorable=False):
                        pos["sl_trigger_quote"] = quote
                        await self._close(symbol, mark, "HARD_SL")
                        continue
                    await self._apply_take_profits(symbol, mark)
                    pos = self.positions.get(symbol)
                    if pos:
                        self._ensure_bad_signal_classification(pos)
                        self._record_stale_conflict_shadow(pos, mark)
                        bars = await self._get_completed_bars(symbol)
                        now = datetime.now(UTC)
                        last_smart_fetch = pos.get("smart_exit_shadow_last_fetch")
                        if (not isinstance(last_smart_fetch, datetime) or
                                (now - last_smart_fetch).total_seconds() >= 60):
                            smart_bars = await self._get_completed_bars(symbol, tf="15m", limit=21)
                            if "smart_exit_shadow" not in pos:
                                # Existing positions lack an entry-time 15m snapshot; exclude them
                                # rather than backfilling a look-ahead-contaminated smart plan.
                                pos["smart_exit_shadow"] = {
                                    "candidate_available": False,
                                    "reason": "PRE_EPOCH_POSITION_NO_ENTRY_SNAPSHOT",
                                    "collection_mode": "shadow_only",
                                }
                            if "smart_exit_liquidity_shadow" not in pos:
                                pos["smart_exit_liquidity_shadow"] = {
                                    "candidate_available": False,
                                    "reason": "PRE_EPOCH_POSITION_NO_ENTRY_SNAPSHOT",
                                    "collection_mode": "shadow_only",
                                }
                            if "smart_exit_router_shadow" not in pos:
                                pos["smart_exit_router_shadow"] = {
                                    "candidate_available": False,
                                    "reason": "PRE_EPOCH_POSITION_NO_ENTRY_SNAPSHOT",
                                    "collection_mode": "shadow_only",
                                }
                            self._update_smart_exit_shadows(pos, smart_bars)
                            pos["smart_exit_shadow_last_fetch"] = now
                        mfe = float(pos.get("shadow_mfe_r", 0) or 0)
                        candle = self._score_completed_bars(
                            bars, pos["side"], entry=float(pos["entry_price"]),
                            sl=float(pos["sl_price"]), mfe_r=mfe)
                        conflicts = self._fresh_conflicts(candle) or 0
                        confirmed = int(pos.get("shadow_conflict_confirmed_bars", 0) or 0)
                        candle_id = candle.get("closed_at") if candle else None
                        is_new = candle_id and candle_id != pos.get("shadow_conflict_last_candle")
                        if is_new:
                            confirmed = confirmed + 1 if conflicts >= 3 else 0
                            pos["shadow_conflict_last_candle"] = candle_id
                        pos["shadow_conflict_confirmed_bars"] = confirmed
                        if self._fresh_conflicts(candle) is not None:
                            pos["shadow_conflict_candle"] = candle
                        else:
                            pos.pop("shadow_conflict_candle", None)
                            pos.pop("shadow_conflict_last_candle", None)
                            pos["shadow_conflict_confirmed_bars"] = 0
                        self._update_audit_shadows(pos, mark, completed_candle=candle)
                        self._smart_sl_tp_snapshot(pos, mark, conflicts, confirmed)
                        if pos.get("tp1_hit") is True:
                            self._shadow_variant_snapshot(pos, mark, conflicts)
                        self._update_shadow_trailing(pos, mark)
                        _persist_positions(self.positions)
                virtual_book = getattr(self, "smart_virtual_book", None)
                if virtual_book is not None:
                    try:
                        await virtual_book.advance()
                    except Exception as vbe:
                        if not getattr(self, "_vb_advance_err_logged", False):
                            logger.warning("[PAPER] virtual book advance disabled: %s", vbe)
                            self._vb_advance_err_logged = True
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("[PAPER] mgmt loop error: %s", e)

    async def _check_aged_position(self, symbol: str, pos: Dict[str, Any], mark: float) -> None:
        """Record max-age counterfactual without changing provider lifecycle."""
        from datetime import datetime, timezone
        
        now = datetime.now(timezone.utc)
        opened_at = pos.get('opened_at')
        
        if not opened_at:
            return
        
        # Handle both datetime and ISO string
        if isinstance(opened_at, str):
            opened_at = datetime.fromisoformat(opened_at.replace('Z', '+00:00'))
        
        # Calculate age
        age_seconds = (now - opened_at).total_seconds()
        age_hours = age_seconds / 3600
        age_days = age_hours / 24
        
        # Check pre-TP1 threshold
        tp1_hit = pos.get('tp1_hit', False)
        
        reason = None
        if not tp1_hit:
            threshold_hours = self.MAX_POSITION_AGE['pre_tp1']
            if age_hours > threshold_hours:
                reason = "MAX_AGE_PRE_TP1"
        else:
            threshold_hours = self.MAX_POSITION_AGE['post_tp1']
            if age_hours > threshold_hours:
                reason = "MAX_AGE_POST_TP1"

        if not reason:
            return

        pos["max_age_shadow"] = {"reason": reason,
            "age_hours": age_hours, "threshold_hours": threshold_hours,
            "observed_price": mark, "observed_at": now.isoformat()}
        logger.warning("[MAX_AGE_SHADOW] %s %s age %.1fh", symbol, reason, age_hours)

        close_enabled = os.getenv("SIGNALCOPY_PAPER_MAX_AGE_CLOSE", "false").lower() == "true"
        real_money = os.getenv("REAL_MONEY", "false").lower() == "true"
        if close_enabled and not real_money and pos.get("status") == "OPEN" and float(pos.get("qty") or 0) > 0:
            await self._close(symbol, mark, reason)
        return

    # ── Dynamic exit layer: faithful port of FusionXomegabot's pre-SL defenses ──
    # Old bot live record: 60 trades, 71.7% win, +38.80%, SCRATCH_EXIT 53.3%,
    # DAMAGE_REDUCER 3.3%, HARD_SL only 1.7%. Ported thresholds are UNCHANGED.
    #
    # SAFETY INVARIANT: every dynamic SL mutation only ever TIGHTENS the stop.
    # LONG uses max(sl, candidate); SHORT uses min(sl, candidate). A dynamic
    # layer can never move the stop further from entry than the provider SL,
    # so max loss per position stays bounded by the original risk budget.

    def _dyn_enabled(self) -> bool:
        """Master gate: paper-only. Real money hard-disabled."""
        if os.getenv("REAL_MONEY", "false").lower() == "true":
            return False
        return _dyn_env_bool("SIGNALCOPY_PAPER_DYNAMIC_EXIT", "false")

    async def _apply_dynamic_exits(self, symbol: str, pos: Dict[str, Any], mark: float) -> bool:
        """Run the dynamic exit layers in old-bot order.

        Returns True if the position was closed by a dynamic layer (caller must
        stop processing it this cycle). SL/TP provider ladder is untouched —
        these layers only fire *before* the provider SL would.

        EPOCH MARKER: positions opened BEFORE 2026-09-06T12:43Z lack the entry
        snapshot required for faithful dynamic-exit decisions (hold_min, regime).
        They are excluded silently (return False immediately) to prevent
        contaminated cohort mixing in audit. New positions get full treatment.
        """
        if not self._dyn_enabled():
            return False
        
        if pos.get("status") != "OPEN" or float(pos.get("qty") or 0) <= 0:
            return False

        entry = float(pos.get("entry_price") or 0)
        if entry <= 0:
            return False
        side = str(pos.get("side") or "").upper()
        sl = float(pos.get("sl_price") or 0)

        # hold_min — faithful to old bot (minutes since open)
        opened = pos.get("opened_at")
        if isinstance(opened, str):
            try:
                opened = datetime.fromisoformat(opened.replace("Z", "+00:00"))
            except ValueError:
                return False
        if not isinstance(opened, datetime):
            return False
        
        # Audit heartbeat (INFO level during audit period)
        opened_str = opened.strftime('%m-%d %H:%M') if isinstance(opened, datetime) else str(opened)[:16]
        logger.info(f"[DYN_EXIT_CHECK] {symbol} {side} opened {opened_str}")
        
        # EPOCH GUARD: exclude pre-epoch positions (no valid dynamic-exit snapshot)
        DYNAMIC_EXIT_EPOCH = datetime(2026, 9, 6, 12, 43, 0, tzinfo=UTC)
        if opened < DYNAMIC_EXIT_EPOCH:
            return False
        
        now = datetime.now(UTC)
        hold_min = (now - opened).total_seconds() / 60.0
        if hold_min < 0:
            return False

        # unrealized — fraction, sign-corrected (matches old bot semantics)
        unrealized = ((mark - entry) / entry) if side == "LONG" else ((entry - mark) / entry)

        # ── 1. SCRATCH_EXIT — kill zombie trades near breakeven ─────────────
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
            
            max_abs = _dyn_env_float("SCRATCH_EXIT_MAX_ABS_PNL_PCT", 0.4)
            if hold_min >= scratch_min and abs(unrealized * 100.0) < max_abs:
                pos["dynamic_exit_layer"] = {"layer": "SCRATCH_EXIT", "hold_min": hold_min,
                                             "unrealized_pct": unrealized * 100.0,
                                             "scratch_timeout_min": scratch_min,
                                             "at": now.isoformat(), "mark": mark}
                await self._close(symbol, mark, "SCRATCH_EXIT")
                return True

        # ── 2. DAMAGE_REDUCER — cut bleeding positions before full SL ───────
        # Old bot closes full qty at market (name is historical).
        if _dyn_env_bool("DAMAGE_REDUCER_ENABLED"):
            min_hold = _dyn_env_float("DAMAGE_REDUCER_MIN_HOLD_MINUTES", 45.0)
            max_loss = _dyn_env_float("DAMAGE_REDUCER_MAX_LOSS_PCT", -2.5)
            if hold_min >= min_hold and (unrealized * 100.0) <= max_loss and sl > 0:
                # Only fire while still ABOVE the provider SL — once SL is hit,
                # the canonical HARD_SL path owns the exit (no double close).
                above_sl = (mark > sl) if side == "LONG" else (mark < sl)
                if above_sl:
                    pos["dynamic_exit_layer"] = {"layer": "DAMAGE_REDUCER", "hold_min": hold_min,
                                                 "unrealized_pct": unrealized * 100.0,
                                                 "at": now.isoformat(), "mark": mark}
                    await self._close(symbol, mark, "DAMAGE_REDUCER")
                    return True

        # ── 3. PROFIT_LOCK — move SL to breakeven+buffer once in profit ─────
        # TP1-GATED: Only lock after TP1 hit to avoid premature locks from volatility spikes
        # Buffer = max(entry*BUFFER_PCT/100, risk_distance*0.15) — old bot exact.
        tp_hit_count = len(pos.get("tp_hit", []))
        if (not pos.get("locked_profit")
                and hold_min > _dyn_env_int("PROFIT_LOCK_MIN_MINUTES", 15)
                and tp_hit_count >= 1):  # ✅ TP1-gated: confirmed profit only
            original_sl = float(pos.get("provider_sl_original") or sl or entry)
            risk_distance = abs(entry - original_sl)
            fixed_buffer = entry * (_dyn_env_float("PROFIT_LOCK_BUFFER_PCT", 0.3) / 100.0)
            buffer = max(fixed_buffer, risk_distance * 0.15)
            if side == "LONG":
                new_sl = max(sl, entry + buffer)
                if new_sl >= mark * 0.998:   # too tight — would fire instantly
                    new_sl = sl
            else:
                new_sl = min(sl, entry - buffer) if sl > 0 else entry - buffer
                if new_sl <= mark * 1.002:   # too tight — would fire instantly
                    new_sl = sl
            if new_sl != sl and new_sl > 0:
                # Invariant: only ever tighten (LONG max / SHORT min above).
                pos["sl_price"] = new_sl
                pos["sl_kind"] = "BREAKEVEN"
                pos["locked_profit"] = True
                pos["profit_lock_at"] = now.isoformat()
                pos["profit_lock_buffer"] = buffer
                _persist_positions(self.positions)
                logger.info("[PROFIT_LOCK] %s %s | new_sl=%.6g | buffer=%.6g | hold=%.0fmin",
                            side, symbol, new_sl, buffer, hold_min)

        # ── 4. TRAILING — only after profit lock + 10min (old bot two-phase) ─
        if pos.get("locked_profit") and hold_min > 10.0 and _dyn_env_bool("TRAIL_SL_ENABLED"):
            atr_pct = None
            adv = pos.get("adv_snapshot")
            if isinstance(adv, dict):
                atr_pct = adv.get("atr_pct")
            try:
                atr_pct = float(atr_pct) if atr_pct is not None else None
            except (TypeError, ValueError):
                atr_pct = None
            base = _dyn_env_float("TRAIL_SL_PCT", 0.8) / 100.0
            mult = _dyn_env_float("TRAIL_SL_ATR_MULTIPLIER", 1.5)
            trail = max(base, (atr_pct / 100.0) * mult) if atr_pct else base

            cur_sl = float(pos.get("sl_price") or 0)
            if side == "LONG":
                wm = float(pos.get("trail_high_watermark") or mark)
                wm = max(wm, mark)
                pos["trail_high_watermark"] = wm
                new_sl = max(cur_sl, wm * (1.0 - trail))
                if new_sl > cur_sl and new_sl < mark:
                    pos["sl_price"] = new_sl
                    pos["sl_kind"] = "TRAILING"
                    _persist_positions(self.positions)
                    logger.info("[TRAILING_SL] %s LONG | new_sl=%.6g | trail=%.2f%%",
                                symbol, new_sl, trail * 100)
            else:
                wm = float(pos.get("trail_low_watermark") or mark)
                wm = min(wm, mark)
                pos["trail_low_watermark"] = wm
                new_sl = min(cur_sl, wm * (1.0 + trail)) if cur_sl > 0 else wm * (1.0 + trail)
                if cur_sl <= 0 or (new_sl < cur_sl and new_sl > mark):
                    pos["sl_price"] = new_sl
                    pos["sl_kind"] = "TRAILING"
                    _persist_positions(self.positions)
                    logger.info("[TRAILING_SL] %s SHORT | new_sl=%.6g | trail=%.2f%%",
                                symbol, new_sl, trail * 100)

        # ── 5. TIME_EXIT — regime max hold, one +30min extension if green ───
        regime = str(pos.get("regime") or "").upper()
        max_hold = {"TRENDING": _dyn_env_int("MAX_HOLD_TRENDING", 480),
                    "RANGING": _dyn_env_int("MAX_HOLD_RANGING", 240),
                    "HIGH_VOL": _dyn_env_int("MAX_HOLD_HIGH_VOL", 120)}.get(
                        regime, _dyn_env_int("MAX_HOLD_MINUTES", 300))
        cap = int(pos.get("dyn_max_hold_minutes") or max_hold)
        if hold_min >= cap:
            if unrealized > 0.005:
                if not pos.get("dyn_time_extended"):
                    pos["dyn_time_extended"] = True
                    pos["dyn_max_hold_minutes"] = cap + 30
                    logger.info("[TIME_EXTEND] %s | unrealized=%.2f%% | +30min",
                                symbol, unrealized * 100)
                else:
                    await self._close(symbol, mark, "TIME_EXIT_EXTENDED")
                    return True
            else:
                await self._close(symbol, mark, "TIME_EXIT")
                return True
        return False

    async def _apply_take_profits(self, symbol: str, mark: float):
        pos = self.positions.get(symbol)
        if not pos:
            return
        ladder = pos.get("tp_ladder") or [pos.get("tp1_price")]
        idx = int(pos.get("next_tp_index", 0))
        while idx < len(ladder):
            tp = float(ladder[idx] or 0)
            hit = (pos["side"] == "LONG" and mark >= tp) or (pos["side"] == "SHORT" and mark <= tp)
            if not tp or not hit:
                break
            if idx == 0:
                pos["tp1_hit"] = True
                pos["tp1_hit_at"] = pos.get("tp1_hit_at") or datetime.now(UTC)
                pos["shadow_cohort"] = True
            self._record_shadow_tp(pos, idx, tp)
            idx += 1
            if idx == len(ladder):
                await self._close(symbol, tp, f"TP{idx}")
                return
            slice_qty = float(pos["initial_qty"]) / len(ladder)
            pos["qty"] = max(0.0, float(pos["qty"]) - slice_qty)
            pos["next_tp_index"] = idx
            pos["tp1_hit"] = True
            _persist_positions(self.positions)
            await self._realize_partial(pos, tp, slice_qty, f"TP{idx}_PARTIAL")

    async def _realize_partial(self, pos: Dict[str, Any], exit_price: float, qty: float, reason: str):
        entry, side = pos["entry_price"], pos["side"]
        actual_exit = self._adverse_fill(exit_price, side, opening=False)
        gross_pnl = (actual_exit - entry) * qty if side == "LONG" else (entry - actual_exit) * qty
        exit_fee = self._fee(actual_exit * qty)
        pnl_usd = gross_pnl - exit_fee
        original_sl = float(pos.get("provider_sl_original", pos.get("sl_price", entry)))
        risk_per_unit = abs(float(entry) - original_sl)
        rr = abs(actual_exit - float(entry)) / risk_per_unit if risk_per_unit else 0.0
        ladder = pos.get("tp_ladder") or []
        next_index = int(pos.get("next_tp_index", 0))
        pos.setdefault("fill_ledger", []).append({
            "timestamp": datetime.now(UTC).isoformat(), "kind": "TP_PARTIAL", "reason": reason,
            "entry_price": entry, "exit_price": actual_exit, "qty": qty,
            "remaining_qty": float(pos.get("qty", 0)), "gross_pnl_usd": gross_pnl,
            "fee_usd": exit_fee, "net_pnl_usd": pnl_usd,
        })
        pos["baseline_partial_net_pnl"] = float(pos.get("baseline_partial_net_pnl", 0)) + pnl_usd
        pnl_pct = pnl_usd / max(entry * qty, 1e-9) * 100
        equity_after = self._apply_equity_delta(pnl_usd)
        await self._notify_close({"symbol": pos["symbol"], "side": side, "reason": reason,
            "is_partial": True, "entry_price": entry, "qty": qty,
            "exit_price": actual_exit, "gross_pnl_usd": gross_pnl, "fee_usd": exit_fee,
            "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
            "hold_minutes": (datetime.now(UTC)-pos["opened_at"]).total_seconds()/60,
            "equity": equity_after or 0.0, "remaining_qty": pos["qty"],
            "sl_original": original_sl,
            "next_tp": ladder[next_index] if next_index < len(ladder) else 0.0, "rr": rr})

    async def _close(self, symbol: str, exit_price: float, reason: str):
        pos = self.positions.get(symbol)
        if not pos:
            return
        entry = pos["entry_price"]; side = pos["side"]
        actual_exit = self._adverse_fill(exit_price, side, opening=False)
        notional = entry * float(pos.get("qty", 0.0))
        qty = float(pos.get("qty", 0.0))
        gross_pnl = (actual_exit - entry) * qty if side == "LONG" else (entry - actual_exit) * qty
        exit_fee = self._fee(actual_exit * qty)
        pnl_usd = gross_pnl - exit_fee
        entry_fee = float(pos.get("entry_fee", 0.0))
        baseline_total = float(pos.get("baseline_partial_net_pnl", 0)) + pnl_usd - entry_fee
        realized_net_pnl = baseline_total
        audit = pos.get("shadow_audit") or {}
        joined = {}
        for name, state in (audit.get("exit_variants") or {}).items():
            shadow_net = state.get("remaining_qty_net_pnl")
            joined[name] = {"triggered": state.get("triggered_at") is not None,
                            "net_pnl": baseline_total if shadow_net is None else
                            float(pos.get("baseline_partial_net_pnl", 0)) + float(shadow_net) - entry_fee}
        for name, ledger in (audit.get("tp_ledgers") or {}).items():
            remaining = float(ledger.get("remaining_qty", 0))
            final = self._shadow_fill(pos, exit_price, remaining)
            joined[name] = {"realized_net_pnl": float(ledger.get("realized_net_pnl", 0)),
                            "final_remaining_net_pnl": final["remaining_qty_net_pnl"],
                            "net_pnl": float(ledger.get("realized_net_pnl", 0)) +
                            final["remaining_qty_net_pnl"] - entry_fee}
        shadow = pos.get("shadow_trailing") or {}
        shadow_triggered = bool(shadow.get("exit_price"))
        shadow_total = (float(pos.get("baseline_partial_net_pnl", 0))
                        + float(shadow.get("remaining_net_pnl", 0)) - entry_fee) if shadow_triggered else baseline_total
        smart_exit_outcome = self._finalize_smart_exit_shadow(pos, provider_exit=exit_price)
        liquidity_exit_outcome = self._finalize_smart_exit_shadow(
            pos, provider_exit=exit_price, key="smart_exit_liquidity_shadow")
        router_exit_outcome = self._finalize_smart_exit_shadow(
            pos, provider_exit=exit_price, key="smart_exit_router_shadow")
        if smart_exit_outcome.get("candidate_available"):
            joined["smart_exit_15m_atr"] = {
                "triggered": smart_exit_outcome["triggered"],
                "net_pnl": smart_exit_outcome["net_pnl_usd"],
                "exit_reason": smart_exit_outcome["exit_reason"],
                "qty": smart_exit_outcome["qty"],
            }
        if liquidity_exit_outcome.get("candidate_available"):
            joined["smart_exit_liquidity_grab"] = {
                "triggered": liquidity_exit_outcome["triggered"],
                "net_pnl": liquidity_exit_outcome["net_pnl_usd"],
                "exit_reason": liquidity_exit_outcome["exit_reason"],
                "qty": liquidity_exit_outcome["qty"],
            }
        if router_exit_outcome.get("candidate_available"):
            joined["smart_exit_router_v1"] = {
                "triggered": router_exit_outcome["triggered"],
                "net_pnl": router_exit_outcome["net_pnl_usd"],
                "exit_reason": router_exit_outcome["exit_reason"],
                "qty": router_exit_outcome["qty"],
                "setup_family": (pos.get("smart_exit_router_shadow") or {}).get("setup_family"),
            }
        comparable_shadow_pnls = [float(value["net_pnl"]) for value in joined.values()
                                  if value.get("net_pnl") is not None]
        best_shadow_net_pnl = max(comparable_shadow_pnls, default=baseline_total)
        bad_signal_shadow = pos.get("bad_signal_shadow") or self._classify_bad_signal(
            side, entry, pos.get("adv_snapshot") or {})
        bad_signal_outcome = self._classify_bad_signal_outcome(
            bad_signal_shadow, baseline_total, best_shadow_net_pnl)
        pnl_pct = realized_net_pnl / max(float(pos.get("notional", notional)), 1e-9) * 100
        hold_min = (datetime.now(UTC) - pos["opened_at"]).total_seconds() / 60
        original_sl = float(pos.get("provider_sl_original", pos.get("sl_price", entry)))
        risk_per_unit = abs(float(entry) - original_sl)
        rr = abs(actual_exit - float(entry)) / risk_per_unit if risk_per_unit else 0.0
        pos.setdefault("fill_ledger", []).append({
            "timestamp": datetime.now(UTC).isoformat(), "kind": "FINAL", "reason": reason,
            "entry_price": entry, "exit_price": actual_exit, "qty": qty,
            "notional_usd": float(pos.get("notional", notional)), "gross_pnl_usd": gross_pnl,
            "fee_usd": exit_fee, "net_pnl_usd": pnl_usd,
        })
        logger.info("[PAPER] CLOSE %s %s @ %.6g | %s | PnL %+.2f (%+.2f%%) | hold %.1fm",
                    side, symbol, actual_exit, reason, realized_net_pnl, pnl_pct, hold_min)
        if self._shadow_enabled():
            try:
                _append_shadow_result({
                    "timestamp_open": pos["opened_at"], "timestamp_close": datetime.now(UTC),
                    "symbol": symbol, "side": side, "actual_reason": reason,
                    "source_chat_id": (pos.get("adv_snapshot") or {}).get("source_chat_id"),
                    "actual_exit_price": actual_exit, "baseline_net_pnl": baseline_total,
                    "shadow_triggered": shadow_triggered,
                    "shadow_exit_price": shadow.get("exit_price"),
                    "shadow_exit_at": shadow.get("exit_at"),
                    "shadow_stop": shadow.get("stop"),
                    "shadow_net_pnl": shadow_total,
                    "shadow_delta_usd": shadow_total - baseline_total,
                    "entry_fee_usd": entry_fee, "exit_fee_usd": exit_fee,
                    "exit_slippage_usd": abs(actual_exit - exit_price) * qty,
                    "trail_pct": self._shadow_trail_pct(pos),
                    "tp1_hit": bool(pos.get("tp1_hit")), "hold_minutes": hold_min,
                    "eligible_for_audit": bool(pos.get("shadow_cohort") or pos.get("tp1_hit")),
                    "cohort": "post_tp1", "tp1_hit_at": pos.get("tp1_hit_at"),
                    "observation_basis": "polled_mark", "intrabar_order_known": False,
                    "shadow_schema_version": 2,
                    "provider_sl_original": pos.get("provider_sl_original", pos.get("sl_price")),
                    "baseline_is_provider_lifecycle": not TRAILING_EXECUTION_ENABLED,
                    "sl_kind_at_exit": pos.get("sl_kind", "ORIGINAL"),
                    "normalized_reason": self._normalize_exit_reason(reason, pos.get("sl_kind", "ORIGINAL")),
                    "shadow_variants": pos.get("shadow_variants") or {},
                    "smart_sl_tp_shadow": pos.get("smart_sl_tp_shadow") or {},
                    "smart_exit_shadow": pos.get("smart_exit_shadow") or {},
                    "smart_exit_outcome": smart_exit_outcome,
                    "smart_exit_liquidity_shadow": pos.get("smart_exit_liquidity_shadow") or {},
                    "smart_exit_liquidity_outcome": liquidity_exit_outcome,
                    "smart_exit_router_shadow": pos.get("smart_exit_router_shadow") or {},
                    "smart_exit_router_outcome": router_exit_outcome,
                    "shadow_audit": audit, "joined_outcomes": joined,
                    "bad_signal_shadow": bad_signal_shadow,
                    "bad_signal_outcome": bad_signal_outcome,
                })
            except Exception as e:
                logger.warning("[SHADOW_TRAIL] journal failed for %s: %s", symbol, e)
        if self._journal:
            await self._journal.write_trade({
                "timestamp_open": pos["opened_at"].isoformat(),
                "timestamp_close": datetime.now(UTC).isoformat(),
                "symbol": symbol, "side": side,
                "entry_price": entry, "exit_price": actual_exit,
                "notional_usd": float(pos.get("notional", notional)), "pnl_pct": pnl_pct,
                "pnl_usd": realized_net_pnl, "final_exit_net_pnl_usd": pnl_usd,
                "gross_pnl_usd": gross_pnl, "entry_fee_usd": pos.get("entry_fee", 0.0),
                "exit_fee_usd": exit_fee,
                "hold_minutes": hold_min, "reason": reason, "raw_reason": reason,
                "normalized_reason": self._normalize_exit_reason(reason, pos.get("sl_kind", "ORIGINAL")),
                "sl_original": pos.get("provider_sl_original", pos["sl_price"]),
                "active_sl_at_exit": pos.get("sl", pos["sl_price"]),
                "sl_kind_at_exit": pos.get("sl_kind", "ORIGINAL"),
                "regime": "PAPER_MAINNET",
                # Observability: forward the enrichment captured at open so the
                # journal records WHY we entered (score/confidence + full metrics
                # blob incl. mtf/tv/vision) instead of blank UNKNOWN columns.
                "adv_snapshot": pos.get("adv_snapshot") or {},
                "score": pos.get("score", 0.0),
                "priority_score": pos.get("score", 0.0),
                "confidence": pos.get("confidence", 0.0),
                "cvd": (pos.get("adv_snapshot") or {}).get("cvd"),
                "oi_15m_pct": (pos.get("adv_snapshot") or {}).get("oi_change_15m_pct"),
                "oi_1h_pct": (pos.get("adv_snapshot") or {}).get("oi_change_1h_pct"),
                "funding_pct": (pos.get("adv_snapshot") or {}).get("funding_rate"),
                "vol_ratio": (pos.get("adv_snapshot") or {}).get("vol_ratio"),
                "rsi": (pos.get("adv_snapshot") or {}).get("rsi"),
                "bad_signal_shadow": bad_signal_shadow,
                "bad_signal_outcome": bad_signal_outcome,
                "joined_shadow_outcomes": joined,
            })

        # Feed realized PnL back into the RiskManager so equity/daily_pnl reflect
        # actual closed trades (was frozen at starting_balance before this).
        equity_after = None
        try:
            rm = self.risk_mgr
            if rm is not None and hasattr(rm, "sync_balance"):
                equity_after = self._apply_equity_delta(pnl_usd)
                if hasattr(rm, "update_after_trade"):
                    rm.update_after_trade({"pnl_pct": pnl_pct, "reason": reason})
        except Exception as e:
            logger.warning("[PAPER] equity sync failed for %s: %s", symbol, e)

        # Fire-and-forget close notification to the trades channel (was never sent).
        try:
            asyncio.create_task(self._notify_close({
                "symbol": symbol, "side": side, "reason": reason,
                "entry_price": entry, "qty": qty,
                "notional_usd": float(pos.get("notional", notional)), "rr": rr,
                "normalized_reason": reason, "exit_price": actual_exit,
                "gross_pnl_usd": gross_pnl, "fee_usd": entry_fee + exit_fee,
                "pnl_pct": pnl_pct, "pnl_usd": realized_net_pnl, "hold_minutes": hold_min,
                "equity": equity_after if equity_after is not None else 0.0,
                "sl_original": pos.get("provider_sl_original", pos["sl_price"]),
                "active_sl_at_exit": pos.get("sl", pos["sl_price"]),
                "sl_kind_at_exit": pos.get("sl_kind", "ORIGINAL"),
                "footer": "Fusion Quantum Dry-Run" if str(pos.get("tag", "")).startswith("fusion_quantum") else "",
            }))
        except Exception as e:
            logger.warning("[PAPER] close-notify dispatch failed for %s: %s", symbol, e)

        # Remove only after durable journals/accounting complete. A writer failure
        # must leave position recoverable instead of losing final accounting.
        self.positions.pop(symbol, None)
        _persist_positions(self.positions)

    async def update_stop(self, symbol: str, new_sl: float) -> Dict[str, Any]:
        pos = self.positions.get(symbol)
        if not pos:
            return {"ok": False, "code": "POSITION_NOT_FOUND"}
        from signal_copy.provider_updates import safer_stop
        if not safer_stop(pos["side"], float(pos["sl_price"]), float(new_sl), float(pos["entry_price"])):
            return {"ok": False, "code": "STOP_WIDENS_RISK"}
        old = pos["sl_price"]
        pos["sl_price"] = float(new_sl)
        try:
            _persist_positions(self.positions)
        except Exception:
            pos["sl_price"] = old
            raise
        return {"ok": True, "code": "STOP_UPDATED", "old_sl": old, "new_sl": new_sl}

    async def close_position(self, symbol: str, reason: str = "PROVIDER_CLOSE") -> Dict[str, Any]:
        pos = self.positions.get(symbol)
        if not pos:
            return {"ok": False, "code": "POSITION_NOT_FOUND"}
        mark = await self._get_mark_price(symbol)
        if mark <= 0:
            return {"ok": False, "code": "NO_MARKET_PRICE"}
        close_reason = str(reason or "PROVIDER_CLOSE").upper()
        sl = float(pos.get("sl_price") or 0)
        if sl and self._level_hit(pos, mark, sl, favorable=False):
            close_reason = "SL_GAP"
        await self._close(symbol, mark, close_reason)
        return {"ok": True, "code": "POSITION_CLOSED", "reason": close_reason, "exit_price": mark}

    async def provider_close(self, symbol: str) -> Dict[str, Any]:
        return await self.close_position(symbol, "PROVIDER_CLOSE")

    async def _notify_close(self, payload: Dict[str, Any]) -> None:
        """Build + send a CLOSE card to the trades channel. Never raises."""
        try:
            from signal_copy.telegram_formatter import build_close_message
            from signal_copy.telegram_transport import send_trades_notification
            msg = build_close_message(payload)
            if await send_trades_notification(msg):
                return
            # Native fallback: bot image may omit python-telegram-bot.
            import json, os, urllib.request
            token = os.getenv("FQ_TELEGRAM_BOT_TOKEN") or os.getenv("SIGNAL_COPY_TRADES_NOTIFY_BOT_TOKEN") or os.getenv("SIGNAL_COPY_PARSER_NOTIFY_BOT_TOKEN")
            chat = os.getenv("FQ_TELEGRAM_CHAT_ID") or os.getenv("SIGNAL_COPY_TRADES_NOTIFY_CHAT_ID") or os.getenv("SIGNAL_COPY_PARSER_NOTIFY_CHAT_ID")
            if not token or not chat:
                raise RuntimeError("Telegram close token/chat not configured")
            body = json.dumps({"chat_id": chat, "text": "🔄 [TRADES] " + msg, "parse_mode": "HTML", "disable_web_page_preview": True}).encode()
            req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body, headers={"Content-Type": "application/json"})
            await asyncio.to_thread(urllib.request.urlopen, req, timeout=10)
        except Exception as e:
            logger.warning("[PAPER] close notification send failed: %s", e)

    # ── introspection (RiskManager may call these) ───────────────
    def position(self, symbol: str) -> float:
        p = self.positions.get(symbol)
        return p["qty"] if p and p["status"] == "OPEN" else 0.0

    def _position_symbols(self):
        return set(self.positions.keys())

    def _position_count(self) -> int:
        return len(self.positions)
