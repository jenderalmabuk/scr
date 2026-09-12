"""Nexus v2 — FastAPI REST server for market data query."""

import asyncio
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import asyncpg
from typing import Optional
import numpy as np

# ── config ────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL environment variable is required (no insecure default). "
        "Example: postgresql://nexus:***@timescaledb:5432/nexus"
    )
# Comma-separated allowed origins; no wildcard default.
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
_pool: Optional[asyncpg.Pool] = None

# ── lifecycle ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    _pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    yield
    await _pool.close()

app = FastAPI(title="Nexus v2 API", version="0.1.0", lifespan=lifespan)
if CORS_ORIGINS:
    app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS,
                       allow_methods=["*"], allow_headers=["*"])

# ── helpers ───────────────────────────────────────────────
async def query(sql: str, *args):
    async with _pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
        return [dict(r) for r in rows]

def _parse_tf(tf: str) -> str:
    """Normalize timeframe string."""
    tf = tf.lower().replace("m", "min").replace("h", "hour").replace("d", "day")
    mapping = {"1min": "1m", "3min": "3m", "5min": "5m", "15min": "15m", "30min": "30m",
               "1hour": "1h", "4hour": "4h", "1day": "1d"}
    return mapping.get(tf, tf)

# ── routes ────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/klines/{exchange}/{symbol}")
async def get_klines(
    exchange: str,
    symbol: str,
    tf: str = Query("1h", description="Timeframe: 1m,5m,15m,1h,4h,1d"),
    limit: int = Query(500, ge=1, le=2000),
):
    """Get candlestick data."""
    tf = _parse_tf(tf)
    rows = await query(
        """SELECT open_time, open, high, low, close, volume, quote_vol, trades,
                  taker_buy_vol, taker_buy_quote_vol
           FROM klines
           WHERE exchange=$1 AND symbol=$2 AND timeframe=$3
           ORDER BY open_time DESC LIMIT $4""",
        exchange, symbol.upper(), tf, limit,
    )
    return {"exchange": exchange, "symbol": symbol.upper(), "tf": tf, "count": len(rows), "data": rows[::-1]}

@app.get("/oi/{exchange}/{symbol}")
async def get_oi(
    exchange: str,
    symbol: str,
    tf: str = Query("15m", description="Timeframe: 5m,15m,1h,4h"),
    limit: int = Query(100, ge=1, le=500),
):
    """Get open interest data."""
    tf = _parse_tf(tf)
    rows = await query(
        """SELECT timestamp, oi_value, oi_delta, oi_delta_pct
           FROM open_interest
           WHERE exchange=$1 AND symbol=$2 AND timeframe=$3
           ORDER BY timestamp DESC LIMIT $4""",
        exchange, symbol.upper(), tf, limit,
    )
    return {"exchange": exchange, "symbol": symbol.upper(), "tf": tf, "count": len(rows), "data": rows[::-1]}

@app.get("/cvd/{exchange}/{symbol}")
async def get_cvd(
    exchange: str,
    symbol: str,
    tf: str = Query("15m", description="Timeframe: 5m,15m"),
    limit: int = Query(100, ge=1, le=500),
):
    """Get cumulative volume delta."""
    tf = _parse_tf(tf)
    rows = await query(
        """SELECT timestamp, cvd_value, cvd_delta, cvd_zscore
           FROM cvd
           WHERE exchange=$1 AND symbol=$2 AND timeframe=$3
           ORDER BY timestamp DESC LIMIT $4""",
        exchange, symbol.upper(), tf, limit,
    )
    return {"exchange": exchange, "symbol": symbol.upper(), "tf": tf, "count": len(rows), "data": rows[::-1]}

@app.get("/funding/{exchange}/{symbol}")
async def get_funding(
    exchange: str,
    symbol: str,
    limit: int = Query(24, ge=1, le=200),
):
    """Get funding rate history."""
    rows = await query(
        """SELECT timestamp, funding_rate, funding_zscore
           FROM funding_rate
           WHERE exchange=$1 AND symbol=$2
           ORDER BY timestamp DESC LIMIT $3""",
        exchange, symbol.upper(), limit,
    )
    return {"exchange": exchange, "symbol": symbol.upper(), "count": len(rows), "data": rows[::-1]}

@app.get("/pairs/{exchange}")
async def get_pairs(exchange: str):
    """List all tracked symbols for an exchange."""
    rows = await query(
        "SELECT symbol FROM universe WHERE exchange=$1 AND active=TRUE ORDER BY symbol",
        exchange,
    )
    return {"exchange": exchange, "count": len(rows), "symbols": [r["symbol"] for r in rows]}


# ── Revo-compatible endpoints ─────────────────────────────

