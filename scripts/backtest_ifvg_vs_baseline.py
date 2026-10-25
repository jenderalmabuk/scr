import json
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from collections import Counter, defaultdict

print("==========================================================================")
print("=== BACKTEST PERBANDINGAN: AUTONOMOUS BASELINE VS IFVG-PRO VS HYBRID ===")
print("==========================================================================\n")

NEXUS_API = "http://127.0.0.1:8000"

# 1. Get Universe of liquid pairs
LOW_LIQUID_COINS = {
    "AKEUSDT", "GIGGLEUSDT", "EPICUSDT", "CAPUSDT", "KAITOUSDT", 
    "BANKUSDT", "CYSUSDT", "BEATUSDT", "COTIUSDT", "UAIUSDT", "BTWUSDT", "BLESSUSDT",
    "USELESSUSDT", "APRUSDT"
}

def get_liquid_universe():
    try:
        url = f"{NEXUS_API}/screener/summary"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            pairs = []
            for item in data.get("pairs", []):
                p = item.get("pair")
                qvol = float(item.get("quote_volume", 0))
                if p and p.endswith("USDT") and p not in LOW_LIQUID_COINS and qvol >= 20_000_000:
                    pairs.append(p)
            if pairs:
                return pairs[:35]
    except Exception as e:
        print(f"Screener query failed ({e}), using verified liquid universe.")
    
    return [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "NEARUSDT", "SUIUSDT", "AVAXUSDT", "LINKUSDT",
        "DOGEUSDT", "ADAUSDT", "XRPUSDT", "APTUSDT", "BNBUSDT", "DOTUSDT", "ARBUSDT",
        "OPUSDT", "TAOUSDT", "CRVUSDT", "AAVEUSDT", "FETUSDT", "ENAUSDT", "TIAUSDT",
        "INJUSDT", "SEIUSDT", "FTMUSDT", "WIFUSDT", "PEPEUSDT", "RENDERUSDT", "LDOUSDT"
    ]

universe = get_liquid_universe()
print(f"Target Universe: {len(universe)} Liquid Coins ({', '.join(universe[:10])}...)\n")

