"""Append-only execution truth ledger keyed by signal ID."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

JOURNAL_PATH = Path(os.getenv(
    "SIGNAL_COPY_LIFECYCLE_JOURNAL",
    "runtime/state/signal_lifecycle.jsonl",
))
TERMINAL_STAGES = {
    "PARSE_REJECTED", "VALIDATION_REJECTED", "DUPLICATE", "ROUTE_REJECTED",
    "EXPIRED", "DRIFT_REJECTED", "THESIS_REJECTED", "GATEWAY_REJECTED",
    "POSITION_CONFIRMED", "CANCELLED", "CALIBRATION_ONLY", "NO_EXECUTOR",
}


def events(signal_id: str | None = None) -> list[dict]:
    try:
        rows = []
        for line in JOURNAL_PATH.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return [row for row in rows if row.get("signal_id") == signal_id] if signal_id else rows
    except FileNotFoundError:
        return []


def latest(signal_id: str) -> dict:
    state = {
        "signal_id": signal_id,
        "execution_intended": False,
        "gateway_accepted": False,
        "position_confirmed": False,
        "terminal": False,
    }
    for row in events(signal_id):
        state.update(row)
    return state


def record(signal_id: str, stage: str, **fields: Any) -> None:
    """Append lifecycle transition; preserve prior truth flags when omitted."""
    try:
        prior = latest(signal_id)
        row = {
            **prior,
            "ts": datetime.now(timezone.utc).isoformat(),
            "signal_id": signal_id,
            "stage": stage,
            **fields,
            "terminal": stage in TERMINAL_STAGES,
        }
        JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with JOURNAL_PATH.open("a") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except Exception as exc:
        raise RuntimeError(f"lifecycle persistence failed: {exc}") from exc
