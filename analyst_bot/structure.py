"""Conservative closed-candle market structure and order-block candidates."""
from __future__ import annotations

def analyze_tf(rows:list[dict])->dict:
 d=[x for x in rows if x.get('closed')]
 if len(d)<45:return {'structure':'UNAVAILABLE','bull_ob':None,'bear_ob':None,'fvg':[],'age_s':None}
 price=d[-1]['close']; trs=[]
 for i,x in enumerate(d):trs.append(max(x['high']-x['low'],abs(x['high']-(d[i-1]['close'] if i else x['open'])),abs(x['low']-(d[i-1]['close'] if i else x['open']))))
 atr=sum(trs[-14:])/14
 recent,prior=d[-20:],d[-40:-20]
 hh=max(x['high'] for x in recent)>max(x['high'] for x in prior); hl=min(x['low'] for x in recent)>min(x['low'] for x in prior)
 lh=max(x['high'] for x in recent)<max(x['high'] for x in prior); ll=min(x['low'] for x in recent)<min(x['low'] for x in prior)
 structure='BULL' if hh and hl else 'BEAR' if lh and ll else 'RANGE'
 bull=bear=None
 for i in range(len(d)-35,len(d)-2):
  c,n=d[i],d[i+1]; body=abs(n['close']-n['open']); width=abs(c['open']-c['low']) if c['close']<c['open'] else abs(c['high']-c['open'])
  if width<.1*atr or body<atr:continue
  if c['close']<c['open'] and n['close']>c['high']:
   z={'low':c['low'],'high':c['open'],'ts':c['ts']}
   # Demand invalid once any later closed candle closes below zone.
   if all(x['close']>=z['low'] for x in d[i+2:]):bull=z
  if c['close']>c['open'] and n['close']<c['low']:
   z={'low':c['open'],'high':c['high'],'ts':c['ts']}
   if all(x['close']<=z['high'] for x in d[i+2:]):bear=z
 def finish(z,side):
  if not z:return None
  # Only actionable demand at/below price and supply at/above price.
  if side=='BULL' and z['low']>price:return None
  if side=='BEAR' and z['high']<price:return None
  q=dict(z); q['distance_pct']=0 if z['low']<=price<=z['high'] else ((z['high']/price-1)*100 if side=='BULL' else (z['low']/price-1)*100)
  q['width_atr']=(z['high']-z['low'])/atr; return q
 fv=[]
 for i in range(len(d)-25,len(d)):
  a,c=d[i-2],d[i]
  if c['low']>a['high'] and c['low']-a['high']>=.1*atr:fv.append({'side':'BULL','low':a['high'],'high':c['low']})
  elif a['low']>c['high'] and a['low']-c['high']>=.1*atr:fv.append({'side':'BEAR','low':c['high'],'high':a['low']})
 return {'structure':structure,'close':price,'atr':atr,'bull_ob':finish(bull,'BULL'),'bear_ob':finish(bear,'BEAR'),'fvg':fv[-3:],
         'age_s':max(0,(__import__('time').time()*1000-d[-1]['close_ms'])/1000)}

def suggestion(s:dict,live:dict,funding_rate:float|None)->dict:
 vals=[s[x]['structure'] for x in ('4h','1h','15m')]; bull=vals.count('BULL'); bear=vals.count('BEAR')
 direction='LONG' if bull>=2 and not bear else 'SHORT' if bear>=2 and not bull else None
 if not direction:return {'verdict':'NO TRADE','direction':None,'reason':'struktur H4/H1/M15 konflik atau range'}
 if live.get('trade_count',0)<3:return {'verdict':'NO TRADE','direction':direction,'reason':'sampel aggTrade tidak cukup'}
 if live.get('age_s') is None or live['age_s']>5:return {'verdict':'NO TRADE','direction':direction,'reason':'websocket stale'}
 m5=s['5m']['structure']; trigger=(direction=='LONG' and m5=='BULL') or(direction=='SHORT' and m5=='BEAR')
 if not trigger:return {'verdict':'WAIT FOR TRIGGER','direction':direction,'reason':f'tunggu struktur M5 {direction}'}
 delta=live['trade_delta_usd']
 if (direction=='LONG' and delta<0)or(direction=='SHORT' and delta>0):return {'verdict':'WATCH','direction':direction,'reason':'agresi trade berlawanan arah'}
 if funding_rate is not None and abs(funding_rate)>.001:return {'verdict':'WATCH','direction':direction,'reason':'funding ekstrem; risiko squeeze'}
 return {'verdict':'WATCH','direction':direction,'reason':'konteks live selaras; OOS belum tervalidasi'}
