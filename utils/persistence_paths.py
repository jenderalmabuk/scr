from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

JOURNAL_DIR = Path(os.environ.get("BOT_JOURNAL_DIR", "journal"))
JOURNAL_DIR.mkdir(parents=True, exist_ok=True)

TRADE_HISTORY_CSV_PATH = JOURNAL_DIR / "trade_history.csv"
TRADE_HISTORY_JSON_PATH = JOURNAL_DIR / "trade_history.json"
ML_HISTORY_JSONL_PATH = JOURNAL_DIR / "ml_history.jsonl"
ADAPTIVE_ENGINE_BACKUP_PATH = JOURNAL_DIR / "adaptive_engine_state.json"
ML_RESTORE_MARKER_PATH = JOURNAL_DIR / ".ml_restore.marker"


def ensure_parent(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    ensure_parent(path)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)
        except OSError:
            pass


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def load_json(path: Path, default: Any = None) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


def append_jsonl(path: Path, row: dict) -> None:
    ensure_parent(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False))
        handle.write("\n")


def read_jsonl_tail(path: Path, limit: int = 5000) -> list[dict]:
    if limit <= 0 or not path.exists():
        return []
    rows: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
        return rows[-limit:]
    except Exception:
        return []


def dedupe_rows(rows: Iterable[dict], *, key_fields: tuple[str, ...]) -> list[dict]:
    seen: set[str] = set()
    result: list[dict] = []
    for row in rows:
        key = "|".join(str(row.get(field, "")) for field in key_fields)
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result
