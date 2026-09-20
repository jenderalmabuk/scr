from __future__ import annotations

import html
from typing import Any, Dict

from signal_copy.signal_copy_config import ADVERSARIAL_MODE, DRY_RUN

_RAW_CAP = 700  # keep total report under Telegram's 4096 limit


def format_price(price: float) -> str:
    if price is None or price == 0:
        return "0"
    price = float(price)
    if price < 0.0001:
        return f"{price:.8f}"
    if price < 0.001:
        return f"{price:.6f}"
    if price < 1:
        return f"{price:.4f}"
    if price < 100:
        return f"{price:.2f}"
    return f"{price:.0f}"


def get_tradingview_link(symbol: str, timeframe: str = "15m") -> str:
    """Generate TradingView link for symbol, defaulting to 15m with MA study loaded."""
    clean_symbol = str(symbol).strip().upper()
    if clean_symbol.endswith(".P"):
        tv_symbol = clean_symbol
    elif clean_symbol.endswith("USDT"):
        tv_symbol = f"{clean_symbol}.P"
    else:
        tv_symbol = f"{clean_symbol}USDT.P"
    interval = "15" if str(timeframe or "15m").lower() in {"15m", "15"} else str(timeframe).lower().rstrip("m")
    return f"https://www.tradingview.com/chart/?symbol=BINANCE:{tv_symbol}&interval={interval}&studies=MASimple@tv-basicstudies"


def _mode_footer() -> str:
    mode_str = "Testnet" if DRY_RUN else "LIVE"
    return f"Fusion Signal Copy • {mode_str}"


