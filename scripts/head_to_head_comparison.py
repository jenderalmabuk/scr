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

# 1. Old Exit Logic (Current Baseline: 1.0R Move SL to BEP with 0% TP, TP2 at 2.0R 75%)
def simulate_old_exit(entry, sl, side, future_candles):
    risk = abs(entry - sl)
    if risk <= 0 or not future_candles:
        return None
    tp1_px = entry + risk if side == 'LONG' else entry - risk
    tp2_px = entry + (2.0 * risk) if side == 'LONG' else entry - (2.0 * risk)
    
    hit_1r = False
    for c in future_candles:
        h, l = c['high'], c['low']
        if not hit_1r:
            is_1r = (h >= tp1_px) if side == 'LONG' else (l <= tp1_px)
            is_sl = (l <= sl) if side == 'LONG' else (h >= sl)
            if is_sl and not is_1r:
                return {'outcome': 'DIRECT_SL', 'net_r': -1.0}
            elif is_1r:
                hit_1r = True
        else: # Stop at BEP (0% profit taken yet)
            is_2r = (h >= tp2_px) if side == 'LONG' else (l <= tp2_px)
            is_bep = (l <= entry * 1.001) if side == 'LONG' else (h >= entry * 0.999)
            if is_2r:
                return {'outcome': 'HIT_2R', 'net_r': +1.5} # 75% at 2R, 25% at BEP
            elif is_bep:
                return {'outcome': 'REVERSED_TO_BEP', 'net_r': 0.0}
    return {'outcome': 'EXPIRED', 'net_r': 0.0}

# 2. Golden Exit Logic (Proposed: TP1 75% at 1.5R, Runner Lock +1.0R & Swing Trail to TP2 3.0R)
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
                return {'outcome': 'DIRECT_SL', 'net_r': -1.0}
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
        return {'outcome': 'WIN_GOLDEN', 'net_r': net_r, 'runner_r': runner_exit_r}
    return {'outcome': 'DIRECT_SL', 'net_r': -1.0}

print("Fetching 15m and 1h klines for 26 liquid pairs...")
candles_15m = {sym: fetch_klines(sym, '15m', 500) for sym in universe}
candles_1h  = {sym: fetch_klines(sym, '1h', 200) for sym in universe}

def get_1h_trend(c_1h, target_time):
    if not c_1h: return 'NEUTRAL'
    closes = [c['close'] for c in c_1h]
    ema20 = calc_ema(closes, 20)
    ema50 = calc_ema(closes, 50)
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

# Model 1: Current Live Autonomous System (15m EMA Pullback + Old Exit)
trades_current = []

# Model 2: Proposed End-to-End System (1h Macro Trend + 15m IFVG Min Gap 0.5 ATR + Golden Exit)
trades_proposed = []

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
        c_prev = c15[i-1]
        c1, c3 = c15[i-2], c15[i]
        atr = atrs[i]
        
        # --- MODEL 1: Current System (EMA Pullback) ---
        # LONG: EMA20 > EMA50, price touched EMA20 and bounced up
        if ema20[i] > ema50[i]:
            if c_prev['low'] <= ema20[i-1] and c['close'] > ema20[i] and c['close'] > c['open']:
                entry = c['close']
                sl = min(min(c['low'], c_prev['low']) - (0.3 * atr), entry * 0.99)
                res = simulate_old_exit(entry, sl, 'LONG', c15[i+1:])
                if res: trades_current.append(res)
        elif ema20[i] < ema50[i]:
            if c_prev['high'] >= ema20[i-1] and c['close'] < ema20[i] and c['close'] < c['open']:
                entry = c['close']
                sl = max(max(c['high'], c_prev['high']) + (0.3 * atr), entry * 1.01)
                res = simulate_old_exit(entry, sl, 'SHORT', c15[i+1:])
                if res: trades_current.append(res)
                
        # --- MODEL 2: Proposed System (MTF 1h Trend + 15m IFVG Inversion Min Gap 0.5 ATR + Golden Exit) ---
        min_gap = atr * 0.5
        if c3['low'] - c1['high'] >= min_gap:
            active_fvgs.append({'type': 'BULLISH', 'top': c3['low'], 'bottom': c1['high'], 'bar': i, 'status': 'ACTIVE'})
        if c1['low'] - c3['high'] >= min_gap:
            active_fvgs.append({'type': 'BEARISH', 'top': c1['low'], 'bottom': c3['high'], 'bar': i, 'status': 'ACTIVE'})
            
        for fvg in active_fvgs:
            if fvg['status'] != 'ACTIVE' or (i - fvg['bar'] < 2) or (i - fvg['bar'] > 35):
                continue
            # LONG Inversion
            if fvg['type'] == 'BEARISH' and c['close'] > fvg['top'] and c15[i-1]['close'] <= fvg['top']:
                fvg['status'] = 'INVERTED'
                # MTF Check: 1h Trend Bullish AND 15m Trend Bullish
                h_trend = get_1h_trend(c1h, c['time'])
                if h_trend == 'BULLISH' and ema20[i] > ema50[i]:
                    entry = c['close']
                    sl = min(min(c15[k]['low'] for k in range(max(0, i-5), i+1)), entry * 0.99) # Filter 3
                    res = simulate_golden_exit(entry, sl, 'LONG', c15[i+1:])
                    if res: trades_proposed.append(res)
                    
            # SHORT Inversion
            elif fvg['type'] == 'BULLISH' and c['close'] < fvg['bottom'] and c15[i-1]['close'] >= fvg['bottom']:
                fvg['status'] = 'INVERTED'
                h_trend = get_1h_trend(c1h, c['time'])
                if h_trend == 'BEARISH' and ema20[i] < ema50[i]:
                    entry = c['close']
                    sl = max(max(c15[k]['high'] for k in range(max(0, i-5), i+1)), entry * 1.01) # Filter 3
                    res = simulate_golden_exit(entry, sl, 'SHORT', c15[i+1:])
                    if res: trades_proposed.append(res)

