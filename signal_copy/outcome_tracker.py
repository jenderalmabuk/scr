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
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from utils.logger import logger
from .channel_performance import get_tracker
from .signal_schema import ParsedSignal, SignalSide
from . import signal_copy_config as scfg

STORE = Path(os.getenv("SIGNAL_COPY_OUTCOME_STORE", "data/signal_outcomes.json"))

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
                "execution_intended": bool(execution_intended),
                "gateway_accepted": False,
                "position_confirmed": False,
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

    def _resolve_one(self, snap: dict, price: float) -> Optional[tuple]:
        """Return (pnl_pct, exit_reason) if resolved, else None."""
        side = snap["side"]
        entry = snap["entry"]
        sl = snap["sl"]
        tp1 = snap["tp1"]
        if price > 0:
            if side == "LONG":
                if price >= tp1:
                    return self._pnl_pct(side, entry, tp1), "TP1"
                if price <= sl:
                    return self._pnl_pct(side, entry, sl), "SL"
            else:  # SHORT
                if price <= tp1:
                    return self._pnl_pct(side, entry, tp1), "TP1"
                if price >= sl:
                    return self._pnl_pct(side, entry, sl), "SL"
        age = time.time() - snap.get("created_at", 0)
        if age >= snap.get("expiry_sec", _DEFAULT_EXPIRY_SEC):
            # mark-to-market at expiry (or entry if no price)
            mtm = price if price > 0 else entry
            return self._pnl_pct(side, entry, mtm), "EXPIRE"
        return None

    async def resolve_open(
        self, price_fn: Callable[[str], Awaitable[float]]
    ) -> int:
        """Poll live price for every open snapshot and record resolved ones.

        price_fn(symbol) -> current price (0.0 if unavailable). Returns the
        number of signals resolved this pass.
        """
        if not self._open:
            return 0
        tracker = get_tracker()
        resolved: List[str] = []
        # snapshot keys first (dict mutated on resolve)
        for sid, snap in list(self._open.items()):
            try:
                price = 0.0
                try:
                    price = float(await price_fn(snap["symbol"]) or 0.0)
                except Exception as exc:
                    logger.debug("[OUTCOME] price fetch %s failed: %s",
                                 snap["symbol"], exc)
                out = self._resolve_one(snap, price)
                if out is None:
                    continue
                pnl_pct, reason = out
                tracker.record_trade(
                    source_chat_id=snap.get("source_chat_id") or 0,
                    source_name=snap.get("source_name") or "",
                    symbol=snap.get("symbol") or "",
                    pnl_pct=pnl_pct,
                    exit_reason=reason,
                )
                logger.info(
                    "[OUTCOME] resolved %s %s %s -> %+.2f%% (%s)",
                    snap["symbol"], snap["side"],
                    (snap.get("source_name") or snap.get("source_chat_id")),
                    pnl_pct, reason,
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
