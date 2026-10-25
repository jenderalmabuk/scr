import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Fetch BTC 1H klines from Nexus API
url = "http://127.0.0.1:8000/klines/bybit/BTCUSDT?tf=1h&limit=120"
btc_klines = []
try:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=6) as resp:
        btc_klines = json.loads(resp.read().decode())
except Exception as e:
    print("Error fetching BTC klines:", e)

print(f"Fetched {len(btc_klines)} BTC 1H klines.")

def compute_ema(series, period):
    if len(series) < period:
        return series[-1] if series else 0.0
    k = 2.0 / (period + 1.0)
    ema = sum(series[:period]) / period
    for val in series[period:]:
        ema = val * k + ema * (1.0 - k)
    return ema

# Precompute BTC 1H EMA20 and EMA50 at each candle timestamp
btc_trend_by_ts = {}
for i in range(20, len(btc_klines)):
    sub = btc_klines[:i+1]
    closes = [float(k["close"]) for k in sub]
    ema20 = compute_ema(closes, 20)
    ema50 = compute_ema(closes, 50)
    c_time = sub[-1].get("open_time") or sub[-1].get("time") or sub[-1].get("start_time")
    # if close > ema50 and ema20 > ema50 => Bullish
    is_bull = closes[-1] > ema50
    is_bear = closes[-1] < ema50
    # store epoch seconds
    t_sec = int(c_time / 1000) if c_time > 1e11 else int(c_time)
    btc_trend_by_ts[t_sec] = "BULLISH" if is_bull else ("BEARISH" if is_bear else "NEUTRAL")

print(f"Computed trend for {len(btc_trend_by_ts)} hourly timestamps.")

def get_btc_trend_at(dt):
    epoch = int(dt.timestamp())
    # find closest prior candle
    candidates = [ts for ts in btc_trend_by_ts.keys() if ts <= epoch]
    if candidates:
        nearest = max(candidates)
        return btc_trend_by_ts[nearest]
    return "NEUTRAL"

# Now evaluate trades with Filter 1 (Strict BTC Macro Guard)
th_path = Path("/opt/signalcopyreal/journal/trade_history.json")
OVERHAUL_CUTOFF = datetime(2026, 10, 18, 16, 22, 0, tzinfo=timezone.utc)
trades = []
if th_path.exists():
    raw = json.loads(th_path.read_text(encoding="utf-8"))
    items = raw if isinstance(raw, list) else list(raw.values())
    for t in items:
        tc_str = t.get("timestamp_close") or t.get("closed_at")
        if tc_str:
            try:
                tc = datetime.fromisoformat(tc_str.replace("Z", "+00:00"))
                if tc >= OVERHAUL_CUTOFF:
                    trades.append(t)
            except Exception:
                pass

print(f"\nReplaying {len(trades)} trades against real BTC 1H Trend...")

LOW_LIQUID_COINS = {
    "AKEUSDT", "GIGGLEUSDT", "EPICUSDT", "CAPUSDT", "KAITOUSDT", 
    "BANKUSDT", "CYSUSDT", "BEATUSDT", "COTIUSDT", "UAIUSDT", "BTWUSDT", "BLESSUSDT"
}

# Run Scenario Analysis:
def run_scenario(name, allow_fn):
    selected = [t for t in trades if allow_fn(t)]
    n = len(selected)
    wins = [t for t in selected if (t.get("pnl_usd") or 0.0) > 0]
    losses = [t for t in selected if (t.get("pnl_usd") or 0.0) <= 0]
    win_pnl = sum((t.get("pnl_usd", 0.0) or 0.0) for t in wins)
    loss_pnl = sum((t.get("pnl_usd", 0.0) or 0.0) for t in losses)
    fees = sum((t.get("entry_fee_usd", 0.0) or 0.0) + (t.get("exit_fee_usd", 0.0) or 0.0) for t in selected)
    net_pnl = win_pnl + loss_pnl
    wr = (len(wins) / n * 100) if n > 0 else 0
    pf = (win_pnl / abs(loss_pnl)) if loss_pnl != 0 else float("inf")
    print(f"--------------------------------------------------")
    print(f"Scenario: {name}")
    print(f"  Trades: {n} | {len(wins)}W / {len(losses)}L | Win Rate: {wr:.1f}%")
    print(f"  Win PnL: ${win_pnl:+.4f} | Loss PnL: ${loss_pnl:+.4f} | Fees: ${fees:.4f}")
    print(f"  NET PnL: ${net_pnl:+.4f} USD | Profit Factor: {pf:.2f}")
    print(f"  Simulated Account Equity ($100 start): ${100.0 + net_pnl:.2f} USDT")

# Scenario 0: Baseline
run_scenario("0. Baseline (Realita Saat Ini)", lambda t: True)

# Scenario 1: Filter 1 Only (Macro Guard: No SHORT if BTC is BULLISH, No LONG if BTC is BEARISH)
def filter_macro(t):
    to_str = t.get("timestamp_open")
    if not to_str:
        return True
    to = datetime.fromisoformat(to_str.replace("Z", "+00:00"))
    btc_trend = get_btc_trend_at(to)
    side = t.get("side")
    if side == "SHORT" and btc_trend == "BULLISH":
        return False # block short in bull
    if side == "LONG" and btc_trend == "BEARISH":
        return False # block long in bear
    return True

run_scenario("1. Filter 1 (Macro Regime Guard: Trend-Following)", filter_macro)

# Scenario 2: Filter 2 Only (Liquid Whitelist: Top 25 Coins Only)
def filter_liquid(t):
    return t.get("symbol") not in LOW_LIQUID_COINS

run_scenario("2. Filter 2 (Liquid Whitelist: Tolak Koin Receh)", filter_liquid)

# Scenario 3: Filter 1 + Filter 2 Combined
run_scenario("3. Kombinasi Filter 1 + 2 (Macro Guard + Liquid Whitelist)", lambda t: filter_macro(t) and filter_liquid(t))

# Scenario 4: Filter 1 + Filter 2 + Require Min SL Distance >= 1.0% (Anti-Wick Noise)
def filter_anti_wick(t):
    sl_pct = t.get("sl_pct") or t.get("adv_snapshot", {}).get("sl_pct") or 1.0
    return float(sl_pct) >= 1.0

run_scenario("4. Kombinasi Filter 1 + 2 + Anti-Wick (SL >= 1.0%)", lambda t: filter_macro(t) and filter_liquid(t) and filter_anti_wick(t))
