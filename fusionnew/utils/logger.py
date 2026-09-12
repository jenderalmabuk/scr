# utils/logger.py - final rotating logger with signal-noise filter
from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional


LOG_DIR = Path(os.getenv("LOG_DIR", "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE = LOG_DIR / "trading_bot.log"
MAX_LOG_BYTES = 50 * 1024 * 1024
BACKUP_COUNT = 3

_SIGNAL_PATTERN = re.compile(r"\[SIGNAL\]")
_SCORE_PATTERN = re.compile(r"Score:\s*([+-]?\d+(?:\.\d+)?)", re.IGNORECASE)


class SignalNoiseFilter(logging.Filter):
    """
    Keep normal logs, but suppress low-value [SIGNAL] lines unless abs(score) > 20.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True

        if not _SIGNAL_PATTERN.search(message):
            return True

        match = _SCORE_PATTERN.search(message)
        if not match:
            return True

        try:
            score = float(match.group(1))
        except (TypeError, ValueError):
            return True

        return abs(score) > 20.0


def _build_formatter() -> logging.Formatter:
    return logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _build_file_handler() -> RotatingFileHandler:
    handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=MAX_LOG_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(_build_formatter())
    handler.addFilter(SignalNoiseFilter())
    return handler


def _build_console_handler() -> logging.StreamHandler:
    handler = logging.StreamHandler()
    handler.setFormatter(_build_formatter())
    handler.addFilter(SignalNoiseFilter())
    return handler


def _configure_logger() -> logging.Logger:
    configured_logger = logging.getLogger("fusion_whale_hunter")
    if getattr(configured_logger, "_fusion_logger_configured", False):
        return configured_logger

    configured_logger.setLevel(logging.INFO)
    configured_logger.propagate = False

    for old_handler in list(configured_logger.handlers):
        configured_logger.removeHandler(old_handler)
        try:
            old_handler.close()
        except Exception:
            pass

    configured_logger.addHandler(_build_file_handler())
    configured_logger.addHandler(_build_console_handler())
    configured_logger._fusion_logger_configured = True
    return configured_logger


logger = _configure_logger()


def set_log_level(level: int) -> None:
    logger.setLevel(level)
    for handler in logger.handlers:
        handler.setLevel(level)


def get_logger(name: Optional[str] = None) -> logging.Logger:
    if not name:
        return logger
    child = logging.getLogger(f"fusion_whale_hunter.{name}")
    child.setLevel(logger.level)
    child.propagate = True
    return child
