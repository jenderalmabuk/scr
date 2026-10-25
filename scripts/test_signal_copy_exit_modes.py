import json
import urllib.request
from collections import Counter
from datetime import datetime, timezone

NEXUS_API = "http://127.0.0.1:8000"

# 1. Load validated signals from committee_decisions.jsonl
print("Reading signals from committee_decisions.jsonl...")
signals = []
seen_ids = set()

with open("/opt/signalcopyreal/runtime/state/committee_decisions.jsonl", "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            sig_id = d.get("signal_id")
            if not sig_id or sig_id in seen_ids:
                continue
            seen_ids.add(sig_id)
            
            sym = d.get("symbol")
            side = d.get("side")
            market = d.get("market") or {}
            price = market.get("price")
            ts = d.get("ts")
            
            # Extract geometry
            comm = d.get("committee") or {}
            votes = comm.get("votes") or []
            sl_pct = 1.0
            tp1_rr = 1.0
            best_rr = 2.0
            for v in votes:
                if v.get("specialist") == "geometry_rr":
                    ev = v.get("evidence") or {}
                    sl_pct = float(ev.get("sl_pct", 1.0) or 1.0)
                    tp1_rr = float(ev.get("tp1_rr", 1.0) or 1.0)
                    best_rr = float(ev.get("best_rr", 2.0) or 2.0)
                    break
            
            # Only consider signals with valid price and sl_pct
            if sym and side in ("LONG", "SHORT") and price and price > 0:
                # Apply Filter 2: exclude illiquid
                LOW_LIQUID_COINS = {
                    "AKEUSDT", "GIGGLEUSDT", "EPICUSDT", "CAPUSDT", "KAITOUSDT", 
                    "BANKUSDT", "CYSUSDT", "BEATUSDT", "COTIUSDT", "UAIUSDT", "BTWUSDT", "BLESSUSDT",
                    "USELESSUSDT", "APRUSDT", "BATUSDT", "WLFIUSDT"
                }
                if sym in LOW_LIQUID_COINS:
                    continue
                    
                # Calculate entry and SL
                entry = float(price)
                # Anti-wick floor >= 1.0%
                actual_sl_pct = max(1.0, sl_pct)
                sl = entry * (1.0 - (actual_sl_pct / 100.0)) if side == "LONG" else entry * (1.0 + (actual_sl_pct / 100.0))
                
                signals.append({
                    "signal_id": sig_id,
                    "symbol": sym,
                    "side": side,
                    "entry": entry,
                    "sl": sl,
                    "sl_pct": actual_sl_pct,
                    "ts": ts,
                    "verdict": d.get("validation_verdict"),
                    "action": comm.get("action")
                })
        except Exception:
            pass

print(f"Total valid candidate signals extracted: {len(signals)}")
# Take recent 250 signals to test forward outcomes
recent_signals = signals[-250:]
print(f"Evaluating {len(recent_signals)} most recent signals...")

# 2. Fetch forward klines from Nexus API
def fetch_klines(symbol, tf="15m", limit=300):
    for exchange in ["bybit", "binance"]:
        try:
            url = f"{NEXUS_API}/klines/{exchange}/{symbol}?tf={tf}&limit={limit}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                candles = []
                for c in data.get("data", []):
                    candles.append({
                        "time": c.get("open_time"),
                        "open": float(c["open"]),
                        "high": float(c["high"]),
                        "low": float(c["low"]),
                        "close": float(c["close"])
                    })
                if candles:
                    return candles
        except Exception:
            pass
    return []

# Pre-fetch candles for distinct symbols
distinct_symbols = set(s["symbol"] for s in recent_signals)
print(f"Fetching 15m klines for {len(distinct_symbols)} unique symbols...")
symbol_candles = {}
for sym in distinct_symbols:
    c = fetch_klines(sym, tf="15m", limit=500)
    if c:
        symbol_candles[sym] = c

print(f"Fetched candles for {len(symbol_candles)} symbols.")

# 3. Simulate Exit Modes on each signal
# Model 1: Current Signal Copy (1.0R Move SL to BEP with 0% TP, 2.0R TP 75%)
def sim_model_1_current(entry, sl, side, future):
    risk = abs(entry - sl)
    tp1 = entry + risk if side == "LONG" else entry - risk
    tp2 = entry + (2.0 * risk) if side == "LONG" else entry - (2.0 * risk)
    
    hit_1r = False
    for c in future:
        h, l = c["high"], c["low"]
        if not hit_1r:
            is_1r = (h >= tp1) if side == "LONG" else (l <= tp1)
            is_sl = (l <= sl) if side == "LONG" else (h >= sl)
            if is_sl and not is_1r:
                return -1.0 # Direct loss
            elif is_1r:
                hit_1r = True
        else: # SL moved to BEP
            is_2r = (h >= tp2) if side == "LONG" else (l <= tp2)
            is_bep = (l <= entry * 1.001) if side == "LONG" else (h >= entry * 0.999)
            if is_2r:
                return +1.5 # 75% at 2R (+1.5R), 25% at BEP (0)
            elif is_bep:
                return 0.0 # Stopped at BEP ($0)
    return 0.0

# Model 2: Golden Scalp-Runner (Disamakan dengan Autonomous: 1.5R TP 75%, Lock +1.0R, Runner 3.0R)
def sim_model_2_golden(entry, sl, side, future):
    risk = abs(entry - sl)
    tp1 = entry + (1.5 * risk) if side == "LONG" else entry - (1.5 * risk)
    tp2 = entry + (3.0 * risk) if side == "LONG" else entry - (3.0 * risk)
    
    hit_tp1 = False
    runner_sl = None
    runner_exit_r = 0.0
    runner_done = False
    
    for idx, c in enumerate(future):
        h, l = c["high"], c["low"]
        if not hit_tp1:
            is_tp1 = (h >= tp1) if side == "LONG" else (l <= tp1)
            is_sl = (l <= sl) if side == "LONG" else (h >= sl)
            if is_sl and not is_tp1:
                return -1.0 # Direct SL
            elif is_tp1:
                hit_tp1 = True
                runner_sl = entry + (1.0 * risk) if side == "LONG" else entry - (1.0 * risk)
        else:
            is_tp2 = (h >= tp2) if side == "LONG" else (l <= tp2)
            if is_tp2:
                runner_exit_r = 3.0
                runner_done = True
                break
                
            if idx > 0:
                prev_c = future[idx-1]
                dyn = prev_c["low"] if side == "LONG" else prev_c["high"]
                if side == "LONG" and dyn > runner_sl: runner_sl = dyn
                elif side == "SHORT" and dyn < runner_sl: runner_sl = dyn
                
            is_stopped = (l <= runner_sl) if side == "LONG" else (h >= runner_sl)
            if is_stopped:
                runner_exit_r = (runner_sl - entry) / risk if side == "LONG" else (entry - runner_sl) / risk
                runner_done = True
                break
                
    if hit_tp1:
        if not runner_done:
            runner_exit_r = 1.0 # at least locked profit
        return (0.75 * 1.5) + (0.25 * runner_exit_r)
    return -1.0

# Model 3: Partial TP di 1.0R (Take 50% profit at 1.0R, move SL to BEP, 50% at 2.0R)
def sim_model_3_partial_1r(entry, sl, side, future):
    risk = abs(entry - sl)
    tp1 = entry + risk if side == "LONG" else entry - risk
    tp2 = entry + (2.0 * risk) if side == "LONG" else entry - (2.0 * risk)
    hit_1 = False
    for c in future:
        h, l = c["high"], c["low"]
        if not hit_1:
            is_1 = (h >= tp1) if side == "LONG" else (l <= tp1)
            is_sl = (l <= sl) if side == "LONG" else (h >= sl)
            if is_sl and not is_1:
                return -1.0
            elif is_1:
                hit_1 = True
        else:
            is_2 = (h >= tp2) if side == "LONG" else (l <= tp2)
            is_bep = (l <= entry * 1.001) if side == "LONG" else (h >= entry * 0.999)
            if is_2:
                return +1.5 # 50% * 1R + 50% * 2R = +1.5R
            elif is_bep:
                return +0.5 # 50% * 1R locked + 50% at 0 = +0.5R
    return 0.0

# Model 4: 100% Fixed TP di 1.5R
def sim_model_4_fixed_15(entry, sl, side, future):
    risk = abs(entry - sl)
    tp = entry + (1.5 * risk) if side == "LONG" else entry - (1.5 * risk)
    for c in future:
        h, l = c["high"], c["low"]
        hit_tp = (h >= tp) if side == "LONG" else (l <= tp)
        hit_sl = (l <= sl) if side == "LONG" else (h >= sl)
        if hit_tp and not hit_sl:
            return +1.5
        elif hit_sl and not hit_tp:
            return -1.0
        elif hit_tp and hit_sl:
            return +1.5
    return 0.0

# Execute simulation
m1_results, m2_results, m3_results, m4_results = [], [], [], []

for s in recent_signals:
    sym = s["symbol"]
    candles = symbol_candles.get(sym)
    if not candles or len(candles) < 30:
        continue
    
    # Take candles
    future = candles[-60:]
    entry = s["entry"]
    sl = s["sl"]
    side = s["side"]
    
    m1_results.append(sim_model_1_current(entry, sl, side, future))
    m2_results.append(sim_model_2_golden(entry, sl, side, future))
    m3_results.append(sim_model_3_partial_1r(entry, sl, side, future))
    m4_results.append(sim_model_4_fixed_15(entry, sl, side, future))

def calc_metrics(res_list, name):
    n = len(res_list)
    wins = [r for r in res_list if r > 0]
    losses = [r for r in res_list if r < 0]
    beps = [r for r in res_list if r == 0]
    
    gw = sum(wins)
    gl = abs(sum(losses))
    net_r = gw - gl
    pf = gw / gl if gl > 0 else 99.0
    wr = len(wins) / n * 100 if n else 0
    
    return {
        "name": name,
        "n": n,
        "wr": wr,
        "wins": len(wins),
        "losses": len(losses),
        "beps": len(beps),
        "net_r": net_r,
        "net_usd": net_r * 2.0,
        "pf": pf
    }

out1 = calc_metrics(m1_results, "Model 1: Saat Ini di Signal Copy (1R BEP, 2R TP 75%)")
out2 = calc_metrics(m2_results, "Model 2: Disamakan dengan Autonomous (1.5R TP 75%, Lock +1R, Runner 3R)")
out3 = calc_metrics(m3_results, "Model 3: Partial di 1.0R (50% TP di 1R, BEP, 50% di 2R)")
out4 = calc_metrics(m4_results, "Model 4: 100% Fixed TP di 1.5R (Tanpa Trailing)")

print("\n" + "="*85)
print(f"=== HASIL UJI EKSPERIMEN EXIT SETELAH ENTRY PADA SINYAL COPY TELEGRAM ===")
print("="*85)
print(f"{'Skema Pengelolaan Exit':<48} | {'WR':<6} | {'BEP ($0)':<9} | {'Net R':<8} | {'Net USD':<9} | {'PF':<5}")
print("-" * 85)
for out in [out1, out2, out3, out4]:
    print(f"{out['name']:<48} | {out['wr']:<5.1f}% | {out['beps']:<9d} | {out['net_r']:>+7.1f}R | ${out['net_usd']:>+7.2f} | {out['pf']:<5.2f}")
print("="*85)
