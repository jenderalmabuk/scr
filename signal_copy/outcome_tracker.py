"""Paper outcome tracker: the missing feedback loop for channel-quality learning.

signal_copy is fire-and-forget — positions are opened via the gateway and their
exits (TP/SL/trailing) are managed by the engine and NEVER reported back. So
only executed signals could ever be scored, and rejected signals leave no trace.
That biases any channel-quality metric.

This tracker snapshots EVERY validated signal (executed OR rejected) that has a
usable entry + TP1 + SL, then polls live price to decide a virtual outcome:
TP1 hit => win, SL hit => loss, expiry => mark-to-market. On resolution it feeds
ChannelPerformanceTracker.record_trade(), so ALL channels accumulate a fair
track record — the raw material for down-weighting bad channels later.

PnL is the raw spot-move % from entry (leverage-agnostic) so channels are
compared apples-to-apples. Open snapshots persist to data/signal_outcomes.json
so a restart does not lose in-flight trackers.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from utils.logger import logger
from .channel_performance import get_tracker
from .signal_schema import ParsedSignal, SignalSide
from . import signal_copy_config as scfg

STORE = Path(os.getenv("SIGNAL_COPY_OUTCOME_STORE", "data/signal_outcomes.json"))


def _append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except Exception as exc:
        logger.debug("[OUTCOME] jsonl append skipped: %s", exc)


# Fallback horizon if the channel has no expiry profile (should not happen since
# expiry_for_channel always returns standard for unmapped ids).
_DEFAULT_EXPIRY_SEC = 10800.0


class SignalOutcomeTracker:
    """Tracks open virtual signals and resolves them against live price."""

    def __init__(self) -> None:
        self._open: Dict[str, dict] = {}
        self._load()

    # ---------- persistence ----------
    def _load(self) -> None:
        if STORE.exists():
            try:
                raw = STORE.read_text().strip()
                self._open = json.loads(raw) if raw else {}
            except Exception as exc:
                logger.warning("[OUTCOME] load failed: %s", exc)
                self._open = {}
        else:
            self._open = {}
        dropped = []
        for sid, snap in list(self._open.items()):
            try:
                side = str(snap.get("side") or "")
                entry = float(snap.get("entry") or 0.0)
                tp1 = float(snap.get("tp1") or 0.0)
                sl = float(snap.get("sl") or 0.0)
                if not self._levels_valid(side, entry, tp1, sl):
                    dropped.append(sid)
                    self._open.pop(sid, None)
            except Exception:
                dropped.append(sid)
                self._open.pop(sid, None)
        if dropped:
            logger.info("[OUTCOME] dropped %d invalid open snapshot(s)", len(dropped))
            self._save()

    def _save(self) -> None:
        try:
            STORE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STORE.with_suffix(STORE.suffix + ".tmp")
            tmp.write_text(json.dumps(self._open, indent=2))
            tmp.replace(STORE)
        except Exception as exc:
            logger.warning("[OUTCOME] save failed: %s", exc)

    # ---------- snapshot ----------
    def track(self, sig: ParsedSignal, metrics: Dict[str, Any],
              *, verdict: str = "", execution_intended: bool = False) -> None:
        """Snapshot a validated signal for virtual outcome tracking.

        Skips signals lacking the data needed to define an outcome, and skips
        signals already resolved at receipt (price already past TP1 or SL —
        we cannot fairly attribute those going forward).
        """
        try:
            sid = sig.signal_id
            if not sid or sid in self._open:
                return
            side = sig.side.value if hasattr(sig.side, "value") else str(sig.side)
            entry = float(sig.rr_entry or 0.0)
            sl = float(sig.stop_loss or 0.0)
            tps = [float(t) for t in (sig.take_profits or []) if t]
            if entry <= 0 or sl <= 0 or not tps:
                return
            tp1 = tps[0]
            if not self._levels_valid(side, entry, tp1, sl):
                logger.info(
                    "[OUTCOME] skip invalid levels %s %s entry=%.6g tp1=%.6g sl=%.6g",
                    sig.symbol, side, entry, tp1, sl,
                )
                return
            price = float(metrics.get("price") or 0.0)
            # skip if already past TP1 or SL at receipt (stale / can't attribute)
            if price > 0:
                if side == "LONG" and (price >= tp1 or price <= sl):
                    return
                if side == "SHORT" and (price <= tp1 or price >= sl):
                    return
            cid = sig.source_chat_id
            expiry = _DEFAULT_EXPIRY_SEC
            try:
                expiry = float(scfg.expiry_for_channel(cid))
            except Exception:
                pass
            committee = metrics.get("adversarial_committee")
            self._open[sid] = {
                "signal_id": sid,
                "source_chat_id": cid,
                "source_name": sig.source_name or "",
                "symbol": sig.symbol,
                "side": side,
                "entry": entry,
                "sl": sl,
                "tp1": tp1,
                "tps": tps,
                "created_at": time.time(),
                "expiry_sec": expiry,
                "verdict": verdict,
                "validation_score": metrics.get("validation_score"),
                "execution_intended": bool(execution_intended),
                "gateway_accepted": False,
                "position_confirmed": False,
                "adversarial_committee": committee,
            }
            self._save()
            logger.info(
                "[OUTCOME] tracking %s %s %s entry=%.6g tp1=%.6g sl=%.6g exp=%.0fs intent=%s",
                sig.symbol, side, (sig.source_name or cid), entry, tp1, sl,
                expiry, execution_intended,
            )
        except Exception as exc:
            logger.warning("[OUTCOME] track failed: %s", exc)

    # ---------- resolution ----------
    @staticmethod
    def _pnl_pct(side: str, entry: float, exit_px: float) -> float:
        if entry <= 0:
            return 0.0
        if side == "LONG":
            return (exit_px - entry) / entry * 100.0
        return (entry - exit_px) / entry * 100.0

    @staticmethod
    def _levels_valid(side: str, entry: float, tp1: float, sl: float) -> bool:
        if entry <= 0 or tp1 <= 0 or sl <= 0:
            return False
        if side == "LONG":
            return tp1 > entry > sl
        return tp1 < entry < sl

    @staticmethod
    def _to_epoch(value: Any) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            ts = float(value)
            return ts / 1000.0 if ts > 10_000_000_000 else ts
        if hasattr(value, "timestamp"):
            try:
                return float(value.timestamp())
            except Exception:
                return None
        raw = str(value).strip()
        if not raw:
            return None
        try:
            ts = float(raw)
            return ts / 1000.0 if ts > 10_000_000_000 else ts
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    def _normalise_candles(self, candles: List[Any], since_ts: float) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for row in candles or []:
            try:
                if isinstance(row, dict):
                    ts = self._to_epoch(row.get("open_time") or row.get("timestamp") or row.get("time") or row.get("ts"))
                    high = float(row.get("high") or 0.0)
                    low = float(row.get("low") or 0.0)
                    close = float(row.get("close") or 0.0)
                elif isinstance(row, (list, tuple)) and len(row) >= 5:
                    ts = self._to_epoch(row[0])
                    high = float(row[2] or 0.0)
                    low = float(row[3] or 0.0)
                    close = float(row[4] or 0.0)
                else:
                    continue
                if ts is None or high <= 0 or low <= 0:
                    continue
                # Only trust full candles that opened after the signal snapshot.
                # The signal-minute candle may contain pre-signal highs/lows.
                if since_ts and ts < since_ts:
                    continue
                out.append({"ts": ts, "high": high, "low": low, "close": close})
            except Exception:
                continue
        out.sort(key=lambda x: x["ts"])
        return out

    def _resolve_one_from_candles(self, snap: dict, candles: List[Any]) -> Optional[tuple]:
        """Return (pnl_pct, exit_reason, meta) using closed 1m candle high/low."""
        side = snap["side"]
        entry = snap["entry"]
        sl = snap["sl"]
        tp1 = snap["tp1"]
        rows = self._normalise_candles(candles, float(snap.get("created_at") or 0.0))
        for row in rows:
            high = row["high"]
            low = row["low"]
            if side == "LONG":
                tp_hit = high >= tp1
                sl_hit = low <= sl
            else:  # SHORT
                tp_hit = low <= tp1
                sl_hit = high >= sl
            meta = {
                "resolution_method": "ohlcv_1m",
                "hit_candle_ts": row["ts"],
                "hit_candle_time": datetime.utcfromtimestamp(row["ts"]).isoformat() + "Z",
                "hit_candle_high": high,
                "hit_candle_low": low,
                "ambiguous": bool(tp_hit and sl_hit),
            }
            if tp_hit and sl_hit:
                # Intrabar ordering is unknowable from OHLCV. Count conservatively
                # as an SL for channel-quality scoring rather than overstating wins.
                return self._pnl_pct(side, entry, sl), "SL_AMBIGUOUS", {**meta, "exit_price": sl}
            if tp_hit:
                return self._pnl_pct(side, entry, tp1), "TP1", {**meta, "exit_price": tp1}
            if sl_hit:
                return self._pnl_pct(side, entry, sl), "SL", {**meta, "exit_price": sl}
        return None

    def _resolve_one(self, snap: dict, price: float) -> Optional[tuple]:
        """Return (pnl_pct, exit_reason, meta) if resolved, else None."""
        side = snap["side"]
        entry = snap["entry"]
        sl = snap["sl"]
        tp1 = snap["tp1"]
        if price > 0:
            if side == "LONG":
                if price >= tp1:
                    return self._pnl_pct(side, entry, tp1), "TP1", {"resolution_method": "last_price", "price": price, "exit_price": tp1}
                if price <= sl:
                    return self._pnl_pct(side, entry, sl), "SL", {"resolution_method": "last_price", "price": price, "exit_price": sl}
            else:  # SHORT
                if price <= tp1:
                    return self._pnl_pct(side, entry, tp1), "TP1", {"resolution_method": "last_price", "price": price, "exit_price": tp1}
                if price >= sl:
                    return self._pnl_pct(side, entry, sl), "SL", {"resolution_method": "last_price", "price": price, "exit_price": sl}
        age = time.time() - snap.get("created_at", 0)
        if age >= snap.get("expiry_sec", _DEFAULT_EXPIRY_SEC):
            # mark-to-market at expiry (or entry if no price)
            mtm = price if price > 0 else entry
            return self._pnl_pct(side, entry, mtm), "EXPIRE", {"resolution_method": "last_price_expiry", "price": price, "exit_price": mtm}
        return None

    async def resolve_open(
        self,
        price_fn: Callable[[str], Awaitable[float]],
        candles_fn: Optional[Callable[[str, float], Awaitable[List[Any]]]] = None,
    ) -> int:
        """Resolve every open snapshot and record finished ones.

        Prefer candles_fn(symbol, since_ts) closed 1m OHLCV high/low proof, then
        fall back to price_fn(symbol) last price (also used for expiry MTM).
        Returns the number of signals resolved this pass.
        """
        if not self._open:
            return 0
        tracker = get_tracker()
        resolved: List[str] = []
        # snapshot keys first (dict mutated on resolve)
        for sid, snap in list(self._open.items()):
            try:
                out = None
                if candles_fn is not None:
                    try:
                        candles = await candles_fn(
                            snap["symbol"], float(snap.get("created_at") or 0.0)
                        )
                        if candles:
                            out = self._resolve_one_from_candles(snap, candles)
                    except Exception as exc:
                        logger.debug("[OUTCOME] candle fetch %s failed: %s",
                                     snap["symbol"], exc)
                price = 0.0
                if out is None:
                    try:
                        price = float(await price_fn(snap["symbol"]) or 0.0)
                    except Exception as exc:
                        logger.debug("[OUTCOME] price fetch %s failed: %s",
                                     snap["symbol"], exc)
                    out = self._resolve_one(snap, price)
                if out is None:
                    continue
                pnl_pct, reason, meta = out
                tracker.record_trade(
                    source_chat_id=snap.get("source_chat_id") or 0,
                    source_name=snap.get("source_name") or "",
                    symbol=snap.get("symbol") or "",
                    pnl_pct=pnl_pct,
                    exit_reason=reason,
                )
                committee = snap.get("adversarial_committee") or {}
                if isinstance(committee, dict) and committee:
                    _append_jsonl(Path(getattr(scfg, "COMMITTEE_OUTCOME_JOURNAL", "runtime/state/committee_outcomes.jsonl")), {
                        "ts": time.time(),
                        "signal_id": sid,
                        "source_chat_id": snap.get("source_chat_id") or 0,
                        "source_name": snap.get("source_name") or "",
                        "symbol": snap.get("symbol") or "",
                        "side": snap.get("side") or "",
                        "validation_verdict": snap.get("verdict") or "",
                        "validation_score": snap.get("validation_score"),
                        "execution_intended": bool(snap.get("execution_intended")),
                        "committee_final_vote": committee.get("final_vote"),
                        "committee_action": committee.get("action"),
                        "committee_score": committee.get("score"),
                        "committee_no_votes": committee.get("no_votes"),
                        "committee_top_reasons": committee.get("top_reasons"),
                        "exit_reason": reason,
                        "pnl_pct": pnl_pct,
                        "resolution_method": meta.get("resolution_method"),
                        "outcome_ambiguous": bool(meta.get("ambiguous")),
                        "hit_candle_ts": meta.get("hit_candle_ts"),
                        "hit_candle_time": meta.get("hit_candle_time"),
                        "exit_price": meta.get("exit_price"),
                    })
                logger.info(
                    "[OUTCOME] resolved %s %s %s -> %+.2f%% (%s via %s)",
                    snap["symbol"], snap["side"],
                    (snap.get("source_name") or snap.get("source_chat_id")),
                    pnl_pct, reason, meta.get("resolution_method", "unknown"),
                )
                resolved.append(sid)
            except Exception as exc:
                logger.warning("[OUTCOME] resolve %s failed: %s", sid, exc)
        for sid in resolved:
            self._open.pop(sid, None)
        if resolved:
            self._save()
        return len(resolved)

    def open_count(self) -> int:
        return len(self._open)


# ---- module-level singleton ----
_TRACKER: Optional[SignalOutcomeTracker] = None


def get_outcome_tracker() -> SignalOutcomeTracker:
    global _TRACKER
    if _TRACKER is None:
        _TRACKER = SignalOutcomeTracker()
    return _TRACKER
