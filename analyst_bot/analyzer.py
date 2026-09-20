"""Read-only Nexus market analyst. Facts first; verdicts fail closed."""
from __future__ import annotations

import json
import math
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

API = os.getenv("NEXUS_API_URL", "http://fastapi:8000").rstrip("/")
EXCHANGE = os.getenv("ANALYST_EXCHANGE", "binance")
WHALE_DIR = Path(os.getenv("WHALE_RUNTIME_DIR", "/app/runtime/whales"))
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}USDT$")
MAX_MARKET_AGE_S = int(os.getenv("ANALYST_MAX_AGE_S", "180"))


def _get(path: str, **params: Any) -> dict:
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=15) as response:
        return json.load(response)


def normalize_symbol(raw: str) -> str:
    value = (raw or "").strip().upper().replace("/", "")
    if not value or not value.isalnum():
        raise ValueError("Pair tidak valid. Contoh: BTCUSDT")
    symbol = value if value.endswith("USDT") else value + "USDT"
    if not SYMBOL_RE.fullmatch(symbol):
        raise ValueError("Pair tidak valid. Contoh: BTCUSDT")
    return symbol


def _iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _age(value: Any) -> float | None:
    stamp = _iso(value)
    return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds()) if stamp else None


def _pct(now: float, old: float) -> float | None:
    return (now / old - 1) * 100 if old else None


def _series_change(rows: list[dict], field: str, bars: int) -> float | None:
    if len(rows) <= bars:
        return None
    return _pct(float(rows[-1][field]), float(rows[-1-bars][field]))


def _price_stats(rows: list[dict], bars: int) -> dict:
    if len(rows) <= bars:
        return {"change": None, "qvol": None}
    window = rows[-bars:]
    return {
        "change": _pct(float(rows[-1]["close"]), float(rows[-1-bars]["close"])),
        "qvol": sum(float(x.get("quote_vol") or 0) for x in window),
    }


def _latest_whales(symbol: str, limit: int = 3) -> list[dict]:
    base = symbol.removesuffix("USDT")
    events = []
    for path in WHALE_DIR.glob(f"latest_whale_*_{base}.json"):
        try:
            item = json.loads(path.read_text())
            item["age_s"] = _age(item.get("timestamp"))
            events.append(item)
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(events, key=lambda x: x.get("timestamp", ""), reverse=True)[:limit]