def _safe_float(payload: Dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        if key in payload and payload[key] is not None:
            try:
                return float(payload[key])
            except Exception:
                continue
    return default


def _safe_int(payload: Dict[str, Any], *keys: str, default: int = 0) -> int:
    for key in keys:
        if key in payload and payload[key] is not None:
            try:
                return int(payload[key])
            except Exception:
                continue
    return default


def _safe_str(payload: Dict[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip() != "":
            return str(value)
    return default


def _safe_bool(payload: Dict[str, Any], *keys: str, default: bool = False) -> bool:
    for key in keys:
        if key in payload and payload[key] is not None:
            value = payload[key]
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in {"1", "true", "yes", "on"}:
                return True
            if text in {"0", "false", "no", "off"}:
                return False
    return default


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "N/A"
    try:
        return f"{float(value):+.2f}%"
    except Exception:
        return "N/A"


def _priority_badge(payload: Dict[str, Any]) -> str:
    labels = []
    priority_score = _safe_float(payload, "priority_score", default=0.0)
    is_medium_priority = _safe_bool(payload, "is_medium_priority", default=False)

    if is_medium_priority:
        labels.append("⭐ MEDIUM PRIORITY")
    if priority_score >= 90:
        labels.append("🔥 HIGH PRIORITY")

    return "\n".join(labels)


def _build_vip_block(payload: Dict[str, Any]) -> str:
    vip_status = _safe_str(payload, "vip_status", default="NONE").upper()
    vip_bias = _safe_str(payload, "vip_directional_bias", default="NEUTRAL").upper()

    vip_line = f"⭐ <b>VIP</b>: {vip_status} • <b>{vip_bias}</b>"
    parts = [vip_line]

    precursor = _safe_str(payload, "precursor_state", default="").upper()
    precursor_score = _safe_float(payload, "precursor_score", default=0.0)
    if precursor:
        parts.append(f"🕵️ <b>Precursor</b>: <b>{precursor}</b> • Score <b>{precursor_score:.1f}</b>")

    vip_note = _safe_str(payload, "vip_note", "vip_reason", default="")
    if vip_note:
        parts.append(f"📝 <b>{vip_note}</b>")

    return "\n".join(parts)


def _build_silent_accumulation_block(payload: Dict[str, Any]) -> str:
    silent_status = _safe_str(
        payload,
        "silent_accumulation_status",
        "sa_status",
        default="",
    ).upper()
    if not silent_status:
        return ""

    score = _safe_float(
        payload,
        "silent_accumulation_score",
        "sa_score",
        default=0.0,
    )
    price_range_pct = _safe_float(
        payload,
        "silent_accumulation_range_pct",
        "sa_range_pct",
        default=0.0,
    )
    vol_vs_baseline = _safe_float(
        payload,
        "silent_accumulation_vol_vs_baseline",
        "sa_vol_vs_baseline",
        default=0.0,
    )
    bullish_close = _safe_float(
        payload,
        "silent_accumulation_bullish_close_pct",
        "sa_bullish_close_pct",
        default=0.0,
    )
    lower_rejection = _safe_float(
        payload,
        "silent_accumulation_lower_rejection_pct",
        "sa_lower_rejection_pct",
        default=0.0,
    )
    reason = _safe_str(
        payload,
        "silent_accumulation_reason",
        "sa_reason",
        default="NONE",
    )

    return "\n".join(
        [
            f"🕵️ Silent Accumulation: <b>{silent_status}</b> • Score {score:.1f}",
            f"📦 Range Compression: {price_range_pct:.2f}%",
            f"🔊 Vol vs Baseline: {vol_vs_baseline:.2f}x",
            f"🧲 Bullish Close: {bullish_close * 100:.0f}% | Lower Rejection: {lower_rejection * 100:.0f}%",
            f"📝 <code>{reason}</code>",
        ]
    )


def _btc_weight_value(payload: Dict[str, Any]) -> float:
    return _safe_float(payload, "btc_weight", "btc_influence_weight", default=0.50)


def _btc_weight_label(payload: Dict[str, Any], weight: float | None = None) -> str:
    explicit = _safe_str(
        payload,
        "btc_weight_label",
        "btc_corr_regime_note",
        default="",
    ).upper()
    if explicit:
        mapping = {
            "FOLLOWING_BTC": "FOLLOWING_BTC",
            "FOLLOWING": "FOLLOWING_BTC",
            "PARTIAL_DECOUPLE": "PARTIAL_DECOUPLE",
            "PARTIAL_DECOUPLED": "PARTIAL_DECOUPLE",
            "DECOUPLED": "DECOUPLED",
            "NEGATIVE_RELATION": "DECOUPLED",
            "SELF_BTC": "FOLLOWING_BTC",
            "BTC_CORR_UNAVAILABLE": "NEUTRAL",
            "UNKNOWN": "NEUTRAL",
        }
        return mapping.get(explicit, explicit)

    w = _btc_weight_value(payload) if weight is None else float(weight)
    if w >= 0.70:
        return "FOLLOWING_BTC"
    if w >= 0.40:
        return "PARTIAL_DECOUPLE"
    if w <= 0.15:
        return "DECOUPLED"
    return "NEUTRAL"


def _normalize_btc_context(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if "BEAR" in raw:
        return "BEAR_CONFIRMED"
    if "BULL" in raw:
        return "BULL_CONFIRMED"
    return "NEUTRAL"


def _btc_context_label(payload: Dict[str, Any]) -> str:
    raw = _safe_str(
        payload,
        "btc_context",
        "btc_fast_state",
        "btc_context_state",
        "btc_bias_macro",
        "btc_bias",
        default="NEUTRAL",
    )
    return _normalize_btc_context(raw)


def _build_committee_block(committee: Dict[str, Any]) -> list[str]:
    mode = _safe_str(committee, "mode", default="shadow").lower()
    if mode in {"shadow", "off"}:
        return []
    final_vote = _safe_str(committee, "final_vote", default="UNKNOWN").upper()
    action = _safe_str(committee, "action", default="NONE").upper()
    score = _safe_float(committee, "score", default=0.0)
    no_votes = _safe_int(committee, "no_votes", default=0)
    warn_votes = _safe_int(committee, "warn_votes", default=0)
    lines = [
        "",
        f"<b>COMMITTEE: {html.escape(final_vote)}</b> ({html.escape(mode)})",
        f"   Risk: {score:.0f}/100 | NO: {no_votes} | WARN: {warn_votes}",
    ]
    reasons = committee.get("top_reasons") or []
    if reasons:
        lines.append("   Top reasons:")
        for reason in reasons[:3]:
            lines.append(f"      • {html.escape(str(reason)[:180])}")
    lines.append(f"   Action: {html.escape(action)}")
    return lines


def build_parser_report(
    sig: Any,
    result: Any,
    cls: Any = None,
    source_name: str = "",
    calib: bool = False,
    adversarial_verdict: str = "",
    committee: Dict[str, Any] | None = None,
) -> str:
    """Build ONE consolidated report: parse + validation + adversarial."""
    symbol = getattr(sig, "symbol", "UNKNOWN").upper()
    side = getattr(sig, "side", "UNKNOWN")
    side_str = side.value if hasattr(side, "value") else str(side).upper()

    entry_low = getattr(sig, "entry_low", 0.0)
    entry_high = getattr(sig, "entry_high", 0.0)
    entry_mid = getattr(sig, "rr_entry", getattr(sig, "entry_mid", 0.0))
    stop_loss = getattr(sig, "stop_loss", 0.0)
    take_profits = getattr(sig, "take_profits", [])
    leverage = getattr(sig, "leverage", 0.0)
    risk_pct = getattr(sig, "risk_pct", 0.0)
    timeframe = getattr(sig, "timeframe", "") or "-"

    verdict = result.verdict.value if hasattr(result.verdict, "value") else str(result.verdict)
    score = getattr(result, "score", 0.0)

    # Metrics
    m = getattr(result, "metrics_snapshot", {}) or getattr(result, "metrics", {}) or {}
    metrics = m
    price = metrics.get("price", 0.0)
    rsi = metrics.get("rsi", 0.0)
    cvd = metrics.get("cvd", metrics.get("cvd_zscore", 0.0))
    oi_5m = metrics.get("oi_change_5m_pct")
    oi_15m = metrics.get("oi_change_15m_pct")
    oi_1h = metrics.get("oi_change_1h_pct")
    oi_source = metrics.get("oi_source")
    oi_now = metrics.get("oi_now")
    qvol_5m = metrics.get("qvol_5m")
    flow_direction = metrics.get("flow_direction")
    flow_source = metrics.get("flow_source")
    data_quality = metrics.get("data_quality")
    data_stale = metrics.get("data_stale")
    funding = metrics.get("funding_rate", 0.0)
    poc = metrics.get("poc", 0.0)
    vol_ratio = metrics.get("vol_ratio", 0.0)
    regime = metrics.get("regime_label", "UNKNOWN")
    btc_corr = metrics.get("btc_correlation", 0.0)
    btc_bias = metrics.get("btc_bias", "NEUTRAL")
    mtf = metrics.get("mtf_alignment", {})
    mtf_score = mtf.get("score", 0.0) if isinstance(mtf, dict) else 0.0
    tv = metrics.get("tradingview", {})
    tv_score = tv.get("score", 0.0) if isinstance(tv, dict) else 0.0

    icon = "🟢" if side_str == "LONG" else "🔴"
    verdict_data = {
        "VALID": ("✅", "VALID"),
        "WEAK": ("⚠️", "WEAK"),
        "REJECT": ("❌", "REJECT"),
    }
    v_icon, v_label = verdict_data.get(verdict, ("❓", verdict))

    # Format entry range
    if entry_low and entry_high:
        entry_str = f"{format_price(entry_low)} — {format_price(entry_high)}"
    else:
        entry_str = format_price(entry_mid)

    # Format TPs dengan numbering
    if take_profits:
        tp_lines = []
        num_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣"]
        for i, tp in enumerate(take_profits):
            num = num_emojis[i] if i < len(num_emojis) else f"{i+1}."
            tp_lines.append(f"   {num} {format_price(tp)}")
        tp_str = "\n".join(tp_lines)
    else:
        tp_str = "   N/A"

    # Risk/Reward
    rr_str = "N/A"
    if entry_mid > 0 and stop_loss > 0:
        risk = abs(entry_mid - stop_loss)
        if risk > 0:
            tp1 = take_profits[0] if take_profits else 0.0
            tp_full = take_profits[-1] if len(take_profits) > 1 else (take_profits[0] if take_profits else 0.0)
            rr1 = abs(tp1 - entry_mid) / risk if tp1 > 0 else 0
            rr_full = abs(tp_full - entry_mid) / risk if tp_full > 0 else 0
            rr_str = f"TP1: {rr1:.2f}R | Full: {rr_full:.2f}R"

    tv_link = get_tradingview_link(symbol, timeframe or "15m")

    # Hard blocks
    hard_blocks = getattr(result, "hard_blocks", []) or []

    # Build consolidated message
    lines = []

    # Header: calib banner or badge
    if calib:
        lines.append("🧪 <b>KALIBRASI MODE</b>")
        lines.append("━" * 20)

    # Section 1: Signal summary
    lines.append(f"{v_icon} <b>{v_label}</b> │ {icon} <b>{side_str}</b> │ <b>{symbol}</b>")
    lines.append(f"📡 Sumber: <i>{source_name or 'Unknown'}</i>")
    lines.append("")
    lines.append("📋 <b>DETAIL SINYAL</b>")
    lines.append(f"   💰 Entry: <b>{entry_str}</b>")
    lines.append(f"   🛑 SL: <b>{format_price(stop_loss)}</b>")
    lines.append(f"   🎯 TP:")
    lines.append(tp_str)
    lines.append(f"   ⚙️ Leverage: {(leverage or 0):.0f}x | TF: {timeframe or '-'}")
    lines.append(f"   ⚖️ RR: {rr_str}")
    lines.append("")

    # Section 2: Validation score
    lines.append("📊 <b>HASIL VALIDASI</b>")
    lines.append(f"   Skor: <b>{score:+.1f}/100</b>")
    dynamic_risk = metrics.get("dynamic_risk_usd")
    sizing_tier = metrics.get("sizing_tier")
    if dynamic_risk is not None:
        tier_label = {
            "TIER_1_HIGH": "Tier 1: High Conviction",
            "TIER_2_MODERATE": "Tier 2: Moderate",
            "TIER_3_PROBE": "Tier 3: Probe/Low",
            "VETO": "VETO (0% - Fakeout/Toxic)",
        }.get(sizing_tier, str(sizing_tier))
        lines.append(f"   💰 Alokasi Risk: <b>${dynamic_risk:.2f}</b> ({tier_label})")
    entry_action = metrics.get("entry_action_preview") or metrics.get("entry_action")
    entry_code = metrics.get("entry_route_code_preview") or metrics.get("entry_route_code")
    entry_conflicts = metrics.get("entry_route_conflicts_preview") or metrics.get("entry_route_conflicts") or []
    if entry_action:
        lines.append(f"   Entry action: <b>{html.escape(str(entry_action))}</b> ({html.escape(str(entry_code or '-'))})")
        if entry_conflicts:
            lines.append("   Konflik entry: " + ", ".join(html.escape(str(x)) for x in entry_conflicts[:3]))
    if mtf_score:
        lines.append(f"   MTF: {mtf_score:.0f}/100 | TV: {tv_score:.0f}/100")
    if hard_blocks:
        lines.append("   ⛔ Block:")
        for b in hard_blocks:
            lines.append(f"      • {b}")
    lines.append("")

    # Section 3: Market data (only show fields with actual data)
    lines.append("📈 <b>DATA PASAR</b>")
    lines.append(f"   Harga: {format_price(price)} | RSI: {rsi:.1f} | Regime: {regime}")
    cvd_val = cvd if abs(cvd) > 0.001 else 0.0
    lines.append(f"   CVD z: {cvd_val:+.2f} {'🟢' if cvd_val >= 0 else '🔴'}")
    lines.append(f"   OI 5m/15m/1h: {_fmt_pct(oi_5m)}/{_fmt_pct(oi_15m)}/{_fmt_pct(oi_1h)}")
    if oi_source or oi_now:
        oi_bits = []
        if oi_now:
            oi_bits.append(f"now {float(oi_now):,.0f}")
        if oi_source:
            oi_bits.append(str(oi_source))
        lines.append(f"   OI Source: {' | '.join(oi_bits)}")
    if flow_direction or qvol_5m or flow_source:
        flow_bits = []
        if flow_direction:
            flow_bits.append(str(flow_direction))
        if qvol_5m:
            flow_bits.append(f"qVol5m ${float(qvol_5m):,.0f}")
        if flow_source:
            flow_bits.append(str(flow_source))
        if data_quality:
            flow_bits.append(str(data_quality))
        if data_stale is not None:
            flow_bits.append("STALE" if data_stale else "fresh")
        lines.append(f"   Flow: {' | '.join(flow_bits)}")
    if abs(funding) > 0:
        lines.append(f"   Funding: {funding:+.4f}%")
    if vol_ratio:
        lines.append(f"   VolRatio: {vol_ratio:.2f}")
    if btc_bias and btc_bias != "NEUTRAL":
        lines.append(f"   BTC Bias: {btc_bias} | Corr: {btc_corr:.2f}")
    elif btc_corr:
        lines.append(f"   BTC Corr: {btc_corr:.2f}")

    # Section 4: Adversarial (if any). Legacy adversarial only runs after the
    # deterministic validator says VALID. In off mode it is advisory-only, so do
    # not imply it opened/blocked anything; calibration mode also never executes.
    if adversarial_verdict:
        lines.append("")
        adv_mode = str(ADVERSARIAL_MODE or "off").lower()
        if adv_mode == "off":
            lines.append("⚠️ <b>ADVERSARIAL: NO — ADVISORY ONLY</b>")
            lines.append("   Mode off: catatan risiko saja, tidak mengubah verdict/entry.")
        elif verdict == "VALID":
            lines.append("⚠️ <b>ADVERSARIAL: NO — DI-OVERRIDE</b>")
            if calib:
                lines.append("   Skor ≥ floor (soft-mode), tapi ini KALIBRASI: tidak ada eksekusi.")
            else:
                lines.append("   Skor ≥ floor (soft-mode): setup high-conviction, entry tetap diizinkan.")
        elif verdict == "WEAK":
            lines.append("🚫 <b>ADVERSARIAL: NO → downgrade WEAK — TIDAK dieksekusi</b>")
        else:
            lines.append("🚫 <b>ADVERSARIAL: REJECT — entry DIBLOKIR</b>")
        lines.append(f"   {adversarial_verdict[:300]}")

    if committee:
        lines.extend(_build_committee_block(committee))

    # Section 5: Calib note
    if calib and verdict == "VALID" and not adversarial_verdict:
        lines.append("")
        lines.append("✅ <b>VALID — kalibrasi mode, tidak ada eksekusi</b>")

    # Section 6: original signal text (expandable, HTML-safe) for quick debug
    raw = (getattr(sig, "raw_text", "") or "").strip()
    if raw:
        truncated = len(raw) > _RAW_CAP
        safe = html.escape(raw[:_RAW_CAP])
        if truncated:
            safe += "\n…(dipotong)"
        lines.append("")
        lines.append(f"📄 <b>SINYAL ASLI</b>\n<blockquote expandable>{safe}</blockquote>")

    # Footer
    lines.append("━" * 20)
    lines.append(f'🔗 <a href="{tv_link}">📊 Chart TradingView 15m + SMA21</a>')
    lines.append(_mode_footer())

    return "\n".join(lines)


def build_execution_message(
    outcome: Any,
    sig: Any,
    result: Any,
) -> str:
    """Build execution result message (entry/TP/SL) for the trades channel."""
    symbol = getattr(sig, "symbol", "UNKNOWN").upper()
    side = getattr(sig, "side", "UNKNOWN")
    side_str = side.value if hasattr(side, "value") else str(side).upper()

    icon = "🟢" if side_str == "LONG" else "🔴"
    ok = bool(getattr(outcome, "position_confirmed", False))
    reason = getattr(outcome, "reason", "UNKNOWN")

    entry_price = getattr(outcome, "entry_price", 0.0)
    notional = getattr(outcome, "notional", 0.0)
    sl_price = getattr(outcome, "sl_price", 0.0)
    tp1 = getattr(outcome, "tp1", 0.0)
    tp_full = getattr(outcome, "tp_full", 0.0)
    risk_amount = getattr(outcome, "risk_amount", 0.0)
    raw = getattr(outcome, "raw", None) or {}
    qty = float(raw.get("qty", 0) or 0)
    entry_fee = float(raw.get("entry_fee", 0) or 0)
    fill_risk = float(raw.get("max_loss_usd_at_fill", risk_amount) or 0)
    tp_ladder = list(getattr(sig, "take_profits", None) or [])

    if ok:
        header = f"✅ <b>FILLED / POSITION CONFIRMED</b> {icon} {side_str} {symbol}"
    else:
        header = f"✅ VALID tapi ❌ <b>NOT EXECUTED / NOT FILLED</b> {icon} {side_str} {symbol}"

    tv_link = get_tradingview_link(symbol)

    lines = [
        header,
        f"💰 Entry: {format_price(entry_price)}",
        "🎯 " + (" | ".join(f"TP{i}: {format_price(tp)}" for i, tp in enumerate(tp_ladder, 1))
                   or f"TP1: {format_price(tp1)} | Full: {format_price(tp_full)}"),
        f"🛑 SL: {format_price(sl_price)}",
        f"📊 Qty: {qty:.8g} | Notional: ${notional:.0f}",
        f"💸 Entry Fee: ${entry_fee:.2f} | Actual-fill Risk: ${fill_risk:.2f}",
        (f"📐 Drift: {getattr(outcome, 'entry_drift_r', 0.0):.2f}R | RR1: {getattr(outcome, 'rr_tp1', 0.0):.2f}R | RR Full: {getattr(outcome, 'rr_full', 0.0):.2f}R" if ok else ""),
        f"📌 Status: {reason}",
    ]

    if not ok:
        lines.append(f"❗ Reason: {reason}")

    lines.extend([
        "────────────────────",
        f'🔗 <a href="{tv_link}">📊 Lihat Chart TradingView 15m + SMA21</a>',
        "────────────────────",
        _mode_footer(),
    ])

    return "\n".join(lines)


def build_close_message(payload: Dict[str, Any]) -> str:
    """Build position close message (TP/SL/manual) for the trades channel."""
    symbol = _safe_str(payload, "symbol", default="UNKNOWN").upper()
    side = _safe_str(payload, "side", "direction", default="LONG").upper()
    reason = _safe_str(payload, "reason", default="UNKNOWN")
    normalized_reason = _safe_str(payload, "normalized_reason", default=reason)
    raw_reason = _safe_str(payload, "raw_engine_reason", "raw_reason", default="")

    exit_price = _safe_float(payload, "exit_price", "price", default=0.0)
    pnl_pct = _safe_float(payload, "pnl_pct", default=0.0)
    pnl_amount = _safe_float(payload, "pnl_amount", "pnl_usd", default=0.0)
    hold_minutes = _safe_float(payload, "hold_minutes", default=0.0)
    equity = _safe_float(payload, "equity", "balance_after", default=0.0)

    partial = _safe_bool(payload, "is_partial", default=False)
    partial_fraction = _safe_float(payload, "partial_fraction", default=0.0)

    original_sl = _safe_float(payload, "original_sl", "sl_original", default=0.0)
    active_stop = _safe_float(payload, "active_stop", "active_sl_at_exit", default=0.0)
    stop_kind = _safe_str(payload, "stop_kind", "sl_kind_at_exit", default="").upper()

    icon = "🟢" if side == "LONG" else "🔴"

    if pnl_pct > 0:
        header_icon = "✅"
    elif pnl_pct < 0:
        header_icon = "❌"
    else:
        header_icon = "✅"

    if partial:
        pct = partial_fraction * 100.0
        suffix = f" {pct:.0f}%" if pct > 0 else ""
        title = f"{header_icon} <b>TP PARTIAL{suffix}</b> {icon} {side} {symbol}"
    else:
        title = f"{header_icon} <b>CLOSE</b> {icon} {side} {symbol}"

    lines = [
        title,
        f"💰 Entry: {format_price(_safe_float(payload, 'entry_price', default=0.0))}",
        f"💰 Exit: {format_price(exit_price)}",
        f"📦 Qty: {_safe_float(payload, 'qty', default=0.0):.8g}",
        f"📊 PnL: <b>{pnl_pct:+.2f}%</b> (${pnl_amount:+.2f})",
        f"⏱️ Hold: {hold_minutes:.1f} min",
        f"💼 Equity: ${equity:.2f}",
        f"📌 Reason: {normalized_reason}",
    ]

    if partial:
        lines.extend([f"📦 Remaining: {_safe_float(payload, 'remaining_qty', default=0.0):.8g}",
                      f"🎯 Next TP: {format_price(_safe_float(payload, 'next_tp', default=0.0))}"])
    else:
        lines.append(f"📊 Notional: ${_safe_float(payload, 'notional_usd', default=0.0):.2f}")
    lines.append(f"📐 RR: {_safe_float(payload, 'rr', default=0.0):.2f}R | Fee: ${_safe_float(payload, 'fee_usd', default=0.0):.2f}")

    if original_sl > 0:
        lines.append(f"🛑 Original SL: {format_price(original_sl)}")
    if active_stop > 0:
        stop_label = stop_kind if stop_kind else "ACTIVE"
        lines.append(f"🎯 Active Stop at Exit: {format_price(active_stop)} ({stop_label})")
    if raw_reason and raw_reason != normalized_reason:
        lines.append(f"🧾 Raw Engine Reason: {raw_reason}")

    tv_link = get_tradingview_link(symbol)
    lines.extend([
        "────────────────────",
        f'🔗 <a href="{tv_link}">📊 Lihat Chart TradingView 15m + SMA21</a>',
        "────────────────────",
        _safe_str(payload, "footer", default="") or _mode_footer(),
    ])

    return "\n".join(lines)


def build_whale_alert_message(payload: Dict[str, Any]) -> str:
    title = _safe_str(payload, "title", default="WHALE ALERT")
    symbol = _safe_str(payload, "symbol", default="UNKNOWN").upper()
    side = _safe_str(payload, "side", "direction", default="UNKNOWN").upper()
    size_usd = _safe_float(payload, "size_usd", "notional", default=0.0)
    price = _safe_float(payload, "price", "entry_price", default=0.0)
    message = _safe_str(payload, "message", default="")
    tv_link = get_tradingview_link(symbol)

    lines = [
        f"🐋 <b>{title}</b>",
        f"📊 Symbol: {symbol}",
    ]

    if side and side != "UNKNOWN":
        lines.append(f"🧭 Side: {side}")
    if size_usd > 0:
        lines.append(f"💰 Size: ${size_usd:,.0f}")
    if price > 0:
        lines.append(f"🏷️ Price: {format_price(price)}")
    if message:
        lines.append(message)

    lines.extend([
        f'🔗 <a href="{tv_link}">📊 Lihat Chart TradingView 15m + SMA21</a>',
        "────────────────────",
        _mode_footer(),
    ])
    return "\n".join(lines)


__all__ = [
    "format_price",
    "get_tradingview_link",
    "build_parser_report",
    "build_execution_message",
    "build_close_message",
    "build_whale_alert_message",
]