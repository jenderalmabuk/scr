"""Nexus data adapter — drop-in replacement for backtest.data.fetch_recent().

Queries the Nexus FastAPI instead of Binance REST directly.
Same interface: fetch_recent(symbol, interval, limit) -> pd.DataFrame

Notes:
 * Nexus only stores CLOSED candles (collector inserts after close), so the
   returned frame normally contains no forming candle.
 * The API returns columns `taker_buy_vol` / `quote_vol`; the engine expects
   `taker_buy_base` / `quote_volume` — mapped explicitly in _to_df().
 * Stale-data guard: df.attrs["lag_sec"] / df.attrs["stale"] are set so the
   engine can skip symbols whose data feed has frozen.
"""

from __future__ import annotations

import datetime as _dt
import os

import httpx
import pandas as pd

_NEXUS_URL = os.environ.get("NEXUS_API_URL", "http://localhost:8000")
_STALE_MULT = float(os.environ.get("NEXUS_STALE_MULT", "5"))

COLUMNS = ["open_time", "open", "high", "low", "close", "volume", "taker_buy_base"]

# Explicit Nexus API -> engine column mapping (schema desync guard)
_COLUMN_MAP = {
    "taker_buy_vol": "taker_buy_base",
    "quote_vol": "quote_volume",
}

TF_SEC = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}


def fetch_recent(symbol: str, interval: str = "1h", limit: int = 300) -> pd.DataFrame:
    """Latest `limit` klines from Nexus. Returns same schema as backtest.data.fetch_recent."""
    # Normalize Binance thousand-lot symbols (PEPEUSDT -> 1000PEPEUSDT)
    _BINANCE_THOUSAND = {
        "PEPEUSDT": "1000PEPEUSDT",
        "BONKUSDT": "1000BONKUSDT",
        "FLOKIUSDT": "1000FLOKIUSDT",
        "SHIBUSDT": "1000SHIBUSDT",
        "LUNCUSDT": "1000LUNCUSDT",
    }
    query_symbol = _BINANCE_THOUSAND.get(symbol, symbol)
    
    client = _get_client()
    tf = interval

    df = pd.DataFrame(columns=COLUMNS)
    now = pd.Timestamp(_dt.datetime.now(_dt.timezone.utc)).tz_localize(None)
    stale_limit = _STALE_MULT * TF_SEC.get(tf, 60)
    # Prefer the freshest source. Binance is tried first, but if its newest
    # candle is stale (e.g. symbol delisted from Binance futures -> the row
    # freezes into a zombie), fall back to Bybit instead of trusting a frozen
    # frame. The last source is kept as fallback if every source is stale.
    for exchange in ("binance", "bybit"):
        try:
            r = client.get(
                f"{_NEXUS_URL}/klines/{exchange}/{query_symbol}",
                params={"tf": tf, "limit": limit},
                timeout=15,
            )
            if r.status_code == 200 and r.json().get("count", 0) > 0:
                cand = _to_df(r.json()["data"], symbol=symbol, tf=tf)
                if len(cand) == 0:
                    continue
                df = cand  # keep as fallback even if stale
                last_open = pd.Timestamp(cand["open_time"].iloc[-1])
                if float((now - last_open).total_seconds()) <= stale_limit:
                    break  # fresh enough — stop here
        except Exception:
            continue

    # If a multi-minute tf is empty/stale on every source, synthesize it from
    # 1m (which may be live on a different exchange). Rescues symbols that lost
    # native 3m when delisted from Binance but still trade on Bybit — Bybit does
    # not store 3m for any symbol, so 3m is otherwise Binance-only.
    _needs_synth = len(df) == 0
    if not _needs_synth:
        _lo = pd.Timestamp(df["open_time"].iloc[-1])
        _needs_synth = float((now - _lo).total_seconds()) > stale_limit
    if _needs_synth and TF_SEC.get(tf, 0) > 60:
        synth = _resample_1m(symbol, tf, limit)
        if len(synth) > 0:
            df = synth

    # ── Stale-data guard: lag = now_utc - last open_time ──
    lag_sec = None
    stale = False
    if len(df) > 0:
        last_open = pd.Timestamp(df["open_time"].iloc[-1])
        lag_sec = float((now - last_open).total_seconds())
        if lag_sec > stale_limit:
            stale = True
            print(f"[WARN] STALE DATA {symbol} {tf} lag={int(lag_sec)}s "
                  f"(> {int(stale_limit)}s = {_STALE_MULT:g}x timeframe)")
    df.attrs["lag_sec"] = lag_sec
    df.attrs["stale"] = stale
    return df


