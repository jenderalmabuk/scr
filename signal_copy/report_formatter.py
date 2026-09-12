"""
Formats validation results into rich Telegram messages (HTML), mirroring the
chart-style breakdown the user shared: setup, levels, RR, and a factor-by-factor
confluence report.
"""

from __future__ import annotations

import html

from .signal_schema import ParsedSignal
from .validation_engine import ValidationResult, Verdict

_RAW_CAP = 700  # keep total report under Telegram's 4096 limit


def _raw_block(sig: ParsedSignal) -> list[str]:
    """Render the original signal text as an expandable, HTML-safe quote.

    Lets you eyeball parser output vs. source without leaving Telegram.
    """
    raw = (getattr(sig, "raw_text", "") or "").strip()
    if not raw:
        return []
    truncated = len(raw) > _RAW_CAP
    safe = html.escape(raw[:_RAW_CAP])
    if truncated:
        safe += "\n…(dipotong)"
    # expandable blockquote collapses by default so the report stays compact
    return ["", f"📄 <b>SINYAL ASLI</b>\n<blockquote expandable>{safe}</blockquote>"]


def _g(v, fmt="{:g}", dash="-"):
    try:
        if v is None:
            return dash
        return fmt.format(v)
    except Exception:
        return dash


def _verdict_badge(verdict: Verdict) -> str:
    return {
        Verdict.VALID: "✅ <b>VALID — layak dieksekusi</b>",
        Verdict.WEAK: "⚠️ <b>WEAK — hati-hati, tidak auto-eksekusi</b>",
        Verdict.REJECT: "⛔ <b>REJECT — tidak disarankan</b>",
    }.get(verdict, str(verdict))


def build_validation_report(result: ValidationResult) -> str:
    sig: ParsedSignal = result.signal
    m = result.metrics_snapshot or {}

    tps = ", ".join(_g(t) for t in sig.take_profits) if sig.take_profits else "-"
    rr_tp1 = sig.rr_ratio(0)
    rr_best = sig.rr_best()

    lines = []
    lines.append("📡 <b>SINYAL MASUK</b>")
    lines.append(f"Sumber: <b>{sig.source_name or sig.source.value}</b> ({sig.source.value})")
    lines.append("")
    lines.append(f"🔹 Pair: <b>{sig.symbol}</b>")
    lines.append(f"🔹 Arah: <b>{sig.side.value}</b>")
    if sig.leverage:
        lines.append(f"🔹 Leverage (sinyal): {_g(sig.leverage)}x")
    lines.append(f"📍 Entry: <b>{_g(sig.entry_low)} - {_g(sig.entry_high)}</b>")
    tp_note = " <i>(improvisasi 1R/2R/3R)</i>" if getattr(sig, "tp_source", "signal") != "signal" else ""
    sl_note = " <i>(improvisasi ATR)</i>" if getattr(sig, "sl_source", "signal") != "signal" else ""
    lines.append(f"🎯 TP: {tps}{tp_note}")
    lines.append(f"⛔ SL: {_g(sig.stop_loss)}{sl_note}")
    if rr_tp1 is not None:
        lines.append(f"⚖️ RR: TP1 {_g(rr_tp1, '{:.2f}')} | terbaik {_g(rr_best, '{:.2f}')}")
    lines.append("")

    lines.append("🔎 <b>HASIL VALIDASI MENDALAM</b>")
    lines.append(f"Skor confluence: <b>{result.score:.1f}/100</b>")
    lines.append(_verdict_badge(result.verdict))
    lines.append("")

    lines.append("<b>Faktor:</b>")
    for f in result.factors:
        mark = "✅" if f.passed else "❌"
        lines.append(f"{mark} {f.name}: {f.detail}")
    lines.append("")

    # live market snapshot
    lines.append("<b>Data pasar saat ini:</b>")
    lines.append(
        f"• Harga: {_g(m.get('price'))} | RSI: {_g(m.get('rsi'), '{:.0f}')} | "
        f"ATR%: {_g(m.get('atr_pct'), '{:.2f}')}"
    )
    lines.append(
        f"• OI 15m: {_g(m.get('oi_change_15m_pct'), '{:+.2f}')}% | "
        f"OI 1h: {_g(m.get('oi_change_1h_pct'), '{:+.2f}')}%"
    )
    lines.append(
        f"• CVD z: {_g(m.get('cvd_zscore'), '{:+.2f}')} | "
        f"Imbalance: {_g(m.get('imbalance'), '{:+.2f}')} | "
        f"Funding: {_g(m.get('funding_rate'), '{:+.5f}')}"
    )
    lines.append(f"• Regime: {m.get('regime_label', '-')}")

    if result.hard_blocks:
        lines.append("")
        lines.append("⛔ <b>Hard blocks:</b>")
        for b in result.hard_blocks:
            lines.append(f"  • {b}")

    lines.extend(_raw_block(sig))

    return "\n".join(lines)


def build_chart_caption(result: ValidationResult) -> str:
    """Concise caption for the chart photo (Telegram caption limit is 1024)."""
    sig = result.signal
    tps = ", ".join(_g(t) for t in sig.take_profits) if sig.take_profits else "-"
    rr_best = sig.rr_best()
    badge = {
        Verdict.VALID: "✅ VALID",
        Verdict.WEAK: "⚠️ WEAK",
        Verdict.REJECT: "⛔ REJECT",
    }.get(result.verdict, str(result.verdict))
    lines = [
        f"📡 <b>{sig.symbol} {sig.side.value}</b> — {sig.source_name or sig.source.value}",
        f"{badge}  •  skor <b>{result.score:.0f}/100</b>",
        f"📍 Entry {_g(sig.entry_low)}-{_g(sig.entry_high)} | ⛔ SL {_g(sig.stop_loss)}",
        f"🎯 TP {tps}" + (f" | RR {rr_best:.2f}" if rr_best else ""),
    ]
    if result.verdict == Verdict.VALID:
        lines.append(f"\n❓ <b>Eksekusi sinyal ini?</b> (1% ekuitas)")
    return "\n".join(lines)


def build_confirmation_prompt(result: ValidationResult) -> str:
    """Short prompt appended under the report asking for a decision."""
    sig = result.signal
    return (
        f"\n\n❓ <b>Eksekusi sinyal {sig.symbol} {sig.side.value} ini?</b>\n"
        f"Balas /ya_{sig.signal_id} atau /tidak_{sig.signal_id}\n"
        f"(atau gunakan tombol di bawah)"
    )


def build_execution_result(symbol: str, side: str, ok: bool, reason: str,
                           entry: float = 0.0, notional: float = 0.0,
                           sl: float = 0.0, tp1: float = 0.0, tp_full: float = 0.0,
                           risk_amount: float = 0.0) -> str:
    if ok:
        return (
            f"🟢 <b>EKSEKUSI {symbol} {side}</b>\n"
            f"Entry: {_g(entry)} | Notional: ${_g(notional, '{:.2f}')}\n"
            f"SL: {_g(sl)} | TP1: {_g(tp1)} | TP akhir: {_g(tp_full)}\n"
            f"Risiko: ${_g(risk_amount, '{:.2f}')}\n"
            f"Posisi sekarang dikelola otomatis (partial TP + trailing + exit invalidasi)."
        )
    return f"🔴 <b>GAGAL EKSEKUSI {symbol} {side}</b>\nAlasan: {reason}"