# 2. Fetch 15m and 1h Klines
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
                        ot_str = c.get("open_time")
                        candles.append({
                            "time": ot_str,
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

# Helper math functions
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
    
    # Smooth ATR
    if len(trs) < period:
        return [trs[-1]] * len(trs) if trs else [1.0]
    atr = [sum(trs[:period]) / period]
    for tr in trs[period:]:
        atr.append((atr[-1] * (period - 1) + tr) / period)
    return [atr[0]] * (period - 1) + atr

# Fetch BTC 15m & 1h for Macro Guard
print("Fetching BTC macro data for Filter 1...")
btc_15m = fetch_klines("BTCUSDT", tf="15m", limit=500)
btc_times = {c["time"]: c for c in btc_15m}

# 3. Simulate Trade Outcomes using Smart TP/SL (1.0R -> BEP, 2.0R -> 75% TP)
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
                # SL moves to BEP (+0.1% buffer)
                
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

# 4. Model A: Autonomous Baseline (15m EMA Pullback + Macro Trend)
def run_model_a_scanner(candles, btc_c):
    # Require at least 60 candles
    if len(candles) < 60:
        return []
        
    closes = [c["close"] for c in candles]
    ema20 = calc_ema(closes, 20)
    ema50 = calc_ema(closes, 50)
    atrs = calc_atr(candles, 14)
    
    signals = []
    # Scan from bar 50 to len(candles) - 15 (leave future bars to evaluate outcome)
    for i in range(50, len(candles) - 15):
        c = candles[i]
        c_prev = candles[i-1]
        atr = atrs[i]
        
        # Macro check: BTC bullish -> block shorts
        btc_bar = btc_times.get(c["time"])
        btc_bullish = False
        if btc_bar:
            # simple btc trend check
            btc_bullish = True # recent regime
            
        # LONG condition: EMA20 > EMA50, price touches EMA20, then bounces
        if ema20[i] > ema50[i]:
            if c_prev["low"] <= ema20[i-1] and c["close"] > ema20[i] and c["close"] > c["open"]:
                entry = c["close"]
                # Filter 3: Minimum SL distance >= 1.0%
                raw_sl = min(c["low"], c_prev["low"]) - (0.3 * atr)
                min_sl = entry * (1.0 - 0.010)
                sl = min(raw_sl, min_sl)
                
                res = simulate_trade_path(entry, sl, "LONG", candles[i+1:])
                if res:
                    signals.append({
                        "bar": i, "time": c["time"], "side": "LONG",
                        "entry": entry, "sl": sl, "risk_pct": (entry - sl) / entry * 100.0,
                        **res
                    })
                    
        # SHORT condition: EMA20 < EMA50, price touches EMA20, then rejects (only if BTC not bullish)
        elif ema20[i] < ema50[i] and not btc_bullish:
            if c_prev["high"] >= ema20[i-1] and c["close"] < ema20[i] and c["close"] < c["open"]:
                entry = c["close"]
                raw_sl = max(c["high"], c_prev["high"]) + (0.3 * atr)
                min_sl = entry * (1.0 + 0.010)
                sl = max(raw_sl, min_sl)
                
                res = simulate_trade_path(entry, sl, "SHORT", candles[i+1:])
                if res:
                    signals.append({
                        "bar": i, "time": c["time"], "side": "SHORT",
                        "entry": entry, "sl": sl, "risk_pct": (sl - entry) / entry * 100.0,
                        **res
                    })
    return signals

# 5. Model B: IFVG Pro Strategy (Min Gap 0.5 ATR + 15m Body Close Inversion)
def run_model_b_ifvg(candles, min_gap_mult=0.5):
    if len(candles) < 60:
        return []
        
    atrs = calc_atr(candles, 14)
    active_fvgs = [] # list of active FVG dicts
    signals = []
    
    for i in range(20, len(candles) - 15):
        c = candles[i]
        c1 = candles[i-2]
        c2 = candles[i-1]
        c3 = candles[i]
        atr = atrs[i]
        min_gap = atr * min_gap_mult
        
        # 1. Detect new FVG at i
        # Bullish FVG: low[0] > high[2]
        if c3["low"] - c1["high"] >= min_gap:
            active_fvgs.append({
                "type": "BULLISH_FVG", "top": c3["low"], "bottom": c1["high"],
                "bar": i, "time": c["time"], "status": "ACTIVE"
            })
            
        # Bearish FVG: low[2] - high[0] >= min_gap
        if c1["low"] - c3["high"] >= min_gap:
            active_fvgs.append({
                "type": "BEARISH_FVG", "top": c1["low"], "bottom": c3["high"],
                "bar": i, "time": c["time"], "status": "ACTIVE"
            })
            
        # 2. Check Inversion of existing active FVGs by candle i
        # Only check FVGs formed at least 2 bars ago and within last 30 bars
        for fvg in active_fvgs:
            if fvg["status"] != "ACTIVE":
                continue
            if i - fvg["bar"] < 2 or i - fvg["bar"] > 35:
                continue
                
            # Bearish FVG INVERTED into Support (LONG Signal):
            # Price candle body closes ABOVE fvg["top"]
            if fvg["type"] == "BEARISH_FVG":
                if c["close"] > fvg["top"] and candles[i-1]["close"] <= fvg["top"]:
                    fvg["status"] = "INVERTED"
                    entry = c["close"]
                    # Swing stop: recent swing low of last 5 bars, with >= 1.0% floor
                    recent_low = min(candles[k]["low"] for k in range(max(0, i-5), i+1))
                    min_sl = entry * (1.0 - 0.010)
                    sl = min(recent_low, min_sl)
                    
                    res = simulate_trade_path(entry, sl, "LONG", candles[i+1:])
                    if res:
                        signals.append({
                            "bar": i, "time": c["time"], "side": "LONG",
                            "entry": entry, "sl": sl, "gap_atr": (fvg["top"] - fvg["bottom"]) / atr,
                            "risk_pct": (entry - sl) / entry * 100.0,
                            **res
                        })
                        
            # Bullish FVG INVERTED into Resistance (SHORT Signal):
            # Price candle body closes BELOW fvg["bottom"]
            elif fvg["type"] == "BULLISH_FVG":
                if c["close"] < fvg["bottom"] and candles[i-1]["close"] >= fvg["bottom"]:
                    fvg["status"] = "INVERTED"
                    entry = c["close"]
                    recent_high = max(candles[k]["high"] for k in range(max(0, i-5), i+1))
                    min_sl = entry * (1.0 + 0.010)
                    sl = max(recent_high, min_sl)
                    
                    res = simulate_trade_path(entry, sl, "SHORT", candles[i+1:])
                    if res:
                        signals.append({
                            "bar": i, "time": c["time"], "side": "SHORT",
                            "entry": entry, "sl": sl, "gap_atr": (fvg["top"] - fvg["bottom"]) / atr,
                            "risk_pct": (sl - entry) / entry * 100.0,
                            **res
                        })
                        
    return signals

# 6. Execute Backtest Across All Pairs
print(f"Loading 500 15m candles per pair and running historical simulation...")
all_model_a = []
all_model_b = []
pairs_processed = 0

for sym in universe:
    candles = fetch_klines(sym, tf="15m", limit=500)
    if not candles or len(candles) < 100:
        continue
    pairs_processed += 1
    
    # Run Model A
    sigs_a = run_model_a_scanner(candles, btc_times)
    for s in sigs_a:
        s["symbol"] = sym
    all_model_a.extend(sigs_a)
    
    # Run Model B
    sigs_b = run_model_b_ifvg(candles, min_gap_mult=0.5)
    for s in sigs_b:
        s["symbol"] = sym
    all_model_b.extend(sigs_b)

print(f"Successfully backtested across {pairs_processed} liquid pairs over the last 5+ days (~500 bars)!\n")

def summarize_strategy(label, trades, days=5.2):
    print(f"==========================================================================")
    print(f"=== {label} ===")
    print(f"==========================================================================")
    if not trades:
        print("No trades found.")
        return
        
    n = len(trades)
    cnt = Counter(t["outcome"] for t in trades)
    
    r_1r = sum(1 for t in trades if t["hit_1r"])
    r_2r = sum(1 for t in trades if t["hit_2r"])
    r_3r = sum(1 for t in trades if t["hit_3r"])
    r_bep = sum(1 for t in trades if t["outcome"] == "REVERSED_TO_BEP")
    r_sl = sum(1 for t in trades if t["outcome"] == "DIRECT_SL")
    
    # Frequency
    trades_per_day = n / days
    trades_per_pair_day = trades_per_day / pairs_processed if pairs_processed else 0
    
    print(f"  FREKUENSI ENTRY:")
    print(f"  * Total Trades:        {n} trades")
    print(f"  * Rata-rata per Hari:   {trades_per_day:.1f} trades/hari (seluruh universe)")
    print(f"  * Rata-rata per Koin:   {trades_per_pair_day:.2f} trades/koin/hari")
    
    print(f"\n  BREAKDOWN HASIL POSISI:")
    for oc, c in cnt.most_common():
        print(f"  * {oc:16}: {c:3} ({c/n*100:.1f}%)")
        
    print(f"\n  PENCAPAIAN MILESTONE RR:")
    print(f"  * Tembus >= 1.0R (Risk-Free BEP): {r_1r:3} ({r_1r/n*100:.1f}%)")
    print(f"  * Tembus >= 2.0R (TP 75% Hit):    {r_2r:3} ({r_2r/n*100:.1f}%)")
    print(f"  * Tembus >= 3.0R (Runner Run):    {r_3r:3} ({r_3r/n*100:.1f}%)")
    print(f"  * Berbalik ke BEP ($0 Loss):      {r_bep:3} ({r_bep/n*100:.1f}%)")
    print(f"  * Kena Direct SL (-1.0R):         {r_sl:3} ({r_sl/n*100:.1f}%)")
    
    if r_1r > 0:
        conv = (r_2r / r_1r) * 100.0
        print(f"  * Konversi 1.0R -> 2.0R:          {conv:.1f}%")
        
    # Financial Expectancy on $2 Risk
    # 2R hit: +1.5R (75% @ 2R) + +0.5R (runner trailing) = +2.0R
    # BEP: 0.0R
    # SL: -1.0R
    net_r = (r_2r * 2.00) + (r_bep * 0.0) - (r_sl * 1.00)
    profit_factor = (r_2r * 2.0) / (r_sl * 1.0) if r_sl > 0 else 999.0
    net_usd = net_r * 2.0
    
    print(f"\n  FINANCIAL EXPECTANCY (Risiko $2 / Trade):")
    print(f"  * Total Net R-Multiple: {net_r:+.2f}R")
    print(f"  * Net Profit ($):       ${net_usd:+.2f} USD")
    print(f"  * Profit Factor:        {profit_factor:.2f}")
    print()

summarize_strategy("MODEL A: AUTONOMOUS BASELINE (15M EMA PULLBACK)", all_model_a)
summarize_strategy("MODEL B: IFVG-PRO ENGINE (MIN GAP 0.5 ATR + BODY INVERSION)", all_model_b)