def _get_client() -> httpx.Client:
    if not hasattr(_get_client, "_client"):
        _get_client._client = httpx.Client(timeout=httpx.Timeout(15))
    return _get_client._client


def _resample_1m(symbol: str, tf: str, limit: int) -> pd.DataFrame:
    """Build `tf` bars from 1m klines (freshest source) when the native tf is
    unavailable/stale on every exchange. Rescues Bybit-only symbols on 3m —
    Bybit stores no 3m, and a delisted Binance symbol freezes its native 3m.
    Returns COLUMNS schema, or an empty frame if 1m is also unusable."""
    tf_sec = TF_SEC.get(tf, 0)
    if tf_sec <= 60 or tf_sec % 60 != 0:
        return pd.DataFrame(columns=COLUMNS)
    step = tf_sec // 60  # 1m bars per synthesized bar
    # Nexus API caps `limit` at 2000. A 1000-bar 3m request otherwise asks for
    # 3000 x 1m, gets HTTP 422, and silently falls back to frozen native data.
    # 2000 x 1m still yields ~667 bars, above engine's 260-bar minimum.
    m1 = fetch_recent(symbol, "1m", min(limit * step, 2000))
    if len(m1) == 0:
        return pd.DataFrame(columns=COLUMNS)
    g = m1.set_index(pd.to_datetime(m1["open_time"]))
    agg = {"open": "first", "high": "max", "low": "min", "close": "last",
           "volume": "sum", "taker_buy_base": "sum"}
    if "quote_volume" in g.columns:
        agg["quote_volume"] = "sum"
    out = (g.resample(f"{step}min", label="left", closed="left")
             .agg(agg).dropna(subset=["open"]).reset_index())
    out = out.rename(columns={out.columns[0]: "open_time"})
    keep = COLUMNS + (["quote_volume"] if "quote_volume" in out.columns else [])
    return out[keep].reset_index(drop=True)


def _to_df(rows: list[dict], symbol: str = "?", tf: str = "?") -> pd.DataFrame:
    """Convert Nexus API response to same DataFrame format as Binance raw."""
    if not rows:
        return pd.DataFrame(columns=COLUMNS)

    df = pd.DataFrame(rows)

    # Explicit schema mapping BEFORE any default-filling: the Nexus API returns
    # taker_buy_vol / quote_vol, the engine expects taker_buy_base / quote_volume.
    for src, dst in _COLUMN_MAP.items():
        if src in df.columns and dst not in df.columns:
            df[dst] = df[src]

    open_times = pd.to_datetime(df["open_time"])

    for col in COLUMNS:
        if col not in df.columns:
            df[col] = 0.0

    for col in ["open", "high", "low", "close", "volume", "taker_buy_base"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    # Schema-mismatch alarm: taker_buy_base all zero while volume is non-zero
    # means the CVD source silently degraded (this is exactly the bug that made
    # CVD always 0). Loud warning so it never happens silently again.
    if float(df["volume"].sum()) > 0 and float(df["taker_buy_base"].abs().sum()) == 0:
        print(f"[WARN] SCHEMA MISMATCH {symbol} {tf}: taker_buy_base is all-zero "
              f"while volume > 0 — CVD will be 0 (check API column names)")

    # Strip timezone (engine expects naive datetimes like Binance raw)
    if open_times.dt.tz is not None:
        open_times = open_times.dt.tz_localize(None)
    df["open_time"] = open_times

    keep = COLUMNS + (["quote_volume"] if "quote_volume" in df.columns else [])
    out = df[keep].reset_index(drop=True)
    if "quote_volume" in out.columns:
        out["quote_volume"] = pd.to_numeric(out["quote_volume"], errors="coerce").fillna(0.0)
    return out
