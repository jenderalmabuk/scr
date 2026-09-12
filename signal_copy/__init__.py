"""
signal_copy: Capture -> Validate -> Confirm -> Execute -> Monitor pipeline
for external trade-call signals (Telegram signal groups + Discord).

This package layers on top of the existing fusion infrastructure:
- execution/  (order routing, managed positions w/ partial TP + trailing)
- signals/    (OI/CVD micro tracker, sentiment)
- risk/       (position sizing)
- notifications/ (Telegram transport)

Public surface is intentionally small; the orchestrator wires everything.
"""

from .signal_schema import ParsedSignal, SignalSide, SignalSource

__all__ = ["ParsedSignal", "SignalSide", "SignalSource"]
