"""Shared DB helpers for the Nexus collectors.

All writes are UPSERTs (ON CONFLICT DO UPDATE) so candles revised by the
exchange (volume/close corrections on the final push) are corrected instead of
being frozen at their first observed value (DO NOTHING would keep stale data).
"""
from __future__ import annotations

import os

import asyncpg

_pool: asyncpg.Pool | None = None


async def pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        # Checked at connect time (not import time) so the module stays importable
        # in tests/tooling without a live database.
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL environment variable is required for collectors")
        _pool = await asyncpg.create_pool(database_url, min_size=1, max_size=6)
    return _pool


UPSERT_KLINE = """
INSERT INTO klines (exchange, symbol, timeframe, open_time, open, high, low, close,
                    volume, close_time, quote_vol, trades, taker_buy_vol, taker_buy_quote_vol)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
ON CONFLICT (exchange, symbol, timeframe, open_time) DO UPDATE SET
    open = EXCLUDED.open,
    high = EXCLUDED.high,
    low = EXCLUDED.low,
    close = EXCLUDED.close,
    volume = EXCLUDED.volume,
    close_time = EXCLUDED.close_time,
    quote_vol = EXCLUDED.quote_vol,
    trades = EXCLUDED.trades,
    taker_buy_vol = EXCLUDED.taker_buy_vol,
    taker_buy_quote_vol = EXCLUDED.taker_buy_quote_vol
"""

UPSERT_OI = """
INSERT INTO open_interest (exchange, symbol, timeframe, timestamp, oi_value, oi_delta, oi_delta_pct)
VALUES ($1,$2,$3,$4,$5,$6,$7)
ON CONFLICT (exchange, symbol, timeframe, timestamp) DO UPDATE SET
    oi_value = EXCLUDED.oi_value,
    oi_delta = EXCLUDED.oi_delta,
    oi_delta_pct = EXCLUDED.oi_delta_pct
"""

UPSERT_FUNDING = """
INSERT INTO funding_rate (exchange, symbol, timestamp, funding_rate, funding_zscore)
VALUES ($1,$2,$3,$4,$5)
ON CONFLICT (exchange, symbol, timestamp) DO UPDATE SET
    funding_rate = EXCLUDED.funding_rate,
    funding_zscore = EXCLUDED.funding_zscore
"""

UPSERT_UNIVERSE = """
INSERT INTO universe (exchange, symbol, active)
VALUES ($1,$2,TRUE)
ON CONFLICT (exchange, symbol) DO UPDATE SET active = TRUE
"""


async def upsert_klines(rows: list[tuple]) -> None:
    if not rows:
        return
    p = await pool()
    async with p.acquire() as conn:
        await conn.executemany(UPSERT_KLINE, rows)


async def upsert_oi(rows: list[tuple]) -> None:
    if not rows:
        return
    p = await pool()
    async with p.acquire() as conn:
        await conn.executemany(UPSERT_OI, rows)


async def upsert_funding(rows: list[tuple]) -> None:
    if not rows:
        return
    p = await pool()
    async with p.acquire() as conn:
        await conn.executemany(UPSERT_FUNDING, rows)


async def upsert_universe(exchange: str, symbols: list[str]) -> None:
    if not symbols:
        return
    p = await pool()
    async with p.acquire() as conn:
        await conn.executemany(UPSERT_UNIVERSE, [(exchange, s) for s in symbols])


async def last_oi(exchange: str, symbol: str, timeframe: str) -> float | None:
    p = await pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT oi_value FROM open_interest
               WHERE exchange=$1 AND symbol=$2 AND timeframe=$3
               ORDER BY timestamp DESC LIMIT 1""",
            exchange, symbol, timeframe,
        )
    return float(row["oi_value"]) if row and row["oi_value"] is not None else None
