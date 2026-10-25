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

def simulate_exits(entry, sl, side, future_candles):
    risk = abs(entry - sl)
    if risk <= 0 or not future_candles:
        return {}

    # Case 1: Standard Creator IFVG: Fixed TP 1.5R, Hard SL -1.0R (No BE move)
    c1_tp = entry + (1.5 * risk) if side == 'LONG' else entry - (1.5 * risk)
    c1_outcome = 'UNKNOWN'
    for c in future_candles:
        h, l = c['high'], c['low']
        hit_tp = (h >= c1_tp) if side == 'LONG' else (l <= c1_tp)
        hit_sl = (l <= sl) if side == 'LONG' else (h >= sl)
        if hit_tp and not hit_sl:
            c1_outcome = 'WIN_1.5R'; break
        elif hit_sl and not hit_tp:
            c1_outcome = 'LOSS_1R'; break
        elif hit_tp and hit_sl:
            c1_outcome = 'WIN_1.5R'; break

    # Case 2: Partial TP at 1.0R (Take 50% profit at 1R, move SL to BEP, let 50% run to 2R)
    c2_tp1 = entry + risk if side == 'LONG' else entry - risk
    c2_tp2 = entry + (2.0 * risk) if side == 'LONG' else entry - (2.0 * risk)
    hit_tp1 = False
    c2_outcome = 'UNKNOWN'
    for c in future_candles:
        h, l = c['high'], c['low']
        if not hit_tp1:
            hit_tp1_now = (h >= c2_tp1) if side == 'LONG' else (l <= c2_tp1)
            hit_sl_now = (l <= sl) if side == 'LONG' else (h >= sl)
            if hit_sl_now and not hit_tp1_now:
                c2_outcome = 'FULL_LOSS'; break
            elif hit_tp1_now:
                hit_tp1 = True
        else:
            hit_tp2 = (h >= c2_tp2) if side == 'LONG' else (l <= c2_tp2)
            hit_bep = (l <= entry) if side == 'LONG' else (h >= entry)
            if hit_tp2:
                c2_outcome = 'WIN_RUNNER'; break
            elif hit_bep:
                c2_outcome = 'WIN_HALF_BEP'; break

    # Case 3: Fixed TP 1.2R (Quick Scalp)
    c3_tp = entry + (1.2 * risk) if side == 'LONG' else entry - (1.2 * risk)
    c3_outcome = 'UNKNOWN'
    for c in future_candles:
        h, l = c['high'], c['low']
        hit_tp = (h >= c3_tp) if side == 'LONG' else (l <= c3_tp)
        hit_sl = (l <= sl) if side == 'LONG' else (h >= sl)
        if hit_tp and not hit_sl:
            c3_outcome = 'WIN_1.2R'; break
        elif hit_sl and not hit_tp:
            c3_outcome = 'LOSS_1R'; break

    # Case 4: The Previous Tested Case (No profit at 1R, just move SL to BEP, wait for 2R)
    c4_tp1 = entry + risk if side == 'LONG' else entry - risk
    c4_tp2 = entry + (2.0 * risk) if side == 'LONG' else entry - (2.0 * risk)
    c4_hit_1r = False
    c4_outcome = 'UNKNOWN'
    for c in future_candles:
        h, l = c['high'], c['low']
        if not c4_hit_1r:
            hit_1 = (h >= c4_tp1) if side == 'LONG' else (l <= c4_tp1)
            hit_s = (l <= sl) if side == 'LONG' else (h >= sl)
            if hit_s and not hit_1:
                c4_outcome = 'DIRECT_SL'; break
            elif hit_1:
                c4_hit_1r = True
        else:
            hit_2 = (h >= c4_tp2) if side == 'LONG' else (l <= c4_tp2)
            hit_b = (l <= entry * 1.001) if side == 'LONG' else (h >= entry * 0.999)
            if hit_2:
                c4_outcome = 'HIT_2R'; break
            elif hit_b:
                c4_outcome = 'BEP_ZERO'; break

    return {'c1': c1_outcome, 'c2': c2_outcome, 'c3': c3_outcome, 'c4': c4_outcome}

all_candles = {sym: fetch_klines(sym, '15m', 500) for sym in universe}
all_candles = {k: v for k, v in all_candles.items() if v and len(v) >= 100}

