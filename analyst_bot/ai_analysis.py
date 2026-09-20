"""LLM narrative over deterministic live facts. Model cannot create evidence."""
from __future__ import annotations
import json, os, urllib.request
from analyst_bot.analyzer import analyze
from analyst_bot.live_market import funding, klines, oi_now, websocket_sample
from analyst_bot.structure import analyze_tf, suggestion

BASE=os.getenv('NINE_ROUTER_BASE','').rstrip('/')
KEY=os.getenv('NINE_ROUTER_API_KEY','')
MODEL=os.getenv('ADVERSARIAL_MODEL','Free-Tiers')

def _rsi(rows,period=14):
 c=[x['close'] for x in rows if x.get('closed')]
 if len(c)<=period:return None
 dif=[c[i]-c[i-1] for i in range(len(c)-period,len(c))]; g=sum(max(x,0) for x in dif)/period; l=sum(max(-x,0) for x in dif)/period
 return 100.0 if l==0 else 100-100/(1+g/l)

def _ema(values,n):
 e=values[0]; a=2/(n+1)
 for v in values[1:]:e=v*a+e*(1-a)
 return e

def facts(symbol:str)->dict:
 old=analyze(symbol); sym=old['symbol']; candles={tf:klines(sym,tf) for tf in ('5m','15m','1h')}
 st={tf:analyze_tf(v) for tf,v in candles.items()}; ws=websocket_sample(sym); fund=funding(sym); oi=oi_now(sym)
 tech={}
 for tf,rows in candles.items():
  c=[x['close'] for x in rows if x.get('closed')]
  tech[tf]={'structure':st[tf]['structure'],'rsi14':_rsi(rows),'ema20':_ema(c[-50:],20),'ema50':_ema(c[-80:],50),
            'price':c[-1],'above_ema20':c[-1]>_ema(c[-50:],20),'above_ema50':c[-1]>_ema(c[-80:],50),
            'bull_ob':st[tf]['bull_ob'],'bear_ob':st[tf]['bear_ob'],'fvg':st[tf]['fvg']}
 return {'symbol':sym,'venue':'Binance Futures','technical':tech,'orderflow':ws,'oi':oi,'funding':fund,
         'zcvd15m':(old.get('flow')or{}).get('cvd_zscore_15m'),'flow_direction':(old.get('flow')or{}).get('flow_direction'),
         'btc_regime':(old.get('btc')or{}).get('btc_regime'),'whales':old.get('whales',[]),
         'mechanical_suggestion':suggestion({**st,'4h':analyze_tf(klines(sym,'4h'))},ws,fund.get('rate'))}

def ai_report(symbol:str)->str:
 if not BASE or not KEY:raise RuntimeError('NINE_ROUTER credentials unavailable')
 evidence=facts(symbol)
 prompt='''Anda analis pasar profesional faktual. Analisis JSON EVIDENCE berikut dalam Bahasa Indonesia.
Wajib format: BIAS, BULLISH CONFLUENCE, BEARISH CONFLUENCE, CONFLICTS, PRIMARY SCENARIO, ALTERNATIVE SCENARIO, INVALIDATION, CONFIDENCE.
Aturan keras: hanya klaim yang eksplisit tersedia di EVIDENCE; sebut TF; jangan menciptakan pattern, level, angka, entry, SL, TP; jangan memberi jaminan; tampilkan kedua sisi; jika konflik/stale/sampel aggTrade<3 maka NO TRADE. Confidence LOW/MEDIUM/HIGH disertai alasan. Maksimal 2500 karakter.
EVIDENCE='''+json.dumps(evidence,default=str,separators=(',',':'))
 body=json.dumps({'model':MODEL,'messages':[{'role':'system','content':'Report facts only. Never invent evidence.'},{'role':'user','content':prompt}],
                  'temperature':0,'max_tokens':900}).encode()
 req=urllib.request.Request(BASE+'/chat/completions',data=body,headers={'Authorization':'Bearer '+KEY,'Content-Type':'application/json'})
 with urllib.request.urlopen(req,timeout=90) as r:data=json.load(r)
 text=data['choices'][0]['message']['content'].strip()
 return f"{evidence['symbol']} · AI CONFLUENCE REVIEW\nModel: {MODEL}\n\n{text}\n\nLLM hanya merangkum evidence; bukan perintah transaksi."

if __name__=='__main__':
 import sys;print(ai_report(sys.argv[1] if len(sys.argv)>1 else 'BTCUSDT'))
