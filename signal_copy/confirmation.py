"""
ConfirmationManager: holds validated signals awaiting a user yes/no decision.

Flow:
  register(result) -> token
  ... user taps a Telegram button or replies ...
  resolve(token, approved=True/False) -> PendingConfirmation (or None)

Pending entries auto-expire so a stale "Ya" cannot fire a trade much later
(price will have moved). Thread/async-safe via an asyncio.Lock.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional

from .validation_engine import ValidationResult
from utils.logger import logger


class ConfirmState(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"


@dataclass
class PendingConfirmation:
    token: str
    result: ValidationResult
    created_at: float = field(default_factory=time.time)
    state: ConfirmState = ConfirmState.PENDING
    expires_in: float = 3600.0         # 1h scalping default
    decided_at: Optional[float] = None
    note: str = ""

    @property
    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.expires_in

    @property
    def age_sec(self) -> float:
        return time.time() - self.created_at


class ConfirmationManager:
    def __init__(self, default_expiry_sec: float = 3600.0):
        self._pending: Dict[str, PendingConfirmation] = {}
        self._lock = asyncio.Lock()
        self.default_expiry_sec = default_expiry_sec

    async def register(self, result: ValidationResult, *, expires_in: Optional[float] = None) -> PendingConfirmation:
        token = result.signal.signal_id
        async with self._lock:
            pc = PendingConfirmation(
                token=token,
                result=result,
                expires_in=expires_in or self.default_expiry_sec,
            )
            self._pending[token] = pc
            logger.info("[SIGNAL_CONFIRM] registered %s (%s) await user decision",
                        token, result.signal.symbol)
            return pc

    async def get(self, token: str) -> Optional[PendingConfirmation]:
        async with self._lock:
            pc = self._pending.get(token)
            if pc and pc.state == ConfirmState.PENDING and pc.is_expired:
                pc.state = ConfirmState.EXPIRED
                pc.decided_at = time.time()
            return pc

    async def resolve(self, token: str, *, approved: bool, note: str = "") -> Optional[PendingConfirmation]:
        """Mark a pending confirmation approved/rejected. Returns it, or None if unknown."""
        async with self._lock:
            pc = self._pending.get(token)
            if pc is None:
                logger.warning("[SIGNAL_CONFIRM] resolve unknown token %s", token)
                return None
            if pc.state != ConfirmState.PENDING:
                logger.info("[SIGNAL_CONFIRM] token %s already %s", token, pc.state.value)
                return pc
            if pc.is_expired:
                pc.state = ConfirmState.EXPIRED
                pc.decided_at = time.time()
                logger.info("[SIGNAL_CONFIRM] token %s expired before decision", token)
                return pc
            pc.state = ConfirmState.APPROVED if approved else ConfirmState.REJECTED
            pc.decided_at = time.time()
            pc.note = note
            logger.info("[SIGNAL_CONFIRM] token %s -> %s", token, pc.state.value)
            return pc

    async def mark(self, token: str, state: ConfirmState, note: str = "") -> None:
        async with self._lock:
            pc = self._pending.get(token)
            if pc:
                pc.state = state
                pc.note = note

    async def sweep_expired(self) -> int:
        """Expire stale pendings; returns count expired."""
        n = 0
        async with self._lock:
            for pc in self._pending.values():
                if pc.state == ConfirmState.PENDING and pc.is_expired:
                    pc.state = ConfirmState.EXPIRED
                    pc.decided_at = time.time()
                    n += 1
        if n:
            logger.info("[SIGNAL_CONFIRM] swept %d expired confirmations", n)
        return n

    async def purge_finished(self, older_than_sec: float = 3600.0) -> int:
        """Drop terminal entries older than threshold to bound memory."""
        now = time.time()
        drop = []
        async with self._lock:
            for token, pc in self._pending.items():
                if pc.state != ConfirmState.PENDING and (now - (pc.decided_at or pc.created_at)) > older_than_sec:
                    drop.append(token)
            for token in drop:
                self._pending.pop(token, None)
        return len(drop)

    async def pending_tokens(self) -> list[str]:
        async with self._lock:
            return [t for t, pc in self._pending.items() if pc.state == ConfirmState.PENDING]
