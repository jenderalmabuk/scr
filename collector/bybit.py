"""Bybit linear perpetual collector: klines + real OI + funding.

Continuous loop (never one-shot). Only CLOSED candles are stored. All writes
are UPSERTs so exchange-revised candles get corrected.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone

import httpx

from collector.db import (last_oi, upsert_funding, upsert_klines, upsert_oi,
                          upsert_universe)
from collector.universe import load_universe

BASE = os.getenv("BYBIT_BASE", "https://api.bybit.com")

KLINE_TFS = ["1m", "5m", "15m", "30m", "1h", "4h"]
# Bybit interval codes
BYBIT_TF = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "4h": "240"}
TF_SEC = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400}
KLINE_LIMIT = int(os.getenv("KLINE_LIMIT", "3"))
LOOP_SEC = int(os.getenv("COLLECTOR_LOOP_SEC", "60"))
OI_LOOP_SEC = int(os.getenv("OI_LOOP_SEC", "300"))
FUNDING_LOOP_SEC = int(os.getenv("FUNDING_LOOP_SEC", "900"))
CONCURRENCY = int(os.getenv("COLLECTOR_CONCURRENCY", "4"))
UNSUPPORTED_RETRY_SEC = int(os.getenv("UNSUPPORTED_RETRY_SEC", "21600"))
RATE_LIMIT_BACKOFF_SEC = int(os.getenv("RATE_LIMIT_BACKOFF_SEC", "120"))

_unsupported_until: dict[str, float] = {}
_rate_limited_until = 0.0


def _trip_rate_limit() -> None:
    global _rate_limited_until
    _rate_limited_until = max(_rate_limited_until, time.time() + RATE_LIMIT_BACKOFF_SEC)


def _rate_limited() -> bool:
    return time.time() < _rate_limited_until


def _due_tfs(now: float, last_run: dict[str, float]) -> list[str]:
    """Fetch a timeframe only when another closed candle can exist."""
    return [tf for tf in KLINE_TFS if now - last_run.get(tf, 0.0) >= TF_SEC[tf]]


def _quarantine(symbol: str) -> None:
    _unsupported_until[symbol] = time.time() + UNSUPPORTED_RETRY_SEC


def _is_quarantined(symbol: str) -> bool:
    until = _unsupported_until.get(symbol, 0.0)
    if until <= time.time():
        _unsupported_until.pop(symbol, None)
        return False
    return True


def _ts(ms: int | float | str) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


def _kline_rows(symbol: str, tf: str, data: list[list]) -> list[tuple]:
    """Bybit returns newest-first: [start, open, high, low, close, volume, turnover].
    Drop the still-forming candle (start + duration > now)."""
    now_ms = time.time() * 1000
    dur_ms = TF_SEC[tf] * 1000
    rows = []
    for k in data:
        start_ms = int(k[0])
        if start_ms + dur_ms > now_ms:  # candle not closed yet — skip
            continue
        vol = float(k[5])
        turnover = float(k[6])
        rows.append((
            "bybit", symbol, tf, _ts(start_ms),
            float(k[1]), float(k[2]), float(k[3]), float(k[4]), vol,
            _ts(start_ms + dur_ms - 1), turnover, 0,
            0.0, 0.0,  # Bybit kline API has no taker split — real CVD comes from trades stream
        ))
    return rows


async def _collect_klines(client: httpx.AsyncClient, sem: asyncio.Semaphore,
                          symbol: str, tf: str) -> None:
    if _rate_limited() or _is_quarantined(symbol):
        return
    async with sem:
        try:
            r = await client.get(f"{BASE}/v5/market/kline",
                                 params={"category": "linear", "symbol": symbol,
                                         "interval": BYBIT_TF[tf], "limit": KLINE_LIMIT})
            if r.status_code in {403, 429}:
                _trip_rate_limit()
                print(f"[bybit] rate-limited (HTTP {r.status_code}) on {symbol}; backing off {RATE_LIMIT_BACKOFF_SEC}s", flush=True)
                return
            if r.status_code in {400, 404}:
                _quarantine(symbol)
                return
            r.raise_for_status()
            res = r.json()
            if res.get("retCode") != 0:
                if res.get("retCode") in {10001, 10002, 10006}:
                    _quarantine(symbol)
                return
            data = res.get("result", {}).get("list", [])
            await upsert_klines(_kline_rows(symbol, tf, data))
        except Exception as exc:
            print(f"[bybit] klines ERR {symbol} {tf}: {type(exc).__name__} {str(exc)[:80]}", flush=True)
        await asyncio.sleep(0.02)


async def _collect_oi(client: httpx.AsyncClient, sem: asyncio.Semaphore, symbol: str) -> None:
    if _rate_limited() or _is_quarantined(symbol):
        return
    async with sem:
        try:
            r = await client.get(f"{BASE}/v5/market/open-interest",
                                 params={"category": "linear", "symbol": symbol,
                                         "intervalTime": "15min", "limit": 30})
            if r.status_code in {403, 429}:
                _trip_rate_limit()
                return
            if r.status_code in {400, 404}:
                _quarantine(symbol)
                return
            r.raise_for_status()
            res = r.json()
            if res.get("retCode") != 0:
                return
            data = res.get("result", {}).get("list", [])
            data = list(reversed(data))  # newest-first -> oldest-first
            prev = await last_oi("bybit", symbol, "15m")
            rows = []
            for d in data:
                oi = float(d["openInterest"])
                delta = oi - prev if prev is not None else 0.0
                pct = (delta / prev * 100) if prev else 0.0
                rows.append(("bybit", symbol, "15m", _ts(d["timestamp"]), oi, delta, pct))
                prev = oi
            await upsert_oi(rows)
        except Exception as exc:
            print(f"[bybit] oi ERR {symbol}: {type(exc).__name__} {str(exc)[:80]}", flush=True)
        await asyncio.sleep(0.02)


async def _collect_funding(client: httpx.AsyncClient, sem: asyncio.Semaphore, symbol: str) -> None:
    if _rate_limited() or _is_quarantined(symbol):
        return
    async with sem:
        try:
            r = await client.get(f"{BASE}/v5/market/funding/history",
                                 params={"category": "linear", "symbol": symbol, "limit": 30})
            if r.status_code in {403, 429}:
                _trip_rate_limit()
                return
            if r.status_code in {400, 404}:
                _quarantine(symbol)
                return
            r.raise_for_status()
            res = r.json()
            if res.get("retCode") != 0:
                return
            data = res.get("result", {}).get("list", [])
            data = list(reversed(data))
            rates = [float(d["fundingRate"]) for d in data]
            rows = []
            for i, d in enumerate(data):
                window = rates[max(0, i - 23):i + 1]
                mean = sum(window) / len(window)
                var = sum((x - mean) ** 2 for x in window) / len(window)
                z = (rates[i] - mean) / (var ** 0.5) if var > 0 else 0.0
                rows.append(("bybit", symbol, _ts(d["fundingRateTimestamp"]), rates[i], z))
            await upsert_funding(rows)
        except Exception as exc:
            print(f"[bybit] funding ERR {symbol}: {type(exc).__name__} {str(exc)[:80]}", flush=True)
        await asyncio.sleep(0.02)


async def run() -> None:
    symbols = load_universe()
    if not symbols:
        print("[bybit] WARNING: universe file empty — nothing to collect", flush=True)
        return
    await upsert_universe("bybit", symbols)
    print(f"[bybit] collector start: {len(symbols)} symbols, TFs={KLINE_TFS}", flush=True)
    sem = asyncio.Semaphore(CONCURRENCY)
    
    t_start = time.time()
    # Stagger timeframes on startup: only 1m runs immediately, higher TFs run as they mature
    last_tf_run: dict[str, float] = {tf: t_start for tf in KLINE_TFS if tf != "1m"}
    last_oi_run = t_start - (OI_LOOP_SEC / 2)
    last_funding_run = t_start - (FUNDING_LOOP_SEC / 2)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        while True:  # continuous loop — collectors are long-lived services
            t0 = time.time()
            due_tfs = _due_tfs(t0, last_tf_run)
            if due_tfs:
                await asyncio.gather(*[
                    _collect_klines(client, sem, sym, tf)
                    for sym in symbols for tf in due_tfs
                ])
                for tf in due_tfs:
                    last_tf_run[tf] = t0

            if time.time() - last_oi_run >= OI_LOOP_SEC:
                await asyncio.gather(*[_collect_oi(client, sem, sym) for sym in symbols])
                last_oi_run = time.time()

            if time.time() - last_funding_run >= FUNDING_LOOP_SEC:
                await asyncio.gather(*[_collect_funding(client, sem, sym) for sym in symbols])
                last_funding_run = time.time()

            took = time.time() - t0
            print(f"[bybit] cycle done in {took:.1f}s ({len(symbols)} symbols, due_tfs={due_tfs})", flush=True)
            await asyncio.sleep(max(5.0, LOOP_SEC - took))


if __name__ == "__main__":
    asyncio.run(run())
