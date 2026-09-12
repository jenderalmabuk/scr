"""Append-only pending-limit lifecycle journal.

Pending setups live in memory (orchestrator._pending_limits) and vanish on
restart, so PENDING -> FILLED / EXPIRED / DRIFT / REJECTED could never be
proven after the fact. Every state change is appended here as one JSON line.

ponytail: append-only file, no rotation/compaction. -> add rotation when the
file passes a few hundred MB or when a query layer is needed.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

JOURNAL_PATH = Path(os.getenv("SIGNAL_COPY_PENDING_JOURNAL",
                              "runtime/signal_copy/pending_lifecycle.jsonl"))
logger = logging.getLogger(__name__)


def signal_from_row(row: dict):
    """Rebuild canonical signal from persisted pending geometry."""
    from .signal_schema import ParsedSignal, SignalSide, SignalSource
    return ParsedSignal(
        symbol=str(row.get("symbol") or ""),
        side=SignalSide(str(row.get("side") or "").upper()),
        entry_low=float(row.get("entry_low")),
        entry_high=float(row.get("entry_high")),
        stop_loss=float(row.get("stop_loss")),
        take_profits=[float(x) for x in (row.get("take_profits") or [])],
        active_entry=float(row["active_entry"]) if row.get("active_entry") is not None else None,
        source=SignalSource(str(row.get("source") or "TELEGRAM").upper()),
        source_name=str(row.get("source_name") or ""),
        source_chat_id=row.get("source_chat_id"),
        raw_text=str(row.get("raw_text") or ""),
        signal_id=str(row.get("signal_id") or ""),
        received_at=float(row.get("received_at") or row.get("created_epoch") or 0.0),
    )


def row_age_seconds(row: dict) -> float:
    """Age from original pending creation, with journal timestamp fallback."""
    import time
    created = row.get("created_epoch") or row.get("received_at")
    if created:
        return max(0.0, time.time() - float(created))
    try:
        ts = datetime.fromisoformat(str(row.get("ts") or "").replace("Z", "+00:00"))
        return max(0.0, time.time() - ts.timestamp())
    except Exception:
        return float("inf")


def load_latest_pending() -> list[dict]:
    """Return latest non-terminal PENDING/WAITING_FOR_SLOT row per signal."""
    try:
        latest: dict[str, dict] = {}
        if not JOURNAL_PATH.exists():
            return []
        for line_number, line in enumerate(JOURNAL_PATH.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning("Skipping corrupt pending journal line %d: %s", line_number, exc)
                continue
            signal_id = str(row.get("signal_id") or "")
            if signal_id:
                latest[signal_id] = row
        return [row for row in latest.values()
                if row.get("event") in {"PENDING", "WAITING_FOR_SLOT"}]
    except Exception:
        return []


def record(event: str, sig: Any, **fields) -> None:
    """Append one lifecycle event. Never raises — journaling must not block trading."""
    try:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "symbol": getattr(sig, "symbol", ""),
            "side": getattr(getattr(sig, "side", None), "value", ""),
            "source_chat_id": getattr(sig, "source_chat_id", None),
            "signal_id": getattr(sig, "signal_id", ""),
            "entry_low": getattr(sig, "entry_low", None),
            "entry_high": getattr(sig, "entry_high", None),
            "active_entry": getattr(sig, "active_entry", None),
            "stop_loss": getattr(sig, "stop_loss", None),
            "take_profits": list(getattr(sig, "take_profits", []) or []),
            "source": getattr(getattr(sig, "source", None), "value", "TELEGRAM"),
            "source_name": getattr(sig, "source_name", ""),
            "raw_text": getattr(sig, "raw_text", ""),
            "received_at": getattr(sig, "received_at", None),
            "created_epoch": fields.pop("created_epoch", datetime.now(timezone.utc).timestamp()),
            **fields,
        }
        JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with JOURNAL_PATH.open("a") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
        from . import signal_lifecycle
        gateway_accepted = bool(fields.get("gateway_accepted", event == "FILLED"))
        position_confirmed = bool(fields.get("position_confirmed", event == "FILLED"))
        stage = "POSITION_CONFIRMED" if position_confirmed else event
        signal_lifecycle.record(
            row["signal_id"], stage, symbol=row["symbol"], side=row["side"],
            gateway_accepted=gateway_accepted or position_confirmed,
            position_confirmed=position_confirmed,
            reason=fields.get("reason", ""),
            gateway_response=fields.get("gateway_response"),
        )
    except Exception:
        pass
