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

all_candles = {sym: fetch_klines(sym, '15m', 500) for sym in universe}
all_candles = {k: v for k, v in all_candles.items() if v and len(v) >= 100}

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
            if fvg['type'] == 'BEARISH' and c['close'] > fvg['top'] and candles[i-1]['close'] <= fvg['top']:
                fvg['status'] = 'INVERTED'
                if ema20[i] > ema50[i]:
                    entry = c['close']
                    sl = min(min(candles[k]['low'] for k in range(max(0, i-5), i+1)), entry * 0.99)
                    raw_setups.append({'sym': sym, 'side': 'LONG', 'entry': entry, 'sl': sl, 'future': candles[i+1:]})
            elif fvg['type'] == 'BULLISH' and c['close'] < fvg['bottom'] and candles[i-1]['close'] >= fvg['bottom']:
                fvg['status'] = 'INVERTED'
                if ema20[i] < ema50[i]:
                    entry = c['close']
                    sl = max(max(candles[k]['high'] for k in range(max(0, i-5), i+1)), entry * 1.01)
                    raw_setups.append({'sym': sym, 'side': 'SHORT', 'entry': entry, 'sl': sl, 'future': candles[i+1:]})

def test_runner_advanced(tp1_r, tp1_pct, runner_lock_r, runner_tp2_r, trail_type):
    trades = []
    for s in raw_setups:
        entry = s['entry']
        sl = s['sl']
        side = s['side']
        future = s['future']
        risk = abs(entry - sl)
        if risk <= 0 or not future:
            continue
            
        tp1_px = entry + (tp1_r * risk) if side == 'LONG' else entry - (tp1_r * risk)
        tp2_px = entry + (runner_tp2_r * risk) if side == 'LONG' else entry - (runner_tp2_r * risk)
        
        hit_tp1 = False
        runner_sl = None
        tp1_exit_r = 0.0
        runner_exit_r = 0.0
        runner_done = False
        
        for idx, c in enumerate(future):
            h, l = c['high'], c['low']
            if not hit_tp1:
                is_tp1 = (h >= tp1_px) if side == 'LONG' else (l <= tp1_px)
                is_sl = (l <= sl) if side == 'LONG' else (h >= sl)
                if is_sl and not is_tp1:
                    trades.append({'net_r': -1.0, 'hit_tp1': False})
                    break
                elif is_tp1:
                    hit_tp1 = True
                    tp1_exit_r = tp1_r
                    runner_sl = entry + (runner_lock_r * risk) if side == 'LONG' else entry - (runner_lock_r * risk)
            else:
                # Check runner TP2
                is_tp2 = (h >= tp2_px) if side == 'LONG' else (l <= tp2_px)
                if is_tp2:
                    runner_exit_r = runner_tp2_r
                    runner_done = True
                    break
                
                # Check dynamic trailing
                if trail_type == 'SWING' and idx > 0:
                    prev_c = future[idx-1]
                    dyn_sl = prev_c['low'] if side == 'LONG' else prev_c['high']
                    if side == 'LONG' and dyn_sl > runner_sl: runner_sl = dyn_sl
                    elif side == 'SHORT' and dyn_sl < runner_sl: runner_sl = dyn_sl
                elif trail_type == 'STEP':
                    cur_mfe = (h - entry) / risk if side == 'LONG' else (entry - l) / risk
                    if cur_mfe >= 2.2:
                        step_sl = entry + (1.5 * risk) if side == 'LONG' else entry - (1.5 * risk)
                        if side == 'LONG' and step_sl > runner_sl: runner_sl = step_sl
                        elif side == 'SHORT' and step_sl < runner_sl: runner_sl = step_sl
                        
                # Check runner stopped
                is_runner_sl = (l <= runner_sl) if side == 'LONG' else (h >= runner_sl)
                if is_runner_sl:
                    runner_exit_r = (runner_sl - entry) / risk if side == 'LONG' else (entry - runner_sl) / risk
                    runner_done = True
                    break
                    
        if hit_tp1:
            if not runner_done:
                last_px = future[-1]['close']
                runner_exit_r = (last_px - entry) / risk if side == 'LONG' else (entry - last_px) / risk
            net_r = (tp1_pct * tp1_exit_r) + ((1.0 - tp1_pct) * runner_exit_r)
            trades.append({'net_r': net_r, 'hit_tp1': True, 'runner_r': runner_exit_r})
            
    n = len(trades)
    wins = [t for t in trades if t['net_r'] > 0]
    losses = [t for t in trades if t['net_r'] < 0]
    gross_win = sum(t['net_r'] for t in wins)
    gross_loss = abs(sum(t['net_r'] for t in losses))
    net_r = gross_win - gross_loss
    pf = (gross_win / gross_loss) if gross_loss > 0 else 99.0
    return {
        'wr': len(wins) / n * 100 if n else 0,
        'net_r': net_r,
        'net_usd': net_r * 2.0,
        'pf': pf,
        'wins': len(wins),
        'losses': len(losses)
    }

print("\n--- PENGUJIAN SKENARIO HYBRID TP1 75% + RUNNER 25% ---")
scenarios = [
    ("A1: TP1 75% di 1.5R, Runner Lock +0.5R, TP2 2.5R (No dynamic trail)", 1.5, 0.75, 0.5, 2.5, 'NONE'),
    ("A2: TP1 75% di 1.5R, Runner Lock +1.0R, TP2 2.5R (No dynamic trail)", 1.5, 0.75, 1.0, 2.5, 'NONE'),
    ("A3: TP1 75% di 1.5R, Runner Lock +1.0R, TP2 3.0R + Step Trail",       1.5, 0.75, 1.0, 3.0, 'STEP'),
    ("A4: TP1 75% di 1.5R, Runner Lock +1.0R, TP2 3.0R + Swing Trail",      1.5, 0.75, 1.0, 3.0, 'SWING'),
    ("A5: TP1 75% di 1.5R, Runner Lock +0.8R, TP2 2.5R + Swing Trail",      1.5, 0.75, 0.8, 2.5, 'SWING'),
    ("B1: TP1 80% di 1.5R, Runner 20% Lock +1.0R, TP2 3.0R + Swing Trail",  1.5, 0.80, 1.0, 3.0, 'SWING'),
    ("B2: TP1 70% di 1.5R, Runner 30% Lock +1.0R, TP2 3.0R + Swing Trail",  1.5, 0.70, 1.0, 3.0, 'SWING'),
]

for name, tp1_r, tp1_pct, lock_r, tp2_r, tr_mode in scenarios:
    r = test_runner_advanced(tp1_r, tp1_pct, lock_r, tp2_r, tr_mode)
    print(f"{name:<68} | WR: {r['wr']:4.1f}% | Net: {r['net_r']:>+5.1f}R (${r['net_usd']:>+6.2f}) | PF: {r['pf']:4.2f}")
