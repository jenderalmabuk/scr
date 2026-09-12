"""OI rollup service: aggregate base-TF open interest into 1h/4h buckets.

Continuous loop. UPSERT so re-rolled buckets are corrected, not frozen.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import timedelta

from collector.db import pool

ROLLUP_SQL = """
INSERT INTO open_interest (exchange, symbol, timeframe, timestamp, oi_value, oi_delta, oi_delta_pct)
SELECT exchange, symbol, $2 AS timeframe,
       time_bucket($3::interval, timestamp) AS bucket,
       last(oi_value, timestamp) AS oi_value,
       last(oi_value, timestamp) - first(oi_value, timestamp) AS oi_delta,
       CASE WHEN first(oi_value, timestamp) > 0
            THEN (last(oi_value, timestamp) - first(oi_value, timestamp))
                 / first(oi_value, timestamp) * 100
            ELSE 0 END AS oi_delta_pct
FROM open_interest
WHERE timeframe = $1 AND timestamp >= NOW() - INTERVAL '2 days'
GROUP BY exchange, symbol, bucket
ON CONFLICT (exchange, symbol, timeframe, timestamp) DO UPDATE SET
    oi_value = EXCLUDED.oi_value,
    oi_delta = EXCLUDED.oi_delta,
    oi_delta_pct = EXCLUDED.oi_delta_pct
"""

LOOP_SEC = int(os.getenv("OI_ROLLUP_LOOP_SEC", "3600"))
ROLLUPS = (("5m", "1h", timedelta(hours=1)), ("5m", "4h", timedelta(hours=4)),
           ("15m", "1h", timedelta(hours=1)), ("15m", "4h", timedelta(hours=4)))


async def rollup_once() -> None:
    p = await pool()
    async with p.acquire() as conn:
        # 5m (binance base) and 15m (bybit base) -> 1h and 4h
        for src, dst, interval in ROLLUPS:
            try:
                await conn.execute(ROLLUP_SQL, src, dst, interval)
            except Exception as exc:
                print(f"[oi_rollup] ERR {src}->{dst}: {type(exc).__name__} {str(exc)[:100]}", flush=True)


async def run() -> None:
    print(f"[oi_rollup] start, every {LOOP_SEC}s", flush=True)
    while True:  # continuous loop
        t0 = time.time()
        await rollup_once()
        print(f"[oi_rollup] rollup done in {time.time() - t0:.1f}s", flush=True)
        await asyncio.sleep(LOOP_SEC)


if __name__ == "__main__":
    asyncio.run(run())