def analyze(raw_symbol: str) -> dict:
    symbol = normalize_symbol(raw_symbol)
    pairs = set(_get(f"/pairs/{EXCHANGE}").get("symbols", []))
    if symbol not in pairs:
        raise ValueError(f"{symbol} tidak aktif di universe {EXCHANGE}")

    k5 = _get(f"/klines/{EXCHANGE}/{symbol}", tf="5m", limit=60).get("data", [])
    kh = _get(f"/klines/{EXCHANGE}/{symbol}", tf="1h", limit=220).get("data", [])
    oi = _get(f"/oi/{EXCHANGE}/{symbol}", tf="5m", limit=50).get("data", [])
    cvd = _get(f"/cvd/{EXCHANGE}/{symbol}", tf="5m", limit=50).get("data", [])
    funding = _get(f"/funding/{EXCHANGE}/{symbol}", limit=24).get("data", [])
    flow = _get(f"/flow/{symbol}", exchange=EXCHANGE)
    btc = _get("/btc_regime", exchange=EXCHANGE)

    if not k5 or not kh:
        raise ValueError("Data OHLCV tidak tersedia")
    last = k5[-1]
    price = float(last["close"])
    market_age = _age(last.get("open_time"))
    high20 = max(float(x["high"]) for x in kh[-20:])
    low20 = min(float(x["low"]) for x in kh[-20:])
    tr = [float(x["high"]) - float(x["low"]) for x in kh[-14:]]
    atr_pct = (sum(tr) / len(tr) / price * 100) if tr and price else None

    oi_changes = {"5m": _series_change(oi, "oi_value", 1),
                  "15m": _series_change(oi, "oi_value", 3),
                  "1h": _series_change(oi, "oi_value", 12),
                  "4h": _series_change(oi, "oi_value", 48)}
    cvd_changes = {}
    for label, bars in (("5m", 1), ("15m", 3), ("1h", 12), ("4h", 48)):
        cvd_changes[label] = (float(cvd[-1].get("cvd_value") or 0) -
                              float(cvd[-1-bars].get("cvd_value") or 0)) if len(cvd) > bars else None
    price_stats = {label: _price_stats(k5, bars) for label, bars in
                   (("5m", 1), ("15m", 3), ("1h", 12), ("4h", 48))}
    fund = float(funding[-1]["funding_rate"]) if funding else None
    fund_z = funding[-1].get("funding_zscore") if funding else None

    whales = _latest_whales(symbol)
    fresh_whales = [x for x in whales if x.get("age_s") is not None and x["age_s"] <= 3600]
    stale = market_age is None or market_age > MAX_MARKET_AGE_S
    reasons = []
    if stale:
        reasons.append("data pasar stale")
    if not oi:
        reasons.append("OI unavailable")
    if flow.get("cvd_zscore_15m") is None:
        reasons.append("zCVD 15m unavailable")
    reasons.append("statistik OOS detector live belum tervalidasi")

    return {
        "symbol": symbol, "exchange": EXCHANGE, "price": price, "market_age_s": market_age,
        "range_h1_20": [low20, high20], "atr_h1_pct": atr_pct,
        "price_stats": price_stats, "oi_changes": oi_changes, "oi_value": float(oi[-1]["oi_value"]) if oi else None,
        "cvd_changes": cvd_changes, "zcvd_15m": flow.get("cvd_zscore_15m"),
        "funding_rate": fund, "funding_zscore": fund_z,
        "flow": flow, "btc": btc, "whales": fresh_whales,
        "verdict": "NO TRADE" if stale else "WATCH",
        "reasons": reasons,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _num(value: Any, digits: int = 2, suffix: str = "") -> str:
    return "UNAVAILABLE" if value is None or not math.isfinite(float(value)) else f"{float(value):,.{digits}f}{suffix}"


def format_report(a: dict) -> str:
    flow = a.get("flow") or {}
    btc = a.get("btc") or {}
    lo, hi = a["range_h1_20"]
    lines = [
        f"{a['symbol']} · H1 · {a['verdict']}",
        f"Venue: {a['exchange']} · Data age: {_num(a['market_age_s'], 0, 's')}", "",
        "MARKET",
        f"Price: {_num(a['price'])}",
        f"H1 range (20): {_num(lo)}–{_num(hi)}",
        f"ATR H1: {_num(a['atr_h1_pct'], 2, '%')}", "",
        "PRICE / VOLUME",
        *[f"{tf}: Δ {_num(v['change'], 2, '%')} · quote vol ${_num(v['qvol'], 0)}" for tf, v in a['price_stats'].items()], "",
        "FLOW",
        f"OI now: ${_num(a['oi_value'], 0)}",
        *[f"OI {tf}: {_num(v, 3, '%')}" for tf, v in a['oi_changes'].items()],
        "OI 15m/1h/4h: derived from 5m snapshots",
        f"zCVD 15m: {_num(a['zcvd_15m'], 4)}",
        f"Funding/8h: {_num(None if a['funding_rate'] is None else a['funding_rate'] * 100, 4, '%')}",
        f"Flow direction: {flow.get('flow_direction', 'UNAVAILABLE')}",
        f"BTC regime: {btc.get('btc_regime', 'UNAVAILABLE')}", "",
        "WHALE",
    ]
    if a["whales"]:
        for w in a["whales"]:
            # Transfers without exchange labels remain neutral; never relabel as BUY/SELL.
            lines.append(f"• {w.get('event_type','EVENT')} {w.get('bias','NEUTRAL')} ${float(w.get('value_usd') or 0):,.0f} · {float(w['age_s'])/60:.0f}m · {w.get('chain','?')}")
    else:
        lines.append("NO RECENT VERIFIED WHALE ACTIVITY")
    lines += ["", "VALIDATION"]
    lines.extend(f"✗ {reason}" for reason in a["reasons"])
    lines += ["", "VERDICT", f"{a['verdict']}. Belum ada izin SETUP VALIDATED.", "Bot read-only; bukan perintah transaksi."]
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    print(format_report(analyze(sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT")))
