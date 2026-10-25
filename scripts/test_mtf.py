import json
import urllib.request

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
                        'time': c.get('open_time'),
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

candles_15m = {sym: fetch_klines(sym, '15m', 500) for sym in universe}
candles_1h  = {sym: fetch_klines(sym, '1h', 200) for sym in universe}

# Match 1h timestamps
def get_1h_trend(c_1h, target_time):
    # Find latest 1h candle before target_time
    if not c_1h: return 'NEUTRAL'
    closes = [c['close'] for c in c_1h]
    ema20 = calc_ema(closes, 20)
    ema50 = calc_ema(closes, 50)
    
    # Target 1h bar
    found_idx = -1
    for i, c in enumerate(c_1h):
        if c['time'] <= target_time:
            found_idx = i
        else:
            break
    if found_idx < 50: return 'NEUTRAL'
    if ema20[found_idx] > ema50[found_idx]: return 'BULLISH'
    if ema20[found_idx] < ema50[found_idx]: return 'BEARISH'
    return 'NEUTRAL'

trades_mtf = []
trades_single = []

for sym in universe:
    c15 = candles_15m.get(sym, [])
    c1h = candles_1h.get(sym, [])
    if len(c15) < 60: continue
    
    closes = [c['close'] for c in c15]
    ema20 = calc_ema(closes, 20)
    ema50 = calc_ema(closes, 50)
    atrs = calc_atr(c15, 14)
    active_fvgs = []
    
    for i in range(20, len(c15) - 15):
        c = c15[i]
        c1, c3 = c15[i-2], c15[i]
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
            if fvg['type'] == 'BEARISH' and c['close'] > fvg['top'] and c15[i-1]['close'] <= fvg['top']:
                fvg['status'] = 'INVERTED'
                entry = c['close']
                sl = min(min(c15[k]['low'] for k in range(max(0, i-5), i+1)), entry * 0.99)
                
                # Single TF (15m trend)
                if ema20[i] > ema50[i]:
                    res = simulate_golden_exit(entry, sl, 'LONG', c15[i+1:])
                    if res: trades_single.append(res)
                    
                    # MTF check (1h trend also bullish)
                    h_trend = get_1h_trend(c1h, c['time'])
                    if h_trend == 'BULLISH':
                        trades_mtf.append(res)
                        
            # SHORT
            elif fvg['type'] == 'BULLISH' and c['close'] < fvg['bottom'] and c15[i-1]['close'] >= fvg['bottom']:
                fvg['status'] = 'INVERTED'
                entry = c['close']
                sl = max(max(c15[k]['high'] for k in range(max(0, i-5), i+1)), entry * 1.01)
                
                if ema20[i] < ema50[i]:
                    res = simulate_golden_exit(entry, sl, 'SHORT', c15[i+1:])
                    if res: trades_single.append(res)
                    
                    h_trend = get_1h_trend(c1h, c['time'])
                    if h_trend == 'BEARISH':
                        trades_mtf.append(res)

def report(name, tr):
    n = len(tr)
    wins = [t for t in tr if t['net_r'] > 0]
    losses = [t for t in tr if t['net_r'] < 0]
    gw = sum(t['net_r'] for t in wins)
    gl = abs(sum(t['net_r'] for t in losses))
    net = gw - gl
    pf = gw / gl if gl > 0 else 99.0
    print(f"=== {name} ===")
    print(f"  Total Trades:     {n} ({n/5.2:4.1f}/hari | {n/5.2/26:4.2f}/koin/hari)")
    print(f"  Win Rate:         {len(wins)/n*100:4.1f}% ({len(wins)}W / {len(losses)}L)")
    print(f"  Net R-Multiple:   {net:>+5.1f}R | Net USD: ${net*2:>+6.2f}")
    print(f"  Profit Factor:    {pf:4.2f}\n")

print("\n--- PERBANDINGAN SINGLE TIMEFRAME (15m) VS MULTI-TIMEFRAME (1h Trend + 15m IFVG) ---")
report("Model 15m Trend Only", trades_single)
report("Model MTF (1h Macro Trend + 15m IFVG Trigger)", trades_mtf)
