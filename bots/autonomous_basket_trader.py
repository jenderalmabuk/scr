#!/usr/bin/env python3
"""Fail-closed autonomous scanner/orchestrator for audited basket executor."""
import argparse, contextlib, fcntl, hashlib, importlib.util, json, os, re, statistics, tempfile, threading, time, urllib.error, urllib.parse, urllib.request

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
 from .autonomous_basket_notifier import (TelegramNotifier, format_close, format_degraded, format_entry, format_rejection, format_setups)
except ImportError:
 _notifier_path=Path(__file__).with_name("autonomous_basket_notifier.py")
 _notifier_spec=importlib.util.spec_from_file_location("autonomous_basket_notifier",_notifier_path)
 _notifier=importlib.util.module_from_spec(_notifier_spec); _notifier_spec.loader.exec_module(_notifier)
 TelegramNotifier=_notifier.TelegramNotifier; format_close=_notifier.format_close; format_degraded=_notifier.format_degraded
 format_entry=_notifier.format_entry; format_rejection=_notifier.format_rejection; format_setups=_notifier.format_setups

_EXECUTOR_PATH=Path(__file__).with_name("basket_live_executor.py")
_spec=importlib.util.spec_from_file_location("basket_live_executor",_EXECUTOR_PATH)
Executor=importlib.util.module_from_spec(_spec); _spec.loader.exec_module(Executor)

EXECUTOR_SHA256="9e81d9540372b69b5390d88903cd29137c71de2c8c760baf450f257bb3913641"
AUDITED_EXECUTOR_SOURCE_SHA256=frozenset({EXECUTOR_SHA256,"5b99b57be681b0f4963b966c7df4615bbbc362ec73dad809e1cec86c9b005081","52531ac1514b09f64076b4403acf764a884ca8c180a67601c9d1c838f30c0e08"})
LEGACY_PREWRITE_FINAL_GATE_CODES=frozenset({"FINAL_CANDLE_STALE_OR_INVALID","FINAL_OI_CHANGE_BELOW_MIN"})
LEGACY_PREWRITE_FATAL_ERRORS=frozenset({"final market invalid: candle stale or invalid","final OI change below minimum"})
POLICY_KEYS={"schema","version","starts_at","max_entries_day","max_open_baskets","max_modeled_loss_trade_usd","max_modeled_loss_day_usd","entry","protection","partial_tp","loss_pause","executor_sha256"}

class FailClosed(RuntimeError): pass

class DurableTupleError(FailClosed):
 def __init__(self,code,tuple_class="DEGRADED"):
  self.code,self.tuple_class=code,tuple_class; super().__init__(code)

_DURABLE_IDENTITY_RE=re.compile(r"^(auto|basket)-([0-9]+)-([A-Z0-9]+USDT)$")

def build_durable_identity(issued,nonce,symbol):
 if (not isinstance(issued,(int,float)) or isinstance(issued,bool)
  or nonce!=f"a{int(issued)}" or not isinstance(symbol,str) or re.fullmatch(r"[A-Z0-9]+USDT",symbol) is None):
  raise FailClosed("invalid durable identity inputs")
 return f"basket-{int(issued)}-{symbol}"

def parse_durable_identity(identity):
 match=_DURABLE_IDENTITY_RE.fullmatch(identity) if isinstance(identity,str) else None
 if match is None: return None
 issued=int(match.group(2)); return issued,f"a{issued}",match.group(3)

def _derived_historical_identity(executor,trade_status):
 try:
  approval=executor["approved"]; issued=approval["issued_at"]; nonce=approval["nonce"]
  candidate=executor["candidate"]; winner=executor["winner"]; plan=executor["planDocument"]
  source=executor["executorSourceSha256"]; plan_map=Executor.validate_plan_document(plan,issued)[1]
  Executor.validate_approval(approval,issued,plan,source)
 except (KeyError,TypeError,ValueError,Executor.PermanentError): return None
 if (trade_status not in {"reserved","ambiguous","open"}
  or not isinstance(issued,(int,float)) or isinstance(issued,bool) or nonce!=f"a{int(issued)}"
  or candidate!=winner or not isinstance(candidate,str) or set(plan_map)!={candidate}
  or source!=EXECUTOR_SHA256 or executor.get("planFingerprint")!=approval.get("fingerprint")):
  return None
 try: return build_durable_identity(issued,nonce,candidate)
 except FailClosed: return None


def classify_durable_tuple(ledger,monitor,executor):
 """Classify already-loaded durable state without I/O or mutation."""
 if ledger is not None and not isinstance(ledger,dict) or monitor is not None and not isinstance(monitor,dict) or executor is not None and not isinstance(executor,dict):
  raise DurableTupleError("MALFORMED_DURABLE_STATE","CORRUPT")
 ledger=ledger or {}; monitor=monitor or {}; executor=executor or {}
 trades=ledger.get("trades",{})
 raw_reserved=ledger.get("modeled_loss_reserved",0)
 try: reserved=float(raw_reserved)
 except (TypeError,ValueError): raise DurableTupleError("INVALID_LEDGER_STRUCTURE","CORRUPT")
 if (isinstance(raw_reserved,bool) or not isinstance(trades,dict)
  or reserved<0 or reserved!=reserved or reserved in (float("inf"),float("-inf"))):
  raise DurableTupleError("INVALID_LEDGER_STRUCTURE","CORRUPT")
 open_id=ledger.get("open_basket"); trade=trades.get(open_id) if isinstance(open_id,str) else None
 status=executor.get("status","watching")
 if open_id is not None and status not in {"fatal","failed_prewrite_repaired"} and (not isinstance(open_id,str) or not isinstance(trade,dict) or trade.get("status") not in {"reserved","ambiguous","open"}):
  raise DurableTupleError("INVALID_LEDGER_STRUCTURE","CORRUPT")
 inert=status=="watching" and not any(executor.get(k) for k in ("candidate","winner","body","orderLinkId","closeBody","closeOrderLinkId"))
 safe_terminal=status in {"fatal","failed_prewrite_repaired"} and executor.get("exposure_possible") is False
 active=Executor.RECOVERY_STATUSES|{"open","exit_reconciling","close_reconciling"}
 known=active|{"watching","fatal","failed_prewrite_repaired","closed"}
 if status not in known: raise DurableTupleError("UNKNOWN_EXECUTOR_STATUS")
 if (inert or not executor) and open_id is not None: raise DurableTupleError("ORPHAN_LEDGER")
 if (inert or not executor) and monitor.get("submitted") is True: raise DurableTupleError("ORPHAN_MONITOR")
 if status in active:
  if open_id is None:
   if status=="reconciling" and executor.get("planDocument")=={"plans":[]}: return ("ACTIVE_OWNED",None)
   raise DurableTupleError("ACTIVE_EXECUTOR_LEDGER_MISSING")
  winner=executor.get("winner") or executor.get("candidate") or (executor.get("body") or {}).get("symbol")
  if monitor.get("submitted") is True and monitor.get("identity")!=open_id: raise DurableTupleError("ACTIVE_EXECUTOR_LEDGER_MISMATCH")
  body=executor.get("body") or {}
  symbols=[x for x in (executor.get("candidate"),executor.get("winner"),body.get("symbol")) if x is not None]
  plan=executor.get("planDocument")
  if plan is not None:
   try: plan_symbols=list(Executor.validate_plan_document(plan,executor.get("approved",{}).get("issued_at"))[1])
   except (KeyError,TypeError,ValueError,Executor.PermanentError): plan_symbols=[]
   if len(plan_symbols)==1: symbols+=plan_symbols
  if monitor.get("submitted") is True and monitor.get("winner") is not None: symbols.append(monitor["winner"])
  if not symbols or any(not isinstance(x,str) for x in symbols) or len(set(symbols))!=1:
   raise DurableTupleError("ACTIVE_EXECUTOR_SYMBOL_MISMATCH")
  durable_ids=[x for x in (executor.get("orderLinkId"),body.get("orderLinkId")) if x is not None]
  if durable_ids:
   linked=all(isinstance(x,str) and x==open_id for x in durable_ids)
  else:
   derived=_derived_historical_identity(executor,trade.get("status"))
   linked=derived==open_id and len(trades)==1 and trade.get("status") in {"reserved","ambiguous","open"}
  if not linked: raise DurableTupleError("ACTIVE_EXECUTOR_LEDGER_MISMATCH")
  if monitor.get("submitted") is True and (monitor.get("identity")!=open_id or monitor.get("winner")!=winner): raise DurableTupleError("MONITOR_IDENTITY_MISMATCH")
  return ("ACTIVE_OWNED",None)
 if status=="watching":
  no_active_trades=all(isinstance(x,dict) and x.get("status") not in {"reserved","ambiguous","open"} for x in trades.values())
  if open_id is None and monitor.get("submitted") is not True and inert and (reserved==0 or no_active_trades): return ("INERT",None)
  if open_id is None and monitor=={} and not executor and (reserved==0 or no_active_trades): return ("INERT",None)
  raise DurableTupleError("WATCHING_TUPLE_MISMATCH")
 if status=="closed":
  residual=any(executor.get(k) for k in ("body","orderLinkId","orderId","pendingOrder","closeBody","closeOrderLinkId"))
  if (open_id is None and reserved==0 and monitor.get("submitted") is not True
   and executor.get("exposure_possible") is False and not residual): return ("INERT",None)
  durable_close=executor.get("closeNotified") is True or (isinstance(executor.get("closeIdentity"),str)
   and executor.get("actualPnl") is not None)
  if open_id is None and reserved==0 and monitor=={} and durable_close and not residual: return ("INERT",None)
  raise DurableTupleError("CLOSED_TUPLE_MISMATCH")
 if status in {"fatal","failed_prewrite_repaired"}:
  if safe_terminal and open_id is None and reserved==0 and monitor.get("submitted") is not True: return ("INERT",None)
  repair_fields=("body","orderLinkId","orderId","pendingOrder","closeBody","closeOrderLinkId")
  repairable=(status=="fatal" and safe_terminal and isinstance(open_id,str) and isinstance(trade,dict)
   and trade.get("status")=="reserved" and monitor.get("submitted") in {False,True}
   and monitor.get("identity")==open_id and not any(executor.get(k) for k in repair_fields))
  if repairable: return ("ACTIVE_OWNED",None)
  raise DurableTupleError("FATAL_TUPLE_MISMATCH")
 raise DurableTupleError("UNCLASSIFIED_DURABLE_TUPLE")

def canonical_bytes(value):
 return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode()

def canonical_hash(value): return hashlib.sha256(canonical_bytes(value)).hexdigest()

def parse_z(value):
 if not isinstance(value,str) or not value.endswith("Z"): raise FailClosed("invalid UTC timestamp")
 try: return datetime.fromisoformat(value[:-1]+"+00:00").timestamp()
 except ValueError as e: raise FailClosed("invalid UTC timestamp") from e

