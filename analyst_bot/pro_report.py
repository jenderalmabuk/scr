"""Fresh-first professional report; Nexus flow shown separately with own age."""
from __future__ import annotations
from analyst_bot.analyzer import analyze
from analyst_bot.live_market import funding,klines,oi_now,websocket_sample
from analyst_bot.structure import analyze_tf,suggestion

def n(v,d=2):return 'UNAVAILABLE' if v is None else f'{v:,.{d}f}'
def zone(z):return 'none' if not z else f"{z['low']:.8g}–{z['high']:.8g} ({z['distance_pct']:+.2f}%, {z['width_atr']:.2f} ATR)"
def report(symbol:str)->str:
 old=analyze(symbol); sym=old['symbol']
 candles={tf:klines(sym,tf) for tf in ('5m','15m','1h','4h')}; st={tf:analyze_tf(v) for tf,v in candles.items()}
 ws=websocket_sample(sym); fund=funding(sym); oi=oi_now(sym); adv=suggestion(st,ws,fund['rate'])
 price=fund['mark'] or candles['5m'][-1]['close']; flow=old.get('flow') or {}; btc=old.get('btc') or {}
 lines=[f"{sym} · MULTI-TF · {adv['verdict']}","Venue: Binance Futures",'',"FRESHNESS",
        f"Candles: 5m {n(st['5m']['age_s'],0)}s | 15m {n(st['15m']['age_s'],0)}s | H1 {n(st['1h']['age_s'],0)}s | H4 {n(st['4h']['age_s'],0)}s",
        f"Trades/depth websocket: {n(ws['age_s'],2)}s | OI REST: {n(oi['age_s'],0)}s | funding REST: {n(fund['age_s'],0)}s",
        f"Nexus flow snapshot: {'STALE' if old['market_age_s'] is None or old['market_age_s']>180 else 'fresh'} ({n(old['market_age_s'],0)}s)",'',
        "MARKET",f"Mark price: {price:.8g}",f"Funding/8h: {n(None if fund['rate'] is None else fund['rate']*100,4)}%",
        f"OI contracts: {n(oi['contracts'],2)}",'',"STRUCTURE / ORDER BLOCKS (closed candles)"]
 for tf,s in st.items():lines.append(f"{tf}: {s['structure']} | BULL OB {zone(s['bull_ob'])} | BEAR OB {zone(s['bear_ob'])}")
 lines += ['',f"ORDER FLOW LIVE ({ws.get('source')}, 4s)",f"Agg trades: {ws['trade_count']} | buy ${n(ws['aggressive_buy_usd'],0)} | sell ${n(ws['aggressive_sell_usd'],0)}",
           f"Trade delta ${n(ws['trade_delta_usd'],0)} | buy share {n(None if ws['buy_share'] is None else ws['buy_share']*100,1)}%",
           f"Depth20 weighted imbalance {n(None if ws['depth20_imbalance'] is None else ws['depth20_imbalance']*100,1)}%",
           '',"NEXUS CONTEXT (secondary)",f"zCVD 15m: {n(flow.get('cvd_zscore_15m'),4)} | direction: {flow.get('flow_direction','UNAVAILABLE')}",
           f"BTC regime: {btc.get('btc_regime','UNAVAILABLE')}",'',"WHALE"]
 if old['whales']:
  for w in old['whales']:lines.append(f"• {w.get('event_type')} {w.get('bias','NEUTRAL')} ${float(w.get('value_usd') or 0):,.0f} · {w['age_s']/60:.0f}m · {w.get('chain','?')}")
 else:lines.append('NO RECENT VERIFIED WHALE ACTIVITY')
 lines += ['',"SUGGESTION",f"{adv['verdict']} {adv.get('direction') or ''}: {adv['reason']}",
           "Belum ada izin SETUP VALIDATED. Bukan perintah transaksi."]
 return '\n'.join(lines)
if __name__=='__main__':
 import sys;print(report(sys.argv[1] if len(sys.argv)>1 else 'BTCUSDT'))
