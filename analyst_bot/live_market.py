"""Fresh Binance Futures context: public REST candles/OI/funding + websocket trades/depth."""
from __future__ import annotations
import asyncio, json, time, urllib.parse, urllib.request
from typing import Any
import aiohttp

REST="https://fapi.binance.com"; WS="wss://fstream.binance.com"

def _get(path:str, **params:Any):
    url=REST+path+"?"+urllib.parse.urlencode(params)
    with urllib.request.urlopen(url,timeout=15) as r: return json.load(r)

def klines(symbol:str, interval:str, limit:int=220)->list[dict]:
    rows=_get('/fapi/v1/klines',symbol=symbol,interval=interval,limit=limit); now=int(time.time()*1000)
    return [{"ts":x[0],"open":float(x[1]),"high":float(x[2]),"low":float(x[3]),"close":float(x[4]),
             "volume":float(x[5]),"qvol":float(x[7]),"closed":x[6]<now,"close_ms":x[6]} for x in rows]

def oi_now(symbol:str):
    try:
        d=_get('/fapi/v1/openInterest',symbol=symbol); return {"contracts":float(d['openInterest']),"age_s":0.0}
    except Exception:return {"contracts":None,"age_s":None}

def funding(symbol:str):
    try:
        d=_get('/fapi/v1/premiumIndex',symbol=symbol)
        return {"rate":float(d['lastFundingRate']),"mark":float(d['markPrice']),"next":d['nextFundingTime'],"age_s":0.0}
    except Exception:return {"rate":None,"mark":None,"next":None,"age_s":None}

async def _sample(symbol:str,seconds:float=4.0)->dict:
    stream=f"{symbol.lower()}@aggTrade"
    buy=sell=0.0; count=0; latest_ms=None
    async with aiohttp.ClientSession() as s:
      async with s.ws_connect(f"{WS}/ws/{stream}",heartbeat=15,timeout=10) as ws:
       end=time.monotonic()+seconds
       while time.monotonic()<end:
        try:m=await asyncio.wait_for(ws.receive(),timeout=max(.1,end-time.monotonic()))
        except asyncio.TimeoutError:break
        if m.type!=aiohttp.WSMsgType.TEXT:continue
        d=json.loads(m.data); event=d.get('e')
        if event=='aggTrade':
            usd=float(d['p'])*float(d['q']); count+=1; latest_ms=d.get('E')
            if d.get('m'):sell+=usd
            else:buy+=usd
    depth=_get('/fapi/v1/depth',symbol=symbol,limit=20)
    bids=[(float(p),float(q)) for p,q in depth.get('bids',[])]; asks=[(float(p),float(q)) for p,q in depth.get('asks',[])]
    # Distance-weighted USD liquidity; closer levels matter more.
    def weighted(levels): return sum(p*q/(i+1) for i,(p,q) in enumerate(levels))
    wb,wa=weighted(bids),weighted(asks); total=wb+wa
    source='websocket'
    if count==0:
        # ponytail: REST fallback until VPS websocket route delivers aggTrade frames.
        recent=_get('/fapi/v1/aggTrades',symbol=symbol,limit=200)
        cutoff=int(time.time()*1000-seconds*1000)
        recent=[x for x in recent if int(x['T'])>=cutoff]; buy=sell=0.0
        for x in recent:
            usd=float(x['p'])*float(x['q'])
            if x.get('m'):sell+=usd
            else:buy+=usd
        count=len(recent); latest_ms=int(recent[-1]['T']) if recent else None; source='aggTrades REST fallback'
    return {"aggressive_buy_usd":buy,"aggressive_sell_usd":sell,"trade_delta_usd":buy-sell,
            "trade_count":count,"buy_share":buy/(buy+sell) if buy+sell else None,
            "depth20_imbalance":(wb-wa)/total if total else None,"bid_depth_usd":wb,"ask_depth_usd":wa,
            "sample_seconds":seconds,"source":source,"age_s":max(0,(time.time()*1000-latest_ms)/1000) if latest_ms else None}

def websocket_sample(symbol:str,seconds:float=4.0)->dict:return asyncio.run(_sample(symbol,seconds))
