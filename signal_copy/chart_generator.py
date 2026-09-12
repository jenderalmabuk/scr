"""
Render an annotated analysis chart (PNG) for a validated signal.

Produces an image similar to the analysis cards used in signal channels:
- 15m candlesticks with SMA21
- Entry zone band (green/red by side), Stop Loss line, Take-Profit lines
- Current price marker
- RSI sub-panel
- Verdict + confluence score + factor checklist side panel

Klines are fetched directly from Binance USDⓈ-M futures (no extra coupling).
Falls back gracefully (returns None) if matplotlib or data is unavailable, so
the pipeline still works in text-only mode.
"""

from __future__ import annotations

import os
import tempfile
import time
from typing import List, Optional

from utils.logger import logger
from .signal_schema import ParsedSignal, SignalSide
from .validation_engine import ValidationResult, Verdict

try:
    import httpx
    _HTTPX = True
except Exception:
    _HTTPX = False

_BINANCE_FAPI = "https://fapi.binance.com"


def _ma(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    s = sum(values[:period])
    out[period - 1] = s / period
    for i in range(period, len(values)):
        s += values[i] - values[i - period]
        out[i] = s / period
    return out


def _rsi(closes: List[float], period: int = 14) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / period, losses / period
    out[period] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1 + avg_g / avg_l)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0.0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0.0)) / period
        out[i] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1 + avg_g / avg_l)
    return out


async def _fetch_klines(symbol: str, interval: str = "15m", limit: int = 96):
    if not _HTTPX:
        return None
    url = f"{_BINANCE_FAPI}/fapi/v1/klines"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, params={"symbol": symbol, "interval": interval, "limit": limit})
            r.raise_for_status()
            rows = r.json()
        # each: [openTime, open, high, low, close, volume, ...]
        return [
            (int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5]))
            for x in rows
        ]
    except Exception as exc:
        logger.warning("[CHART] kline fetch failed %s: %s", symbol, exc)
        return None


