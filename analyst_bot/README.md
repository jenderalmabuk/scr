# Nexus Analyst Bot

Bot Telegram read-only. Data: Nexus FastAPI (`klines`, OI, CVD, funding, flow, BTC regime) dan JSON whale scanner lokal.

## Commands

- `/analyze BTCUSDT`
- `BTCUSDT`

## Environment

```env
ANALYST_TELEGRAM_BOT_TOKEN=...
ANALYST_TELEGRAM_ALLOWED_CHAT_IDS=578305627
ANALYST_EXCHANGE=binance
ANALYST_MAX_AGE_S=180
```

Jangan commit token. Container tidak menerima API key trading dan tidak mount gateway/state secara writable.

## Verdict ceiling

Versi awal hanya `NO TRADE` atau `WATCH`. `SETUP VALIDATED` sengaja diblokir sampai:

1. `probe_live_vs_backtest.py` selesai.
2. Bursa data dan executor konsisten.
3. Statistik `nearest_unmitigated_setups()` pada universe live tersedia.
4. Segmen punya n>=100, net OOS PF>1.1, expectancy net>0, mayoritas fold profit.

LLM tidak dipakai. Semua output deterministik. Transfer wallet tanpa label exchange tetap `NEUTRAL`.

## Checks

```bash
python3 -m analyst_bot.self_check
python3 -m analyst_bot.analyzer BTCUSDT
```
