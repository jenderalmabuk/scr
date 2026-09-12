"""
Human-readable "read report" for an ingested message.

Shows exactly what the bot understood from a message: the routing class, and —
for trade signals — every field with its value and where it came from
(signal text vs improvised). Used for logs and (optionally) Telegram so the
parsing can be calibrated and trusted.
"""

from __future__ import annotations

from typing import Optional

from .classifier import ClassifyResult
from .signal_schema import ParsedSignal


def _g(v: Optional[float]) -> str:
    return f"{v:g}" if isinstance(v, (int, float)) else "-"


def build_read_report(text: str, cls: ClassifyResult,
                      sig: Optional[ParsedSignal] = None) -> str:
    """Return a compact, detailed read of a message (plain text, log-friendly)."""
    head = f"[READ {cls.label} conf={cls.confidence:.2f}] reasons={','.join(cls.reasons) or '-'}"
    if sig is None:
        preview = " ".join((text or "").split())[:120]
        return f"{head} :: {preview}"

    entry = (f"{_g(sig.entry_low)}-{_g(sig.entry_high)}"
             if sig.entry_low != sig.entry_high else _g(sig.entry_low))
    tps = ", ".join(_g(t) for t in sig.take_profits) if sig.take_profits else "-"
    lines = [
        head,
        f"  pair={sig.symbol or '-'} side={getattr(sig.side, 'value', '-')}",
        f"  entry={entry} type={sig.entry_type}",
        f"  sl={_g(sig.stop_loss)} ({sig.sl_source})",
        f"  tp=[{tps}] ({sig.tp_source}) n={len(sig.take_profits)}",
        f"  leverage={_g(sig.leverage)} timeframe={sig.timeframe or '-'}",
        f"  source={sig.source_name or sig.source.value}",
    ]
    return "\n".join(lines)


def build_read_report_html(text: str, cls: ClassifyResult,
                           sig: Optional[ParsedSignal] = None) -> str:
    """Telegram-friendly (HTML) version for calibration sessions."""
    if sig is None:
        preview = " ".join((text or "").split())[:160]
        return (f"🔎 <b>BACA</b> [{cls.label} • {cls.confidence:.0%}]\n"
                f"<i>{preview}</i>")
    entry = (f"{_g(sig.entry_low)}-{_g(sig.entry_high)}"
             if sig.entry_low != sig.entry_high else _g(sig.entry_low))
    tps = ", ".join(_g(t) for t in sig.take_profits) if sig.take_profits else "-"
    return (
        f"🔎 <b>BACA</b> [{cls.label} • {cls.confidence:.0%}]\n"
        f"• Pair: <b>{sig.symbol or '-'}</b>  Arah: <b>{getattr(sig.side,'value','-')}</b>\n"
        f"• Entry: {entry} ({sig.entry_type})\n"
        f"• SL: {_g(sig.stop_loss)} ({sig.sl_source})\n"
        f"• TP: {tps} ({sig.tp_source})\n"
        f"• Leverage: {_g(sig.leverage)}  TF: {sig.timeframe or '-'}\n"
        f"• Sumber: {sig.source_name or sig.source.value}"
    )