async def build_chart(result: ValidationResult, *, out_dir: Optional[str] = None,
                      klines: Optional[list] = None) -> Optional[str]:
    """Render the analysis chart PNG. Returns the file path, or None on failure.

    klines: optional pre-fetched OHLCV rows
            [(openTimeMs, open, high, low, close, volume), ...]. If omitted,
            they are fetched live from Binance futures at the signal's timeframe
            (default 15m).
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle
        from datetime import datetime, timezone
    except Exception as exc:
        logger.info("[CHART] matplotlib unavailable (%s); skipping image", exc)
        return None

    sig: ParsedSignal = result.signal
    if klines is None:
        interval = "15m"
        try:
            from .timeframe import binance_interval
            interval = binance_interval(getattr(sig, "timeframe", None)) or "15m"
        except Exception:
            interval = "15m"
        klines = await _fetch_klines(sig.symbol, interval=interval)
    if not klines or len(klines) < 20:
        logger.info("[CHART] not enough kline data for %s; skipping image", sig.symbol)
        return None

    times = [datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc) for k in klines]
    opens = [k[1] for k in klines]
    highs = [k[2] for k in klines]
    lows = [k[3] for k in klines]
    closes = [k[4] for k in klines]

    sma21 = _ma(closes, 21)
    rsi = _rsi(closes, 14)
    xs = list(range(len(klines)))

    is_long = sig.side == SignalSide.LONG
    green, red, blue = "#26a69a", "#ef5350", "#42a5f5"

    plt.style.use("dark_background")
    fig = plt.figure(figsize=(12, 8), dpi=110)
    gs = fig.add_gridspec(3, 3, height_ratios=[3, 1, 0.05], width_ratios=[3, 3, 1.4], hspace=0.12, wspace=0.05)
    ax = fig.add_subplot(gs[0, :2])
    ax_rsi = fig.add_subplot(gs[1, :2], sharex=ax)
    ax_side = fig.add_subplot(gs[:, 2])

    # --- candlesticks ---
    width = 0.6
    for i in xs:
        c_up = closes[i] >= opens[i]
        color = green if c_up else red
        ax.plot([i, i], [lows[i], highs[i]], color=color, linewidth=0.7, zorder=2)
        lo = min(opens[i], closes[i])
        hi = max(opens[i], closes[i])
        ax.add_patch(Rectangle((i - width / 2, lo), width, max(hi - lo, 1e-9),
                               facecolor=color, edgecolor=color, zorder=3))

    ax.plot(xs, [m if m else float("nan") for m in sma21], color="#ffb74d", linewidth=1.2, label="SMA21")

    # --- entry zone band ---
    zone_color = green if is_long else red
    ax.axhspan(sig.entry_low, sig.entry_high, color=zone_color, alpha=0.18, zorder=1)
    ax.axhline(sig.entry_low, color=zone_color, linewidth=0.8, linestyle="--", alpha=0.6)
    ax.axhline(sig.entry_high, color=zone_color, linewidth=0.8, linestyle="--", alpha=0.6)
    ax.text(0, sig.entry_high, f" ENTRY {sig.entry_low:g}-{sig.entry_high:g}",
            color=zone_color, fontsize=8, va="bottom", fontweight="bold")

    # --- SL ---
    if sig.stop_loss is not None:
        ax.axhline(sig.stop_loss, color=red, linewidth=1.3, zorder=4)
        ax.text(len(xs) - 1, sig.stop_loss, f" SL {sig.stop_loss:g} ", color="white",
                fontsize=8, va="center", ha="right",
                bbox=dict(boxstyle="round,pad=0.2", fc=red, ec="none"))

    # --- TPs ---
    for idx, tp in enumerate(sig.take_profits, 1):
        ax.axhline(tp, color=green, linewidth=1.0, linestyle="--", zorder=4, alpha=0.9)
        ax.text(len(xs) - 1, tp, f" TP{idx} {tp:g} ", color="white", fontsize=8,
                va="center", ha="right",
                bbox=dict(boxstyle="round,pad=0.2", fc=green, ec="none"))

    # --- current price ---
    price = float(result.metrics_snapshot.get("price") or closes[-1])
    ax.axhline(price, color="#cfd8dc", linewidth=0.8, linestyle=":", zorder=4)
    ax.text(len(xs) - 1, price, f" {price:g} ", color="black", fontsize=8,
            va="center", ha="left", bbox=dict(boxstyle="round,pad=0.2", fc="#cfd8dc", ec="none"))

    _tf = getattr(sig, "timeframe", None) or "15m"
    ax.set_title(f"{sig.symbol}  •  {sig.side.value}  •  {_tf}   (sumber: {sig.source_name or sig.source.value})",
                 fontsize=12, fontweight="bold", loc="left")
    ax.legend(loc="upper left", fontsize=8, framealpha=0.3)
    ax.grid(True, alpha=0.12)
    ax.margins(x=0.01)
    ax.tick_params(labelbottom=False)

    # --- RSI panel ---
    ax_rsi.plot(xs, [r if r else float("nan") for r in rsi], color="#ba68c8", linewidth=1.0)
    ax_rsi.axhline(70, color=red, linewidth=0.6, linestyle="--", alpha=0.5)
    ax_rsi.axhline(30, color=green, linewidth=0.6, linestyle="--", alpha=0.5)
    ax_rsi.set_ylim(0, 100)
    ax_rsi.set_ylabel("RSI", fontsize=8)
    ax_rsi.grid(True, alpha=0.12)
    ax_rsi.margins(x=0.01)
    ax_rsi.tick_params(labelbottom=False)

    # --- side panel: verdict + factors ---
    ax_side.axis("off")
    vmap = {Verdict.VALID: green, Verdict.WEAK: "#ffb74d", Verdict.REJECT: red}
    vcolor = vmap.get(result.verdict, "#cfd8dc")
    ax_side.text(0.5, 0.98, result.verdict.value, color="white", fontsize=15,
                 fontweight="bold", ha="center", va="top",
                 bbox=dict(boxstyle="round,pad=0.4", fc=vcolor, ec="none"))
    ax_side.text(0.5, 0.88, f"Skor: {result.score:.0f}/100", color="white",
                 fontsize=11, ha="center", va="top")

    rr_best = sig.rr_best()
    sl_pct = sig.sl_distance_pct()
    info = []
    if sl_pct is not None:
        info.append(f"SL dist: {sl_pct:.2f}%")
    if rr_best is not None:
        info.append(f"RR (best): {rr_best:.2f}")
    if sig.leverage:
        info.append(f"Lev: {sig.leverage:g}x")
    y = 0.80
    for line in info:
        ax_side.text(0.04, y, line, color="#b0bec5", fontsize=9, va="top")
        y -= 0.035

    y -= 0.02
    ax_side.text(0.04, y, "Faktor confluence:", color="white", fontsize=9.5,
                 fontweight="bold", va="top")
    y -= 0.045
    for f in result.factors:
        mark = "✓" if f.passed else "✗"
        mcolor = green if f.passed else red
        ax_side.text(0.04, y, mark, color=mcolor, fontsize=9, va="top", fontweight="bold")
        ax_side.text(0.12, y, f"{f.name} ({f.score:.0f}/{f.max_score:.0f})",
                     color="#cfd8dc", fontsize=8, va="top")
        y -= 0.038

    if result.hard_blocks:
        y -= 0.01
        ax_side.text(0.04, y, "HARD BLOCKS:", color=red, fontsize=8.5, fontweight="bold", va="top")
        y -= 0.032
        for b in result.hard_blocks[:4]:
            ax_side.text(0.04, y, f"• {b[:32]}", color=red, fontsize=7, va="top")
            y -= 0.03

    out_dir = out_dir or tempfile.gettempdir()
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"signal_{sig.symbol}_{sig.signal_id}_{int(time.time())}.png")
    try:
        fig.savefig(path, bbox_inches="tight", facecolor=fig.get_facecolor())
    except Exception as exc:
        logger.warning("[CHART] savefig failed: %s", exc)
        path = None
    finally:
        plt.close(fig)
    if path:
        logger.info("[CHART] rendered %s", path)
    return path