def format_z(ts): return datetime.fromtimestamp(ts,timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z")

def atomic_json(path,value):
 path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
 fd,tmp=tempfile.mkstemp(prefix=path.name+".",dir=path.parent)
 try:
  with os.fdopen(fd,"wb") as f:
   f.write(canonical_bytes(value)+b"\n"); f.flush(); os.fsync(f.fileno())
  os.replace(tmp,path)
  dfd=os.open(path.parent,os.O_DIRECTORY)
  try: os.fsync(dfd)
  finally: os.close(dfd)
 finally:
  if os.path.exists(tmp): os.unlink(tmp)

_SECRET_KEYS=re.compile(r"(?i)(secret|token|api.?key|signature|authorization|password)")

def _redact(value):
 if isinstance(value,dict): return {k:("[REDACTED]" if _SECRET_KEYS.search(str(k)) else _redact(v)) for k,v in value.items()}
 if isinstance(value,list): return [_redact(v) for v in value]
 return value

def load_rejections(path):
 try: raw=Path(path).read_bytes()
 except FileNotFoundError: return []
 records=[]
 for line in raw.splitlines():
  try: value=json.loads(line)
  except json.JSONDecodeError: continue
  if isinstance(value,dict): records.append(value)
 return records

def append_rejection(path,record):
 """Append one fsynced identity; repair only incomplete trailing crash fragment."""
 path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
 clean=_redact(record); identity=clean.get("identity")
 if not isinstance(identity,str) or not identity: raise FailClosed("rejection telemetry identity")
 lock=path.with_suffix(path.suffix+".lock")
 with process_lock(lock):
  existing=path.read_bytes() if path.exists() else b""
  complete=existing[:existing.rfind(b"\n")+1] if existing and not existing.endswith(b"\n") else existing
  if any(x.get("identity")==identity for x in load_rejections(path)): return False
  line=canonical_bytes(clean)+b"\n"
  with open(path,"r+b" if path.exists() else "wb") as f:
   if complete!=existing: f.truncate(len(complete))
   f.seek(0,os.SEEK_END); f.write(line); f.flush(); os.fsync(f.fileno())
  dfd=os.open(path.parent,os.O_DIRECTORY)
  try: os.fsync(dfd)
  finally: os.close(dfd)
 return True

def evaluate_rejection(record,candles,horizon_ts):
 intended=record.get("intended")
 try:
  entry=float(intended["entry"]); stop=float(intended["stop_loss"]); tp=float(intended["take_profit"]); decision=float(record["decision_ts"]); side=record["candidate"]["side"]
  risk=abs(entry-stop); raw_cost=intended.get("modeled_round_trip_cost")
  if raw_cost is None: raise ValueError
  cost=float(raw_cost)
  if risk<=0 or cost<0 or side not in {"Buy","Sell"}: raise ValueError
 except (KeyError,TypeError,ValueError): return {"identity":record.get("identity"),"outcome":"UNVERIFIABLE","gross_r":None,"net_r":None}
 for candle in sorted(candles,key=lambda x:x.get("ts",0)):
  try: ts=float(candle["ts"]); high=float(candle["high"]); low=float(candle["low"])
  except (KeyError,TypeError,ValueError): return {"identity":record.get("identity"),"outcome":"AMBIGUOUS_DATA","gross_r":None,"net_r":None}
  if ts<=decision: continue
  if ts>horizon_ts: break
  tp_hit=high>=tp if side=="Buy" else low<=tp; sl_hit=low<=stop if side=="Buy" else high>=stop
  if tp_hit or sl_hit:
   outcome="SL_FIRST" if sl_hit else "TP_FIRST"; gross=-1.0 if sl_hit else abs(tp-entry)/risk
   return {"identity":record["identity"],"outcome":outcome,"gross_r":gross,"net_r":gross-cost/risk,"touch_ts":ts}
 return {"identity":record["identity"],"outcome":"NEITHER","gross_r":0.0,"net_r":-cost/risk}

def audit_rejections(path,fetch_candles,now,horizon_hours=24):
 results=[]
 for record in load_rejections(path):
  decision=record.get("decision_ts"); expiry=record.get("plan_expires_ts")
  end=min(now,max(float(expiry or 0),float(decision or now)+horizon_hours*3600)) if decision is not None else now
  candles=[] if decision is None else fetch_candles(record.get("candidate",{}).get("symbol"),float(decision),end)
  results.append(evaluate_rejection(record,candles,end))
 counts={k:sum(x["outcome"]==k for x in results) for k in ("TP_FIRST","SL_FIRST","NEITHER","AMBIGUOUS_DATA","UNVERIFIABLE")}
 verified=counts["TP_FIRST"]+counts["SL_FIRST"]; reasons={}
 for record,result in zip(load_rejections(path),results):
  code=record.get("rejection",{}).get("code","HISTORICAL_MISSING_REASON"); bucket=reasons.setdefault(code,{"count":0,"outcomes":{}}); bucket["count"]+=1; bucket["outcomes"][result["outcome"]]=bucket["outcomes"].get(result["outcome"],0)+1
 return {"counts":counts,"win_rate":counts["TP_FIRST"]/verified if verified else None,"gross_r":sum(x["gross_r"] for x in results if x["gross_r"] is not None),"net_r":sum(x["net_r"] for x in results if x["net_r"] is not None),"by_rejection_reason":reasons,"results":results}

SHADOW_FINAL_CODES={"FINAL_RESPONSE_STALE","FINAL_TICKER_STALE","FINAL_SPREAD_EXCEEDED","FINAL_FUNDING_EXCEEDED","FINAL_OI_CHANGE_BELOW_MIN","FINAL_BTC_CHANGE_BELOW_MIN","FINAL_CANDLE_STALE_OR_INVALID","FINAL_FRESHNESS_FAILED","FINAL_OI_CHANGE_FAILED","FINAL_BTC_CHANGE_FAILED"}

def is_shadow_final_reason(code): return code in SHADOW_FINAL_CODES

def build_double_gate_shadow(identity,code,candidate,quote,metadata,risk_usd,decision_ts,executor_hash,policy_hash,positions=(),orders=(),available_balance=1e18,scan_observations=None,final_observations=None):
 if not is_shadow_final_reason(code): return None
 base={"schema":"nexus.shadow-double-gate","version":1,"identity":identity,"decision_ts":decision_ts,"candidate":{"symbol":candidate.get("symbol"),"side":candidate.get("side")},"live":{"decision":"REJECT","reason":code},"observations":{"scan":scan_observations or {},"final":final_observations or {}},"bindings":{"executor_source_sha256":executor_hash,"policy_sha256":policy_hash},"zero_write_proof":{"exchange_writes":0,"risk_reserved":False,"ledger_mutated":False}}
 def no(reason): return {**base,"shadow":{"eligible":False,"reason":reason,"entry":None,"stop_loss":None,"take_profit":None,"qty":None,"rr":None,"modeled_round_trip_cost":None}}
 try:
  symbol,side=candidate["symbol"],candidate["side"]
  if side not in {"Buy","Sell"}: return no("MALFORMED_SIDE")
  if any(float(x.get("size",0)) and x.get("symbol")==symbol for x in positions) or any(x.get("symbol")==symbol and x.get("orderStatus") in Executor.ACTIVE_ORDERS for x in orders): return no("EXPOSURE_OR_OWNERSHIP")
  if float(risk_usd)>2: return no("RISK_CAP")
  plan=Executor.Plan(side,tuple(map(float,candidate["zone"])),float(candidate["zone"][1] if side=="Buy" else candidate["zone"][0]),float(candidate["stop"]),float(candidate["tp"]),None)
  bid,ask=float(quote["bid"]),float(quote["ask"]); entry=bid if side=="Buy" else ask
  body=Executor.build_payload(symbol,entry,metadata,"shadow-proof",0,bid,ask,{symbol:plan})
  Executor.check_margin(body,float(available_balance))
  entry,stop,tp=map(float,(body["price"],body["stopLoss"],body["takeProfit"])); risk=abs(entry-stop)
  shadow={"eligible":True,"reason":"ELIGIBLE","entry":body["price"],"stop_loss":body["stopLoss"],"take_profit":body["takeProfit"],"qty":body["qty"],"rr":Executor._decimal(abs(tp-entry)/risk),"modeled_round_trip_cost":Executor._decimal(Executor.modeled_round_trip_cost(plan,entry,stop))}
  return {**base,"shadow":shadow}
 except (KeyError,TypeError,ValueError,OverflowError,Executor.PermanentError) as e: return no(re.sub(r"[^A-Z0-9]+","_",str(e).upper()).strip("_") or "UNVERIFIABLE")

def append_shadow(path,record): return append_rejection(path,record)

def audit_double_gate_records(records,fetch_candles,now,horizon_hours=24):
 results=[]
 for record in records:
  shadow=record.get("shadow") if record.get("schema")=="nexus.shadow-double-gate" and record.get("version")==1 else None
  try:
   if not shadow or shadow.get("eligible") is not True or shadow.get("modeled_round_trip_cost") is None: raise ValueError
   entry,stop,tp,cost=map(float,(shadow["entry"],shadow["stop_loss"],shadow["take_profit"],shadow["modeled_round_trip_cost"])); decision=float(record["decision_ts"]); side=record["candidate"]["side"]; risk=abs(entry-stop)
   if risk<=0 or side not in {"Buy","Sell"}: raise ValueError
  except (KeyError,TypeError,ValueError): results.append((record,"UNVERIFIABLE",None,None)); continue
  outcome="NEITHER"; gross=0.; end=min(now,decision+horizon_hours*3600)
  for candle in sorted(fetch_candles(record,decision,end),key=lambda x:x.get("ts",0)):
   try: ts,high,low=float(candle["ts"]),float(candle["high"]),float(candle["low"])
   except (KeyError,TypeError,ValueError): outcome="UNVERIFIABLE"; gross=None; break
   if ts<=decision or ts>end: continue
   tph=high>=tp if side=="Buy" else low<=tp; slh=low<=stop if side=="Buy" else high>=stop
   if tph or slh: outcome="SL_FIRST" if slh else "TP_FIRST"; gross=-1. if slh else abs(tp-entry)/risk; break
  net=None if gross is None else gross-cost/risk; results.append((record,outcome,gross,net))
 keys=("TP_FIRST","SL_FIRST","NEITHER","UNVERIFIABLE"); outcomes={k:sum(x[1]==k for x in results) for k in keys}; by_reason={}; by_symbol={}; by_side={}
 for r,o,g,n in results:
  reason=r.get("live",{}).get("reason","HISTORICAL_MISSING_REASON"); symbol=r.get("candidate",{}).get("symbol") or "UNKNOWN"; side=r.get("candidate",{}).get("side") or "UNKNOWN"
  for group,key in ((by_reason,reason),(by_symbol,symbol),(by_side,side)):
   b=group.setdefault(key,{"total":0,"outcomes":{}}); b["total"]+=1; b["outcomes"][o]=b["outcomes"].get(o,0)+1
 gross=sum(x[2] for x in results if x[2] is not None); net=sum(x[3] for x in results if x[3] is not None)
 return {"total":len(records),"eligible":len(records)-outcomes["UNVERIFIABLE"],"unverifiable":outcomes["UNVERIFIABLE"],"outcomes":outcomes,"gross_r":gross,"net_r":net,"missed_winners":outcomes["TP_FIRST"],"avoided_losers":outcomes["SL_FIRST"],"delta_net_r":net,"by_reason":by_reason,"by_symbol":by_symbol,"by_side":by_side,"results":[{"identity":r.get("identity"),"outcome":o,"gross_r":g,"net_r":n} for r,o,g,n in results]}

def validate_policy(p,now):
 if not isinstance(p,dict) or set(p)!=POLICY_KEYS: raise FailClosed("policy shape")
 expected={"schema":"nexus.autonomous-basket-policy","version":1,"max_entries_day":2,"max_open_baskets":1,"max_modeled_loss_trade_usd":"2","max_modeled_loss_day_usd":"4","entry":"PostOnly-no-chase","protection":"native-single-TP-plus-SL","partial_tp":False,"loss_pause":2,"executor_sha256":EXECUTOR_SHA256}
 if any(p.get(k)!=v for k,v in expected.items()): raise FailClosed("policy expansion or source mismatch")
 if parse_z(p["starts_at"])>now: raise FailClosed("policy not started")
 return p

@dataclass(frozen=True)
class LoadedPolicy:
 document: dict
 digest: str

def write_policy(path,p): validate_policy(p,parse_z(p["starts_at"])); atomic_json(path,p)
def load_policy(path,now):
 raw=Path(path).read_bytes(); p=json.loads(raw); validate_policy(p,now)
 if raw!=canonical_bytes(p)+b"\n": raise FailClosed("noncanonical policy")
 return LoadedPolicy(p,canonical_hash(p))

def utc_day(ts): return datetime.fromtimestamp(ts,timezone.utc).date().isoformat()

class DailyLedger:
 def __init__(self,path,clock):
  self.path,self.clock,self._mutex=Path(path),clock,threading.Lock(); self._load()
 def _blank(self): return {"day":utc_day(self.clock()),"submissions":0,"modeled_loss_reserved":"0","loss_streak":0,"open_basket":None,"trades":{}}
 def _load(self):
  try: self.data=json.loads(self.path.read_text())
  except FileNotFoundError: self.data=self._blank()
  today=utc_day(self.clock())
  if self.data.get("day")!=today:
   open_id=self.data.get("open_basket"); trade=self.data.get("trades",{}).get(open_id) if open_id else None
   if open_id and isinstance(trade,dict) and trade.get("status") in {"reserved","ambiguous","open"}: return
   trades=self.data.get("trades",{})
   self.data={**self._blank(),"trades":trades}; self._save()
 def _save(self): atomic_json(self.path,self.data)
 def assert_entry_allowed(self):
  if self.data["loss_streak"]>=2: raise FailClosed("loss pause")
  if self.data["submissions"]>=2: raise FailClosed("entry cap")
  if self.data["open_basket"] is not None: raise FailClosed("basket already open")
  if float(self.data["modeled_loss_reserved"])>=4: raise FailClosed("daily risk cap")
 def reserve(self,identity,modeled_loss):
  if not isinstance(identity,str) or not identity or not 0<=float(modeled_loss)<=2: raise FailClosed("invalid reservation")
  with self._mutex:
   self._load(); self.assert_entry_allowed()
   if identity in self.data["trades"]: raise FailClosed("duplicate identity")
   if float(self.data["modeled_loss_reserved"])+float(modeled_loss)>4: raise FailClosed("daily risk cap")
   self.data["submissions"]+=1; self.data["modeled_loss_reserved"]=str(float(self.data["modeled_loss_reserved"])+float(modeled_loss))
   self.data["open_basket"]=identity; self.data["trades"][identity]={"status":"reserved","modeled_loss":str(modeled_loss)}; self._save(); return identity
 def mark_ambiguous(self,identity):
  with self._mutex: self._load(); self.data["trades"][identity]["status"]="ambiguous"; self._save()
 def release_prewrite_failure(self,identity):
  with self._mutex:
   self._load(); trade=self.data["trades"].get(identity)
   if not trade or trade.get("status") not in {"reserved","ambiguous"} or self.data.get("open_basket")!=identity:
    raise FailClosed("ledger reservation mismatch")
   loss=float(trade["modeled_loss"]); trade["status"]="failed_prewrite"
   self.data["open_basket"]=None; self.data["submissions"]-=1
   self.data["modeled_loss_reserved"]=str(max(0,float(self.data["modeled_loss_reserved"])-loss)); self._save()
 def close(self,identity,pnl):
  with self._mutex:
   self._load(); trade=self.data["trades"].get(identity)
   if not trade: raise FailClosed("unknown trade")
   trade.update(status="closed",pnl=str(pnl)); self.data["open_basket"]=None; self.data["modeled_loss_reserved"]="0"
   self.data["loss_streak"]=self.data["loss_streak"]+1 if float(pnl)<0 else 0; self._save()

SETUP_TTL_SECONDS=4*60*60

def build_plan(rows,now,scan_id):
 if not isinstance(scan_id,str) or not scan_id: raise FailClosed("scan provenance")
 plans=[]; snapshots={}; allowed={"bid","ask","spread","funding","oiChange","btcChange","atr","volumeRatio","response_ts","ticker_ts","oi_ts","btc_ts"}
 for row in sorted(rows,key=lambda x:(-float(x["score"]),x["symbol"])):
  plans.append({k:row[k] for k in ("symbol","side","zone","trigger","invalid","stop","tp")})
  market=row.get("market",{}); snapshots[row["symbol"]]={k:market[k] for k in sorted(allowed & set(market))}
 return {"schema":"nexus.basket-plan","version":1,"generated_at":format_z(now),"expires_at":format_z(now+SETUP_TTL_SECONDS),"source":"deterministic-market-wide-scan","provenance":{"scan_id":scan_id,"rules":"score-desc-symbol-asc-v1","scan_snapshots":snapshots},"plans":plans}

def validate_scan_plan(doc,now,scanned_universe):
 if parse_z(doc["generated_at"])>now or parse_z(doc["expires_at"])-parse_z(doc["generated_at"])>SETUP_TTL_SECONDS or now>=parse_z(doc["expires_at"]): raise FailClosed("stale plan")
 symbols={x["symbol"] for x in doc["plans"]}
 if not symbols or not symbols<=set(scanned_universe): raise FailClosed("unscanned pair expansion")
 return doc

def assert_account_isolated(positions,basket,owned_link):
 active=[p for p in positions if float(p.get("size",0))!=0]
 if any(p.get("symbol") not in basket for p in active): raise FailClosed("manual exposure present")
 if len(active)>1: raise FailClosed("multiple basket exposure")
 return active

def require_fresh(value,now,max_age,future_skew=2):
 ts=value.get("_response_ts")
 if not isinstance(ts,(int,float)) or ts-now>future_skew or now-ts>max_age: raise FailClosed("stale or ambiguous API")
 return value

def _fresh_private(runtime,path,params,now,max_age=30):
 value=runtime.read(path,params,private=True)
 transport_clock=getattr(getattr(runtime,"transport",None),"wall_clock",None)
 transport_now=getattr(getattr(runtime,"transport",None),"now",None)
 decision_now=transport_clock() if callable(transport_clock) else (transport_now if isinstance(transport_now,(int,float)) and not isinstance(transport_now,bool) else now)
 return require_fresh(value,decision_now,max_age)

def require_native_protection(position):
 if not position.get("stopLoss") or not position.get("takeProfit"): raise FailClosed("native protection absent")
 return position

def owned_position(rows,symbol,link):
 matches=[x for x in rows if x.get("symbol")==symbol and float(x.get("size",0))!=0]
 if any(x.get("orderLinkId") not in (None,"",link) for x in matches): raise FailClosed("position ownership ambiguous")
 if len(matches)!=1 or len([x for x in rows if x.get("symbol")==symbol and float(x.get("size",0))!=0])!=1: raise FailClosed("position ownership ambiguous")
 return matches[0]

@dataclass
class ExitState:
 side: str; entry: float; stop: float; tp: float; qty: float; symbol: str

def exit_action(state,candle,cost):
 if not candle.get("closed"): return None
 close=float(candle["close"]); risk=abs(state.entry-state.stop); favorable=(close-state.entry) if state.side=="Buy" else (state.entry-close)
 if candle.get("structure_break") and (candle.get("oi_confirm") or candle.get("volume_confirm")): return {"kind":"close","reduceOnly":True,"closeOnTrigger":True}
 candidate=None; reason=None
 if favorable>=2*risk:
  distance=max(1.5*float(candle["atr"]),abs(close-float(candle["swing"])))
  candidate=close-distance if state.side=="Buy" else close+distance; reason="trailing"
 elif favorable>=risk: candidate=state.entry+cost if state.side=="Buy" else state.entry-cost; reason="breakeven"
 tighter=candidate is not None and ((state.side=="Buy" and candidate>state.stop) or (state.side=="Sell" and candidate<state.stop))
 return {"kind":"stop","stop":candidate,"reason":reason} if tighter else None

def volume_confirmed(volumes):
 baseline=statistics.median(volumes[:-1]) if len(volumes)>=4 else 0
 return baseline>0 and volumes[-1]>1.5*baseline

def _notice_value(value):
 return str(value).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def _setup_notice(plan):
 rows=[]
 for row in plan.get("plans",[])[:5]:
  values={k:_notice_value(row.get(k)) for k in ("symbol","side","zone","trigger","stop","tp")}
  rows.append("{symbol} {side} zone={zone} trigger={trigger} SL={stop} TP={tp}".format(**values))
 return "setup expiry="+_notice_value(plan.get("expires_at"))+"\n"+"\n".join(rows)

def _entry_notice(durable,status):
 body=durable.get("body") or {}; symbol=body.get("symbol") or durable.get("candidate"); side=body.get("side")
 entry=body.get("price"); stop=body.get("stopLoss"); tp=body.get("takeProfit")
 try: rr=abs(float(tp)-float(entry))/abs(float(entry)-float(stop))
 except (TypeError,ValueError,ZeroDivisionError): rr=None
 values={"status":status,"symbol":symbol,"side":side,"entry":entry,"qty":body.get("qty"),"sl":stop,"tp":tp,"rr":rr,"link":body.get("orderLinkId") or durable.get("orderLinkId")}
 return ("entry {status}: {symbol} {side} entry={entry} qty={qty} SL={sl} TP={tp} RR={rr} orderLinkId={link} "
         "https://www.tradingview.com/chart/?symbol=BYBIT%3A{symbol}.P").format(**{k:_notice_value(v) for k,v in values.items()})

class Notifier:
 def __init__(self,sink,token,chat_id,log=lambda _text:None): self.sink,self.secrets,self.log=sink,[str(token),str(chat_id)],log
 def send(self,text):
  for secret in self.secrets: text=text.replace(secret,"[REDACTED]")
  text=re.sub(r"(?i)(token=)[^&\s]+",r"\1[REDACTED]",text)
  try: return self.sink(text) is not False
  except Exception:
   self.log("notifier_error:telegram delivery failed"); return False

class DurableNotifier:
 def __init__(self,notifier,path): self.notifier,self.path=notifier,Path(path)
 def send(self,text,identity=None,event=None):
  if identity is None or event is None:
   event=str(text).partition(":")[0].strip() or "notice"
   identity=hashlib.sha256(str(text).encode()).hexdigest()
  key=event+":"+identity
  try: data=json.loads(self.path.read_text())
  except FileNotFoundError: data={"sent":[]}
  except (OSError,json.JSONDecodeError): return False
  if key in data.get("sent",[]): return True
  if not self.notifier.send(text): return False
  data.setdefault("sent",[]).append(key); atomic_json(self.path,data)
  return True

def cli_notifier(mode,recovering=False,log=lambda text:print(text,flush=True)):
 token,chat=os.environ.get("TELEGRAM_BOT_TOKEN"),os.environ.get("TELEGRAM_CHAT_ID")
 if mode=="live" and not recovering and (not token or not chat): raise FailClosed("Telegram token and chat ID required for fresh live startup")
 if not token or not chat: return Notifier(lambda _text:False,token or "",chat or "",log)
 def send(text):
  data=urllib.parse.urlencode({"chat_id":chat,"text":text}).encode()
  urllib.request.urlopen(urllib.request.Request("https://api.telegram.org/bot"+token+"/sendMessage",data=data),timeout=5).close()
 return Notifier(send,token,chat,log)

TRANSITIONS={"IDLE":{"WATCHING"},"WATCHING":{"SUBMITTING","IDLE"},"SUBMITTING":{"AMBIGUOUS","OPEN","FATAL"},"AMBIGUOUS":{"OPEN","IDLE","FATAL"},"OPEN":{"CLOSED","FATAL"},"CLOSED":{"IDLE"},"FATAL":set()}
class StateStore:
 def __init__(self,path):
  self.path=Path(path)
  try: self.data=json.loads(self.path.read_text())
  except FileNotFoundError: self.data={"state":"IDLE"}
 def transition(self,target,**fields):
  if target not in TRANSITIONS.get(self.data["state"],set()): raise FailClosed("invalid state transition")
  self.data.update(fields,state=target); atomic_json(self.path,self.data)

def recovery_action(state):
 return "reconcile_order" if state.get("state") in {"SUBMITTING","AMBIGUOUS"} else "monitor_owned_position" if state.get("state")=="OPEN" else "none"

def _recover_executor(runtime,path,now,notify):
 return Executor.run_live_session(runtime,None,None,now,state_path=path,
  lock_path=Path(path).with_suffix(".lock"),notify=notify,now_fn=getattr(runtime,"wall_clock",None) or (lambda:now))

def _owned_open_position(runtime,durable,now):
 body=durable.get("body") or {}; symbol=durable.get("winner")
 if not symbol or body.get("symbol")!=symbol or not durable.get("orderLinkId") or body.get("orderLinkId")!=durable["orderLinkId"]:
  raise FailClosed("missing durable ownership identity")
 result=_fresh_private(runtime,"/v5/position/list",{"category":"linear","settleCoin":"USDT"},now)
 active=[p for p in result.get("list",[]) if float(p.get("size") or 0)]
 expected=float(durable.get("filledQty",body.get("cumExecQty",body.get("qty"))) or 0)
 candidate=[p for p in active if p.get("symbol")==symbol]
 exact=[p for p in candidate if p.get("side")==body.get("side")
        and int(p.get("positionIdx",-1))==int(body.get("positionIdx",-2))
        and abs(float(p.get("size") or 0)-expected)<1e-12]
 other_bot=[p for p in active if p.get("symbol")!=symbol and isinstance(p.get("orderLinkId"),str) and p.get("orderLinkId").startswith(("basket-","auto-","autoclose-"))]
 if len(candidate)>1 or (candidate and len(exact)!=1) or other_bot: raise FailClosed("position ownership ambiguous")
 return exact[0] if exact else None

def _final_owned_position(runtime,durable,now):
 position=_owned_open_position(runtime,durable,now)
 orders=_fresh_private(runtime,"/v5/order/realtime",{"category":"linear","settleCoin":"USDT","openOnly":0},now)
 active=[x for x in orders.get("list",[]) if x.get("orderStatus") in Executor.ACTIVE_ORDERS]
 for order in active:
  link=order.get("orderLinkId")
  if order.get("symbol")==durable["winner"] or not isinstance(link,str) or not link or link.startswith(("basket-","auto-","autoclose-")): raise FailClosed("position ownership ambiguous")
 if position is None: raise FailClosed("owned position absent")
 return position

def _open_action(runtime,durable,position,now):
 symbol=durable["winner"]; candles,response_ts=Executor._closed(runtime,symbol,5,now,3)
 if now-response_ts>30: raise FailClosed("stale or ambiguous API")
 oi=require_fresh(runtime.read("/v5/market/open-interest",{"category":"linear","symbol":symbol,"intervalTime":"2h","limit":2}),now,7200)
 points=oi.get("list",[]); volumes=[float(x["volume"]) for x in candles]; closes=[x["close"] for x in candles]
 ranges=[max(x["high"]-x["low"],abs(x["high"]-p),abs(x["low"]-p)) for x,p in zip(candles[1:],closes[:-1])]
 directional_oi=len(points)>=2 and ((position["side"]=="Buy" and float(points[-1]["openInterest"])<float(points[0]["openInterest"])) or (position["side"]=="Sell" and float(points[-1]["openInterest"])>float(points[0]["openInterest"])))
 candle={"closed":True,"close":closes[-1],"atr":sum(ranges)/len(ranges),"swing":min(x["low"] for x in candles) if position["side"]=="Buy" else max(x["high"] for x in candles),
         "structure_break":closes[-1]<closes[-2] if position["side"]=="Buy" else closes[-1]>closes[-2],
         "oi_confirm":directional_oi,"volume_confirm":volume_confirmed(volumes)}
 state=ExitState(position["side"],float(position.get("entryPrice") or durable["body"]["price"]),float(position["stopLoss"]),float(position["takeProfit"]),float(position["size"]),symbol)
 return exit_action(state,candle,.002*state.entry)

def _reconcile_exit(runtime,state_dir,durable,now,notify):
 close_body=durable.get("closeBody") or {}; close_link=durable.get("closeOrderLinkId")
 symbol=durable.get("winner")
 if not close_link or close_body.get("orderLinkId")!=close_link:
  if _owned_open_position(runtime,durable,now) is not None: raise Executor.AmbiguousOrder("close position remains")
  pnl_rows=_fresh_private(runtime,"/v5/position/closed-pnl",{"category":"linear","symbol":symbol,"limit":50},now).get("list",[])
  entry_qty=durable.get("body",{}).get("qty")
  if not entry_qty: raise FailClosed("missing entry qty for native exit")
  try: entry_qty=float(entry_qty)
  except (TypeError,ValueError): raise FailClosed("invalid entry qty")
  exact=[row for row in pnl_rows if abs(float(row.get("closedSize") or row.get("qty") or 0)-entry_qty)<=1e-12]
  if len(exact)!=1: raise Executor.AmbiguousOrder("native close pnl unresolved")
  close_id=exact[0].get("orderId"); pnl=exact[0].get("closedPnl")
 else:
  rows=_fresh_private(runtime,"/v5/order/realtime",{"category":"linear","symbol":symbol,"orderLinkId":close_link,"openOnly":0},now).get("list",[])
  keys=("orderLinkId","symbol","side","positionIdx","qty","orderType","reduceOnly")
  exact=[row for row in rows if all(str(row.get(k,""))==str(close_body.get(k,"")) for k in keys)]
  if len(exact)!=1 or exact[0].get("orderStatus")!="Filled": raise Executor.AmbiguousOrder("close reconciliation unresolved")
  if _owned_open_position(runtime,durable,now) is not None: raise Executor.AmbiguousOrder("close position remains")
  close_id=exact[0].get("orderId")
  pnl_rows=_fresh_private(runtime,"/v5/position/closed-pnl",{"category":"linear","symbol":symbol,"limit":50},now).get("list",[])
  pnl_exact=[row for row in pnl_rows if row.get("orderId")==close_id]
  if not close_id or len(pnl_exact)!=1: raise Executor.AmbiguousOrder("close pnl unresolved")
  pnl=pnl_exact[0].get("closedPnl")
 ledger=DailyLedger(state_dir/"ledger.json",lambda:now); identity=ledger.data.get("open_basket")
 if not identity: raise FailClosed("ledger ownership missing")
 ledger.close(identity,pnl); durable.update(status="closed",actualPnl=str(pnl),closeIdentity=identity,body=None,orderLinkId=None,orderId=None,pendingOrder=None,closeBody=None,closeOrderLinkId=None)
 Executor.save_state(durable,state_dir/"executor.json")
 if not durable.get("closeNotified"): notify("closed:"+symbol+":"+str(pnl))
 return {"mode":"live","status":"closed","pnl":str(pnl)}

def _emergency_close(runtime,state_dir,durable,position,now):
 link=("autoclose-"+durable["orderLinkId"])[-36:]
 body={"category":"linear","symbol":position["symbol"],"side":"Sell" if position["side"]=="Buy" else "Buy","positionIdx":position["positionIdx"],"orderType":"Market","qty":str(position["size"]),"reduceOnly":True,"closeOnTrigger":True,"orderLinkId":link}
 durable.update(status="close_reconciling",exit_intent_type="close",closeOrderLinkId=link,closeBody=body,exitIntentAt=now)
 Executor.save_state(durable,state_dir/"executor.json")
 _final_owned_position(runtime,durable,now)
 runtime.write("/v5/order/create",body)
 return {"mode":"live","status":"open","action":"emergency_close"}

def _reconcile_protection(runtime,state_dir,durable,now):
 position=_owned_open_position(runtime,durable,now)
 if position is None: raise FailClosed("owned position absent")
 kind=durable.get("exit_intent_type"); body=durable.get("protectionBody") if kind=="protection_repair" else durable.get("stopBody")
 if not isinstance(body,dict): raise FailClosed("missing durable protection intent")
 current=float(position.get("stopLoss") or 0); target=float(body["stopLoss"])
 applied=(str(position.get("stopLoss"))==str(body["stopLoss"]) and (kind=="stop" or str(position.get("takeProfit"))==str(body["takeProfit"])))
 if kind=="stop": applied=applied or (position["side"]=="Buy" and current>=target) or (position["side"]=="Sell" and current<=target and current>0)
 if applied:
  durable.update(status="open",repairAttempts=0); Executor.save_state(durable,state_dir/"executor.json")
  return {"mode":"live","status":"open","action":"stop_reconciled" if kind=="stop" else "protection_repaired"}
 if int(durable.get("repairAttempts",0))>=1: return _emergency_close(runtime,state_dir,durable,position,now)
 durable["repairAttempts"]=1; Executor.save_state(durable,state_dir/"executor.json")
 _final_owned_position(runtime,durable,now)
 try: runtime.write("/v5/position/trading-stop",body)
 except (TimeoutError,ConnectionError,Executor.TransientError) as e:
  durable["status"]="stop_reconciling" if kind=="stop" else "protection_reconciling"; Executor.save_state(durable,state_dir/"executor.json")
  raise Executor.AmbiguousOrder("protection write unresolved") from e
 checked=_owned_open_position(runtime,durable,now)
 if not checked or str(checked.get("stopLoss"))!=str(body["stopLoss"]) or (kind=="protection_repair" and str(checked.get("takeProfit"))!=str(body["takeProfit"])):
  durable["status"]="stop_reconciling" if kind=="stop" else "protection_reconciling"; Executor.save_state(durable,state_dir/"executor.json")
  return {"mode":"live","status":"reconciling","action":kind}
 durable["status"]="open"; Executor.save_state(durable,state_dir/"executor.json")
 return {"mode":"live","status":"open","action":kind}

def _monitor_open(runtime,state_dir,durable,now,notify):
 ledger=DailyLedger(state_dir/"ledger.json",lambda:now); identity=ledger.data.get("open_basket")
 if not identity or identity not in ledger.data.get("trades",{}): raise FailClosed("ledger ownership missing")
 position=_owned_open_position(runtime,durable,now)
 if position:
  if not position.get("stopLoss") or not position.get("takeProfit"):
   body={"category":"linear","symbol":durable["winner"],"positionIdx":position["positionIdx"],"stopLoss":durable["body"]["stopLoss"],"takeProfit":durable["body"]["takeProfit"]}
   durable.update(exit_intent_type="protection_repair",protectionBody=body,repairAttempts=0)
   Executor.save_state(durable,state_dir/"executor.json")
   _final_owned_position(runtime,durable,now)
   try: runtime.write("/v5/position/trading-stop",body)
   except (TimeoutError,ConnectionError,Executor.TransientError) as e:
    durable["status"]="protection_reconciling"; durable["repairAttempts"]=1; Executor.save_state(durable,state_dir/"executor.json")
    raise Executor.AmbiguousOrder("protection write unresolved") from e
   checked=_owned_open_position(runtime,durable,now)
   if not checked or str(checked.get("stopLoss"))!=str(body["stopLoss"]) or str(checked.get("takeProfit"))!=str(body["takeProfit"]):
    durable["status"]="protection_reconciling"; durable["repairAttempts"]=1; Executor.save_state(durable,state_dir/"executor.json"); raise Executor.AmbiguousOrder("protection repair unconfirmed")
   durable["status"]="open"; Executor.save_state(durable,state_dir/"executor.json"); notify("protection:"+durable["winner"])
   return {"mode":"live","status":"open","action":"protection_repair"}
  action=_open_action(runtime,durable,position,now)
  if action:
   durable["exitIntent"]={**action,"at":now}; Executor.save_state(durable,state_dir/"executor.json")
   def confirm_stop(body):
    checked=_owned_open_position(runtime,durable,now)
    if not checked: return False
    current,target=float(checked.get("stopLoss") or 0),float(body["stopLoss"])
    return current>=target if checked["side"]=="Buy" else 0<current<=target
   try:
    outcome=execute_exit(runtime,StateStore(state_dir/"executor.json"),{**position,"orderLinkId":durable["orderLinkId"]},durable["orderLinkId"],action,now,
                         verify=lambda:_final_owned_position(runtime,durable,now),confirm_stop=confirm_stop)
   except (TimeoutError,ConnectionError,Executor.TransientError) as e:
    durable=Executor.load_state(state_dir/"executor.json"); durable["status"]="stop_reconciling" if action["kind"]=="stop" else "exit_reconciling"; durable["repairAttempts"]=1 if action["kind"]=="stop" else durable.get("repairAttempts",0); Executor.save_state(durable,state_dir/"executor.json")
    raise Executor.AmbiguousOrder("exit write unresolved") from e
   if isinstance(outcome,dict) and outcome.get("status")=="reconciling": return outcome
  return {"mode":"live","status":"open",**({"action":action["kind"]} if action else {})}
 return {"mode":"live","status":"open"}

def m5_causal_floor(generated_at):
 return int((generated_at+299)//300*300)

def sanitize_trigger_observations(monitor):
 floor=m5_causal_floor(parse_z(monitor["plan"]["generated_at"]))
 monitor.setdefault("touches",{}); monitor.setdefault("triggerTs",{})
 symbols={row["symbol"] for row in monitor["plan"]["plans"]}
 poisoned=False
 for symbol in symbols:
  touch=monitor["touches"].get(symbol); trigger=monitor["triggerTs"].get(symbol)
  if not isinstance(touch,(int,float)) or isinstance(touch,bool) or touch<floor:
   poisoned|=touch is not None or trigger is not None
   monitor["touches"][symbol]=None; monitor["triggerTs"].pop(symbol,None)
  elif not isinstance(trigger,(int,float)) or isinstance(trigger,bool) or trigger<floor or trigger<=touch:
   poisoned|=trigger is not None
   monitor["triggerTs"].pop(symbol,None)
 if poisoned:
  if monitor.get("submitted") or monitor.get("reservation"): raise FailClosed("causally invalid reserved monitor")
  for key in ("winner","identity","winnerPlan","reservation","disarmed","triggerM15Ts","triggerM15Close"):
   monitor.pop(key,None)
 return floor

def observe_trigger(monitor,candles,m15_invalid,now):
 eligible=[]; monitor.setdefault("triggerTs",{})
 floor=sanitize_trigger_observations(monitor)
 for row in monitor["plan"]["plans"]:
  symbol=row["symbol"]
  if m15_invalid.get(symbol) is not False:
   monitor["touches"][symbol]=None; monitor["triggerTs"].pop(symbol,None); continue
  low,high=map(float,row["zone"]); trigger=float(row["trigger"])
  for candle in sorted(candles.get(symbol,()),key=lambda x:x["ts"]):
   if not candle.get("closed") or candle["ts"]<floor or candle["ts"]+300>now: continue
   touched=monitor["touches"].get(symbol)
   if touched is None and float(candle["low"])<=high and float(candle["high"])>=low:
    monitor["touches"][symbol]=candle["ts"]; touched=candle["ts"]
   beyond=float(candle["close"])>trigger if row["side"]=="Buy" else float(candle["close"])<trigger
   if isinstance(touched,(int,float)) and candle["ts"]>touched and beyond:
    monitor["triggerTs"].setdefault(symbol,candle["ts"]); eligible.append((candle["ts"],-float(row.get("score",0)),symbol))
 return min(eligible)[2] if eligible else None

@contextlib.contextmanager
def process_lock(path):
 path=Path(path); path.parent.mkdir(parents=True,exist_ok=True); fd=os.open(path,os.O_CREAT|os.O_RDWR,0o600)
 try:
  try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError as e: raise FailClosed("watcher already running") from e
  yield
 finally: os.close(fd)

def canonical_lock_path(state_dir): return Path(state_dir).resolve().with_suffix(".lock")

class Controller:
 def __init__(self,transport,live=False): self.transport,self.live=transport,live
 def write(self,path,body):
  if not self.live: raise FailClosed("live writes disabled")
  return Executor.Runtime(self.transport,"live").write(path,body)

class TransientScanReadError(RuntimeError): pass
class PermanentScanReadError(RuntimeError): pass

class ScanRuntime:
 """Pace and retry public scan reads only; private safety reads and writes bypass it."""
 def __init__(self,runtime,clock=time.monotonic,sleep=time.sleep,min_interval=.12,retries=2,wall_clock=None):
  self.runtime,self.clock,self.sleep,self.min_interval,self.retries,self.wall_clock=runtime,clock,sleep,min_interval,retries,wall_clock
  self._last=None
 def read(self,path,params=None,private=False):
  if private: return self.runtime.read(path,params,private=True)
  for attempt in range(self.retries+1):
   if self._last is not None:
    wait=self.min_interval-(self.clock()-self._last)
    if wait>0: self.sleep(wait)
   self._last=self.clock()
   try: return self.runtime.read(path,params)
   except urllib.error.HTTPError as e:
    if e.code!=429 and not 500<=e.code<600: raise PermanentScanReadError("permanent public HTTP read failure") from e
    if attempt>=self.retries: raise TransientScanReadError("transient public HTTP read failure") from e
    try: delay=max(0,min(4.0,float(e.headers.get("Retry-After"))))
    except (AttributeError,TypeError,ValueError): delay=min(4.0,2.0**attempt)
    self.sleep(delay)
   except (Executor.TransientError,urllib.error.URLError,TimeoutError,ConnectionError) as e:
    if attempt>=self.retries: raise TransientScanReadError("transient public read failure") from e
    self.sleep(min(4.0,2.0**attempt))
 def write(self,path,body): return self.runtime.write(path,body)

def next_cycle_delay(degraded_count,normal):
 return min(120,max(30,30*2**(degraded_count-1))) if degraded_count else max(1,normal)

_SCAN_NOTICE={}
def _degraded(mode,state_dir,now,notify):
 key=str(Path(state_dir)); last=_SCAN_NOTICE.get(key)
 if last is None or now-last>=300:
  notify("warning:scan degraded; transient public read failure; no candidate selected")
  _SCAN_NOTICE[key]=now
 return {"mode":mode,"status":"degraded","backoff":30}

def require_live_gate(loaded,now):
 validate_policy(loaded.document,now)
 if os.environ.get("AUTONOMOUS_BASKET_LIVE")!="YES": raise FailClosed("live env gate")
 if not hashlib.sha256(_EXECUTOR_PATH.read_bytes()).hexdigest()==EXECUTOR_SHA256: raise FailClosed("executor source mismatch")
 if not __import__("hmac").compare_digest(os.environ.get("AUTONOMOUS_BASKET_POLICY_SHA256",""),loaded.digest): raise FailClosed("policy digest mismatch")
 return loaded

def _scan_row(symbol,rows,turnover,oi_latest=None,oi_prior=None):
 try:
  candles=sorted(rows,key=lambda r:int(r[0])); closes=[float(r[4]) for r in candles]; highs=[float(r[2]) for r in candles]; lows=[float(r[3]) for r in candles]; volumes=[float(r[5]) for r in candles]
  if len(closes)<5 or min(lows)<=0: return None
  side="Buy" if closes[-1]>closes[0] else "Sell"; low=min(lows[-3:]); high=max(highs[-3:])
  if not low<high: return None
  atr=statistics.median(max(h-l,abs(h-p),abs(l-p)) for h,l,p in zip(highs[1:],lows[1:],closes[:-1])); width=max(high-low,atr)
  trigger=high if side=="Buy" else low; invalid=low if side=="Buy" else high; stop=low-width*.5 if side=="Buy" else high+width*.5; tp=high+width*2 if side=="Buy" else low-width*2
  if stop<=0 or tp<=0 or atr<=0: return None
  baseline=statistics.median(volumes[:-1]); vr=volumes[-1]/baseline if baseline>0 else 0; oi_change=float(oi_latest)/float(oi_prior)-1 if oi_latest and oi_prior else 0
  return {"symbol":symbol,"score":float(turnover)*(1+max(-.5,min(.5,oi_change))+min(vr,5)/10),"side":side,"zone":[str(low),str(high)],"trigger":str(trigger),"invalid":str(invalid),"stop":str(stop),"tp":str(tp),"market":{"atr":atr,"volumeRatio":vr,"oiChange":oi_change}}
 except (KeyError,IndexError,TypeError,ValueError,statistics.StatisticsError,ZeroDivisionError): return None

def scan_market(runtime,now,scan_id):
 wrapper_clock=getattr(runtime,"wall_clock",None) if isinstance(runtime,ScanRuntime) else None
 transport_clock=getattr(getattr(runtime,"transport",None),"wall_clock",None)
 transport_now=getattr(getattr(runtime,"transport",None),"now",None)
 wall_clock=wrapper_clock if callable(wrapper_clock) else (transport_clock if callable(transport_clock) else (lambda:transport_now if isinstance(transport_now,(int,float)) and not isinstance(transport_now,bool) else now))
 def fresh(value,max_age): return require_fresh(value,wall_clock(),max_age)
 instruments=fresh(runtime.read("/v5/market/instruments-info",{"category":"linear"}),30).get("list",[])
 tickers=fresh(runtime.read("/v5/market/tickers",{"category":"linear"}),30)
 positions=fresh(runtime.read("/v5/position/list",{"category":"linear","settleCoin":"USDT"},private=True),30)
 orders=fresh(runtime.read("/v5/order/realtime",{"category":"linear","settleCoin":"USDT","openOnly":0},private=True),30)
 exposed={x.get("symbol") for x in positions.get("list",[]) if float(x.get("size") or 0)} | {x.get("symbol") for x in orders.get("list",[]) if x.get("orderStatus") in Executor.ACTIVE_ORDERS}
 eligible={x["symbol"] for x in instruments if x.get("status")=="Trading" and x.get("contractType")=="LinearPerpetual"
  and isinstance(x.get("launchTime"),str) and int(x["launchTime"])<=int((wall_clock()-90*86400)*1000)
  and re.fullmatch(r"[A-Z0-9]{2,20}USDT",x.get("symbol","")) and x["symbol"] not in exposed}
 market={}
 for x in tickers.get("list",[]):
  try:
   bid,ask,turn=float(x["bid1Price"]),float(x["ask1Price"]),float(x["turnover24h"])
   if x.get("fundingRate") is None or turn<10_000_000 or bid<=0 or ask<bid or (ask-bid)/bid>.0012: continue
   float(x["fundingRate"]); market[x["symbol"]]=turn
  except (KeyError,TypeError,ValueError): continue
 def closed(symbol,minutes):
  result=fresh(runtime.read("/v5/market/kline",{"category":"linear","symbol":symbol,"interval":str(minutes),"limit":20}),minutes*60+60)
  current=wall_clock(); raw=[[str(int(r[0])//1000),*r[1:]] for r in result.get("list",[]) if int(r[0])//1000+minutes*60<=current]
  return Executor.parse_closed_candles(raw,minutes*60,current,2)
 try: btc=closed("BTCUSDT",60)
 except (Executor.PermanentError,KeyError,TypeError,ValueError): return build_plan([],wall_clock(),scan_id)
 btc_up=btc[-1]["close"]>=btc[-2]["close"]
 rows=[]
 for symbol in sorted(eligible & market.keys(),key=lambda s:(-market[s],s)):
  try:
   m5,m15,h1=closed(symbol,5),closed(symbol,15),closed(symbol,60)
   oi=fresh(runtime.read("/v5/market/open-interest",{"category":"linear","symbol":symbol,"intervalTime":"2h","limit":2}),7200)
   if len(oi.get("list",[]))<2 or any(float(x["openInterest"])<=0 for x in oi["list"]): continue
   points=oi["list"]; row=_scan_row(symbol,[[str(x["ts"]*1000),str(x["open"]),str(x["high"]),str(x["low"]),str(x["close"]),str(x["volume"])] for x in m15],market[symbol],points[-1]["openInterest"],points[0]["openInterest"])
   if row and row["side"] in ("Buy","Sell") and ( (row["side"]=="Buy")==btc_up or (row["side"]=="Sell")!=btc_up ) and m5 and h1: rows.append(row)
  except (Executor.PermanentError,KeyError,IndexError,TypeError,ValueError): continue
 return build_plan(rows[:5],wall_clock(),scan_id)

def delegate_entry(*,runtime,plan,policy,now,state_path,lock_path,notify,winner_evidence,wall_clock=None):
 approval=Executor.make_approval(("a"+str(int(now)))[-13:],now,ttl=300,plan_document=plan,source_hash=EXECUTOR_SHA256)
 confirmation=approval["nonce"]+":"+approval["fingerprint"]
 return Executor.run_live_session(runtime,approval,confirmation,now,state_path=state_path,lock_path=lock_path,
                                  plan_document=plan,source_hash=EXECUTOR_SHA256,notify=notify,
                                  winner_evidence=winner_evidence,policy_snapshot=policy,
                                  policy_digest=canonical_hash(policy),now_fn=wall_clock or (lambda:now))

def execute_exit(runtime,store,position,owned_link,action,now,verify=lambda:None,confirm_stop=lambda _body:True):
 require_native_protection(position)
 if position.get("orderLinkId") not in (None,"",owned_link): raise FailClosed("position ownership ambiguous")
 symbol=position["symbol"]; side="Sell" if position.get("side")=="Buy" else "Buy"
 if action["kind"]=="stop":
  old=float(position["stopLoss"]); new=float(action["stop"])
  if not ((position["side"]=="Buy" and new>old) or (position["side"]=="Sell" and new<old)): raise FailClosed("stop not tighter")
  body={"category":"linear","symbol":symbol,"positionIdx":position["positionIdx"],"stopLoss":str(new)}; endpoint="/v5/position/trading-stop"
  fields={"exitIntentAt":now,"exit_intent_type":"stop","stopBody":body,"repairAttempts":0}
 else:
  link=("autoclose-"+owned_link)[-36:]; body={"category":"linear","symbol":symbol,"side":side,"positionIdx":position["positionIdx"],"orderType":"Market","qty":str(position["size"]),"reduceOnly":True,"closeOnTrigger":True,"orderLinkId":link}; endpoint="/v5/order/create"
  fields={"exitIntentAt":now,"exit_intent_type":"close","closeBody":body,"status":"exit_reconciling","closeOrderLinkId":body["orderLinkId"]}
 store.data.update(fields); atomic_json(store.path,store.data)
 verify()
 result=runtime.write(endpoint,body)
 if action["kind"]=="stop" and not confirm_stop(body):
  store.data["status"]="stop_reconciling"; atomic_json(store.path,store.data)
  return {"mode":"live","status":"reconciling","action":"stop"}
 return result

LEGACY_BASKET_EXPOSURE_PREWRITE_SHA256="35f2fbcf292004061c71c58ac89d2c89dc6be792dd8657aef73c59af4cca6cac"
CLUSDT_ORPHAN_IDENTITY="basket-1786685403-CLUSDT"
CLUSDT_ORPHAN_SOURCE_SHA256="f7ccbf216643b8bea284826604b846cc75e110ef3f7ebe524c5d3d7b1e9682d4"


def recover_clusdt_orphan(runtime,state_dir,now,_fault=None):
 """Recover one production prewrite crash only after fresh exact GET-only absence proof."""
 state_dir=Path(state_dir); names=("ledger","monitor","executor"); paths={n:state_dir/(n+".json") for n in names}; journal_path=state_dir/"clusdt_orphan_recovery.json"
 def crash(stage):
  if _fault==stage: raise RuntimeError("injected "+stage)
 def sync_dir():
  dfd=os.open(state_dir,os.O_DIRECTORY)
  try: os.fsync(dfd)
  finally: os.close(dfd)
 with process_lock(state_dir/"repair.lock"):
  if journal_path.exists():
   try: sealed=json.loads(journal_path.read_text())
   except (OSError,json.JSONDecodeError) as e: raise FailClosed("invalid CLUSDT recovery journal") from e
   journal={k:v for k,v in sealed.items() if k!="seal"}
   if (sealed.get("seal")!=canonical_hash(journal) or journal.get("schema")!="nexus.clusdt-orphan-recovery"
    or journal.get("version")!=1 or journal.get("identity")!=CLUSDT_ORPHAN_IDENTITY): raise FailClosed("invalid CLUSDT recovery journal")
  else:
   try: ledger,monitor,executor=(json.loads(paths[n].read_text()) for n in names)
   except (OSError,json.JSONDecodeError,TypeError) as e: raise FailClosed("CLUSDT orphan malformed") from e
   identity=CLUSDT_ORPHAN_IDENTITY; trade=ledger.get("trades",{}).get(identity); approval=executor.get("approved",{})
   exact=(ledger.get("open_basket")==identity and ledger.get("submissions")==1 and float(ledger.get("modeled_loss_reserved",-1))==2
    and isinstance(trade,dict) and trade.get("status") in {"reserved","ambiguous"} and float(trade.get("modeled_loss",-1))==2
    and monitor.get("identity")==identity and monitor.get("submitted") is True and monitor.get("winner")=="CLUSDT"
    and executor.get("status")=="watching" and executor.get("candidate") is None and executor.get("winner") is None
    and executor.get("executorSourceSha256")==CLUSDT_ORPHAN_SOURCE_SHA256 and approval.get("nonce")=="a1786685403"
    and int(approval.get("issued_at",0))==1786685403 and not any(executor.get(k) for k in ("body","orderLinkId","orderId","pendingOrder","closeBody","closeOrderLinkId")))
   if not exact: raise FailClosed("CLUSDT orphan identity mismatch")
   queries=(("/v5/order/realtime",{"category":"linear","symbol":"CLUSDT","openOnly":0}),
    ("/v5/order/history",{"category":"linear","symbol":"CLUSDT","orderLinkId":identity}),
    ("/v5/execution/list",{"category":"linear","symbol":"CLUSDT","orderLinkId":identity}),
    ("/v5/position/list",{"category":"linear","symbol":"CLUSDT"}))
   evidence=[_fresh_private(runtime,path,params,now).get("list",[]) for path,params in queries]
   if any(evidence[:3]) or any(float(x.get("size") or 0) for x in evidence[3]): raise FailClosed("CLUSDT exchange absence not proven")
   target_ledger=json.loads(json.dumps(ledger)); target_ledger["trades"][identity]["status"]="failed_prewrite"; target_ledger["open_basket"]=None; target_ledger["submissions"]-=1; target_ledger["modeled_loss_reserved"]="0.0"
   target_monitor={**monitor,"submitted":False,"state":"recovered","failedIdentity":identity,"recoveredAt":now}
   symbols={p["symbol"]:None for p in monitor.get("plan",{}).get("plans",[]) if isinstance(p,dict) and isinstance(p.get("symbol"),str)}
   target_executor={**Executor.new_state(None,symbols),"exposure_possible":False}
   targets={"ledger":target_ledger,"monitor":target_monitor,"executor":target_executor}
   journal={"schema":"nexus.clusdt-orphan-recovery","version":1,"identity":identity,"expectedHashes":{n:canonical_hash(v) for n,v in zip(names,(ledger,monitor,executor))},"targetHashes":{n:canonical_hash(targets[n]) for n in names},"targets":targets}
   atomic_json(journal_path,{**journal,"seal":canonical_hash(journal)}); crash("after_journal")
  for name in names:
   try: current=canonical_hash(json.loads(paths[name].read_text()))
   except (OSError,json.JSONDecodeError) as e: raise FailClosed("CLUSDT recovery state mismatch") from e
   if current==journal["expectedHashes"][name]: atomic_json(paths[name],journal["targets"][name])
   elif current!=journal["targetHashes"][name]: raise FailClosed("CLUSDT recovery state mismatch")
   crash("after_"+name)
  if any(canonical_hash(json.loads(paths[n].read_text()))!=journal["targetHashes"][n] for n in names): raise FailClosed("CLUSDT recovery target verification failed")
  journal_path.unlink(); sync_dir(); return {"status":"recovered","identity":CLUSDT_ORPHAN_IDENTITY}


def consume_clusdt_recovered_terminal(state_dir,now,_fault=None):
 state_dir=Path(state_dir); monitor_path=state_dir/"monitor.json"; journal_path=state_dir/"clusdt_terminal_reset.json"
 def crash(stage):
  if _fault==stage: raise RuntimeError("injected "+stage)
 def sync_dir():
  dfd=os.open(state_dir,os.O_DIRECTORY)
  try: os.fsync(dfd)
  finally: os.close(dfd)
 with process_lock(state_dir/"repair.lock"):
  if journal_path.exists():
   try: sealed=json.loads(journal_path.read_text())
   except (OSError,json.JSONDecodeError) as e: raise FailClosed("invalid CLUSDT terminal reset journal") from e
   journal={k:v for k,v in sealed.items() if k!="seal"}
   if sealed.get("seal")!=canonical_hash(journal) or journal.get("schema")!="nexus.clusdt-terminal-reset" or journal.get("version")!=1: raise FailClosed("invalid CLUSDT terminal reset journal")
  else:
   try:
    monitor=json.loads(monitor_path.read_text()); ledger=json.loads((state_dir/"ledger.json").read_text()); executor=Executor.load_state(state_dir/"executor.json")
   except (OSError,json.JSONDecodeError,TypeError) as e: raise FailClosed("CLUSDT recovered terminal malformed") from e
   if monitor.get("state")=="recovery_consumed" and monitor.get("failedIdentity")==CLUSDT_ORPHAN_IDENTITY: return False
   trade=ledger.get("trades",{}).get(CLUSDT_ORPHAN_IDENTITY)
   exact=(monitor.get("state")=="recovered" and monitor.get("failedIdentity")==CLUSDT_ORPHAN_IDENTITY and monitor.get("submitted") is False
    and ledger.get("open_basket") is None and ledger.get("submissions")==0 and float(ledger.get("modeled_loss_reserved",-1))==0
    and isinstance(trade,dict) and trade.get("status")=="failed_prewrite" and executor.get("status")=="watching" and executor.get("exposure_possible") is False
    and not any(executor.get(k) for k in ("candidate","winner","body","orderLinkId","orderId","pendingOrder","closeBody","closeOrderLinkId")))
   if not exact: raise FailClosed("CLUSDT recovered terminal mismatch")
   target={**monitor,"state":"recovery_consumed","recoveryConsumedAt":now}
   journal={"schema":"nexus.clusdt-terminal-reset","version":1,"expectedHash":canonical_hash(monitor),"targetHash":canonical_hash(target),"target":target}
   atomic_json(journal_path,{**journal,"seal":canonical_hash(journal)}); crash("after_journal")
  try: current=json.loads(monitor_path.read_text())
  except (OSError,json.JSONDecodeError) as e: raise FailClosed("CLUSDT terminal reset state mismatch") from e
  digest=canonical_hash(current)
  if digest==journal["expectedHash"]: atomic_json(monitor_path,journal["target"])
  elif digest!=journal["targetHash"]: raise FailClosed("CLUSDT terminal reset state mismatch")
  crash("after_monitor")
  if canonical_hash(json.loads(monitor_path.read_text()))!=journal["targetHash"]: raise FailClosed("CLUSDT terminal reset verification failed")
  journal_path.unlink(); sync_dir(); return True


def repair_prewrite_fatal(runtime,state_dir,now,_fault=None,wall_clock=None):
 runtime.wall_clock=wall_clock or getattr(runtime,"wall_clock",None) or (lambda:now)
 state_dir=Path(state_dir); paths={k:state_dir/(k+".json") for k in ("executor","monitor","ledger")}; journal_path=state_dir/"prewrite_repair.json"
 def crash(stage):
  if _fault==stage: raise RuntimeError("injected "+stage)
 def remove_journal():
  journal_path.unlink(); dfd=os.open(state_dir,os.O_DIRECTORY)
  try: os.fsync(dfd)
  finally: os.close(dfd)
 with process_lock(state_dir/"repair.lock"):
  if journal_path.exists():
   try: journal=json.loads(journal_path.read_text())
   except (OSError,json.JSONDecodeError) as e: raise FailClosed("invalid repair journal") from e
   seal=journal.pop("seal",None)
   if seal!=canonical_hash(journal) or journal.get("schema")!="nexus.prewrite-repair" or journal.get("version")!=1: raise FailClosed("invalid repair journal")
  else:
   executor=Executor.load_state(paths["executor"]); monitor=json.loads(paths["monitor"].read_text()); ledger=json.loads(paths["ledger"].read_text())
   forbidden=("body","orderLinkId","orderId","pendingOrder","closeBody","closeOrderLinkId")
   if executor.get("status")!="fatal" or executor.get("exposure_possible") is not False or any(executor.get(k) for k in forbidden): raise FailClosed("prewrite fatal state ambiguous")
   identity=monitor.get("identity"); winner=monitor.get("winner"); errors=executor.get("errors",{})
   normal_errors=isinstance(errors,dict) and set(errors)=={winner}
   approval=executor.get("approved",{}); nonce=approval.get("nonce") if isinstance(approval,dict) else None
   legacy_ts=nonce[1:] if isinstance(nonce,str) and re.fullmatch(r"a[0-9]+",nonce) else None
   winner_plan=monitor.get("winnerPlan"); plan=executor.get("planDocument") or winner_plan
   source=executor.get("executorSourceSha256") or approval.get("executorSourceSha256")
   fingerprint=executor.get("planFingerprint") or approval.get("fingerprint")
   current_final=executor.get("finalGate") if isinstance(executor.get("finalGate"),dict) else {}
   current_identity=parse_durable_identity(identity)
   current_errors=(normal_errors and current_identity is not None and current_identity[2]==winner
    and current_identity[:2]==(int(approval.get("issued_at",-1)),approval.get("nonce"))
    and source==EXECUTOR_SHA256 and approval.get("executorSourceSha256")==source
    and isinstance(plan,dict) and set(Executor.validate_plan_document(plan,approval.get("issued_at"))[1])=={winner}
    and fingerprint==approval.get("fingerprint")==Executor.plan_fingerprint(plan,source,approval)
    and executor.get("candidate")==executor.get("winner")==winner
    and current_final.get("code") in LEGACY_PREWRITE_FINAL_GATE_CODES
    and current_final.get("message")==errors.get(winner)==executor.get("fatal_reason"))
   legacy_reason=errors.get("unknown") if isinstance(errors,dict) and set(errors)=={"unknown"} else None
   legacy_basket_exposure=(legacy_reason=="basket exposure not clear" and executor.get("fatal_reason")==legacy_reason
    and source==LEGACY_BASKET_EXPOSURE_PREWRITE_SHA256 and approval.get("executorSourceSha256")==source)
   legacy_errors=(legacy_reason in {"exactly one candidate required","winner current market invalid"} or legacy_basket_exposure) and legacy_ts is not None
   legacy_errors=(legacy_errors and identity==f"auto-{legacy_ts}-{winner}" and isinstance(plan,dict) and plan==winner_plan
    and isinstance(plan.get("plans"),list) and len(plan["plans"])==1 and plan["plans"][0].get("symbol")==winner
    and fingerprint==approval.get("fingerprint")
    and fingerprint==Executor.plan_fingerprint(plan,source,approval))
   accepted_errors=current_errors or (normal_errors and (not current_final or isinstance(identity,str) and identity.startswith("auto-")))
   monitor_submission_proven=monitor.get("submitted") is True or (monitor.get("submitted") is False and accepted_errors)
   if not isinstance(identity,str) or not re.fullmatch(r"(?:auto|basket)-[A-Za-z0-9_-]+-"+re.escape(str(winner)),identity) or not monitor_submission_proven or not winner or not (accepted_errors or legacy_errors): raise FailClosed("monitor/executor ownership mismatch")
   positions=_fresh_private(runtime,"/v5/position/list",{"category":"linear","settleCoin":"USDT"},now)
   orders=_fresh_private(runtime,"/v5/order/realtime",{"category":"linear","settleCoin":"USDT","openOnly":0},now)
   active=[x for x in orders.get("list",[]) if x.get("orderStatus") in Executor.ACTIVE_ORDERS]
   candidate_rejection=(normal_errors and errors.get(winner)=="candidate exposure not clear before create") or legacy_basket_exposure
   winner_manual_position=any(x.get("symbol")==winner and float(x.get("size") or 0) for x in positions.get("list",[]))
   winner_manual_order=any(x.get("symbol")==winner and x.get("orderLinkId") not in {identity} and not str(x.get("orderLinkId") or "").startswith(("auto-","autoclose-")) for x in active)
   bot_or_ambiguous_order=any(Executor._bot_link_class(x.get("orderLinkId"),identity)!="external" for x in active)
   if bot_or_ambiguous_order or ((winner_manual_position or winner_manual_order) and not candidate_rejection): raise FailClosed("account exposure not clear")
   trade=ledger.get("trades",{}).get(identity)
   if ledger.get("open_basket")!=identity or not isinstance(trade,dict) or trade.get("status") not in {"reserved","ambiguous"}: raise FailClosed("ledger reservation mismatch")
   targets={"executor":{**executor,"status":"failed_prewrite_repaired","candidate":None,"winner":None,"repairedAt":now},"monitor":{**monitor,"state":"repair_complete","submitted":False,"failedIdentity":identity,"repairedAt":now}}
   target_ledger=json.loads(json.dumps(ledger)); target_trade=target_ledger["trades"][identity]; loss=float(target_trade["modeled_loss"]); target_trade["status"]="failed_prewrite"; target_ledger["open_basket"]=None; target_ledger["submissions"]-=1; target_ledger["modeled_loss_reserved"]=str(max(0.0,float(target_ledger["modeled_loss_reserved"])-loss)); targets["ledger"]=target_ledger
   journal={"schema":"nexus.prewrite-repair","version":1,"identity":identity,"expectedHashes":{k:canonical_hash(v) for k,v in (("executor",executor),("monitor",monitor),("ledger",ledger))},"targetHashes":{k:canonical_hash(v) for k,v in targets.items()},"targets":targets,"terminal":"failed_prewrite_repaired"}; journal["seal"]=canonical_hash(journal); atomic_json(journal_path,journal); journal.pop("seal"); crash("after_journal")
  for name in ("ledger","monitor","executor"):
   try: current=json.loads(paths[name].read_text())
   except (OSError,json.JSONDecodeError) as e: raise FailClosed("journal state mismatch") from e
   digest=canonical_hash(current)
   if digest==journal["targetHashes"][name]: pass
   elif digest==journal["expectedHashes"][name]: atomic_json(paths[name],journal["targets"][name])
   else: raise FailClosed("journal state mismatch")
   crash("after_"+name)
  if any(canonical_hash(json.loads(paths[k].read_text()))!=journal["targetHashes"][k] for k in paths): raise FailClosed("repair target verification failed")
  identity=journal["identity"]; remove_journal(); return {"status":"repaired","identity":identity}


def _reservation_targets(state_dir,monitor,winner,winner_plan,identity,modeled_loss,executor):
 state_dir=Path(state_dir)
 try: ledger=DailyLedger(state_dir/"ledger.json",lambda:monitor["reservationAt"]).data
 except (OSError,json.JSONDecodeError,KeyError) as e: raise FailClosed("reservation source malformed") from e
 if ledger.get("open_basket") is not None or identity in ledger.get("trades",{}): raise FailClosed("reservation source mismatch")
 target_ledger=json.loads(json.dumps(ledger)); target_ledger["submissions"]+=1
 target_ledger["modeled_loss_reserved"]=str(float(target_ledger["modeled_loss_reserved"])+float(modeled_loss))
 target_ledger["open_basket"]=identity; target_ledger["trades"][identity]={"status":"reserved","modeled_loss":str(modeled_loss)}
 target_monitor={**monitor,"submitted":True,"winner":winner,"winnerPlan":winner_plan,"disarmed":[x["symbol"] for x in monitor.get("plan",{}).get("plans",[]) if x["symbol"]!=winner],"identity":identity}
 return {"ledger":target_ledger,"monitor":target_monitor,"executor":executor}


def _reservation_transaction(state_dir,targets=None,_fault=None):
 state_dir=Path(state_dir); journal_path=state_dir/"reservation_txn.json"; paths={k:state_dir/(k+".json") for k in ("ledger","monitor","executor")}
 def crash(stage):
  if _fault==stage: raise RuntimeError("injected "+stage)
 def sync_dir():
  dfd=os.open(state_dir,os.O_DIRECTORY)
  try: os.fsync(dfd)
  finally: os.close(dfd)
 if journal_path.exists():
  try: sealed=json.loads(journal_path.read_text())
  except (OSError,json.JSONDecodeError) as e: raise FailClosed("invalid reservation journal") from e
  journal={k:v for k,v in sealed.items() if k!="seal"}
  if sealed.get("seal")!=canonical_hash(journal) or journal.get("schema")!="nexus.reservation-transaction" or journal.get("version")!=1 or set(journal.get("targets",{}))!=set(paths): raise FailClosed("invalid reservation journal")
 elif targets is None: return None
 else:
  if set(targets)!=set(paths): raise FailClosed("reservation targets malformed")
  expected={}
  for name,path in paths.items():
   if name=="executor" and not path.exists(): expected[name]=None
   else:
    try: expected[name]=canonical_hash(json.loads(path.read_text()))
    except (OSError,json.JSONDecodeError) as e: raise FailClosed("reservation source malformed") from e
  identity=targets["monitor"].get("identity")
  if not isinstance(identity,str) or targets["ledger"].get("open_basket")!=identity: raise FailClosed("reservation identity mismatch")
  journal={"schema":"nexus.reservation-transaction","version":1,"identity":identity,"expectedHashes":expected,"targetHashes":{k:canonical_hash(v) for k,v in targets.items()},"targets":targets,"committed":False}
  atomic_json(journal_path,{**journal,"seal":canonical_hash(journal)}); crash("after_journal")
 for name,path in paths.items():
  try: current=canonical_hash(json.loads(path.read_text())) if path.exists() else None
  except (OSError,json.JSONDecodeError) as e: raise FailClosed("journal state mismatch") from e
  if current==journal["expectedHashes"][name]: atomic_json(path,journal["targets"][name])
  elif current!=journal["targetHashes"][name]: raise FailClosed("journal state mismatch")
  crash("after_"+name)
 if any(canonical_hash(json.loads(paths[k].read_text()))!=journal["targetHashes"][k] for k in paths): raise FailClosed("reservation target verification failed")
 if not journal.get("committed"):
  journal["committed"]=True; atomic_json(journal_path,{**journal,"seal":canonical_hash(journal)})
 crash("after_commit")
 journal_path.unlink(); sync_dir(); return journal["targets"]


def _reset_repaired_terminal(state_dir,_fault=None):
 state_dir=Path(state_dir); paths={k:state_dir/(k+".json") for k in ("executor","monitor","ledger")}; journal_path=state_dir/"terminal_reset.json"
 def crash(stage):
  if _fault==stage: raise RuntimeError("injected "+stage)
 def sync_dir():
  dfd=os.open(state_dir,os.O_DIRECTORY)
  try: os.fsync(dfd)
  finally: os.close(dfd)
 with process_lock(state_dir/"repair.lock"):
  if journal_path.exists():
   try: sealed=json.loads(journal_path.read_text())
   except (OSError,json.JSONDecodeError) as e: raise FailClosed("invalid reset journal") from e
   journal={k:v for k,v in sealed.items() if k!="seal"}
   if (sealed.get("seal")!=canonical_hash(journal) or journal.get("schema")!="nexus.terminal-reset" or journal.get("version")!=1
    or journal.get("monitorTombstone") is not True): raise FailClosed("invalid reset journal")
  else:
   try: executor=Executor.load_state(paths["executor"]); monitor=json.loads(paths["monitor"].read_text()); ledger=json.loads(paths["ledger"].read_text())
   except (OSError,json.JSONDecodeError,TypeError) as e: raise FailClosed("repaired terminal malformed") from e
   forbidden=("body","orderLinkId","orderId","pendingOrder","closeBody","closeOrderLinkId","winner")
   identity,winner=monitor.get("identity"),monitor.get("winner"); trades=ledger.get("trades",{}); trade=trades.get(identity) if isinstance(trades,dict) else None
   parsed_identity=parse_durable_identity(identity)
   repaired=executor.get("status")=="failed_prewrite_repaired" and monitor.get("state")=="repair_complete" and parsed_identity is not None and parsed_identity[2]==winner
   already_released=executor.get("status")=="fatal" and isinstance(identity,str) and ledger.get("modeled_loss_reserved") in {"0",0}
   legacy=False
   if executor.get("status")=="fatal" and monitor.get("submitted") is False and ledger.get("modeled_loss_reserved")=="0" and isinstance(trades,dict):
    candidate=executor.get("candidate"); legacy_winner=executor.get("winner"); approval=executor.get("approved"); plan=executor.get("planDocument"); source=executor.get("executorSourceSha256"); errors=executor.get("errors")
    try:
     issued=approval["issued_at"]; nonce=approval["nonce"]
     plan_map=Executor.validate_plan_document(plan,issued)[1]
     Executor.validate_approval(approval,issued,plan,source)
     equivalent_ids={f"{prefix}-{int(issued)}-{candidate}" for prefix in ("auto","basket")}
     matching=[key for key,value in trades.items() if (parse_durable_identity(key)==(int(issued),f"a{int(issued)}",candidate)
      or isinstance(key,str) and (key.startswith(tuple(x+"-" for x in equivalent_ids)) or key.startswith(tuple(x for x in equivalent_ids))))]
     identity=matching[0] if len(matching)==1 and matching[0] in equivalent_ids else None
     trade=trades.get(identity)
     final_code=executor.get("finalGate",{}).get("code")
     legacy=(isinstance(issued,(int,float)) and not isinstance(issued,bool) and nonce==f"a{int(issued)}"
      and candidate==legacy_winner and set(plan_map)=={candidate} and source in AUDITED_EXECUTOR_SOURCE_SHA256
      and executor.get("planFingerprint")==approval.get("fingerprint")
      and isinstance(errors,dict) and set(errors)=={candidate} and errors.get(candidate) in LEGACY_PREWRITE_FATAL_ERRORS
      and final_code in LEGACY_PREWRITE_FINAL_GATE_CODES
      and isinstance(trade,dict) and trade.get("status")=="failed_prewrite")
    except (KeyError,TypeError,ValueError,Executor.PermanentError): legacy=False
   repaired_valid=repaired and all(executor.get(k) is None for k in forbidden)
   released_valid=already_released and all(executor.get(k) is None for k in forbidden)
   modern_valid=repaired_valid or released_valid
   legacy_valid=legacy and all(executor.get(k) is None for k in forbidden if k!="winner")
   valid=((modern_valid or legacy_valid) and executor.get("exposure_possible") is False
    and monitor.get("submitted") is False and ledger.get("open_basket") is None
    and isinstance(trade,dict) and trade.get("status")=="failed_prewrite" and not (state_dir/"prewrite_repair.json").exists())
   if not valid: raise FailClosed("repaired terminal mismatch")
   target=Executor.new_state(None); journal={"schema":"nexus.terminal-reset","version":1,"identity":identity,"expectedHashes":{"executor":canonical_hash(executor),"monitor":canonical_hash(monitor),"ledger":canonical_hash(ledger)},"targetHashes":{"executor":canonical_hash(target),"ledger":canonical_hash(ledger)},"executorTarget":target,"monitorTombstone":True}
   atomic_json(journal_path,{**journal,"seal":canonical_hash(journal)}); crash("after_journal")
  try: ledger=json.loads(paths["ledger"].read_text())
  except (OSError,json.JSONDecodeError) as e: raise FailClosed("journal state mismatch") from e
  if canonical_hash(ledger)!=journal["expectedHashes"]["ledger"]: raise FailClosed("journal state mismatch")
  try: executor=Executor.load_state(paths["executor"])
  except (OSError,json.JSONDecodeError,TypeError) as e: raise FailClosed("journal state mismatch") from e
  digest=canonical_hash(executor)
  if digest==journal["expectedHashes"]["executor"]: atomic_json(paths["executor"],journal["executorTarget"])
  elif digest!=journal["targetHashes"]["executor"]: raise FailClosed("journal state mismatch")
  crash("after_executor")
  if paths["monitor"].exists():
   try: monitor=json.loads(paths["monitor"].read_text())
   except (OSError,json.JSONDecodeError) as e: raise FailClosed("journal state mismatch") from e
   if canonical_hash(monitor)!=journal["expectedHashes"]["monitor"]: raise FailClosed("journal state mismatch")
   paths["monitor"].unlink(); sync_dir()
  crash("after_monitor")
  if canonical_hash(Executor.load_state(paths["executor"]))!=journal["targetHashes"]["executor"] or paths["monitor"].exists(): raise FailClosed("reset target verification failed")
  journal_path.unlink(); sync_dir()


def _rollover_inert_monitor(state_dir,loaded,plan,_fault=None):
 state_dir=Path(state_dir); monitor_path=state_dir/"monitor.json"; executor_path=state_dir/"executor.json"; ledger_path=state_dir/"ledger.json"; journal_path=state_dir/"monitor_rollover.json"
 def crash(stage):
  if _fault==stage: raise RuntimeError("injected "+stage)
 def sync_dir():
  dfd=os.open(state_dir,os.O_DIRECTORY)
  try: os.fsync(dfd)
  finally: os.close(dfd)
 def load_candidate():
  if (state_dir/"prewrite_repair.json").exists() or (state_dir/"terminal_reset.json").exists(): raise FailClosed("rollover journal conflict")
  try: monitor=json.loads(monitor_path.read_text()); executor=Executor.load_state(executor_path)
  except (OSError,json.JSONDecodeError,TypeError) as e: raise FailClosed("malformed inert rollover state") from e
  if not isinstance(monitor,dict) or not Executor.is_inert_state(executor): raise FailClosed("inert rollover state mismatch")
  required={"plan","touches","submitted","policy_snapshot","policy_digest","autonomousPolicySnapshot","autonomousPolicyDigest"}
  if not required<=set(monitor) or monitor.get("submitted") is not False or any(monitor.get(k) is not None for k in ("winner","identity","winnerPlan","reservation")):
   raise FailClosed("inert rollover state mismatch")
  if not isinstance(monitor.get("touches"),dict): raise FailClosed("inert rollover state mismatch")
  old=monitor["policy_snapshot"]; old_digest=monitor["policy_digest"]
  if monitor["autonomousPolicySnapshot"]!=old or monitor["autonomousPolicyDigest"]!=old_digest or not isinstance(old,dict) or set(old)!=POLICY_KEYS or old_digest!=canonical_hash(old) or old==loaded.document:
   raise FailClosed("old monitor policy binding invalid")
  ledger_raw=None
  if ledger_path.exists():
   ledger_raw=ledger_path.read_bytes()
   try: ledger=json.loads(ledger_raw)
   except (json.JSONDecodeError,TypeError) as e: raise FailClosed("malformed rollover ledger") from e
   if not isinstance(ledger,dict): raise FailClosed("rollover ledger open")
   trades=ledger.get("trades")
   if ledger.get("open_basket") is not None or not isinstance(trades,dict) or any(not isinstance(trade,dict) or trade.get("status") in {"reserved","ambiguous"} for trade in trades.values()): raise FailClosed("rollover ledger open")
  return monitor,executor,ledger_raw
 if journal_path.exists(): candidate=None
 else: candidate=load_candidate()
 with process_lock(state_dir/"repair.lock"):
  if journal_path.exists():
   try: sealed=json.loads(journal_path.read_text())
   except (OSError,json.JSONDecodeError) as e: raise FailClosed("invalid rollover journal") from e
   journal={k:v for k,v in sealed.items() if k!="seal"}
   if sealed.get("seal")!=canonical_hash(journal) or journal.get("schema")!="nexus.monitor-policy-rollover" or journal.get("version")!=1: raise FailClosed("invalid rollover journal")
  else:
   monitor,executor,ledger_raw=load_candidate()
   if plan is None: raise FailClosed("fresh rollover plan required")
   target_monitor={"plan":plan,"touches":{p["symbol"]:None for p in plan["plans"]},"submitted":False,"policy_snapshot":loaded.document,"policy_digest":loaded.digest,"autonomousPolicySnapshot":loaded.document,"autonomousPolicyDigest":loaded.digest}
   target_executor=Executor.new_state(None,{p["symbol"]:None for p in plan["plans"]})
   journal={"schema":"nexus.monitor-policy-rollover","version":1,"expectedHashes":{"monitor":canonical_hash(monitor),"executor":canonical_hash(executor),"ledger":hashlib.sha256(ledger_raw).hexdigest() if ledger_raw is not None else None},"targetHashes":{"monitor":canonical_hash(target_monitor),"executor":canonical_hash(target_executor)},"monitorTarget":target_monitor,"executorTarget":target_executor}
   atomic_json(journal_path,{**journal,"seal":canonical_hash(journal)}); crash("after_journal")
  if (state_dir/"prewrite_repair.json").exists() or (state_dir/"terminal_reset.json").exists(): raise FailClosed("rollover journal conflict")
  if journal["expectedHashes"]["ledger"] is None:
   if ledger_path.exists(): raise FailClosed("journal state mismatch")
  elif not ledger_path.exists() or hashlib.sha256(ledger_path.read_bytes()).hexdigest()!=journal["expectedHashes"]["ledger"]: raise FailClosed("journal state mismatch")
  for name,path in (("monitor",monitor_path),("executor",executor_path)):
   try: current=json.loads(path.read_text())
   except (OSError,json.JSONDecodeError) as e: raise FailClosed("journal state mismatch") from e
   digest=canonical_hash(current)
   if digest==journal["expectedHashes"][name]: atomic_json(path,journal[name+"Target"])
   elif digest!=journal["targetHashes"][name]: raise FailClosed("journal state mismatch")
   crash("after_"+name)
  if canonical_hash(json.loads(monitor_path.read_text()))!=journal["targetHashes"]["monitor"] or canonical_hash(Executor.load_state(executor_path))!=journal["targetHashes"]["executor"]: raise FailClosed("rollover target verification failed")
  journal_path.unlink(); sync_dir()


def _repair_inert_symbol_consensus(state_dir,monitor,ledger=None):
 state_dir=Path(state_dir); path=state_dir/"executor.json"
 if not isinstance(monitor,dict) or monitor.get("submitted") is not False or not isinstance(monitor.get("plan"),dict): return
 symbols={p["symbol"]:None for p in monitor["plan"].get("plans",[]) if isinstance(p,dict) and isinstance(p.get("symbol"),str)}
 if len(symbols)!=len(monitor["plan"].get("plans",[])): raise DurableTupleError("WATCHING_SYMBOL_CONSENSUS_MISMATCH")
 if not symbols: return
 target=Executor.new_state(None,symbols)
 if not path.exists(): atomic_json(path,target); return
 try: current=json.loads(path.read_text())
 except (OSError,json.JSONDecodeError,TypeError) as e: raise DurableTupleError("MALFORMED_EXECUTOR","CORRUPT") from e
 if current==target or current=={**target,"exposure_possible":False}: return
 base=Executor.new_state(None); maps=("touched","permanent_attempts","transient_attempts")
 inert=(isinstance(current,dict) and set(current) in (set(base),set(base)|{"exposure_possible"})
  and all(current.get(k)==v for k,v in base.items() if k not in maps)
  and all(isinstance(current.get(k),dict) for k in maps)
  and current.get("exposure_possible",False) is False)
 trades=(ledger or {}).get("trades",{}) if isinstance(ledger,dict) else {}
 ledger_clear=(not ledger or ledger.get("open_basket") is None and isinstance(trades,dict)
  and not any(isinstance(x,dict) and x.get("status") in {"reserved","ambiguous","open"} for x in trades.values()))
 if not inert or not ledger_clear: raise DurableTupleError("WATCHING_SYMBOL_CONSENSUS_MISMATCH")
 atomic_json(path,target)

_reservation_fault=None


def run_once(mode,transport,now,policy_path=Path(__file__).with_name("configs")/"autonomous_basket_policy.json",state_dir=Path("runtime/autonomous_basket"),notify=lambda _:None,scan_clock=time.monotonic,scan_sleep=time.sleep,wall_clock=None):
 if mode not in {"dry-run","live"}: raise FailClosed("invalid mode")
 runtime=Executor.Runtime(transport,mode); runtime.wall_clock=wall_clock or (lambda:now); state_dir=Path(state_dir); executor_path=state_dir/"executor.json"
 recovered_reservation=_reservation_transaction(state_dir,None) if mode=="live" and (state_dir/"reservation_txn.json").exists() else None
 if recovered_reservation:
  loaded=require_live_gate(load_policy(policy_path,now),now); monitor=recovered_reservation["monitor"]; winner=monitor["winner"]
  evidence={"symbol":winner,"touchTs":monitor["touches"][winner],"triggerTs":monitor["triggerTs"][winner],"triggerM15Ts":monitor["triggerM15Ts"],"triggerM15Close":monitor["triggerM15Close"]}
  result=delegate_entry(runtime=runtime,plan=monitor["winnerPlan"],policy=loaded.document,now=float(monitor["reservationAt"]),state_path=executor_path,lock_path=state_dir/"executor.lock",notify=notify,winner_evidence=evidence,wall_clock=runtime.wall_clock)
  return {"mode":"live","status":result.get("status"),"candidate":winner}
 if mode=="live" and (state_dir/"terminal_reset.json").exists(): _reset_repaired_terminal(state_dir)
 if mode=="live" and (state_dir/"clusdt_terminal_reset.json").exists(): consume_clusdt_recovered_terminal(state_dir,runtime.wall_clock())
 if mode=="live" and (state_dir/"monitor_rollover.json").exists():
  loaded=require_live_gate(load_policy(policy_path,now),now); _rollover_inert_monitor(state_dir,loaded,None)
 if mode=="live" and (state_dir/"clusdt_orphan_recovery.json").exists():
  return {"mode":"live",**recover_clusdt_orphan(runtime,state_dir,runtime.wall_clock())}
 if mode=="live":
  documents={}
  for name in ("ledger","monitor","executor"):
   path=state_dir/(name+".json")
   try: documents[name]=json.loads(path.read_text()) if path.exists() else None
   except (OSError,json.JSONDecodeError,TypeError) as e: raise DurableTupleError("MALFORMED_"+name.upper(),"CORRUPT") from e
  try: tuple_class=classify_durable_tuple(documents["ledger"],documents["monitor"],documents["executor"])
  except DurableTupleError as error:
   if error.code=="ORPHAN_LEDGER" and documents["ledger"].get("open_basket")==CLUSDT_ORPHAN_IDENTITY:
    return {"mode":"live",**recover_clusdt_orphan(runtime,state_dir,runtime.wall_clock())}
   raise
  if (tuple_class[0]=="INERT" and isinstance(documents["monitor"],dict) and documents["monitor"].get("state")=="recovered"
   and documents["monitor"].get("failedIdentity")==CLUSDT_ORPHAN_IDENTITY):
   consume_clusdt_recovered_terminal(state_dir,runtime.wall_clock()); documents["monitor"]=json.loads((state_dir/"monitor.json").read_text())
 if mode=="live" and executor_path.exists():
  durable=documents["executor"]
  if durable.get("status")=="fatal":
   try: _reset_repaired_terminal(state_dir); durable=Executor.load_state(executor_path)
   except FailClosed: return {"mode":"live",**repair_prewrite_fatal(runtime,state_dir,now,wall_clock=runtime.wall_clock)}
  if durable.get("status")=="failed_prewrite_repaired":
   _reset_repaired_terminal(state_dir); durable=Executor.load_state(executor_path)
  recovery_statuses=Executor.RECOVERY_STATUSES|{"open","exit_reconciling","close_reconciling"}
  has_b6_meta=all(durable.get(k) is not None for k in ("planDocument","executorSourceSha256","planFingerprint","approved"))
  if durable.get("status") in recovery_statuses and (has_b6_meta or durable.get("autonomousPolicyRequired")):
   try: doc,_=Executor.validate_durable_recovery(durable)
   except (Executor.PermanentError,KeyError,TypeError) as e: raise FailClosed(str(e)) from e
   if durable.get("autonomousPolicyRequired"):
    try: Executor._durable_policy(durable)
    except Executor.PermanentError as e: raise FailClosed(str(e)) from e
  if durable.get("status") in {"protection_reconciling","stop_reconciling"}:
   return _reconcile_protection(runtime,state_dir,durable,now)
  if durable.get("status") in {"exit_reconciling","close_reconciling"}:
   return _reconcile_exit(runtime,state_dir,durable,now,notify)
  if durable.get("status")=="closed":
   return {"mode":"live","status":"closed","pnl":durable.get("actualPnl")}
  if durable.get("status") in {"filled","open"}:
   return _monitor_open(runtime,state_dir,durable,now,notify)
  if durable.get("status") in Executor.RECOVERY_STATUSES:
   try: result=_recover_executor(runtime,executor_path,runtime.wall_clock(),notify)
   except Executor.PermanentError as e: raise FailClosed(str(e)) from e
   return {"mode":"live","status":result["status"]}
 loaded=require_live_gate(load_policy(policy_path,now),now) if mode=="live" else None
 if mode=="live":
  monitor_path=state_dir/"monitor.json"
  monitor=json.loads(monitor_path.read_text()) if monitor_path.exists() else None
  ledger_path=state_dir/"ledger.json"; ledger=json.loads(ledger_path.read_text()) if ledger_path.exists() else None
  if monitor and monitor.get("policy_digest")==loaded.digest and monitor.get("policy_snapshot")==loaded.document:
   _repair_inert_symbol_consensus(state_dir,monitor,ledger)
 scan_id="scan-"+str(int(now))
 wall_clock=wall_clock or (lambda:now)
 try: plan=scan_market(ScanRuntime(runtime,clock=scan_clock,sleep=scan_sleep,min_interval=.12 if transport is Executor.api else 0,wall_clock=wall_clock),now,scan_id)
 except (TransientScanReadError,PermanentScanReadError): return _degraded(mode,state_dir,wall_clock(),notify)
 if mode=="dry-run": return {"mode":mode,"plan":plan}
 monitor_path=state_dir/"monitor.json"
 try: monitor=json.loads(monitor_path.read_text())
 except FileNotFoundError: monitor={"plan":plan,"touches":{p["symbol"]:None for p in plan["plans"]},"submitted":False,"policy_snapshot":loaded.document,"policy_digest":loaded.digest,"autonomousPolicySnapshot":loaded.document,"autonomousPolicyDigest":loaded.digest}
 except (OSError,json.JSONDecodeError,TypeError) as e: raise FailClosed("malformed monitor state") from e
 if monitor.get("state")!="recovery_consumed" and (monitor.get("policy_digest")!=loaded.digest or monitor.get("policy_snapshot")!=loaded.document):
  _rollover_inert_monitor(state_dir,loaded,plan); monitor=json.loads(monitor_path.read_text())
 if monitor.get("state")=="recovery_consumed" or wall_clock()>=parse_z(monitor["plan"]["expires_at"]):
  audit={k:monitor[k] for k in ("failedIdentity","recoveredAt","recoveryConsumedAt") if k in monitor}
  monitor={"plan":plan,"touches":{p["symbol"]:None for p in plan["plans"]},"submitted":False,"policy_snapshot":loaded.document,"policy_digest":loaded.digest,"autonomousPolicySnapshot":loaded.document,"autonomousPolicyDigest":loaded.digest,**audit}
 _repair_inert_symbol_consensus(state_dir,monitor,documents.get("ledger"))
 before=monitor_path.read_bytes() if monitor_path.exists() else None; sanitize_trigger_observations(monitor)
 if before!=canonical_bytes(monitor): atomic_json(monitor_path,monitor)
 candles={}; invalid={}; closed_m15={}
 for row in monitor["plan"]["plans"]:
  symbol=row["symbol"]
  try:
   candle_now=wall_clock(); m5,_=Executor._closed(runtime,symbol,5,candle_now); candle_now=wall_clock(); m15,_=Executor._closed(runtime,symbol,15,candle_now)
   candles[symbol]=[{"ts":x["ts"],"low":x["low"],"high":x["high"],"close":x["close"],"closed":True} for x in m5]
   closed_m15[symbol]=m15[-1]
   invalid[symbol]=Executor.is_invalid(Executor.validate_plan_document(monitor["plan"],parse_z(monitor["plan"]["generated_at"]))[1][symbol],m15[-1]["close"])
  except (Executor.PermanentError,KeyError,TypeError,ValueError): invalid[symbol]=True
 candidate=observe_trigger(monitor,candles,invalid,wall_clock())
 if candidate:
  trigger=monitor["triggerTs"][candidate]; evidence=closed_m15.get(candidate)
  if not evidence or not evidence["ts"] <= trigger < evidence["ts"]+900:
   candidate=None
  else: monitor.update(triggerM15Ts=evidence["ts"],triggerM15Close=evidence["close"])
 if not candidate:
  fresh_setup=not monitor.get("setupNotified"); monitor["setupNotified"]=True; atomic_json(monitor_path,monitor)
  if fresh_setup: notify(_setup_notice(monitor["plan"]))
  return {"mode":mode,"status":"watching"}
 if monitor.get("submitted"): return {"mode":mode,"status":"reserved"}
 winner=next(x for x in monitor["plan"]["plans"] if x["symbol"]==candidate)
 winner_plan={**monitor["plan"],"plans":[winner]}
 reservation_now=wall_clock(); ledger=DailyLedger(state_dir/"ledger.json",lambda:reservation_now); ledger.assert_entry_allowed()
 if not ledger.path.exists(): ledger._save()
 nonce=("a"+str(int(reservation_now)))[-13:]
 identity=build_durable_identity(reservation_now,nonce,candidate)
 approval=Executor.make_approval(nonce,reservation_now,ttl=300,plan_document=winner_plan,source_hash=EXECUTOR_SHA256)
 executor=Executor.new_state(approval,Executor.validate_plan_document(winner_plan,reservation_now)[1],winner_plan,approval["fingerprint"])
 executor.update(planDocument=winner_plan,planFingerprint=approval["fingerprint"],executorSourceSha256=EXECUTOR_SHA256,autonomousPolicyRequired=True,autonomousPolicySnapshot=loaded.document,autonomousPolicyDigest=loaded.digest,policy_snapshot=loaded.document,policy_digest=loaded.digest)
 monitor["reservationAt"]=reservation_now
 targets=_reservation_targets(state_dir,monitor,candidate,winner_plan,identity,2,executor); _reservation_transaction(state_dir,targets,_reservation_fault)
 ledger.data=targets["ledger"]; monitor=targets["monitor"]
 evidence={"symbol":candidate,"touchTs":monitor["touches"][candidate],"triggerTs":monitor["triggerTs"][candidate],
           "triggerM15Ts":monitor["triggerM15Ts"],"triggerM15Close":monitor["triggerM15Close"]}
 try: result=delegate_entry(runtime=runtime,plan=winner_plan,policy=loaded.document,now=reservation_now,state_path=state_dir/"executor.json",lock_path=state_dir/"executor.lock",notify=notify,winner_evidence=evidence,wall_clock=wall_clock)
 except Executor.PrewriteRejected as rejection:
  durable=Executor.load_state(executor_path)
  forbidden=("body","orderLinkId","orderId","pendingOrder","closeBody","closeOrderLinkId")
  if durable.get("status")=="fatal" and durable.get("exposure_possible") is False and not any(durable.get(k) for k in forbidden):
   message=str(rejection); final_gate=durable.get("finalGate") if isinstance(durable.get("finalGate"),dict) else {}
   code=final_gate.get("code") or re.sub(r"[^A-Z0-9]+","_",message.upper()).strip("_")
   body=durable.get("body") or {}; entry=body.get("price") or winner.get("trigger"); stop=body.get("stopLoss") or winner.get("stop"); tp=body.get("takeProfit") or winner.get("tp")
   try: modeled_cost=Executor.modeled_round_trip_cost(Executor.validate_plan_document(winner_plan,parse_z(winner_plan["generated_at"]))[1][candidate],float(entry),float(stop))
   except (Executor.PermanentError,KeyError,TypeError,ValueError): modeled_cost=None
   try: rr=str(abs(float(tp)-float(entry))/abs(float(entry)-float(stop)))
   except (TypeError,ValueError,ZeroDivisionError): rr=None
   snapshot=durable.get("snapshots",{}).get(candidate,{}) if isinstance(durable.get("snapshots"),dict) else {}
   record={"schema":"nexus.rejected-candidate","version":1,"identity":identity,"candidate":{"symbol":candidate,"side":winner["side"]},"decision_ts":now,"rejection_ts":wall_clock(),
    "plan_expires_ts":parse_z(winner_plan["expires_at"]),"trigger_m15":{"open_ts":evidence["triggerM15Ts"],"close_ts":evidence["triggerM15Ts"]+900},"winner_evidence":evidence,
    "intended":{"entry":entry,"stop_loss":stop,"take_profit":tp,"qty":body.get("qty"),"rr":rr,"modeled_round_trip_cost":modeled_cost,"modeled_round_trip_cost_status":"exact" if modeled_cost is not None else "unavailable","modeled_loss":ledger.data["trades"][identity]["modeled_loss"]},
    "observations":{"quote":{"bid":snapshot.get("bid"),"ask":snapshot.get("ask"),"spread":snapshot.get("spread")},"zone":winner.get("zone"),"final":durable.get("finalObservations")},
    "rejection":{"code":code,"message":final_gate.get("message") or message,"gate_evidence":{**final_gate,"executor_errors":durable.get("errors"),"fatal_reason":durable.get("fatal_reason")}},
    "bindings":{"executor_source_sha256":durable.get("executorSourceSha256") or EXECUTOR_SHA256,"policy_sha256":loaded.digest},
    "zero_write_proof":{"body":durable.get("body"),"orderLinkId":durable.get("orderLinkId"),"orderId":durable.get("orderId"),"exposure_possible":durable.get("exposure_possible")},"reservation_disposition":"failed_prewrite"}
   append_rejection(state_dir/"rejected_candidates.jsonl",record)
   shadow=build_double_gate_shadow(identity,code,{"symbol":candidate,"side":winner["side"],"stop":winner["stop"],"tp":winner["tp"],"zone":winner["zone"]},snapshot,durable.get("instrumentMetadata") or {},2,now,durable.get("executorSourceSha256") or EXECUTOR_SHA256,loaded.digest,
    available_balance=0,scan_observations=winner_plan.get("provenance",{}).get("scan_snapshots",{}).get(candidate),final_observations=durable.get("finalObservations"))
   if shadow: append_shadow(state_dir/"shadow_double_gate.jsonl",shadow)
   repair_prewrite_fatal(runtime,state_dir,now,wall_clock=wall_clock)
   notify("rejected: "+_notice_value(candidate)+" "+_notice_value(code)+" "+_notice_value(final_gate.get("message") or message))
   return {"mode":mode,"status":"rejected","candidate":candidate}
  ledger.mark_ambiguous(identity); raise
 except Exception:
  ledger.mark_ambiguous(identity); raise
 if result.get("status") in {"submitted","filled","open"}: notify(_entry_notice(Executor.load_state(executor_path),result.get("status")))
 return {"mode":mode,"status":result.get("status"),"candidate":candidate}

def _durable_snapshot(state_dir):
 def load(name):
  try: return json.loads((Path(state_dir)/(name+".json")).read_text())
  except (FileNotFoundError,OSError,json.JSONDecodeError): return None
 return {name:load(name) for name in ("monitor","executor","health")}

def _lifecycle_events(before,after):
 events=[]; old_monitor=before.get("monitor") or {}; monitor=after.get("monitor") or {}
 old_plan=old_monitor.get("plan"); plan=monitor.get("plan")
 if isinstance(plan,dict) and plan!=old_plan and monitor.get("submitted") is False:
  fingerprint=plan.get("fingerprint") or plan.get("digest") or canonical_hash(plan)
  setups=[{"symbol":x.get("symbol"),"side":x.get("side"),"zone":x.get("zone"),"trigger":x.get("trigger"),"sl":x.get("stop"),"tp":x.get("tp"),"expiry":plan.get("expires_at")} for x in plan.get("plans",[])[:5]]
  events.append(("setup:"+str(fingerprint)+":"+str(plan.get("generated_at")),format_setups(setups)))
 old=before.get("executor") or {}; state=after.get("executor") or {}; status=state.get("status")
 gate=state.get("finalGate") if isinstance(state.get("finalGate"),dict) else {}
 if status=="fatal" and gate and (old.get("status"),old.get("finalGate"))!=(status,gate):
  symbol=state.get("winner") or state.get("candidate") or (state.get("body") or {}).get("symbol")
  events.append(("rejection:"+canonical_hash({"symbol":symbol,"gate":gate}),format_rejection(symbol,gate.get("code"),gate.get("message"))))
 body=state.get("body") if isinstance(state.get("body"),dict) else {}; link=body.get("orderLinkId") or state.get("orderLinkId")
 if status in {"submitted","pending"} and body and link and (old.get("status"),old.get("body"))!=(status,body):
  events.append(("submitted:"+str(link)+":"+canonical_hash(body),{"event":"entry submitted","body":body,"orderLinkId":link}))
 if status in {"filled","open"} and body and link and old.get("status") not in {"filled","open"}:
  entry=state.get("avgPrice") or state.get("entryPrice") or body.get("price"); sl=body.get("stopLoss"); tp=body.get("takeProfit")
  try: rr=abs(float(tp)-float(entry))/abs(float(entry)-float(sl))
  except (TypeError,ValueError,ZeroDivisionError): rr=None
  symbol=body.get("symbol") or state.get("winner"); tv="https://www.tradingview.com/chart/?symbol=BYBIT%3A"+str(symbol)+".P"
  events.append(("fill:"+str(link),format_entry(symbol,body.get("side"),entry,state.get("cumExecQty") or state.get("executedQty") or body.get("qty"),sl,tp,rr,link,tv)))
 if status in {"closed","emergency_closed","tp","sl"} and not state.get("closeNotified"):
  reason=state.get("closeReason") or state.get("exitReason") or status; pnl=state.get("actualPnl") if "actualPnl" in state else state.get("realizedPnl"); price=state.get("closePrice") or state.get("exitPrice")
  if any(x is not None for x in (pnl,price)):
   identity=state.get("closeIdentity") or link or state.get("closeOrderLinkId")
   events.append(("close:"+str(identity)+":"+str(status),format_close(reason,pnl,price)+"\nidentity: "+str(identity)))
 health=after.get("health") or {}; old_health=before.get("health") or {}
 if health.get("state")=="degraded":
  message=(health.get("exception") or {}).get("message")
  incident=health.get("incident_id") or canonical_hash({"code":health.get("code"),"message":message})
  events.append(("degraded:"+str(incident),format_degraded(health.get("code"),message)))
 return events

def emit_lifecycle_events(before,after,notifier):
 delivered=[]
 if notifier:
  for event_id,payload in _lifecycle_events(before,after):
   if notifier.emit(event_id,payload): delivered.append(event_id)
 return delivered

def _mark_close_notified(state_dir,delivered):
 if not any(x.startswith("close:") for x in delivered): return
 path=Path(state_dir)/"executor.json"
 try: state=Executor.load_state(path)
 except (OSError,json.JSONDecodeError,TypeError): return
 if state.get("status") in {"closed","emergency_closed","tp","sl"} and not state.get("closeNotified"):
  state["closeNotified"]=True; Executor.save_state(state,path)

def run_cycle(mode,transport,now,policy_path=Path(__file__).with_name("configs")/"autonomous_basket_policy.json",state_dir=Path("runtime/autonomous_basket"),notify=lambda _:None,scan_clock=time.monotonic,scan_sleep=time.sleep,wall_clock=None,run_once_fn=run_once,telegram_notifier=None):
 """Run one daemon boundary; persist exact health for expected operational failures."""
 clock=wall_clock or (lambda:now); state_dir=Path(state_dir); before=_durable_snapshot(state_dir)
 try:
  if mode=="live" and (state_dir/"ledger.json").exists():
   try: raw={name:json.loads((state_dir/(name+".json")).read_text()) if (state_dir/(name+".json")).exists() else {} for name in ("ledger","monitor","executor")}
   except (OSError,json.JSONDecodeError,TypeError): raw=None
   if raw:
    executor=raw["executor"]; monitor=raw["monitor"]
    inert=executor.get("status","watching")=="watching" and not any(executor.get(k) for k in ("candidate","winner","body","orderLinkId","closeBody","closeOrderLinkId"))
    if inert and monitor.get("submitted") is not True: DailyLedger(state_dir/"ledger.json",clock)
  result=run_once_fn(mode,transport,now,policy_path=policy_path,state_dir=state_dir,notify=notify,scan_clock=scan_clock,scan_sleep=scan_sleep,wall_clock=clock)
 except (DurableTupleError,FailClosed,Executor.AmbiguousOrder,TransientScanReadError,Executor.TransientError,urllib.error.URLError,TimeoutError,ConnectionError) as error:
  code=error.code if isinstance(error,DurableTupleError) else type(error).__name__
  health={"schema":"nexus.autonomous-basket-health","version":1,"state":"degraded","code":code,"exception":{"type":type(error).__name__,"message":str(error)},"ts":clock()}
  if isinstance(error,DurableTupleError): health["tuple_class"]=error.tuple_class
  atomic_json(state_dir/"health.json",health)
  notify("degraded: "+_notice_value(code)+" "+_notice_value(str(error)))
  _mark_close_notified(state_dir,emit_lifecycle_events(before,_durable_snapshot(state_dir),telegram_notifier))
  return {"mode":mode,"status":"degraded","code":code}
 atomic_json(state_dir/"health.json",{"schema":"nexus.autonomous-basket-health","version":1,"state":"healthy","status":result.get("status"),"ts":clock()})
 _mark_close_notified(state_dir,emit_lifecycle_events(before,_durable_snapshot(state_dir),telegram_notifier))
 return result


def _fetch_audit_m1(symbol,start,end):
 rows=[]; cursor=int(start//60*60+60)
 while cursor<=end:
  limit=min(1000,int((end-cursor)//60)+1); result=Executor.api("/v5/market/kline",{"category":"linear","symbol":symbol,"interval":"1","start":cursor*1000,"end":int(end*1000),"limit":limit})
  batch=[]
  for row in result.get("list",[]):
   ts=int(row[0])//1000
   if start<ts<=end: batch.append({"ts":ts,"open":row[1],"high":row[2],"low":row[3],"close":row[4]})
  if not batch: break
  rows.extend(batch); next_cursor=max(x["ts"] for x in batch)+60
  if next_cursor<=cursor: break
  cursor=next_cursor
 return sorted({x["ts"]:x for x in rows}.values(),key=lambda x:x["ts"])

def main(argv=None):
 parser=argparse.ArgumentParser(); parser.add_argument("mode",choices=("dry-run","live","repair-prewrite-fatal","audit-rejections","audit-double-gate")); parser.add_argument("--once",action="store_true"); parser.add_argument("--max-cycles",type=int); parser.add_argument("--interval",type=float,default=5); parser.add_argument("--state-dir",default="runtime/autonomous_basket"); parser.add_argument("--horizon-hours",type=float,default=24)
 args=parser.parse_args(argv)
 if args.max_cycles is not None and args.max_cycles<1: parser.error("--max-cycles must be positive")
 if args.mode=="audit-rejections":
  print(json.dumps(audit_rejections(Path(args.state_dir)/"rejected_candidates.jsonl",_fetch_audit_m1,time.time(),args.horizon_hours),sort_keys=True)); return
 if args.mode=="audit-double-gate":
  fetch=lambda record,start,end:_fetch_audit_m1(record["candidate"]["symbol"],start,end)
  print(json.dumps(audit_double_gate_records(load_rejections(Path(args.state_dir)/"shadow_double_gate.jsonl"),fetch,time.time(),args.horizon_hours),sort_keys=True)); return
 if args.mode=="repair-prewrite-fatal":
  print(json.dumps(repair_prewrite_fatal(Executor.Runtime(Executor.api,"live"),Path(args.state_dir),time.time()),sort_keys=True)); return
 state_dir=Path(args.state_dir)
 notifier=DurableNotifier(cli_notifier(args.mode,recovering=args.mode=="live" and state_dir.exists() and any(state_dir.iterdir())),state_dir/"notices.json")
 telegram_notifier=TelegramNotifier(state_dir)
 with process_lock(canonical_lock_path(Path(args.state_dir))):
  degraded=0; cycles=0
  while True:
   result=run_cycle(args.mode,Executor.api,time.time(),state_dir=Path(args.state_dir),notify=notifier.send,wall_clock=time.time,telegram_notifier=telegram_notifier)
   print(json.dumps(result,sort_keys=True),flush=True); cycles+=1
   if args.once or args.max_cycles is not None and cycles>=args.max_cycles: break
   degraded=degraded+1 if result.get("status")=="degraded" else 0
   time.sleep(args.interval if args.max_cycles is not None else next_cycle_delay(degraded,args.interval))

if __name__=="__main__": main()