def get_stats(trades, label, days=5.2):
    n = len(trades)
    wins = [t for t in trades if t['net_r'] > 0]
    losses = [t for t in trades if t['net_r'] < 0]
    beps = [t for t in trades if t['net_r'] == 0]
    
    gw = sum(t['net_r'] for t in wins)
    gl = abs(sum(t['net_r'] for t in losses))
    net_r = gw - gl
    pf = gw / gl if gl > 0 else 99.0
    
    # Calculate Max Consecutive Losses (Streak)
    max_loss_streak = 0
    cur_loss_streak = 0
    for t in trades:
        if t['net_r'] < 0:
            cur_loss_streak += 1
            if cur_loss_streak > max_loss_streak:
                max_loss_streak = cur_loss_streak
        else:
            cur_loss_streak = 0
            
    return {
        'label': label,
        'n': n,
        'per_day': n / days,
        'per_coin_day': (n / days) / 26,
        'wins': len(wins),
        'losses': len(losses),
        'beps': len(beps),
        'wr': len(wins) / n * 100 if n else 0,
        'loss_rate': len(losses) / n * 100 if n else 0,
        'bep_rate': len(beps) / n * 100 if n else 0,
        'net_r': net_r,
        'net_usd': net_r * 2.0,
        'pf': pf,
        'loss_streak': max_loss_streak
    }

s_curr = get_stats(trades_current, "SISTEM SAAT INI (Current Autonomous)")
s_prop = get_stats(trades_proposed, "SISTEM USULAN BARU (Proposed MTF IFVG-Pro)")

print("\n" + "="*75)
print("=== HASIL HEAD-TO-HEAD: SISTEM SAAT INI VS SISTEM USULAN BARU ===")
print("="*75)

print(f"{'Metrik Pengujian':<36} | {'Sistem Saat Ini':<16} | {'Sistem Usulan Baru':<16}")
print("-" * 75)
print(f"{'Trigger Entry':<36} | {'15m EMA Pullback':<16} | {'MTF 1h + 15m IFVG':<16}")
print(f"{'Filter Gap FVG':<36} | {'None (Indikator)':<16} | {'>= 0.50 ATR':<16}")
print(f"{'Exit Logic':<36} | {'1R BEP, 2R TP 75%':<16} | {'1.5R TP75%, Lock1R':<16}")
print("-" * 75)
print(f"{'Total Trades (5.2 Hari)':<36} | {s_curr['n']:<16d} | {s_prop['n']:<16d}")
print(f"{'Frekuensi per Hari':<36} | {s_curr['per_day']:<16.1f} | {s_prop['per_day']:<16.1f}")
print(f"{'Frekuensi per Koin / Hari':<36} | {s_curr['per_coin_day']:<16.2f} | {s_prop['per_coin_day']:<16.2f}")
print(f"{'Win Rate (%)':<36} | {s_curr['wr']:<15.1f}% | {s_prop['wr']:<15.1f}%")
print(f"{'Loss Rate (Kena SL Penuh)':<36} | {s_curr['loss_rate']:<15.1f}% | {s_prop['loss_rate']:<15.1f}%")
print(f"{'Breakeven ($0 Profit)':<36} | {s_curr['bep_rate']:<15.1f}% | {'0.0% (Terkunci!)':<16}")
print(f"{'Max Loss Streak (Kalah Beruntun)':<36} | {s_curr['loss_streak']:<16d} | {s_prop['loss_streak']:<16d}")
print("-" * 75)
print(f"{'Total Net R-Multiple':<36} | {s_curr['net_r']:>+14.1f}R | {s_prop['net_r']:>+14.1f}R")
print(f"{'Net Profit USD ($2 Risk)':<36} | ${s_curr['net_usd']:>+14.2f} | ${s_prop['net_usd']:>+14.2f}")
print(f"{'Profit Factor':<36} | {s_curr['pf']:<16.2f} | {s_prop['pf']:<16.2f}")
print("="*75)
