import json
import urllib.request
from collections import Counter

NEXUS_API = "http://127.0.0.1:8000"

universe = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "NEARUSDT", "SUIUSDT", "AVAXUSDT", "LINKUSDT",
    "DOGEUSDT", "ADAUSDT", "XRPUSDT", "APTUSDT", "BNBUSDT", "DOTUSDT", "ARBUSDT",
    "OPUSDT", "TAOUSDT", "CRVUSDT", "AAVEUSDT", "FETUSDT", "ENAUSDT", "TIAUSDT",
    "INJUSDT", "SEIUSDT", "FTMUSDT", "WIFUSDT", "PEPEUSDT", "RENDERUSDT", "LDOUSDT"
]

def fetch_klines(symbol, tf='15m', limit=500):
    for exchange in ['bybit', 'binance']:
        try:
            url = f"{NEXUS_API}/klines/{exchange}/{symbol}?tf={tf}&limit={limit}"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                candles = []
                for c in data.get('data', []):
                    candles.append({
                        'open': float(c['open']),
                        'high': float(c['high']),
                        'low': float(c['low']),
                        'close': float(c['close']),
                        'volume': float(c.get('volume', 0))
                    })
                if candles:
                    return candles
        except Exception:
            pass
    return []

def calc_atr(candles, period=14):
    trs = []
    for i in range(len(candles)):
        if i == 0:
            trs.append(candles[i]['high'] - candles[i]['low'])
        else:
            h = candles[i]['high']
            l = candles[i]['low']
            pc = candles[i-1]['close']
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return [trs[-1]] * len(trs) if trs else [1.0]
    atr = [sum(trs[:period]) / period]
    for tr in trs[period:]:
        atr.append((atr[-1] * (period - 1) + tr) / period)
    return [atr[0]] * (period - 1) + atr

def calc_ema(values, period):
    if len(values) < period:
        return [values[-1]] * len(values) if values else []
    ema = [sum(values[:period]) / period]
    k = 2.0 / (period + 1.0)
    for v in values[period:]:
        ema.append(v * k + ema[-1] * (1.0 - k))
    return [ema[0]] * (period - 1) + ema

# Candle-by-candle simulation of:
# Partial TP at `tp_r` (fraction `tp_pct`, default 0.75)
# Runner (1 - tp_pct) managed by `trailing_mode`:
# - 'BEP': SL at entry
# - 'LOCK_HALF': SL at +0.5R
# - 'LOCK_1R': SL at +1.0R
# - 'STEP_TRAIL': Once reaches 2.5R -> SL 1.5R; 3.5R -> SL 2.5R, etc.
# - 'ATR_TRAIL': Trailing by 1.5x ATR behind peak
# - 'SWING_TRAIL': Trailing behind previous bar's low/high
def simulate_partial_and_trail(entry, sl, side, future_candles, tp_r=1.5, tp_pct=0.75, trailing_mode='BEP'):
    risk = abs(entry - sl)
    if risk <= 0 or not future_candles:
        return None

    runner_pct = 1.0 - tp_pct
    tp_price = entry + (tp_r * risk) if side == 'LONG' else entry - (tp_r * risk)
    
    hit_tp = False
    hit_sl_initial = False
    runner_closed = False
    
    tp_exit_r = 0.0
    runner_exit_r = 0.0
    runner_sl = None
    peak_price = entry
    
    for idx, c in enumerate(future_candles):
        h, l = c['high'], c['low']
        
        # Phase 1: Before TP is hit
        if not hit_tp:
            is_tp = (h >= tp_price) if side == 'LONG' else (l <= tp_price)
            is_sl = (l <= sl) if side == 'LONG' else (h >= sl)
            
            if is_sl and not is_tp:
                # Direct loss on 100% position
                return {
                    'outcome': 'DIRECT_SL',
                    'net_r': -1.0,
                    'hit_tp': False,
                    'runner_r': 0.0
                }
            elif is_tp:
                hit_tp = True
                tp_exit_r = tp_r
                peak_price = tp_price
                
                # Initialize runner stop
                if trailing_mode == 'BEP':
                    runner_sl = entry
                elif trailing_mode == 'LOCK_HALF':
                    runner_sl = entry + (0.5 * risk) if side == 'LONG' else entry - (0.5 * risk)
                elif trailing_mode == 'LOCK_1R':
                    runner_sl = entry + (1.0 * risk) if side == 'LONG' else entry - (1.0 * risk)
                elif trailing_mode in ['STEP_TRAIL', 'ATR_TRAIL', 'SWING_TRAIL']:
                    runner_sl = entry + (0.5 * risk) if side == 'LONG' else entry - (0.5 * risk)
                    
                # Note: if runner_pct == 0, we are done
                if runner_pct == 0:
                    runner_closed = True
                    runner_exit_r = tp_r
                    break
                    
        # Phase 2: After TP is hit, manage Runner
        else:
            # Update peak price
            if side == 'LONG' and h > peak_price:
                peak_price = h
            elif side == 'SHORT' and l < peak_price:
                peak_price = l
                
            cur_r = (peak_price - entry) / risk if side == 'LONG' else (entry - peak_price) / risk
            
            # Dynamic trailing adjustments
            if trailing_mode == 'STEP_TRAIL':
                # If peak reaches 2.5R, move stop to 1.5R
                if cur_r >= 2.5:
                    new_sl = entry + ((cur_r - 1.0) * risk) if side == 'LONG' else entry - ((cur_r - 1.0) * risk)
                    if side == 'LONG' and new_sl > runner_sl: runner_sl = new_sl
                    elif side == 'SHORT' and new_sl < runner_sl: runner_sl = new_sl
            elif trailing_mode == 'ATR_TRAIL':
                bar_atr = c.get('atr', risk * 0.5) # approximate
                trail_dist = max(risk * 0.8, bar_atr * 1.5)
                new_sl = peak_price - trail_dist if side == 'LONG' else peak_price + trail_dist
                if side == 'LONG' and new_sl > runner_sl: runner_sl = new_sl
                elif side == 'SHORT' and new_sl < runner_sl: runner_sl = new_sl
            elif trailing_mode == 'SWING_TRAIL' and idx > 0:
                prev_c = future_candles[idx - 1]
                new_sl = prev_c['low'] if side == 'LONG' else prev_c['high']
                if side == 'LONG' and new_sl > runner_sl: runner_sl = new_sl
                elif side == 'SHORT' and new_sl < runner_sl: runner_sl = new_sl

            # Check if runner hits stop
            is_runner_stopped = (l <= runner_sl) if side == 'LONG' else (h >= runner_sl)
            if is_runner_stopped:
                runner_closed = True
                runner_exit_r = (runner_sl - entry) / risk if side == 'LONG' else (entry - runner_sl) / risk
                break

    # If simulation ended while runner still open
    if hit_tp and not runner_closed:
        last_c = future_candles[-1]
        last_px = last_c['close']
        runner_exit_r = (last_px - entry) / risk if side == 'LONG' else (entry - last_px) / risk
        
    net_r = (tp_pct * tp_exit_r) + (runner_pct * runner_exit_r)
    return {
        'outcome': 'TP_AND_RUNNER',
        'net_r': net_r,
        'hit_tp': True,
        'tp_r': tp_exit_r,
        'runner_r': runner_exit_r
    }