@app.get("/flow/all")
async def get_all_flow(
    exchange: str = Query("bybit"),
    timeframe: str = Query("1m"),
    max_age_sec: int = Query(900, ge=60, le=3600),
):
    """Flow for every symbol with a fresh DB candle; no ranking/cap."""
    syms = await query(
        """SELECT symbol
           FROM klines
           WHERE exchange=$1 AND timeframe=$2
           GROUP BY symbol
           HAVING MAX(open_time) >= NOW() - ($3 * INTERVAL '1 second')
           ORDER BY symbol""",
        exchange, timeframe, max_age_sec,
    )
    # Parallel flow computation, bounded to protect the DB pool.
    sem = asyncio.Semaphore(16)

    async def _bounded(sym: str):
        async with sem:
            return sym, await _compute_flow(sym, exchange)

    results = await asyncio.gather(*(_bounded(r["symbol"]) for r in syms))
    pairs = dict(results)
    entry_ready = sum(1 for p in pairs.values() if p.get("flow_direction") in ("LONG_ONLY", "BOTH_ALLOWED"))
    return {
        "generated_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "profile": "nexus_flow_v1",
        "pairs": pairs,
        "summary": {"total": len(pairs), "tradeable": entry_ready},
    }


@app.get("/flow/{symbol}")
async def get_flow(symbol: str, exchange: str = Query("binance")):
    """Return flow context for a single symbol — matches Revo flow_context format."""
    sym = symbol.upper()
    flow = await _compute_flow(sym, exchange)
    return flow


@app.get("/btc_regime")
async def get_btc_regime(exchange: str = Query("binance")):
    """BTC regime from 5m klines — matches Revo btc_context format."""
    rows = await query(
        """SELECT open_time, open, high, low, close, volume
           FROM klines
           WHERE exchange=$1 AND symbol='BTCUSDT' AND timeframe='5m'
           ORDER BY open_time DESC LIMIT 250""",
        exchange,
    )
    if len(rows) < 200:
        raise HTTPException(404, "Not enough BTC 5m data")
    closes = [float(r["close"]) for r in rows[::-1]]
    close = np.array(closes)
    ema50 = _ema(close, 50)
    ema200 = _ema(close, 200)
    ret_1h = (close[-1] / close[-13] - 1) * 100 if len(close) > 13 else 0
    if close[-1] > ema50 and ema50 > ema200:
        regime = "risk_on"
    elif close[-1] < ema50 and ema50 < ema200:
        regime = "risk_off"
    elif ret_1h < -2.0:
        regime = "panic"
    else:
        regime = "neutral"
    from datetime import datetime, timezone
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "symbol": "BTCUSDT",
        "btc_price": close[-1],
        "btc_ema50": float(ema50),
        "btc_ema200": float(ema200),
        "btc_ret_1h_pct": float(ret_1h),
        "btc_regime": regime,
        "mode": regime.upper(),
    }


@app.get("/universe/top")
async def get_top_universe(
    exchange: str = Query("binance"),
    n: int = Query(200, ge=1, le=691),
    min_volume: float = Query(600_000),
    tf: str = Query("5m"),
    sort_by: str = Query("abs_change", pattern="abs_change|volume"),
):
    """Top universe by 24h volume or absolute price change — matches Revo scanner."""
    tf = _parse_tf(tf)
    rows = await query(
        """SELECT symbol,
                  SUM(quote_vol)  AS qvol_24h,
                  (ARRAY_AGG(close ORDER BY open_time))[1] AS open_first,
                  (ARRAY_AGG(close ORDER BY open_time DESC))[1] AS close_last,
                  MAX(high) AS high_24h,
                  MIN(low)  AS low_24h
           FROM klines
           WHERE exchange=$1 AND timeframe=$2
             AND open_time >= NOW() - INTERVAL '24 hours'
           GROUP BY symbol""",
        exchange, tf,
    )
    if not rows:
        rows = await query(
            """SELECT symbol,
                      SUM(quote_vol)  AS qvol_24h,
                      (ARRAY_AGG(close ORDER BY open_time))[1] AS open_first,
                      (ARRAY_AGG(close ORDER BY open_time DESC))[1] AS close_last,
                      MAX(high) AS high_24h,
                      MIN(low)  AS low_24h
               FROM klines
               WHERE exchange=$1 AND timeframe='5m'
                 AND open_time >= NOW() - INTERVAL '24 hours'
               GROUP BY symbol""",
            exchange,
        )
    out = []
    for r in rows:
        qvol = float(r["qvol_24h"] or 0)
        if qvol < min_volume:
            continue
        first = float(r["open_first"] or 0)
        last = float(r["close_last"] or 0)
        change_pct = ((last / first - 1) * 100) if first > 0 else 0
        out.append({
            "pair": r["symbol"],
            "quote_volume": qvol,
            "price_change_pct": change_pct,
        })
    if sort_by == "abs_change":
        out.sort(key=lambda x: abs(x["price_change_pct"]), reverse=True)
    else:
        out.sort(key=lambda x: x["quote_volume"], reverse=True)
    return {"exchange": exchange, "count": len(out[:n]), "pairs": out[:n]}


