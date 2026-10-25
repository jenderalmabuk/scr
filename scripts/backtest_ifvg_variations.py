import json
import urllib.request
from collections import Counter

NEXUS_API = "http://127.0.0.1:8000"

LOW_LIQUID_COINS = {
    "AKEUSDT", "GIGGLEUSDT", "EPICUSDT", "CAPUSDT", "KAITOUSDT", 
    "BANKUSDT", "CYSUSDT", "BEATUSDT", "COTIUSDT", "UAIUSDT", "BTWUSDT", "BLESSUSDT",
    "USELESSUSDT", "APRUSDT"
}

def get_liquid_universe():
    return [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "NEARUSDT", "SUIUSDT", "AVAXUSDT", "LINKUSDT",
        "DOGEUSDT", "ADAUSDT", "XRPUSDT", "APTUSDT", "BNBUSDT", "DOTUSDT", "ARBUSDT",
        "OPUSDT", "TAOUSDT", "CRVUSDT", "AAVEUSDT", "FETUSDT", "ENAUSDT", "TIAUSDT",
        "INJUSDT", "SEIUSDT", "FTMUSDT", "WIFUSDT", "PEPEUSDT", "RENDERUSDT", "LDOUSDT"
    ]

universe = get_liquid_universe()

def fetch_klines(symbol, tf="15m", limit=500):
    for exchange in ["bybit", "binance"]:
        try:
            url = f"{NEXUS_API}/klines/{exchange}/{symbol}?tf={tf}&limit={limit}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                raw_candles = data.get("data", [])
                if raw_candles:
                    candles = []
                    for c in raw_candles:
                        candles.append({
                            "time": c.get("open_time"),
                            "open": float(c["open"]),
                            "high": float(c["high"]),
                            "low": float(c["low"]),
                            "close": float(c["close"]),
                            "volume": float(c.get("volume", 0))
                        })
                    return candles
        except Exception:
            pass
    return []

def calc_ema(values, period):
    if len(values) < period:
        return [values[-1]] * len(values) if values else []
    ema = [sum(values[:period]) / period]
    k = 2.0 / (period + 1.0)
    for v in values[period:]:
        ema.append(v * k + ema[-1] * (1.0 - k))
    return [ema[0]] * (period - 1) + ema

def calc_atr(candles, period=14):
    trs = []
    for i in range(len(candles)):
        if i == 0:
            trs.append(candles[i]["high"] - candles[i]["low"])
        else:
            h = candles[i]["high"]
            l = candles[i]["low"]
            pc = candles[i-1]["close"]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return [trs[-1]] * len(trs) if trs else [1.0]
    atr = [sum(trs[:period]) / period]
    for tr in trs[period:]:
        atr.append((atr[-1] * (period - 1) + tr) / period)
    return [atr[0]] * (period - 1) + atr

def simulate_trade_path(entry, sl, side, future_candles):
    risk = abs(entry - sl)
    if risk <= 0 or not future_candles:
        return None
        
    tp1_price = entry + risk if side == "LONG" else entry - risk
    tp2_price = entry + (2.0 * risk) if side == "LONG" else entry - (2.0 * risk)
    tp3_price = entry + (3.0 * risk) if side == "LONG" else entry - (3.0 * risk)
    
    hit_1r = False
    hit_2r = False
    hit_3r = False
    hit_sl = False
    hit_bep = False
    max_mfe_r = 0.0
    outcome = "UNKNOWN"
    
    for c in future_candles:
        h = c["high"]
        l = c["low"]
        cur_mfe = (h - entry) / risk if side == "LONG" else (entry - l) / risk
        if cur_mfe > max_mfe_r:
            max_mfe_r = cur_mfe
            
        if not hit_1r:
            is_sl = (l <= sl) if side == "LONG" else (h >= sl)
            is_1r = (h >= tp1_price) if side == "LONG" else (l <= tp1_price)
            if is_sl and not is_1r:
                hit_sl = True
                outcome = "DIRECT_SL"
                break
            elif is_1r:
                hit_1r = True
                
        if hit_1r and not hit_2r:
            is_bep = (l <= entry * 1.001) if side == "LONG" else (h >= entry * 0.999)
            is_2r = (h >= tp2_price) if side == "LONG" else (l <= tp2_price)
            if is_2r:
                hit_2r = True
                outcome = "HIT_2R"
                if (h >= tp3_price) if side == "LONG" else (l <= tp3_price):
                    hit_3r = True
                break
            elif is_bep:
                hit_bep = True
                outcome = "REVERSED_TO_BEP"
                break
                
    if outcome == "UNKNOWN":
        if hit_2r: outcome = "HIT_2R"
        elif hit_1r: outcome = "REVERSED_TO_BEP"
        elif hit_sl: outcome = "DIRECT_SL"
        else: outcome = "STILL_OPEN"
        
    return {
        "max_mfe_r": max_mfe_r,
        "hit_1r": hit_1r,
        "hit_2r": hit_2r,
        "hit_3r": hit_3r,
        "hit_bep": hit_bep,
        "hit_sl": hit_sl,
        "outcome": outcome
    }