# Gather candles
all_candles = {sym: fetch_klines(sym, '15m', 500) for sym in universe}
all_candles = {k: v for k, v in all_candles.items() if v and len(v) >= 100}

# Extract all trend-aligned IFVG setups
raw_setups = []
for sym, candles in all_candles.items():
    closes = [c['close'] for c in candles]
    ema20 = calc_ema(closes, 20)
    ema50 = calc_ema(closes, 50)
    atrs = calc_atr(candles, 14)
    active_fvgs = []
    
    for i in range(20, len(candles) - 15):
        c = candles[i]
        c1, c3 = candles[i-2], candles[i]
        atr = atrs[i]
        min_gap = atr * 0.5
        
        if c3['low'] - c1['high'] >= min_gap:
            active_fvgs.append({'type': 'BULLISH', 'top': c3['low'], 'bottom': c1['high'], 'bar': i, 'status': 'ACTIVE'})
        if c1['low'] - c3['high'] >= min_gap:
            active_fvgs.append({'type': 'BEARISH', 'top': c1['low'], 'bottom': c3['high'], 'bar': i, 'status': 'ACTIVE'})
            
        for fvg in active_fvgs:
            if fvg['status'] != 'ACTIVE' or (i - fvg['bar'] < 2) or (i - fvg['bar'] > 35):
                continue
            # LONG
            if fvg['type'] == 'BEARISH' and c['close'] > fvg['top'] and candles[i-1]['close'] <= fvg['top']:
                fvg['status'] = 'INVERTED'
                if ema20[i] > ema50[i]:
                    entry = c['close']
                    sl = min(min(candles[k]['low'] for k in range(max(0, i-5), i+1)), entry * 0.99)
                    raw_setups.append({
                        'sym': sym, 'side': 'LONG', 'entry': entry, 'sl': sl,
                        'future': candles[i+1:]
                    })
            # SHORT
            elif fvg['type'] == 'BULLISH' and c['close'] < fvg['bottom'] and candles[i-1]['close'] >= fvg['bottom']:
                fvg['status'] = 'INVERTED'
                if ema20[i] < ema50[i]:
                    entry = c['close']
                    sl = max(max(candles[k]['high'] for k in range(max(0, i-5), i+1)), entry * 1.01)
                    raw_setups.append({
                        'sym': sym, 'side': 'SHORT', 'entry': entry, 'sl': sl,
                        'future': candles[i+1:]
                    })