# ── flow helpers ───────────────────────────────────────────

async def _compute_flow(symbol: str, exchange: str = "binance") -> dict:
    """Compute flow context for a symbol from klines + OI + funding."""
    from datetime import datetime, timezone
    tf = "15m"
    rows = await query(
        """SELECT open_time, open, high, low, close, volume,
                  taker_buy_vol, quote_vol
           FROM klines
           WHERE exchange=$1 AND symbol=$2 AND timeframe=$3
           ORDER BY open_time DESC LIMIT 48""",
        exchange, symbol, tf,
    )
    if len(rows) < 20:
        return {"pair": symbol, "data_ready": False, "flow_direction": "NO_TRADE", "source": "none"}

    close = np.array([float(r["close"]) for r in rows[::-1]])
    volume = np.array([float(r["volume"]) for r in rows[::-1]])
    taker_buy = np.array([float(r.get("taker_buy_vol") or 0) for r in rows[::-1]])
    quote_vol = np.array([float(r.get("quote_vol") or 0) for r in rows[::-1]])

    # CVD proxy: taker_buy_vol * 2 - volume (buy pressure)
    if taker_buy.sum() > 0:
        cvd_delta = (taker_buy * 2 - volume).cumsum()
        cvd_z = _zscore(cvd_delta[-15:]) if len(cvd_delta) >= 15 else 0
        source = "real"
    else:
        cvd_proxy = ((close - np.roll(close, 3)) / np.roll(close, 3) * volume).cumsum()
        cvd_z = _zscore(cvd_proxy[-15:]) if len(cvd_proxy) >= 15 else 0
        source = "proxy"

    vol_z = _zscore(volume[-15:]) if len(volume) >= 15 else 1.0
    qvol_5m = float(quote_vol[-5:].sum())

    # OI from DB (Bybit)
    oi_rows = await query(
        """SELECT oi_value, oi_delta_pct FROM open_interest
           WHERE exchange='bybit' AND symbol=$1 AND timeframe='15m'
           ORDER BY timestamp DESC LIMIT 3""",
        symbol,
    )
    oi_delta_pct = float(oi_rows[-1]["oi_delta_pct"]) if oi_rows and oi_rows[-1].get("oi_delta_pct") else 0

    # Funding from DB (Bybit)
    funding_rows = await query(
        """SELECT funding_rate, funding_zscore FROM funding_rate
           WHERE exchange='bybit' AND symbol=$1
           ORDER BY timestamp DESC LIMIT 30""",
        symbol,
    )
    funding_rate = float(funding_rows[-1]["funding_rate"]) if funding_rows else 0
    funding_zscores = [float(r["funding_zscore"]) for r in funding_rows if r.get("funding_zscore") is not None]
    funding_z = _zscore(np.array(funding_zscores)) if len(funding_zscores) >= 5 else 0

    # Flow direction
    long_signals = (cvd_z > 0) + (oi_delta_pct > 0) + (funding_rate <= 0)
    short_signals = (cvd_z < -1.0) + (oi_delta_pct < -2.0) + (funding_rate >= 0.0003)
    if long_signals >= 2:
        flow_direction = "LONG_ONLY"
    elif short_signals >= 2:
        flow_direction = "SHORT_ONLY"
    elif cvd_z > 0:
        flow_direction = "BOTH_ALLOWED"
    else:
        flow_direction = "NO_TRADE"

    return {
        "pair": symbol,
        "ts": datetime.now(timezone.utc).isoformat(),
        "flow_direction": flow_direction,
        "cvd_delta_15m": float(cvd_z),
        "cvd_zscore_15m": float(cvd_z),
        "oi_delta_pct_15m": float(oi_delta_pct),
        "funding_rate": funding_rate,
        "funding_zscore": float(funding_z),
        "volume_zscore_15m": float(vol_z),
        "data_ready": True,
        "data_stale": False,
        "source": source,
        "qvol_5m": qvol_5m,
    }


def _ema(data: np.ndarray, period: int) -> float:
    """Simple EMA — returns last value."""
    alpha = 2 / (period + 1)
    ema = data[0]
    for v in data[1:]:
        ema = alpha * v + (1 - alpha) * ema
    return ema


def _zscore(data: np.ndarray) -> float:
    """Z-score of last value vs rest of array."""
    if len(data) < 3:
        return 0.0
    mean = float(np.mean(data))
    std = float(np.std(data))
    if std == 0:
        return 0.0
    return float((data[-1] - mean) / std)

# ── main ──────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