def run_ifvg_engine(candles, btc_times, min_gap_mult=0.5, require_trend=False, require_vol=False, require_btc_guard=False):
    if len(candles) < 60:
        return []
        
    closes = [c["close"] for c in candles]
    vols = [c["volume"] for c in candles]
    ema20 = calc_ema(closes, 20)
    ema50 = calc_ema(closes, 50)
    atrs = calc_atr(candles, 14)
    active_fvgs = []
    signals = []
    
    for i in range(20, len(candles) - 15):
        c = candles[i]
        c1 = candles[i-2]
        c2 = candles[i-1]
        c3 = candles[i]
        atr = atrs[i]
        min_gap = atr * min_gap_mult
        
        # 1. Detect new FVG
        if c3["low"] - c1["high"] >= min_gap:
            active_fvgs.append({
                "type": "BULLISH_FVG", "top": c3["low"], "bottom": c1["high"],
                "bar": i, "status": "ACTIVE"
            })
            
        if c1["low"] - c3["high"] >= min_gap:
            active_fvgs.append({
                "type": "BEARISH_FVG", "top": c1["low"], "bottom": c3["high"],
                "bar": i, "status": "ACTIVE"
            })
            
        # 2. Check Inversions
        for fvg in active_fvgs:
            if fvg["status"] != "ACTIVE":
                continue
            if i - fvg["bar"] < 2 or i - fvg["bar"] > 35:
                continue
                
            # BEARISH FVG inverted to SUPPORT -> LONG
            if fvg["type"] == "BEARISH_FVG":
                if c["close"] > fvg["top"] and candles[i-1]["close"] <= fvg["top"]:
                    fvg["status"] = "INVERTED"
                    
                    if require_trend and not (ema20[i] > ema50[i]):
                        continue
                        
                    if require_vol:
                        avg_vol = sum(vols[max(0, i-20):i]) / max(1, len(vols[max(0, i-20):i]))
                        if c["volume"] < 1.2 * avg_vol:
                            continue
                            
                    entry = c["close"]
                    recent_low = min(candles[k]["low"] for k in range(max(0, i-5), i+1))
                    min_sl = entry * (1.0 - 0.010)
                    sl = min(recent_low, min_sl)
                    
                    res = simulate_trade_path(entry, sl, "LONG", candles[i+1:])
                    if res:
                        signals.append({"bar": i, "side": "LONG", "entry": entry, "sl": sl, **res})
                        
            # BULLISH FVG inverted to RESISTANCE -> SHORT
            elif fvg["type"] == "BULLISH_FVG":
                if c["close"] < fvg["bottom"] and candles[i-1]["close"] >= fvg["bottom"]:
                    fvg["status"] = "INVERTED"
                    
                    if require_trend and not (ema20[i] < ema50[i]):
                        continue
                        
                    if require_btc_guard:
                        continue
                        
                    if require_vol:
                        avg_vol = sum(vols[max(0, i-20):i]) / max(1, len(vols[max(0, i-20):i]))
                        if c["volume"] < 1.2 * avg_vol:
                            continue
                            
                    entry = c["close"]
                    recent_high = max(candles[k]["high"] for k in range(max(0, i-5), i+1))
                    min_sl = entry * (1.0 + 0.010)
                    sl = max(recent_high, min_sl)
                    
                    res = simulate_trade_path(entry, sl, "SHORT", candles[i+1:])
                    if res:
                        signals.append({"bar": i, "side": "SHORT", "entry": entry, "sl": sl, **res})
                        
    return signals

btc_15m = fetch_klines("BTCUSDT", tf="15m", limit=500)
btc_times = {c["time"]: c for c in btc_15m}

all_candles = {}
for sym in universe:
    c = fetch_klines(sym, tf="15m", limit=500)
    if c and len(c) >= 100:
        all_candles[sym] = c

print(f"Loaded {len(all_candles)} coins successfully.")

def print_metrics(name, sigs, days=5.2):
    n = len(sigs)
    if n == 0:
        print(f"[{name}] No trades generated.")
        return
    cnt = Counter(s["outcome"] for s in sigs)
    r1 = sum(1 for s in sigs if s["hit_1r"])
    r2 = sum(1 for s in sigs if s["hit_2r"])
    bep = cnt["REVERSED_TO_BEP"]
    sl = cnt["DIRECT_SL"]
    
    net_r = (r2 * 1.5) - (sl * 1.0)
    profit = r2 * 3.00
    loss = sl * 2.00
    pf = (profit / loss) if loss > 0 else 99.0
    
    print(f"==================================================")
    print(f"=== {name} ===")
    print(f"==================================================")
    print(f"  Total Trades:     {n} ({n/days:.1f}/hari | {n/days/len(all_candles):.2f}/koin/hari)")
    print(f"  Hit 1R (BEP):     {r1} ({r1/n*100:.1f}%)")
    print(f"  Hit 2R (TP):      {r2} ({r2/n*100:.1f}%)")
    print(f"  BEP Reversal:     {bep} ({bep/n*100:.1f}%)")
    print(f"  Direct SL:        {sl} ({sl/n*100:.1f}%)")
    print(f"  Net R-Multiple:   {net_r:+.1f}R  |  Net USD ($2 risk): ${net_r * 2:+.2f}")
    print(f"  Profit Factor:    {pf:.2f}\n")

v1, v2, v3, v4 = [], [], [], []

for sym, candles in all_candles.items():
    v1.extend(run_ifvg_engine(candles, btc_times, min_gap_mult=0.5, require_trend=False, require_vol=False, require_btc_guard=False))
    v2.extend(run_ifvg_engine(candles, btc_times, min_gap_mult=0.5, require_trend=True, require_vol=False, require_btc_guard=False))
    v3.extend(run_ifvg_engine(candles, btc_times, min_gap_mult=0.5, require_trend=True, require_vol=False, require_btc_guard=True))
    v4.extend(run_ifvg_engine(candles, btc_times, min_gap_mult=0.5, require_trend=True, require_vol=True, require_btc_guard=True))

print_metrics("Variation 1: Raw IFVG (Gap >= 0.5 ATR)", v1)
print_metrics("Variation 2: IFVG + Trend Alignment (EMA20/50)", v2)
print_metrics("Variation 3: IFVG + Trend + Macro BTC Guard (Filter 1)", v3)
print_metrics("Variation 4: IFVG + Trend + Macro BTC + Volume Surge (>1.2x)", v4)