print(f"Total Trend-Aligned IFVG Setups to evaluate: {len(raw_setups)}\n")

def evaluate_configuration(name, tp_r, tp_pct, trailing_mode):
    results = []
    for s in raw_setups:
        res = simulate_partial_and_trail(s['entry'], s['sl'], s['side'], s['future'], tp_r=tp_r, tp_pct=tp_pct, trailing_mode=trailing_mode)
        if res:
            results.append(res)
            
    n = len(results)
    wins = [r for r in results if r['net_r'] > 0]
    losses = [r for r in results if r['net_r'] < 0]
    beps = [r for r in results if r['net_r'] == 0]
    
    gross_profit_r = sum(r['net_r'] for r in wins)
    gross_loss_r = abs(sum(r['net_r'] for r in losses))
    net_r = gross_profit_r - gross_loss_r
    pf = (gross_profit_r / gross_loss_r) if gross_loss_r > 0 else 99.0
    wr = len(wins) / n * 100 if n else 0
    
    # Calculate runner performance
    runner_rs = [r['runner_r'] for r in wins if r.get('hit_tp')]
    avg_runner_r = sum(runner_rs) / len(runner_rs) if runner_rs else 0.0
    max_runner_r = max(runner_rs) if runner_rs else 0.0
    
    return {
        'name': name,
        'wr': wr,
        'wins': len(wins),
        'losses': len(losses),
        'gross_win': gross_profit_r,
        'gross_loss': gross_loss_r,
        'net_r': net_r,
        'net_usd': net_r * 2.0, # $2 risk per trade
        'pf': pf,
        'avg_runner_r': avg_runner_r,
        'max_runner_r': max_runner_r
    }

configs = [
    # Benchmark Case 1
    ("Benchmark Case 1: 100% Fixed TP 1.5R (No trailing)", 1.5, 1.0, 'BEP'),
    
    # 75% TP at 1.5R with various runner trailing styles
    ("Skema 1A: 75% TP di 1.5R + Runner SL di BEP (0.0R)", 1.5, 0.75, 'BEP'),
    ("Skema 1B: 75% TP di 1.5R + Runner SL di Lock +0.5R", 1.5, 0.75, 'LOCK_HALF'),
    ("Skema 1C: 75% TP di 1.5R + Runner SL di Lock +1.0R", 1.5, 0.75, 'LOCK_1R'),
    ("Skema 1D: 75% TP di 1.5R + Runner Step Trailing (Trail saat >=2.5R)", 1.5, 0.75, 'STEP_TRAIL'),
    ("Skema 1E: 75% TP di 1.5R + Runner Swing Trailing (Prev Bar Low/High)", 1.5, 0.75, 'SWING_TRAIL'),
    
    # Different TP levels with 75% TP + Lock +0.5R
    ("Skema 2A: 75% TP di 1.3R + Runner Lock +0.5R", 1.3, 0.75, 'LOCK_HALF'),
    ("Skema 2B: 75% TP di 1.4R + Runner Lock +0.5R", 1.4, 0.75, 'LOCK_HALF'),
    ("Skema 2C: 75% TP di 1.6R + Runner Lock +0.5R", 1.6, 0.75, 'LOCK_HALF'),

    # Different sizing ratios with 1.5R TP + Lock +0.5R
    ("Skema 3A: 70% TP di 1.5R + 30% Runner (Lock +0.5R)", 1.5, 0.70, 'LOCK_HALF'),
    ("Skema 3B: 80% TP di 1.5R + 20% Runner (Lock +0.5R)", 1.5, 0.80, 'LOCK_HALF'),
    ("Skema 3C: 50% TP di 1.5R + 50% Runner (Lock +0.5R)", 1.5, 0.50, 'LOCK_HALF'),
]

print("="*85)
print(f"{'Konfigurasi / Skema':<52} | {'WR':<6} | {'Net R':<8} | {'Net USD':<9} | {'PF':<5}")
print("="*85)

all_res = []
for name, tp_r, tp_pct, t_mode in configs:
    res = evaluate_configuration(name, tp_r, tp_pct, t_mode)
    all_res.append(res)
    print(f"{res['name']:<52} | {res['wr']:<5.1f}% | {res['net_r']:>+7.1f}R | ${res['net_usd']:>+7.2f} | {res['pf']:<5.2f}")

print("="*85)

# Sort by Profit Factor and Net USD
best_pf = sorted(all_res, key=lambda x: x['pf'], reverse=True)[0]
best_net = sorted(all_res, key=lambda x: x['net_usd'], reverse=True)[0]

print(f"\nJuara Profit Factor Tertinggi : {best_pf['name']} (PF: {best_pf['pf']:.2f}, Net: ${best_pf['net_usd']:+.2f})")
print(f"Juara Net Profit Tertinggi    : {best_net['name']} (PF: {best_net['pf']:.2f}, Net: ${best_net['net_usd']:+.2f})")