trades = []
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
                    exits = simulate_exits(entry, sl, 'LONG', candles[i+1:])
                    if exits: trades.append(exits)
            # SHORT
            elif fvg['type'] == 'BULLISH' and c['close'] < fvg['bottom'] and candles[i-1]['close'] >= fvg['bottom']:
                fvg['status'] = 'INVERTED'
                if ema20[i] < ema50[i]:
                    entry = c['close']
                    sl = max(max(candles[k]['high'] for k in range(max(0, i-5), i+1)), entry * 1.01)
                    exits = simulate_exits(entry, sl, 'SHORT', candles[i+1:])
                    if exits: trades.append(exits)

print("="*60)
print(f"Total Trend-Aligned IFVG Trades Evaluated: {len(trades)}")
print("="*60)

# Evaluation:
# Case 1: Creator Fixed 1.5R (No premature BE choking)
c1_wins = sum(1 for t in trades if t['c1'] == 'WIN_1.5R')
c1_losses = sum(1 for t in trades if t['c1'] == 'LOSS_1R')
c1_wr = c1_wins / (c1_wins + c1_losses) * 100 if (c1_wins + c1_losses) else 0
c1_net_r = (c1_wins * 1.5) - (c1_losses * 1.0)
c1_pf = (c1_wins * 1.5) / (c1_losses * 1.0) if c1_losses else 0
print(f"Case 1: Creator Baseline (Fixed TP 1.5R, Hard SL -1R, No BE choking)")
print(f"  Win Rate: {c1_wr:.1f}% ({c1_wins}W / {c1_losses}L)")
print(f"  Net R: {c1_net_r:+.1f}R | Profit Factor: {c1_pf:.2f}\n")

# Case 2: Partial TP at 1.0R (Take 50% profit at 1R, move SL to BEP, let 50% run to 2R)
c2_full_loss = sum(1 for t in trades if t['c2'] == 'FULL_LOSS')
c2_half_bep = sum(1 for t in trades if t['c2'] == 'WIN_HALF_BEP')
c2_runner = sum(1 for t in trades if t['c2'] == 'WIN_RUNNER')
c2_gross_win = (c2_half_bep * 0.5) + (c2_runner * 1.5)
c2_gross_loss = c2_full_loss * 1.0
c2_net_r = c2_gross_win - c2_gross_loss
c2_pf = c2_gross_win / c2_gross_loss if c2_gross_loss else 0
print(f"Case 2: Smart Partial Take Profit (50% TP at 1.0R, Move SL to BEP, 50% at 2.0R)")
print(f"  Trades: {c2_runner} Hit 2R | {c2_half_bep} Hit 1R then BEP (+0.5R locked!) | {c2_full_loss} SL (-1R)")
print(f"  Net R: {c2_net_r:+.1f}R | Profit Factor: {c2_pf:.2f}\n")

# Case 3: Fixed TP 1.2R (Quick Scalp)
c3_wins = sum(1 for t in trades if t['c3'] == 'WIN_1.2R')
c3_losses = sum(1 for t in trades if t['c3'] == 'LOSS_1R')
c3_wr = c3_wins / (c3_wins + c3_losses) * 100 if (c3_wins + c3_losses) else 0
c3_net_r = (c3_wins * 1.2) - (c3_losses * 1.0)
c3_pf = (c3_wins * 1.2) / (c3_losses * 1.0) if c3_losses else 0
print(f"Case 3: High Win-Rate Scalp (Fixed TP 1.2R, Hard SL -1R)")
print(f"  Win Rate: {c3_wr:.1f}% ({c3_wins}W / {c3_losses}L)")
print(f"  Net R: {c3_net_r:+.1f}R | Profit Factor: {c3_pf:.2f}\n")

# Case 4: The Previous Tested Case (No profit at 1R, just move SL to BEP, wait for 2R)
c4_r2 = sum(1 for t in trades if t['c4'] == 'HIT_2R')
c4_bep = sum(1 for t in trades if t['c4'] == 'BEP_ZERO')
c4_sl = sum(1 for t in trades if t['c4'] == 'DIRECT_SL')
c4_net_r = (c4_r2 * 1.5) - (c4_sl * 1.0)
c4_pf = (c4_r2 * 1.5) / (c4_sl * 1.0) if c4_sl else 0
print(f"Case 4: Previous Tested Logic (0% TP at 1R -> Move SL to BEP -> TP 75% at 2R)")
print(f"  Trades: {c4_r2} Hit 2R (+1.5R) | {c4_bep} Stopped at BEP ($0) | {c4_sl} Direct SL (-1R)")
print(f"  Net R: {c4_net_r:+.1f}R | Profit Factor: {c4_pf:.2f}\n")
