# Signal Copy Pipeline — Architecture & Progress

Goal: capture trade-call signals from Telegram groups + Discord, deep-validate
them against live market data (OI, CVD, funding, RSI, structure), ask the user
on Telegram to confirm, then execute on confirmation with 1% equity risk and
manage the position (partial TP, trailing, invalidation exit) to completion.

This package layers on top of the existing `fusion` infrastructure and reuses:
- `data/advanced_data.py` → live OI/CVD/funding/RSI/price metrics
- `risk/risk_engine.py`   → 1% equity position sizing
- `execution/`            → order routing + ManagedPosition (partial TP, trailing)
- `notifications/`        → Telegram transport

## Pipeline

```
Telegram group ─┐
                ├─► Listener ─► parse_signal() ─► ParsedSignal
Discord channel ┘                                   │
                                                    ▼
                                   get_advanced_metrics(symbol)
                                                    │
                                                    ▼
                                   validate_signal() ─► ValidationResult
                                                    │
                              ┌── REJECT ──► notify "rejected + why"
                              │
                              └── VALID ──► ConfirmationManager
                                                    │
                                   Telegram message w/ report + [✅ Ya] [❌ Tidak]
                                                    │
                              ┌── Tidak / timeout ──► drop
                              │
                              └── Ya ──► SignalExecutor.execute()
                                                    │
                                   risk sizing (1%) + submit_open()
                                                    │
                                                    ▼
                                   ManagedPosition (monitored by engine):
                                     - partial TP at signal TP1..TPn
                                     - trailing stop after TP1
                                     - invalidation exit (SL or thesis break)
```

## Modules

| File | Status | Responsibility |
|------|--------|----------------|
| `signal_schema.py` | DONE | `ParsedSignal` canonical model + RR/SL helpers |
| `signal_parser.py` | DONE | free-text → `ParsedSignal` (TG/Discord formats) |
| `validation_config.py` | DONE | factor weights + thresholds |
| `validation_engine.py` | DONE | deep multi-factor validation → verdict |
| `executor.py` | DONE | size 1% + open via engine + register signal TPs |
| `confirmation.py` | DONE | pending-signal registry + yes/no resolution |
| `telegram_confirm_bot.py` | DONE | interactive confirm bot (buttons + report) |
| `listeners/telegram_listener.py` | DONE | read signal groups (Telethon/aiogram) |
| `listeners/discord_listener.py` | DONE | read Discord channels (discord.py) |
| `orchestrator.py` | DONE | wire capture→validate→confirm→execute |
| `run_signal_copy.py` (repo root) | DONE | entrypoint |

## Validation factors (0-100 confluence)
1. Price vs entry-zone freshness (W=20)
2. Geometry / risk-reward, best-target aware (W=18)
3. Open-interest conviction (W=16)
4. CVD / taker order-flow alignment (W=16)
5. Funding-rate crowding context (W=10)
6. RSI / momentum not-exhausted (W=10)
7. Short-term trend / regime alignment (W=10)

Verdict: VALID ≥ 62, WEAK ≥ 45, else REJECT. Hard blocks (no data, SL beyond
hard cap, RR below hard floor) force REJECT regardless of score.

## Verified so far
- Parser correctly extracts the two real ZEC signal formats; rejects chatter.
- Validation engine scores a confluent signal VALID (~92) and a toxic one REJECT.
- Full pipeline smoke test (mocked trader/risk/metrics): ingest → validate →
  confirm (NO blocks, YES executes) → 1% sizing produces correct notional that
  risks exactly 1% of equity to the signal's stop loss.

## Safety notes
- `config.TESTNET_MODE = True` → executes on Bybit/Binance testnet by default.
- Auto-execution only after explicit user "Ya". WEAK signals notify but never auto-execute.
- 1% equity sizing via existing RiskManager; signal SL is respected, not overridden.
