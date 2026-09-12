# Nexus — Universal Market Scanner & Multi-Bot Trading Backend

Centralized market data infrastructure and scanner pipeline for all trading bots.
TimescaleDB + FastAPI + multi-exchange collectors + Revo Adaptive bridge.

## Quick Start

```bash
cp .env.example .env
# Edit .env with your API keys (Telegram, exchange credentials)
docker compose -f docker/docker-compose.yml up -d
```

All services auto-start. Health check: `curl http://localhost:8000/health`

## Services

| Service | Port | Description |
|---|---:|---|
| TimescaleDB | 5432 | All market data (klines, OI, CVD, funding, trades) |
| FastAPI | 8000 | REST API for querying data + Revo endpoints |
| Binance Collector | — | Fetches klines + OI from Binance Futures |
| Bybit Collector | — | Fetches klines + real OI + real CVD + funding from Bybit |
| OI Rollup | — | Aggregates 5m OI → 1h/4h every hour |
| Nexus Scanner | — | Universal scanner: writes runtime files for all bots |

## Core API Endpoints

```
GET  /health
GET  /klines/{exchange}/{symbol}?tf=1h&limit=500
GET  /oi/{exchange}/{symbol}?tf=15m&limit=100
GET  /cvd/{exchange}/{symbol}?tf=15m&limit=100
GET  /funding/{exchange}/{symbol}?limit=24
GET  /pairs/{exchange}
```

## Revo Adaptive Endpoints

```
GET  /flow/{symbol}          Flow context per symbol (CVD, OI, funding)
GET  /flow/all?limit=300      Bulk flow context for top N pairs
GET  /btc_regime              BTC regime: risk_on/neutral/risk_off/panic
GET  /universe/top?n=300      Top N pairs by 24h volume + price change
```

## Runtime Output (Revo Bridge)

Nexus scanner writes Revo-compatible runtime files to `runtime/revo/`:

| File | Purpose |
|---|---:|
| `freqtrade_pairlist.json` | Pairlist consumed by Freqtrade RemotePairList |
| `revo_flow_context.json` | Flow context per pair (CVD, OI, funding, vol_z) |
| `btc_context_v135.json` | BTC regime snapshot |
| `regime_context.json` | Full regime context per pair |
| `candidate_context.json` | Candidate scoring + permission (ENTRY_READY/WATCH/NO_TRADE) |
| `candidate_context_summary.json` | Summary: entry_ready, watch, no_trade, blockers |
| `blocker_matrix.json` | Blocker counts per category |
| `blocker_matrix.txt` | Human-readable blocker matrix for tmux/report |
| `pair_universe_all.json/csv` | Full universe with volume and price change |
| `revo_flow_context_collector.json/csv` | Raw flow data for audit |
| `NEXUS_SCANNER_HEARTBEAT.jsonl` | Scanner heartbeat log |
| `NEXUS_SCANNER_HEARTBEAT_LATEST.json` | Latest heartbeat snapshot |
| `UNIVERSE_SCANNER_COMPACT.txt` | Universe scanner compact report |
| `TOP100_FLOW_ENGINE_COMPACT.txt` | Flow engine compact report |
| `BTC_MODE_ROUTER_COMPACT.txt` | BTC regime compact report |
| `BYBIT_FLOW_COLLECTOR_HEARTBEAT_COMPACT.txt` | Flow collector heartbeat |

## Architecture

```
Binance + Bybit market data
       ↓
  Collectors → TimescaleDB
       ↓
  FastAPI (REST endpoints)
       ↓
  Nexus Scanner (universal scanner)
       ↓
  runtime/revo/ (Revo adapter output)
       ↓
  Freqtrade RevoAdaptiveStrategy (paper/dry-run)
```

Nexus = pure data pipeline + scanner. No filtering, no trading decisions.
Each bot queries what it needs and applies its own filters.

## Monitoring

```bash
# Scanner live log
tmux attach -t 7

# Revo Freqtrade live log
tmux attach -t 8

# M30/H1 bot logs
tmux attach -t 5   # H1 log
tmux attach -t 6   # M30 log

# Report
tmux attach -t 4
```

## Directory Layout

```
fusion_omega_nexus/
├── api/                    FastAPI application
│   └── main.py             All endpoints
├── bots/
│   ├── nexus_scanner.py    Universal scanner (writes runtime/revo/)
│   ├── nexus_m30_imbalance.py   M30 imbalance bot
│   └── nexus_h1_imbalance.py    H1 imbalance bot
├── collectors/             Exchange data collectors
├── docker/
│   ├── docker-compose.yml  Main compose (all services)
│   └── bot/                Bot Dockerfile
├── runtime/
│   ├── revo/               Revo adapter output (Freqtrade reads this)
│   └── state/              Bot state persistence
└── scripts/                Utility scripts
```

## Connecting Revo Adaptive Freqtrade

1. Freqtrade container needs this volume mount:

```yaml
volumes:
  - /home/fusion_omega/fusion_omega_nexus/runtime/revo:/external_runtime:ro
```

2. Freqtrade env:

```env
REVO_FLOW_CONTEXT_PATH=/external_runtime/revo_flow_context.json
```

3. Freqtrade pairlist config:

```json
{
  "pairlists": [{
    "method": "RemotePairList",
    "pairlist_url": "file:///external_runtime/freqtrade_pairlist.json"
  }]
}
```

## Environment Variables

Copy `.env.example` to `.env` and fill in:

```env
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id
BINANCE_API_KEY=your_key
BINANCE_API_SECRET=your_secret
BYBIT_API_KEY=your_key
BYBIT_API_SECRET=your_secret
```

## Validation Protocol

Before real money:

- [ ] 7D smoke backtest passes (PF >= 1.10)
- [ ] 30D recent backtest passes (PF >= 1.20)
- [ ] 50+ dry-run trades with PF > 1.2, DD < 3%
- [ ] No stale pipeline incidents
- [ ] Telegram + blocker matrix stable
- [ ] Manual review of trade samples

See `docs/VALIDATION_PROTOCOL.md` in `revo_adaptive` repo for full protocol.

## Status

| Component | Status |
|---|---:|
| TimescaleDB | ✅ running |
| FastAPI + Revo endpoints | ✅ running |
| Binance/Bybit collectors | ✅ running |
| Nexus scanner (Revo bridge) | ✅ running |
| Revo Freqtrade (paper) | ✅ connected |
| M30 imbalance bot | ✅ running |
| H1 imbalance bot | ✅ running |

## Related Repos

- `revo_adaptive/` — Revo Adaptive strategy + Freqtrade config (being migrated to Nexus)
- `fusionnew/` — Legacy M30/H1 bots (being migrated to Nexus)

---

*Blueprint-first architecture. Universal scanner with legacy-compatible outputs.*
