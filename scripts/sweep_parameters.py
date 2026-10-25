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

def simulate_golden_exit(entry, sl, side, future_candles):
    risk = abs(entry - sl)
    if risk <= 0 or not future_candles:
        return None
    tp1_px = entry + (1.5 * risk) if side == 'LONG' else entry - (1.5 * risk)
    tp2_px = entry + (3.0 * risk) if side == 'LONG' else entry - (3.0 * risk)
    
    hit_tp1 = False
    runner_sl = None
    runner_exit_r = 0.0
    runner_done = False
    
    for idx, c in enumerate(future_candles):
        h, l = c['high'], c['low']
        if not hit_tp1:
            is_tp1 = (h >= tp1_px) if side == 'LONG' else (l <= tp1_px)
            is_sl = (l <= sl) if side == 'LONG' else (h >= sl)
            if is_sl and not is_tp1:
                return {'net_r': -1.0, 'hit_tp1': False}
            elif is_tp1:
                hit_tp1 = True
                runner_sl = entry + (1.0 * risk) if side == 'LONG' else entry - (1.0 * risk)
        else:
            is_tp2 = (h >= tp2_px) if side == 'LONG' else (l <= tp2_px)
            if is_tp2:
                runner_exit_r = 3.0
                runner_done = True
                break
                
            if idx > 0:
                prev_c = future_candles[idx - 1]
                dyn_sl = prev_c['low'] if side == 'LONG' else prev_c['high']
                if side == 'LONG' and dyn_sl > runner_sl: runner_sl = dyn_sl
                elif side == 'SHORT' and dyn_sl < runner_sl: runner_sl = dyn_sl
                
            is_stopped = (l <= runner_sl) if side == 'LONG' else (h >= runner_sl)
            if is_stopped:
                runner_exit_r = (runner_sl - entry) / risk if side == 'LONG' else (entry - runner_sl) / risk
                runner_done = True
                break
                
    if hit_tp1:
        if not runner_done:
            last_c = future_candles[-1]
            last_px = last_c['close']
            runner_exit_r = (last_px - entry) / risk if side == 'LONG' else (entry - last_px) / risk
        net_r = (0.75 * 1.5) + (0.25 * runner_exit_r)
        return {'net_r': net_r, 'hit_tp1': True, 'runner_r': runner_exit_r}
    return {'net_r': -1.0, 'hit_tp1': False}

def run_backtest_with_params(candles_dict, min_gap_mult=0.5):
    trades = []
    for sym, candles in candles_dict.items():
        if len(candles) < 60: continue
        closes = [c['close'] for c in candles]
        ema20 = calc_ema(closes, 20)
        ema50 = calc_ema(closes, 50)
        atrs = calc_atr(candles, 14)
        active_fvgs = []
        
        for i in range(20, len(candles) - 15):
            c = candles[i]
            c1, c3 = candles[i-2], candles[i]
            atr = atrs[i]
            min_gap = atr * min_gap_mult
            
            if c3['low'] - c1['high'] >= min_gap:
                active_fvgs.append({'type': 'BULLISH', 'top': c3['low'], 'bottom': c1['high'], 'bar': i, 'status': 'ACTIVE'})
            if c1['low'] - c3['high'] >= min_gap:
                active_fvgs.append({'type': 'BEARISH', 'top': c1['low'], 'bottom': c3['high'], 'bar': i, 'status': 'ACTIVE'})
                
            for fvg in active_fvgs:
                if fvg['status'] != 'ACTIVE' or (i - fvg['bar'] < 2) or (i - fvg['bar'] > 35):
                    continue
                if fvg['type'] == 'BEARISH' and c['close'] > fvg['top'] and candles[i-1]['close'] <= fvg['top']:
                    fvg['status'] = 'INVERTED'
                    if ema20[i] > ema50[i]:
                        entry = c['close']
                        sl = min(min(candles[k]['low'] for k in range(max(0, i-5), i+1)), entry * 0.99)
                        res = simulate_golden_exit(entry, sl, 'LONG', candles[i+1:])
                        if res: trades.append(res)
                elif fvg['type'] == 'BULLISH' and c['close'] < fvg['bottom'] and candles[i-1]['close'] >= fvg['bottom']:
                    fvg['status'] = 'INVERTED'
                    if ema20[i] < ema50[i]:
                        entry = c['close']
                        sl = max(max(candles[k]['high'] for k in range(max(0, i-5), i+1)), entry * 1.01)
                        res = simulate_golden_exit(entry, sl, 'SHORT', candles[i+1:])
                        if res: trades.append(res)
                        
    n = len(trades)
    if n == 0: return {'n': 0, 'wr': 0, 'net_r': 0, 'pf': 0}
    wins = [t for t in trades if t['net_r'] > 0]
    losses = [t for t in trades if t['net_r'] < 0]
    gwin = sum(t['net_r'] for t in wins)
    gloss = abs(sum(t['net_r'] for t in losses))
    net_r = gwin - gloss
    pf = gwin / gloss if gloss > 0 else 99.0
    return {
        'n': n,
        'wr': len(wins) / n * 100,
        'net_r': net_r,
        'net_usd': net_r * 2.0,
        'pf': pf
    }

# 1. Sweep GAP parameters on 15m
print("Loading 15m candles for 26 pairs...")
candles_15m = {sym: fetch_klines(sym, '15m', 500) for sym in universe}
candles_15m = {k: v for k, v in candles_15m.items() if v and len(v) >= 100}

print("\n=== SWEEP PARAMETER MIN GAP (Timeframe 15m) ===")
gap_results = {}
for gap in [0.2, 0.3, 0.5, 0.75, 1.0]:
    res = run_backtest_with_params(candles_15m, min_gap_mult=gap)
    gap_results[gap] = res
    print(f"Min Gap {gap:4.2f} ATR | Trades: {res['n']:3d} ({res['n']/5.2:4.1f}/hari) | WR: {res['wr']:4.1f}% | Net R: {res['net_r']:>+5.1f}R (${res['net_usd']:>+6.2f}) | PF: {res['pf']:4.2f}")

# 2. Sweep TIMEFRAMES with best gap
print("\n=== SWEEP TIMEFRAME (Min Gap 0.5 ATR) ===")
for tf, days in [('5m', 1.74), ('15m', 5.2), ('1h', 20.8)]:
    print(f"Loading {tf} data for universe...")
    tf_candles = {sym: fetch_klines(sym, tf, 500) for sym in universe}
    tf_candles = {k: v for k, v in tf_candles.items() if v and len(v) >= 100}
    res = run_backtest_with_params(tf_candles, min_gap_mult=0.5)
    print(f"Timeframe {tf:<4} | Period: ~{days:4.1f} hari | Trades: {res['n']:3d} ({res['n']/days:4.1f}/hari) | WR: {res['wr']:4.1f}% | Net R: {res['net_r']:>+5.1f}R (${res['net_usd']:>+6.2f}) | PF: {res['pf']:4.2f}")
