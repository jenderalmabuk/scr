-- Nexus v2 — TimescaleDB Schema
-- All market data for multi-bot consumption

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ============================================================
-- KLINES (candlestick data)
-- ============================================================
CREATE TABLE IF NOT EXISTS klines (
    exchange    TEXT NOT NULL,          -- 'binance' | 'bybit'
    symbol      TEXT NOT NULL,          -- 'BTCUSDT'
    timeframe   TEXT NOT NULL,          -- '1m' | '5m' | '15m' | '1h' | '4h' | '1d'
    open_time   TIMESTAMPTZ NOT NULL,   -- candle open time
    open        DOUBLE PRECISION,
    high        DOUBLE PRECISION,
    low         DOUBLE PRECISION,
    close       DOUBLE PRECISION,
    volume      DOUBLE PRECISION,
    close_time  TIMESTAMPTZ,
    quote_vol   DOUBLE PRECISION,
    trades      INTEGER,
    taker_buy_vol       DOUBLE PRECISION,
    taker_buy_quote_vol DOUBLE PRECISION,
    PRIMARY KEY (exchange, symbol, timeframe, open_time)
);
SELECT create_hypertable('klines', 'open_time', if_not_exists => TRUE);

-- Index for symbol+tf lookups
CREATE INDEX IF NOT EXISTS idx_klines_symbol_tf 
    ON klines (exchange, symbol, timeframe, open_time DESC);

-- ============================================================
-- OPEN INTEREST
-- ============================================================
CREATE TABLE IF NOT EXISTS open_interest (
    exchange    TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    timeframe   TEXT NOT NULL,          -- '5m' | '15m' | '1h' | '4h'
    timestamp   TIMESTAMPTZ NOT NULL,
    oi_value    DOUBLE PRECISION,       -- nominal OI in USDT
    oi_delta    DOUBLE PRECISION,       -- change from previous
    oi_delta_pct DOUBLE PRECISION,      -- % change
    PRIMARY KEY (exchange, symbol, timeframe, timestamp)
);
SELECT create_hypertable('open_interest', 'timestamp', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_oi_symbol_tf 
    ON open_interest (exchange, symbol, timeframe, timestamp DESC);

-- ============================================================
-- CVD (Cumulative Volume Delta)
-- ============================================================
CREATE TABLE IF NOT EXISTS cvd (
    exchange    TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    timeframe   TEXT NOT NULL,          -- '5m' | '15m'
    timestamp   TIMESTAMPTZ NOT NULL,
    cvd_value   DOUBLE PRECISION,       -- absolute CVD
    cvd_delta   DOUBLE PRECISION,       -- change from previous
    cvd_zscore  DOUBLE PRECISION,       -- rolling z-score (15-period)
    PRIMARY KEY (exchange, symbol, timeframe, timestamp)
);
SELECT create_hypertable('cvd', 'timestamp', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_cvd_symbol_tf 
    ON cvd (exchange, symbol, timeframe, timestamp DESC);

-- ============================================================
-- FUNDING RATE
-- ============================================================
CREATE TABLE IF NOT EXISTS funding_rate (
    exchange    TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    timestamp   TIMESTAMPTZ NOT NULL,
    funding_rate    DOUBLE PRECISION,
    funding_zscore  DOUBLE PRECISION,   -- rolling z-score (24-period)
    PRIMARY KEY (exchange, symbol, timestamp)
);
SELECT create_hypertable('funding_rate', 'timestamp', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_funding_symbol 
    ON funding_rate (exchange, symbol, timestamp DESC);

-- ============================================================
-- TRADES (bot execution log)
-- ============================================================
CREATE TABLE IF NOT EXISTS trades (
    bot_name    TEXT NOT NULL,           -- 'm30_imbalance' | 'h1_imbalance'
    exchange    TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL,           -- 'LONG' | 'SHORT'
    entry_time  TIMESTAMPTZ,
    exit_time   TIMESTAMPTZ,
    entry_price DOUBLE PRECISION,
    exit_price  DOUBLE PRECISION,
    quantity    DOUBLE PRECISION,
    pnl         DOUBLE PRECISION,
    pnl_pct     DOUBLE PRECISION,
    exit_reason TEXT,                    -- 'TP' | 'SL' | 'TRAIL' | 'MANUAL'
    PRIMARY KEY (bot_name, symbol, entry_time)
);
SELECT create_hypertable('trades', 'entry_time', if_not_exists => TRUE);

-- ============================================================
-- UNIVERSE (tracked pairs)
-- ============================================================
CREATE TABLE IF NOT EXISTS universe (
    exchange    TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    added_at    TIMESTAMPTZ DEFAULT NOW(),
    active      BOOLEAN DEFAULT TRUE,
    PRIMARY KEY (exchange, symbol)
);

-- ============================================================
-- RETENTION POLICY: keep 90 days of data
-- ============================================================
SELECT add_retention_policy('klines', INTERVAL '90 days', if_not_exists => TRUE);
SELECT add_retention_policy('open_interest', INTERVAL '90 days', if_not_exists => TRUE);
SELECT add_retention_policy('cvd', INTERVAL '90 days', if_not_exists => TRUE);
SELECT add_retention_policy('funding_rate', INTERVAL '90 days', if_not_exists => TRUE);
SELECT add_retention_policy('trades', INTERVAL '365 days', if_not_exists => TRUE);
