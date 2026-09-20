#!/usr/bin/env python3
"""Fail-closed Bybit basket executor. Never import this module to arm it.

Modeled loss includes conservative fee/slippage, but cannot guarantee actual gap loss <=$2.
"""
import hashlib, hmac, json, math, os, re, time, urllib.error, urllib.parse, urllib.request
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

BASE = "https://api.bybit.com"
RISK_CAP, MAX_NOTIONAL, EXPIRY = 2.0, 150.0, 4 * 3600
RISK_WARNING = "Modeled loss <=$2; cannot guarantee actual gap loss <=$2."
SUPPORTS_ATOMIC_TP_SL = True
STATE_PATH = Path("/home/fusion_omega/fusion_omega_nexus/runtime/basket_live_executor.json")
LOCK_PATH = STATE_PATH.with_suffix(".lock")

@dataclass(frozen=True)
class Plan:
    side: str
    zone: tuple
    trigger: float
    stop: float
    tp: float
    invalid: float | None = None

PLANS = {
    "ZECUSDT": Plan("Sell", (509.05, 509.80), 509.05, 510.83, 504.60, 510.45),
    "XAUUSDT": Plan("Buy", (4345.41, 4348.53), 4348.53, 4341.14, 4367, 4342.69),
    "SKHYNIXUSDT": Plan("Sell", (1010.37, 1013.22), 1010.37, 1017.14, 993.45, 1015.71),
    "PENGUUSDT": Plan("Buy", (.006630, .006651), .006651, .006602, .006773, .006612),
    "XAGUSDT": Plan("Buy", (64.09, 64.18), 64.18, 63.98, 64.68, 64.02),
}
TERMINAL = {"submitted", "filled", "cancelled", "expired"}
ACTIVE_ORDERS = {"New", "PartiallyFilled", "Untriggered"}
BOT_LINK_STEMS = ("basket","auto","autoclose")
WRITE_PATHS = {"/v5/order/create", "/v5/order/cancel", "/v5/position/trading-stop"}
AUTONOMOUS_POLICY_KEYS = {"schema","version","starts_at","max_entries_day","max_open_baskets","max_modeled_loss_trade_usd","max_modeled_loss_day_usd","entry","protection","partial_tp","loss_pause","executor_sha256"}

class PermanentError(RuntimeError): pass
class PrewriteRejected(PermanentError): pass
class TransientError(RuntimeError): pass
class AmbiguousOrder(RuntimeError): pass
class LockBusy(RuntimeError): pass


def require_response_fresh(envelope, now, max_age, label):
    timestamp=envelope.get("_response_ts") if isinstance(envelope,dict) else None
    if (not isinstance(timestamp,(int,float)) or isinstance(timestamp,bool)
            or not math.isfinite(timestamp) or not -2 <= now-timestamp <= max_age):
        raise TransientError(label+" response freshness invalid")
    return envelope

PLAN_DOCUMENT_KEYS = {"schema","version","generated_at","expires_at","source","provenance","plans"}
PLAN_KEYS = {"symbol","side","zone","trigger","invalid","stop","tp"}


def _utc_timestamp(value):
    if not isinstance(value,str) or not value.endswith("Z"):
        raise PermanentError("invalid plan timestamp")
    try: return datetime.fromisoformat(value[:-1]+"+00:00").timestamp()
    except ValueError as e: raise PermanentError("invalid plan timestamp") from e


def _plan_decimal(value):
    if not isinstance(value,str): raise PermanentError("invalid plan decimal")
    try: result=Decimal(value)
    except Exception as e: raise PermanentError("invalid plan decimal") from e
    if not result.is_finite() or result <= 0: raise PermanentError("invalid plan decimal")
    return result


def validate_plan_document(document, now):
    """Validate strict JSON-shaped plan snapshot; return detached document and plan map."""
    if not isinstance(document,dict) or set(document) != PLAN_DOCUMENT_KEYS:
        raise PermanentError("invalid plan document keys")
    if document["schema"] != "nexus.basket-plan" or document["version"] != 1:
        raise PermanentError("unsupported plan document")
    if not isinstance(document["source"],str) or not document["source"] or not isinstance(document["provenance"],dict):
        raise PermanentError("invalid plan provenance")
    generated,expires=_utc_timestamp(document["generated_at"]),_utc_timestamp(document["expires_at"])
    if generated > now or expires <= generated: raise PermanentError("invalid plan timing")
    if now >= expires: raise PermanentError("stale plan document")
    rows=document["plans"]
    if not isinstance(rows,list) or not rows: raise PermanentError("invalid plans")
    plans={}
    for row in rows:
        if not isinstance(row,dict) or set(row) != PLAN_KEYS: raise PermanentError("invalid plan keys")
        symbol=row["symbol"]
        if not isinstance(symbol,str) or not re.fullmatch(r"[A-Z0-9]{2,20}USDT",symbol) or symbol in plans:
            raise PermanentError("invalid or duplicate symbol")
        if row["side"] not in {"Buy","Sell"} or not isinstance(row["zone"],list) or len(row["zone"]) != 2:
            raise PermanentError("invalid plan side or zone")
        low,high=map(_plan_decimal,row["zone"]); trigger=_plan_decimal(row["trigger"])
        invalid=_plan_decimal(row["invalid"]); stop=_plan_decimal(row["stop"]); tp=_plan_decimal(row["tp"])
        if not low < high or trigger not in (low,high) or invalid not in (low,high) or trigger == invalid:
            raise PermanentError("invalid plan geometry")
        valid = ((row["side"] == "Buy" and trigger == high and invalid == low and stop < low < high < tp)
          or (row["side"] == "Sell" and trigger == low and invalid == high and tp < high < low < stop))
        valid |= (row["side"] == "Sell" and trigger == low and invalid == high and tp < low < high < stop)
        if not valid: raise PermanentError("invalid plan geometry")
        plans[symbol]=Plan(row["side"],(low,high),trigger,stop,tp,invalid)
    return json.loads(json.dumps(document)),plans


def plan_fingerprint(document, source_hash, approval):
    if not isinstance(source_hash,str) or not re.fullmatch(r"[0-9a-f]{64}",source_hash):
        raise PermanentError("invalid executor source hash")
    timing={k:approval.get(k) for k in ("issued_at","expires_at")}
    raw={"plan":document,"executor_source_sha256":source_hash,"approval_timing":timing}
    return hashlib.sha256(json.dumps(raw,sort_keys=True,separators=(",",":")).encode()).hexdigest()


def executor_source_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def canonical_document_digest(document):
    return hashlib.sha256(json.dumps(document,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode()).hexdigest()


def validate_autonomous_policy(document, digest, executor_hash=None):
    if not isinstance(document,dict) or set(document) != AUTONOMOUS_POLICY_KEYS:
        raise PermanentError("invalid autonomous policy snapshot")
    try:
        safe=(document["schema"]=="nexus.autonomous-basket-policy" and document["version"]==1
              and int(document["max_entries_day"])==2 and int(document["max_open_baskets"])==1
              and Decimal(str(document["max_modeled_loss_trade_usd"]))==2
              and Decimal(str(document["max_modeled_loss_day_usd"]))==4
              and document["entry"]=="PostOnly-no-chase"
              and document["protection"]=="native-single-TP-plus-SL"
              and document["loss_pause"]==2 and document["partial_tp"] is False
              and document["starts_at"]=="2026-08-10T00:00:00Z"
              and document["executor_sha256"]==(executor_hash or executor_source_sha256()))
    except (KeyError,TypeError,ValueError,ArithmeticError) as e:
        raise PermanentError("invalid autonomous policy snapshot") from e
    actual=canonical_document_digest(document)
    if not safe or not isinstance(digest,str) or not hmac.compare_digest(actual,digest):
        raise PermanentError("autonomous policy digest or ceilings invalid")
    return json.loads(json.dumps(document)),actual


def _durable_policy(state):
    if not state.get("autonomousPolicyRequired"): return None
    snapshot=state.get("policy_snapshot",state.get("autonomousPolicySnapshot"))
    digest=state.get("policy_digest",state.get("autonomousPolicyDigest"))
    return validate_autonomous_policy(snapshot,digest,state.get("executorSourceSha256"))


def validate_durable_recovery(state):
    document=state.get("planDocument"); source=state.get("executorSourceSha256")
    fingerprint=state.get("planFingerprint"); approval=state.get("approved")
    if not isinstance(document,dict) or not isinstance(approval,dict): raise PermanentError("missing durable plan metadata")
    if not isinstance(source,str) or not re.fullmatch(r"[0-9a-f]{64}",source): raise PermanentError("invalid executor source hash")
    if approval.get("executorSourceSha256") != source: raise PermanentError("durable approval source mismatch")
    plan_map=validate_plan_document(document,_utc_timestamp(document["generated_at"]))[1]
    if not isinstance(fingerprint,str) or not hmac.compare_digest(fingerprint,plan_fingerprint(document,source,approval)):
        raise PermanentError("durable plan fingerprint mismatch")
    return document,plan_map


def basket_fingerprint(plans=None):
    plan_map = PLANS if plans is None else plans
    raw = {s: [p.side, p.zone, p.trigger, p.stop, p.tp, p.invalid] for s, p in plan_map.items()}
    return hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":"),default=str).encode()).hexdigest()


def _validate_nonce(nonce):
    # Longest composition: basket- + nonce + -SKHYNIXUSDT <= Bybit's 36 chars.
    if not isinstance(nonce,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,13}",nonce):
        raise PermanentError("approval nonce invalid")
    return nonce


def make_approval(nonce, now, ttl=EXPIRY, plan_document=None, source_hash=None):
    _validate_nonce(nonce)
    approval={"nonce":nonce,"issued_at":now,"expires_at":now+ttl}
    if plan_document is None:
        approval["fingerprint"]=basket_fingerprint()
    else:
        document,_=validate_plan_document(plan_document,now)
        source_hash=source_hash or executor_source_sha256()
        approval.update(executorSourceSha256=source_hash,
                        fingerprint=plan_fingerprint(document,source_hash,approval))
    return approval


def validate_approval(approval, now, plan_document=None, source_hash=None):
    expected=basket_fingerprint() if plan_document is None else plan_fingerprint(
        validate_plan_document(plan_document,now)[0],source_hash or executor_source_sha256(),approval)
    if not isinstance(approval, dict) or approval.get("fingerprint") != expected:
        raise PermanentError("approval fingerprint mismatch")
    if plan_document is not None and approval.get("executorSourceSha256") != (source_hash or executor_source_sha256()):
        raise PermanentError("approval executor source mismatch")
    if not isinstance(approval.get("expires_at"), (int, float)) or now >= approval["expires_at"]:
        raise PermanentError("approval expired")
    issued = approval.get("issued_at")
    if not isinstance(issued, (int, float)) or issued > now or now-issued > 300:
        raise PermanentError("approval issued_at invalid")
    if approval["expires_at"] <= issued or approval["expires_at"]-issued > EXPIRY:
        raise PermanentError("approval ttl invalid")
    _validate_nonce(approval.get("nonce"))
    return approval


def validate_mode(mode, approval=None, confirmation=None, now=None, plan_document=None, source_hash=None):
    if mode not in {"dry-run", "live"}: raise PermanentError("invalid execution mode")
    if mode == "live":
        if not SUPPORTS_ATOMIC_TP_SL: raise PermanentError("atomic TP/SL unsupported")
        validate_approval(approval, time.time() if now is None else now, plan_document, source_hash)
        expected = approval["nonce"] + ":" + approval["fingerprint"]
        if not hmac.compare_digest(confirmation or "", expected): raise PermanentError("live confirmation mismatch")


class Runtime:
    def __init__(self, transport=api if "api" in globals() else None, mode="dry-run"):
        self.transport, self.mode = transport, mode
        if mode not in {"dry-run", "live"}: raise PermanentError("invalid execution mode")
    def read(self, path, params=None, private=False):
        return self.transport(path, params or {}, private=private)
    def wall_clock(self):
        clock=getattr(self.transport,"wall_clock",None)
        if callable(clock): return clock()
        current=getattr(self.transport,"now",None)
        return current if isinstance(current,(int,float)) and not isinstance(current,bool) else time.time()
    def write(self, path, body):
        if self.mode != "live": raise PermanentError("writes forbidden in dry-run")
        if path not in WRITE_PATHS: raise PermanentError("write endpoint not allowlisted")
        return self.transport(path, method="POST", body=body, private=True)


def read_response_fresh(runtime, path, params, max_age, label, private=False):
    envelope=runtime.read(path,params,private=private)
    return require_response_fresh(envelope,runtime.wall_clock(),max_age,label)


def _one_fresh(runtime, path, params, max_age, label):
    rows=read_response_fresh(runtime,path,params,max_age,label,True).get("list",[])
    return rows[0] if rows else {}


def parse_instrument(info):
    try:
        p, q = info["priceFilter"], info["lotSizeFilter"]
        result = {"tick": float(p["tickSize"]), "qty_step": float(q["qtyStep"]),
                  "min_qty": float(q["minOrderQty"]), "min_notional": float(q["minNotionalValue"])}
    except (KeyError, TypeError, ValueError) as e: raise PermanentError("invalid instrument metadata") from e
    if any(v <= 0 or not math.isfinite(v) for v in result.values()): raise PermanentError("invalid instrument metadata")
    return result


def detect_position_mode(positions):
    indexes = {int(p["positionIdx"]) for p in positions if "positionIdx" in p}
    if indexes == {0}: return "MergedSingle"
    if indexes and indexes <= {1, 2}: return "BothSide"
    raise PermanentError("cannot detect position mode")


def parse_closed_candles(rows, interval, now, minimum=2):
    try:
        candles = sorted(({"ts":int(r[0]), "open":float(r[1]), "high":float(r[2]), "low":float(r[3]),
                           "close":float(r[4]), "volume":float(r[5])} for r in rows), key=lambda c:c["ts"])
    except (IndexError, TypeError, ValueError) as e: raise PermanentError("invalid candles") from e
    if len(candles) < minimum: raise PermanentError("missing candle history")
    if any(c["low"] <= 0 or c["high"] < c["low"] or c["volume"] < 0 for c in candles):
        raise PermanentError("invalid candles")
    if any(b["ts"] - a["ts"] != interval for a, b in zip(candles, candles[1:])):
        raise PermanentError("candle gap")
    if candles[-1]["ts"] + interval > now: raise PermanentError("open candle")
    return candles


def _seconds(value):
    value = int(value)
    return value // 1000 if value > 10_000_000_000 else value


def _closed(runtime, symbol, minutes, now, minimum=2):
    result = runtime.read("/v5/market/kline", {"category":"linear", "symbol":symbol,
                          "interval":str(minutes), "limit":max(3, minimum + 1)})
    rows = [[str(int(r[0]) // 1000), *r[1:]] for r in result.get("list", [])]
    closed = [r for r in rows if int(r[0]) + minutes * 60 <= now]
    return parse_closed_candles(closed, minutes * 60, now, minimum), _seconds(result["_response_ts"])


def _references(runtime, symbol, minutes, now, minimum=2):
    result = runtime.read("/v5/market/kline", {"category":"linear", "symbol":symbol,
                          "interval":str(minutes), "limit":max(3, minimum + 1)})
    rows = [[str(int(r[0]) // 1000), *r[1:]] for r in result.get("list", []) if int(r[0]) // 1000 < now]
    # Reference timestamps identify completed boundaries, unlike entry candles whose timestamps identify opens.
    candles = parse_closed_candles(rows, minutes * 60, now + minutes * 60, minimum)
    if candles[-1]["ts"] != (now - 1) // (minutes * 60) * (minutes * 60):
        raise PermanentError("missing reference boundary")
    return candles, _seconds(result["_response_ts"])


def fetch_snapshot(runtime, symbol, now):
    m5, r5 = _closed(runtime, symbol, 5, now)
    m15, r15 = _closed(runtime, symbol, 15, now)
    btc, rb = _references(runtime, "BTCUSDT", 60, now)
    oi = runtime.read("/v5/market/open-interest", {"category":"linear", "symbol":symbol,
                      "intervalTime":"2h", "limit":2})
    ticker = runtime.read("/v5/market/tickers", {"category":"linear", "symbol":symbol})
    try:
        points = sorted(((int(x["timestamp"]) // 1000, float(x["openInterest"])) for x in oi["list"]))
        if len(points) < 2 or points[-1][1] <= 0 or points[0][1] <= 0: raise ValueError
        item = ticker["list"][0]; price=float(item["lastPrice"])
        bid, ask = float(item["bid1Price"]), float(item["ask1Price"])
        if price <= 0 or bid <= 0 or ask < bid: raise ValueError
    except (KeyError, IndexError, TypeError, ValueError) as e: raise PermanentError("invalid snapshot") from e
    response_ts = _seconds(ticker["_response_ts"])
    return {"symbol":symbol, "price":price, "bid":bid, "ask":ask,
            "ticker_ts":response_ts, "response_ts":response_ts,
            "m5_close":m5[-1]["close"], "m5_ts":m5[-1]["ts"],
            "m15_close":m15[-1]["close"], "m15_ts":m15[-1]["ts"],
            "oi_change":(points[-1][1]/points[0][1]-1)*100, "oi_ts":points[-1][0],
            "btc_change":(btc[-1]["close"]/btc[-2]["close"]-1)*100, "btc_ts":btc[-1]["ts"],
            "funding":float(item.get("fundingRate") or 0), "spread":(ask-bid)/price*100,
            "source_response_ts":min(r5, r15, rb, _seconds(oi["_response_ts"]), response_ts)}


def _pid_alive(pid):
    try: os.kill(pid, 0); return True
    except ProcessLookupError: return False
    except (PermissionError, ValueError): return True


@contextmanager
def recoverable_lock(path=LOCK_PATH, reconciled=lambda: False):
    path = Path(path)
    if path.exists():
        try: pid = int(path.read_text().strip())
        except (OSError, ValueError): raise LockBusy(str(path))
        if _pid_alive(pid) or not reconciled(): raise LockBusy(str(path))
        path.unlink()
    with global_lock(path): yield


def new_state(approved, plans=None, plan_document=None, fingerprint=None):
    plan_map = PLANS if plans is None else plans
    return {"approved": approved, "status": "watching", "candidate": None, "winner": None,
            "trigger_ts": None, "touched": {s: None for s in plan_map}, "ineligible": [],
            "permanent_attempts": {s: 0 for s in plan_map},
            "transient_attempts": {s: 0 for s in plan_map}, "errors": {},
            **({"planDocument":json.loads(json.dumps(plan_document)),"planFingerprint":fingerprint}
               if plan_document is not None else {})}


def _json_state(value):
    """Return JSON-safe state; preserve Decimal's exact canonical text."""
    if isinstance(value,Decimal): return str(value)
    if isinstance(value,dict): return {key:_json_state(item) for key,item in value.items()}
    if isinstance(value,list): return [_json_state(item) for item in value]
    if isinstance(value,tuple): return [_json_state(item) for item in value]
    return value


def save_state(state, path=STATE_PATH):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w") as f:
        json.dump(_json_state(state), f, indent=2); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)
    dfd = os.open(path.parent, os.O_DIRECTORY)
    try: os.fsync(dfd)
    finally: os.close(dfd)


def load_or_create_state(approved, path=STATE_PATH):
    path = Path(path)
    if path.exists():
        state = json.loads(path.read_text())
        if state.get("approved") != approved: raise PermanentError("approval state mismatch")
        return state
    state = new_state(approved); save_state(state, path); return state


def load_state(path=STATE_PATH): return json.loads(Path(path).read_text())


def is_inert_state(state):
    expected = new_state(None)
    if not isinstance(state, dict) or set(state) not in (set(expected), set(expected) | {"exposure_possible"}):
        return False
    return all(state.get(key) == value for key, value in expected.items()) and state.get("exposure_possible", False) is False


def is_repairable_fatal_state(state, approved):
    """Check if fatal state with 'winner current market invalid' can be repaired.
    
    Accepts fatal states with exact approval binding (nonce/timestamp/plan/fingerprint)
    where error is 'winner current market invalid' and exposure_possible=False.
    Tamper fails closed: different nonce/fingerprint rejects.
    """
    if not isinstance(state, dict) or state.get("status") != "fatal":
        return False
    if state.get("exposure_possible", True) is not False:
        return False
    
    # Check for specific repairable error
    fatal_reason = state.get("fatal_reason", "")
    if fatal_reason != "winner current market invalid":
        return False
    
    # Verify approval binding matches exactly
    stored_approval = state.get("approved")
    if not isinstance(stored_approval, dict):
        return False
    
    # Nonce, issued_at, expires_at must match
    if (stored_approval.get("nonce") != approved.get("nonce") or
        stored_approval.get("issued_at") != approved.get("issued_at") or
        stored_approval.get("expires_at") != approved.get("expires_at")):
        return False
    
    # For dynamic plans: fingerprint and source hash must match
    if "fingerprint" in stored_approval:
        if (stored_approval.get("fingerprint") != approved.get("fingerprint") or
            stored_approval.get("executorSourceSha256") != approved.get("executorSourceSha256")):
            return False
    
    return True


def rebind_inert_state(runtime, approved, path=STATE_PATH):
    try: state = load_state(path)
    except (OSError, json.JSONDecodeError, TypeError) as e:
        raise PermanentError("approval state mismatch") from e
    
    # Accept either inert state OR repairable fatal state
    if not is_inert_state(state) and not is_repairable_fatal_state(state, approved):
        raise PermanentError("approval state mismatch")
    
    _require_entry_clear(*_account_exposure(runtime))
    
    # Preserve plan metadata when repairing dynamic fatal state
    plans_from_fatal = None
    if is_repairable_fatal_state(state, approved):
        plan_document = state.get("planDocument")
        plan_fingerprint = state.get("planFingerprint")
        if plan_document and plan_fingerprint:
            plans_from_fatal = (plan_document, plan_fingerprint)
    
    if plans_from_fatal:
        plan_document, fingerprint = plans_from_fatal
        plan_map = validate_plan_document(plan_document, _utc_timestamp(plan_document["generated_at"]))[1]
        state = new_state(approved, plan_map, plan_document, fingerprint)
    else:
        state = new_state(approved)
    
    save_state(state, path)
    return state


@contextmanager
def global_lock(path=LOCK_PATH):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    try: fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as e: raise LockBusy(str(path)) from e
    try:
        os.write(fd, str(os.getpid()).encode()); os.fsync(fd); yield
    finally:
        os.close(fd); path.unlink(missing_ok=True)


def modeled_loss(plan, qty, entry, stop=None):
    # stop distance + 0.11% round-trip fee + 0.4% conservative slippage
    stop = plan.stop if stop is None else stop
    return qty * abs(entry-stop) + qty * (entry+stop) * .00055 + qty * entry * .004


def safe_qty(plan, entry, cap, step, stop=None):
    return math.floor((cap / modeled_loss(plan, 1, entry, stop)) / step) * step


def data_fresh(data, now, max_age=60):
    limits = {"ticker_ts":max_age, "response_ts":max_age, "m5_ts":360,
              "m15_ts":960, "oi_ts":7200, "btc_ts":3660}
    return all(isinstance(data.get(k), (int, float)) and 0 <= now-data[k] <= age
               for k, age in limits.items())


def maker_entry(symbol, data, plans=None):
    plan = (PLANS if plans is None else plans)[symbol]
    entry = data.get("bid") if plan.side == "Buy" else data.get("ask")
    if not isinstance(entry, (int, float)) or not plan.zone[0] <= entry <= plan.zone[1]:
        raise PermanentError("maker quote outside approved entry zone")
    return entry


def market_ok(data, now):
    return (data_fresh(data, now) and abs(data["funding"]) <= .0005 and data["spread"] <= .08
            and data["oi_change"] >= -3 and data["btc_change"] >= -1.5)


def _final_observations(data,now,scan_snapshot=None):
    keys=("bid","ask","price","spread","funding","oi_change","btc_change","ticker_ts","response_ts",
          "source_response_ts","m5_ts","m15_ts","oi_ts","btc_ts","m5_close","m15_close")
    result={k:data.get(k) for k in keys}
    result["ages"]={k:(now-data[k] if isinstance(data.get(k),(int,float)) and not isinstance(data.get(k),bool) else None)
                    for k in ("ticker_ts","response_ts","source_response_ts","m5_ts","m15_ts","oi_ts","btc_ts")}
    result["scan"]=scan_snapshot
    return result


def final_market_diagnostic(data,now,scan_snapshot=None):
    observations=_final_observations(data,now,scan_snapshot)
    def failed(code,message,gate,observed,operator,threshold):
        return {"ok":False,"code":code,"message":message,"gate":gate,"observed":observed,
                "operator":operator,"threshold":threshold,"observations":observations}
    finite=lambda k:isinstance(data.get(k),(int,float)) and not isinstance(data.get(k),bool) and math.isfinite(data[k])
    if not finite("bid") or not finite("ask") or not 0<data["bid"]<=data["ask"]:
        return failed("FINAL_BID_ASK_INVALID","final bid/ask invalid","bid_ask",{"bid":data.get("bid"),"ask":data.get("ask")},"0 < bid <= ask",None)
    for key,code,age in (("response_ts","FINAL_RESPONSE_STALE",60),("ticker_ts","FINAL_TICKER_STALE",60),("source_response_ts","FINAL_RESPONSE_STALE",60)):
        if not finite(key) or not 0<=now-data[key]<=age: return failed(code,"final "+key+" stale",key,observations["ages"][key],"age <=",age)
    if not finite("spread") or not 0<=data["spread"]<=.08: return failed("FINAL_SPREAD_EXCEEDED","final spread exceeded","spread",data.get("spread"),"<=",.08)
    if not finite("funding") or abs(data["funding"])>.0005: return failed("FINAL_FUNDING_EXCEEDED","final funding exceeded","abs(funding)",abs(data["funding"]) if finite("funding") else data.get("funding"),"<=",.0005)
    if not finite("oi_change") or data["oi_change"] < -3: return failed("FINAL_OI_CHANGE_BELOW_MIN","final OI change below minimum","oi_change",data.get("oi_change"),">=",-3)
    if not finite("btc_change") or data["btc_change"] < -1.5: return failed("FINAL_BTC_CHANGE_BELOW_MIN","final BTC change below minimum","btc_change",data.get("btc_change"),">=",-1.5)
    for key,age in (("m5_ts",360),("m15_ts",960),("oi_ts",7200),("btc_ts",3660)):
        if not finite(key) or not 0<=now-data[key]<=age: return failed("FINAL_CANDLE_STALE_OR_INVALID","final market invalid: candle stale or invalid",key,observations["ages"][key],"age <=",age)
    return {"ok":True,"code":None,"message":"final market valid","gate":None,"observed":None,"operator":None,"threshold":None,"observations":observations}


def final_entry_diagnostic(plan,data,now,scan_snapshot=None,m15_close=None):
    result=final_market_diagnostic(data,now,scan_snapshot)
    close=data.get("m15_close") if m15_close is None else m15_close
    if not result["ok"]: return result
    entry=data["bid"] if plan.side=="Buy" else data["ask"]
    if not plan.zone[0]<=entry<=plan.zone[1] or is_invalid(plan,close):
        return {"ok":False,"code":"FINAL_ZONE_OR_INVALIDATION_FAILED","message":"final zone or invalidation failed",
                "gate":"zone_or_invalidation","observed":{"entry":entry,"m15_close":close},"operator":"entry in zone and not invalidated",
                "threshold":{"zone":list(plan.zone),"invalid":plan.invalid},"observations":result["observations"]}
    return result


def market_data_fresh_for_entry(data, now):
    """Preserve historical boolean API; structured boundary owns diagnostics."""
    return final_market_diagnostic(data,now)["ok"]


def modeled_round_trip_cost_per_unit(plan,entry,stop=None):
    stop=plan.stop if stop is None else stop
    return (entry+stop)*.00055+entry*.004


def modeled_round_trip_cost_usd(plan,qty,entry,stop=None):
    return qty*modeled_round_trip_cost_per_unit(plan,entry,stop)


# Historical API returned per-unit friction despite ambiguous name.
def modeled_round_trip_cost(plan,entry,stop=None):
    return modeled_round_trip_cost_per_unit(plan,entry,stop)


def is_invalid(plan, m15_close):
    if plan.invalid is None: return False
    return m15_close < plan.invalid if plan.side == "Buy" else m15_close > plan.invalid


def can_attempt(state, symbol):
    return symbol not in state["ineligible"] and state["status"] == "watching"


def select_candidate(state, data, now, plans=None):
    if state.get("winner") or state.get("candidate") or state.get("status") != "watching": return None
    valid = []
    for symbol, plan in (PLANS if plans is None else plans).items():
        if not can_attempt(state, symbol) or symbol not in data: continue
        d = data[symbol]
        if not market_ok(d, now) or is_invalid(plan, d["m15_close"]): continue
        if plan.zone[0] <= d["price"] <= plan.zone[1] and state["touched"].get(symbol) is None:
            state["touched"][symbol] = d["m5_ts"]
        crossed = d["m5_close"] > plan.trigger if plan.side == "Buy" else d["m5_close"] < plan.trigger
        touched = state["touched"].get(symbol)
        if (isinstance(touched, (int, float)) and not isinstance(touched, bool)
                and 0 < d["m5_ts"]-touched <= 15*60 and crossed):
            valid.append((d["m5_ts"], symbol))
    if not valid: return None
    ts, symbol = min(valid)
    state.update(candidate=symbol, trigger_ts=ts)
    return symbol


def position_index(mode, side):
    if mode == "MergedSingle": return 0
    if mode == "BothSide": return 1 if side == "Buy" else 2
    raise PermanentError(f"unsupported position mode: {mode}")


def _decimal(value): return format(value, ".15g")

def _tick(value, tick):
    return float((Decimal(str(value))/Decimal(str(tick))).quantize(Decimal("1"), rounding=ROUND_HALF_UP)*Decimal(str(tick)))


def build_payload(symbol, entry, meta, order_link_id, position_idx, bid=None, ask=None, plans=None):
    required = ("tick", "qty_step", "min_qty", "min_notional")
    if any(not isinstance(meta.get(k), (int, float)) or meta[k] <= 0 for k in required):
        raise PermanentError("invalid instrument metadata")
    plan = (PLANS if plans is None else plans)[symbol]; tick = meta["tick"]
    entry = _tick(entry,tick); stop = _tick(plan.stop,tick); tp = _tick(plan.tp,tick)
    if not plan.zone[0] <= entry <= plan.zone[1]: raise PermanentError("rounded entry outside approved zone")
    if bid is not None and ask is not None:
        if not all(isinstance(x,(int,float)) and math.isfinite(x) and x > 0 for x in (bid,ask)) or ask < bid:
            raise PermanentError("invalid fresh bid/ask")
        if (plan.side == "Buy" and entry >= ask) or (plan.side == "Sell" and entry <= bid):
            raise PermanentError("rounded entry not PostOnly")
    if not (stop < entry < tp if plan.side == "Buy" else tp < entry < stop):
        raise PermanentError("invalid rounded order geometry")
    qty = min(safe_qty(plan, entry, RISK_CAP, meta["qty_step"], stop),
              math.floor((MAX_NOTIONAL/entry)/meta["qty_step"])*meta["qty_step"])
    if qty < meta["min_qty"] or qty*entry < meta["min_notional"]: raise PermanentError("minimum order not met")
    if modeled_loss(plan, qty, entry, stop) > RISK_CAP or qty*entry > MAX_NOTIONAL: raise PermanentError("risk guard")
    return {"category":"linear", "symbol":symbol, "side":plan.side, "orderType":"Limit",
            "qty":_decimal(qty), "price":_decimal(entry), "timeInForce":"PostOnly",
            "positionIdx":position_idx, "takeProfit":_decimal(tp), "stopLoss":_decimal(stop),
            "tpTriggerBy":"MarkPrice", "slTriggerBy":"MarkPrice", "orderLinkId":order_link_id}


def check_margin(body, available, leverage=1.0):
    needed = float(body["qty"])*float(body["price"])/leverage
    if not math.isfinite(available) or available < needed: raise PermanentError("estimated margin exceeds available balance")
    return needed


def basket_account_clear(api_call, candidate=None):
    positions=api_call("/v5/position/list",{"category":"linear","settleCoin":"USDT"},private=True).get("list",[])
    orders=api_call("/v5/order/realtime",{"category":"linear","settleCoin":"USDT","openOnly":0},private=True).get("list",[])
    positions=[p for p in positions if float(p.get("size") or 0)]
    orders=[o for o in orders if o.get("orderStatus") in ACTIVE_ORDERS]
    try: _require_entry_clear(positions,orders,candidate)
    except PermanentError: return False
    return True


def preflight_readonly(runtime, mode, approval=None, confirmation=None, now=None):
    now = time.time() if now is None else now
    metadata = {}
    for symbol in PLANS:
        info = runtime.read("/v5/market/instruments-info", {"category":"linear", "symbol":symbol})
        try: metadata[symbol] = parse_instrument(info["list"][0])
        except (KeyError, IndexError) as e: raise PermanentError("missing instrument metadata") from e
    all_positions=runtime.read("/v5/position/list",{"category":"linear","settleCoin":"USDT"},private=True).get("list",[])
    all_orders=runtime.read("/v5/order/realtime",{"category":"linear","settleCoin":"USDT","openOnly":0},private=True).get("list",[])
    positions=[p for p in all_positions if float(p.get("size") or 0)]
    orders=[o for o in all_orders if o.get("orderStatus") in ACTIVE_ORDERS]
    _require_entry_clear(positions,orders)
    position_mode = detect_position_mode(all_positions)
    validate_mode(mode, approval, confirmation, now)
    exclusions=sorted(({p.get("symbol") for p in positions}|{o.get("symbol") for o in orders})-{None})
    return {"metadata":metadata,"position_mode":position_mode,"clear":True,"exclusions":exclusions}


def reconcile_state(runtime, state, path=STATE_PATH):
    if state.get("status") not in {"submitting", "reconciling", "submitted"}: return state
    link, symbol = state.get("orderLinkId"), state.get("candidate") or state.get("winner")
    if not link or symbol not in PLANS: raise AmbiguousOrder("missing durable order identity")
    orders = runtime.read("/v5/order/realtime", {"category":"linear", "symbol":symbol,
                          "orderLinkId":link, "openOnly":0}, private=True).get("list", [])
    exact = [o for o in orders if o.get("orderLinkId") == link and o.get("symbol", symbol) == symbol]
    positions = runtime.read("/v5/position/list", {"category":"linear", "symbol":symbol}, private=True).get("list", [])
    exposed = [p for p in positions if p.get("symbol", symbol) == symbol and float(p.get("size") or 0)]
    if len(exact) != 1:
        state["status"] = "reconciling"; save_state(state, path)
        raise AmbiguousOrder("order/position reconciliation unresolved")
    order = exact[0]
    state.update(status="submitted", winner=symbol, candidate=None, orderId=order.get("orderId"))
    if exposed and order.get("orderStatus") not in {"PartiallyFilled", "Filled"}:
        raise AmbiguousOrder("position does not match order")
    save_state(state, path); return state


def submit_once(state, path, body, create, lookup, save=save_state, plans=None):
    plan_map = PLANS if plans is None else plans
    symbol = body.get("symbol") or state.get("candidate") or "reconcile"
    state.update(status="submitting", candidate=None, winner=symbol,
                 orderLinkId=body["orderLinkId"], body=body,
                 disarmed=[s for s in plan_map if s != symbol])
    save(state, path)  # winner, losers, stable id, and body durable before request
    try: create(body)
    except (TimeoutError, ConnectionError, urllib.error.URLError):
        state["status"] = "reconciling"; save(state, path)
    try:
        found = lookup(body["orderLinkId"])
        rows = found.get("list", []) if isinstance(found, dict) and "list" in found else [found]
        exact = [o for o in rows if isinstance(o, dict) and
                 (o.get("orderLinkId") == body["orderLinkId"] or ("list" not in found and "orderId" in o))]
        if len(exact) != 1: raise AmbiguousOrder("exact order lookup unresolved")
        result = exact[0]
    except Exception as e:
        state.update(status="reconciling", exposure_possible=True); save(state, path)
        if isinstance(e, AmbiguousOrder): raise
        raise AmbiguousOrder("exact order lookup unresolved") from e
    state.update(status="submitted", winner=body.get("symbol") or state["candidate"],
                 candidate=None, orderId=result.get("orderId"))
    state["pendingOrder"] = result
    save(state, path); return result


def _exact_protection(position, symbol, plan, filled, position_idx, tick):
    try:
        if position.get("symbol", symbol) != symbol or int(position.get("positionIdx")) != position_idx:
            return False
        if position.get("side") not in (None, "", plan.side) or abs(float(position.get("size") or 0)-filled) > 1e-12:
            return False
        half = tick / 2
        if abs(float(position.get("stopLoss") or 0)-_tick(plan.stop,tick)) > half:
            return False
        if abs(float(position.get("takeProfit") or 0)-_tick(plan.tp,tick)) > half:
            return False
        return all(position.get(k) in (None, "", "MarkPrice") for k in ("slTriggerBy","tpTriggerBy"))
    except (TypeError, ValueError):
        return False


def _verify_order_identity(order, body):
    fields=("orderLinkId","symbol","side","positionIdx","qty","price","orderType")
    if not isinstance(body,dict) or any(k not in order or k not in body or str(order[k]) != str(body[k]) for k in fields):
        raise PermanentError("order ownership mismatch")


def monitor_order(symbol, plan, order_link_id, order, get_position, write, notify,
                  stale=False, position_idx=0, tick=1e-12, cancel_requested=False,
                  ownership_check=lambda _qty:None, durable_body=None, cancel_lookup=None,
                  protection_lookup=None):
    """Resolve one bot-owned order only; caller supplies exact link lookup/position."""
    status = order.get("orderStatus")
    if order.get("orderLinkId", order_link_id) != order_link_id or order.get("symbol", symbol) != symbol:
        raise PermanentError("order ownership mismatch")
    filled, leaves = float(order.get("cumExecQty") or 0), float(order.get("leavesQty") or 0)
    if status in {"Rejected", "Deactivated"} and not filled:
        notify("rejection"); return "rejected"
    if status in {"Cancelled", "Expired"} and not filled: return status.lower()
    if status in {"Cancelled", "Expired", "Rejected", "Deactivated"} and filled: leaves = 0
    if leaves and cancel_requested: return "cancelling"
    if leaves and (stale or filled):
        expected=(status,filled,leaves)
        fresh=(cancel_lookup or (lambda:order))()
        _verify_order_identity(fresh, durable_body)
        fresh_tuple=(fresh.get("orderStatus"),float(fresh.get("cumExecQty") or 0),
                 float(fresh.get("leavesQty") or 0))
        if fresh_tuple != expected or fresh_tuple[0] not in ACTIVE_ORDERS:
            raise AmbiguousOrder("order changed before cancel")
        ownership_check(filled)
        write("/v5/order/cancel", {"category":"linear", "symbol":symbol,
                                    "orderLinkId":order_link_id})
        return "cancelling"
    if not filled: return "submitted"
    notify("partial" if leaves else "fill")
    causal_order,position = protection_lookup() if protection_lookup else (order,get_position())
    if protection_lookup:
        _verify_order_identity(causal_order,durable_body)
        if (causal_order.get("orderStatus"),float(causal_order.get("cumExecQty") or 0),
                float(causal_order.get("leavesQty") or 0)) != (status,filled,leaves):
            raise AmbiguousOrder("order changed before protection")
    pos_size=float(position.get("size") or 0)
    if pos_size != filled:
        if pos_size == 0 and filled > 0:
            return "exit_reconciling"
        raise PermanentError("filled quantity/position mismatch")
    protected = _exact_protection(position,symbol,plan,filled,position_idx,tick)
    if not protected:
        ownership_check(filled)
        notify("protection")
        try:
            write("/v5/position/trading-stop", {"category":"linear", "symbol":symbol,
                  "positionIdx":position_idx, "stopLoss":_decimal(_tick(plan.stop,tick)),
                  "takeProfit":_decimal(_tick(plan.tp,tick)), "slTriggerBy":"MarkPrice", "tpTriggerBy":"MarkPrice"})
        except Exception:
            position = get_position()
            protected = _exact_protection(position,symbol,plan,filled,position_idx,tick)
            if not protected: return "emergency"
        else:
            position = get_position()
            protected = _exact_protection(position,symbol,plan,filled,position_idx,tick)
    return "filled" if protected else "emergency"
    return "filled" if protected else "emergency"


def poll_order(state, path, lookup, get_position, write, notify, now=time.time, sleep=time.sleep,
               timeout=30, interval=1, position_idx=0, metadata=None, ownership_check=lambda _qty:None,
               read_retries=2, plans=None, cancel_lookup=None, protection_lookup=None):
    plan_map = PLANS if plans is None else plans
    started = now(); cancelling = False; polls = 0; read_failures = 0
    tick = (metadata or {}).get("tick", 1e-12)
    while True:
        polls += 1
        if polls > max(3, int(timeout / max(interval, 1e-9)) + 3):
            raise PermanentError("poll clock made no progress")
        try:
            order = lookup(state["orderLinkId"])
            if not isinstance(order,dict) or not isinstance(order.get("orderStatus"),str):
                raise TransientError("malformed order read")
            read_failures = 0
        except (TransientError,TimeoutError,ConnectionError,urllib.error.URLError,TypeError,ValueError,KeyError) as e:
            state["status"]="reconciling"; save_state(state,path); read_failures += 1
            if read_failures > read_retries: raise AmbiguousOrder("poll read unresolved") from e
            sleep(interval); continue
        try:
            result = monitor_order(state["winner"], plan_map[state["winner"]], state["orderLinkId"],
                                   order, get_position, write, notify,
                                   stale=now()-started >= timeout, position_idx=position_idx,tick=tick,
                                   cancel_requested=cancelling,ownership_check=ownership_check,
                                   durable_body=state.get("body"),
                                   cancel_lookup=cancel_lookup or (lambda:lookup(state["orderLinkId"])),
                                   protection_lookup=protection_lookup)
        except TransientError as e:
            state["status"]="reconciling"; save_state(state,path)
            raise AmbiguousOrder(str(e)) from e
        if result == "cancelling": cancelling = True; state["status"]="partial" if float(order.get("cumExecQty") or 0) else "submitted"; save_state(state,path)
        elif result == "emergency":
            filled=float(order.get("cumExecQty") or 0); close_link=state["orderLinkId"]+"-close"
            close_body={"category":"linear","symbol":state["winner"],"side":"Sell" if plan_map[state["winner"]].side=="Buy" else "Buy",
                "orderType":"Market","qty":_decimal(filled),"reduceOnly":True,"positionIdx":position_idx,"orderLinkId":close_link}
            state.update(status="emergency_closing",closeOrderLinkId=close_link,closeBody=close_body); save_state(state,path); notify("emergency")
            try: ownership_check(filled)
            except TransientError as e:
                state["status"]="reconciling"; save_state(state,path)
                raise AmbiguousOrder(str(e)) from e
            try: write("/v5/order/create",close_body)
            except (TimeoutError,ConnectionError,urllib.error.URLError) as e:
                state["status"]="reconciling"; save_state(state,path)
                raise AmbiguousOrder("emergency close outcome ambiguous") from e
            emergency_polls = 0
            while now()-started <= timeout and emergency_polls < max(3, int(timeout/max(interval,1e-9))+3):
                emergency_polls += 1
                close=lookup(close_link)
                if close.get("orderLinkId",close_link)==close_link and close.get("orderStatus")=="Filled":
                    position=get_position()
                    if (position.get("symbol",state["winner"])==state["winner"] and int(position.get("positionIdx",position_idx))==position_idx
                            and float(position.get("size") or 0)==0):
                        state["status"]="emergency_closed"; save_state(state,path); return "emergency_closed"
                sleep(interval)
            state["status"]="reconciling"; save_state(state,path)
            raise AmbiguousOrder("emergency close unconfirmed")
        elif result != "submitted":
            state["status"] = result
            if result == "filled": state["filledQty"] = _decimal(float(order.get("cumExecQty") or 0))
            save_state(state, path); return result
        elif not cancelling and now()-started >= timeout: notify("order_timeout"); cancelling=True
        sleep(interval)


def run_cycle(runtime, state, now, state_path=STATE_PATH, symbols=None, approval=None,
              confirmation=None, metadata=None, position_mode=None, write=None, lookup=None,
              notify=lambda _:None, lock_path=LOCK_PATH, snapshot_fetch=fetch_snapshot):
    symbols = list(symbols or PLANS)
    snapshots = {symbol:snapshot_fetch(runtime, symbol, now) for symbol in symbols}
    if runtime.mode == "dry-run":
        shadow = json.loads(json.dumps(state))
        candidate = select_candidate(shadow, snapshots, now)
        return {"mode":"dry-run", "candidate":candidate, "snapshots":snapshots,
                "status":state["status"]}
    candidate = select_candidate(state, snapshots, now)
    validate_mode("live", approval, confirmation, now)
    if candidate is None or sum(1 for s in snapshots if s == candidate) != 1:
        raise PermanentError("exactly one candidate required")
    if not data_fresh(snapshots[candidate], now): raise PermanentError("stale market data")
    if metadata is None or position_mode is None: raise PermanentError("preflight required")
    writer = write or runtime.write
    finder = lookup or (lambda link: runtime.read("/v5/order/realtime", {
        "category":"linear", "symbol":candidate, "orderLinkId":link, "openOnly":0}, private=True)["list"][0])
    link = "basket-" + approval["nonce"].removeprefix("a") + "-" + candidate
    body = build_payload(candidate, maker_entry(candidate, snapshots[candidate]), metadata[candidate], link,
                         position_index(position_mode, PLANS[candidate].side),
                         snapshots[candidate]["bid"], snapshots[candidate]["ask"])
    with global_lock(lock_path):
        result = submit_once(state, state_path, body,
                             lambda payload:writer("/v5/order/create", payload), finder)
    notify("submitted:" + candidate)
    return {"mode":"live", "status":state["status"], "candidate":candidate,
            "orderId":result.get("orderId")}


RECOVERY_STATUSES = {"submitting", "reconciling", "submitted", "partial", "filled", "protection_reconciling", "stop_reconciling", "emergency", "emergency_closing"}


def _one(runtime, path, params):
    rows=runtime.read(path,params,private=True).get("list",[])
    return rows[0] if rows else {}


def _exact_position(rows, symbol, position_idx, side):
    exact=[p for p in rows if p.get("symbol",symbol)==symbol
           and int(p.get("positionIdx",-1))==int(position_idx)
           and p.get("side") in (side,"",None)]
    if len(exact) != 1: raise AmbiguousOrder("exact position unresolved")
    return exact[0]


def _get_exact_position(runtime, symbol, position_idx, side):
    rows=runtime.read("/v5/position/list",{"category":"linear","symbol":symbol},private=True).get("list",[])
    return _exact_position(rows,symbol,position_idx,side)


def _get_exact_position_fresh(runtime, symbol, position_idx, side):
    rows=read_response_fresh(runtime,"/v5/position/list",{"category":"linear","symbol":symbol},30,
                             "exact position",True).get("list",[])
    return _exact_position(rows,symbol,position_idx,side)


def _fatal(state, path, error, notify, plans=None):
    symbol=state.get("winner") or state.get("candidate") or "unknown"
    state.update(status="fatal",fatal_reason=str(error)); state.setdefault("errors",{})[symbol]=str(error)
    if symbol in (PLANS if plans is None else plans) and symbol not in state.setdefault("ineligible",[]): state["ineligible"].append(symbol)
    save_state(state,path)
    try: notify("fatal:"+str(error))
    except Exception: pass


def _verify_owned(runtime, symbol, body, qty, plans=None):
    positions=read_response_fresh(runtime,"/v5/position/list",{"category":"linear","settleCoin":"USDT"},30,
                                  "recovery ownership positions",True).get("list",[])
    orders=read_response_fresh(runtime,"/v5/order/realtime",{"category":"linear","settleCoin":"USDT","openOnly":0},30,
                               "recovery ownership orders",True).get("list",[])
    durable_link=body.get("orderLinkId")
    order_fields=("orderLinkId","symbol","side","positionIdx","qty","price","orderType")
    exact_orders=[o for o in orders if all(str(o.get(k,""))==str(body.get(k,"")) for k in order_fields)
                  and abs(float(o.get("cumExecQty") or 0)-qty)<=1e-12]
    strict=bool(qty and durable_link and orders and all(k in body for k in order_fields))
    if strict and len(exact_orders)!=1: raise PermanentError("recovery ownership mismatch")
    active=[p for p in positions if float(p.get("size") or 0)]
    candidate=[p for p in active if p.get("symbol")==symbol]
    matching=[p for p in candidate if int(p.get("positionIdx",-1))==int(body["positionIdx"])
              and p.get("side")==body["side"] and (not strict or p.get("orderLinkId")==durable_link)
              and abs(float(p.get("size") or 0)-qty)<=1e-12]
    other_bot=[p for p in active if p.get("symbol")!=symbol and _bot_link_class(p.get("orderLinkId"),durable_link)!="external"]
    if len(candidate)>1 or (candidate and len(matching)!=1) or other_bot:
        raise PermanentError("recovery ownership mismatch")
    active_orders=[o for o in orders if o.get("orderStatus") in ACTIVE_ORDERS]
    own=[o for o in active_orders if o.get("orderLinkId")==durable_link and o.get("symbol")==symbol]
    for order in active_orders:
        link=order.get("orderLinkId")
        if order.get("symbol")==symbol and order not in own or _bot_link_class(link,durable_link)!="external" and order not in own:
            raise PermanentError("recovery ownership mismatch")
    if len(own)>1: raise PermanentError("recovery ownership mismatch")
    expected = 0 if qty == 0 else 1
    if len(matching) != expected: raise PermanentError("recovery ownership mismatch")


def _account_exposure(runtime):
    positions=read_response_fresh(runtime,"/v5/position/list",{"category":"linear","settleCoin":"USDT"},30,
                                  "account exposure positions",True).get("list",[])
    orders=read_response_fresh(runtime,"/v5/order/realtime",{"category":"linear","settleCoin":"USDT","openOnly":0},30,
                               "account exposure orders",True).get("list",[])
    return ([p for p in positions if float(p.get("size") or 0)],
            [o for o in orders if o.get("orderStatus") in ACTIVE_ORDERS])


def _bot_link_class(link, durable_link=None):
    """Ownership hint only; write authorization still requires full durable identity."""
    if link in (None, ""): return "external"
    if not isinstance(link,str): return "malformed"
    if durable_link and hmac.compare_digest(link,durable_link): return "durable"
    return "bot" if link.startswith(BOT_LINK_STEMS) else "external"


def _require_entry_clear(positions, orders, candidate=None):
    if candidate is not None and (any(p.get("symbol")==candidate for p in positions)
                                  or any(o.get("symbol")==candidate for o in orders)):
        raise PrewriteRejected("candidate exposure not clear before create")
    for position in positions:
        link=position.get("orderLinkId")
        if _bot_link_class(link)!="external":
            raise PermanentError("bot or ambiguous exposure before create")
    for order in orders:
        link=order.get("orderLinkId")
        if _bot_link_class(link)!="external":
            raise PermanentError("bot or ambiguous exposure before create")


def _account_exposed_symbols(runtime):
    positions,orders=_account_exposure(runtime)
    return {p.get("symbol") for p in positions} | {o.get("symbol") for o in orders}


def _create_if_account_clear(runtime, body):
    positions=read_response_fresh(runtime,"/v5/position/list",{"category":"linear","settleCoin":"USDT"},30,"final positions",True).get("list",[])
    orders=read_response_fresh(runtime,"/v5/order/realtime",{"category":"linear","settleCoin":"USDT","openOnly":0},30,"final orders",True).get("list",[])
    positions=[p for p in positions if float(p.get("size") or 0)]
    orders=[o for o in orders if o.get("orderStatus") in ACTIVE_ORDERS]
    _require_entry_clear(positions,orders,body["symbol"])
    return runtime.write("/v5/order/create",body)


def run_live_session(runtime, approval, confirmation, now, state_path=STATE_PATH, lock_path=LOCK_PATH,
                     symbols=None, snapshot_fetch=fetch_snapshot, notify=lambda _:None,
                     now_fn=None, sleep=time.sleep, timeout=30, plan_document=None,
                     source_hash=None, initial_touches=None, policy_snapshot=None, policy_digest=None,
                     winner_evidence=None):
    """Own one lock across recovery or new-entry lifecycle; dynamic plan snapshot is mandatory for entry."""
    now_fn=now_fn or (lambda:now)
    exists=Path(state_path).exists()
    if not exists and plan_document is None: raise PermanentError("plan document required for fresh live entry")
    state=load_state(state_path) if exists else None
    recovering=bool(state and (state.get("status") in RECOVERY_STATUSES or
                    (state.get("status") == "fatal" and state.get("exposure_possible"))))
    if recovering:
        durable,plan_map=validate_durable_recovery(state)
        _durable_policy(state)
    else:
        document,plan_map=validate_plan_document(plan_document,now_fn())
        source_hash=source_hash or executor_source_sha256()
        if source_hash != executor_source_sha256(): raise PermanentError("fresh entry requires current executor source")
        validate_mode("live",approval,confirmation,now_fn(),document,source_hash)
        if approval.get("executorSourceSha256") != source_hash: raise PermanentError("approval executor source mismatch")
        if state is None:
            state=new_state(approval,plan_map,document,approval["fingerprint"])
            if initial_touches is not None:
                if not isinstance(initial_touches,dict) or set(initial_touches)!=set(plan_map): raise PermanentError("invalid initial touches")
                state["touched"]={s:initial_touches[s] for s in plan_map}
        state.update(planDocument=document,planFingerprint=approval["fingerprint"],executorSourceSha256=source_hash)
        if policy_snapshot is not None or policy_digest is not None:
            policy_snapshot,policy_digest=validate_autonomous_policy(policy_snapshot,policy_digest)
            state.update(autonomousPolicyRequired=True,autonomousPolicySnapshot=policy_snapshot,
                         autonomousPolicyDigest=policy_digest,policy_snapshot=policy_snapshot,
                         policy_digest=policy_digest)
    plans=plan_map
    evidence_keys={"symbol","touchTs","triggerTs","triggerM15Ts","triggerM15Close"}
    if winner_evidence is not None:
        if not isinstance(winner_evidence,dict) or set(winner_evidence)!=evidence_keys:
            raise PermanentError("invalid winner evidence")
        candidate=winner_evidence["symbol"]
        numeric=lambda value:isinstance(value,(int,float)) and not isinstance(value,bool)
        if not isinstance(candidate,str) or not all(numeric(winner_evidence[k]) for k in evidence_keys-{"symbol"}):
            raise PermanentError("invalid winner evidence")
        if len(plans)!=1 or candidate not in plans: raise PermanentError("winner evidence symbol mismatch")
    if state.get("status") == "fatal" and state.get("exposure_possible"):
        state["status"] = "reconciling"; save_state(state,state_path)
    def reconciled(): return not Path(state_path).exists() or load_state(state_path).get("status") not in RECOVERY_STATUSES
    with recoverable_lock(lock_path,reconciled):
        try:
            if state.get("status") in RECOVERY_STATUSES:
                if state["status"] in {"submitting","reconciling"} and not state.get("closeBody"):
                    reconcile_state(runtime,state,state_path)
                symbol=state.get("winner") or state.get("body",{}).get("symbol")
                if symbol not in plans or not state.get("orderLinkId") or not state.get("body"): raise PermanentError("missing durable recovery identity")
                lookup=lambda link:_one(runtime,"/v5/order/realtime",{"category":"linear","symbol":symbol,"orderLinkId":link,"openOnly":0})
                if state["status"] in {"emergency","emergency_closing"} or (state["status"] == "reconciling" and state.get("closeBody")):
                    close_link,close_body=state.get("closeOrderLinkId"),state.get("closeBody")
                    if not close_link or not close_body: raise PermanentError("missing durable close identity")
                    rows=read_response_fresh(runtime,"/v5/order/realtime",{"category":"linear","symbol":symbol,
                        "orderLinkId":close_link,"openOnly":0},30,"late close order",True).get("list",[])
                    identity=("category","orderLinkId","symbol","side","positionIdx","qty","orderType","reduceOnly")
                    exact=[o for o in rows if all(str(o.get(k,"")) == str(close_body.get(k,"")) for k in identity)]
                    if len(exact) != 1 or exact[0].get("orderStatus") != "Filled":
                        raise AmbiguousOrder("close reconciliation unresolved")
                    active_positions,active_orders=_account_exposure(runtime)
                    try: _require_entry_clear(active_positions,active_orders,symbol)
                    except PrewriteRejected as e: raise AmbiguousOrder("candidate exposure remains after emergency close") from e
                    state["status"]="emergency_closed"; save_state(state,state_path)
                    return {"mode":"live","status":"emergency_closed","candidate":symbol}
                order=_one_fresh(runtime,"/v5/order/realtime",{"category":"linear","symbol":symbol,
                    "orderLinkId":state["orderLinkId"],"openOnly":0},30,"recovery exact entry order"); body=state["body"]
                identity=("orderLinkId","symbol","side","positionIdx","qty","price","orderType")
                if any(str(order.get(k,"")) != str(body.get(k,"")) for k in identity):
                    raise AmbiguousOrder("recovery ownership unresolved")
                owned=float(order.get("cumExecQty") or 0)
                for basket_symbol in plans:
                    rows=read_response_fresh(runtime,"/v5/position/list",{"category":"linear","symbol":basket_symbol},30,
                                             "recovery basket position",True).get("list",[])
                    for position in rows:
                        size=float(position.get("size") or 0)
                        if not size: continue
                        if (basket_symbol != symbol or position.get("symbol",basket_symbol) != symbol
                                or int(position.get("positionIdx",-1)) != int(body["positionIdx"])
                                or position.get("side") != body["side"] or abs(size-owned)>1e-12):
                            raise PermanentError("recovery ownership mismatch")
                getpos=lambda:_get_exact_position(runtime,symbol,int(body["positionIdx"]),body["side"])
                final_order=lambda:_one_fresh(runtime,"/v5/order/realtime",{"category":"linear","symbol":symbol,
                    "orderLinkId":state["orderLinkId"],"openOnly":0},30,"final order authorization")
                recovered_metadata=state.get("instrumentMetadata")
                if not isinstance(recovered_metadata,dict) or not recovered_metadata.get("tick"): raise PermanentError("missing durable instrument metadata")
                status=poll_order(state,state_path,lookup,getpos,runtime.write,notify,now=now_fn,sleep=sleep,timeout=timeout,
                    position_idx=int(state["body"].get("positionIdx",0)),metadata=recovered_metadata,
                    ownership_check=lambda qty: _verify_owned(runtime,symbol,body,qty,plans),plans=plans,
                    cancel_lookup=final_order,
                    protection_lookup=lambda:(final_order(),_get_exact_position_fresh(runtime,symbol,int(body["positionIdx"]),body["side"])))
                return {"mode":"live","status":status,"candidate":symbol}
            if source_hash != executor_source_sha256(): raise PermanentError("fresh entry requires current executor source")
            validate_mode("live",approval,confirmation,now_fn(),document,source_hash)
            if state.get("approved") != approval:
                state = rebind_inert_state(runtime, approval, state_path)
            all_positions=read_response_fresh(runtime,"/v5/position/list",{"category":"linear","settleCoin":"USDT"},30,"initial positions",True).get("list",[])
            all_orders=read_response_fresh(runtime,"/v5/order/realtime",{"category":"linear","settleCoin":"USDT","openOnly":0},30,"initial orders",True).get("list",[])
            positions=[p for p in all_positions if float(p.get("size") or 0)]
            orders=[o for o in all_orders if o.get("orderStatus") in ACTIVE_ORDERS]
            candidate_hint=winner_evidence.get("symbol") if isinstance(winner_evidence,dict) else None
            if candidate_hint is not None: state["candidate"]=candidate_hint
            _require_entry_clear(positions,orders,candidate_hint)
            exposed={p.get("symbol") for p in positions} | {o.get("symbol") for o in orders}
            chosen=[candidate_hint] if candidate_hint is not None else [s for s in (symbols or plans) if s not in exposed]
            if not all_positions and chosen:
                all_positions=read_response_fresh(runtime,"/v5/position/list",{"category":"linear","symbol":chosen[0]},30,"position mode",True).get("list",[])
            mode=detect_position_mode(all_positions)
            metadata={}
            for symbol in chosen:
                info=read_response_fresh(runtime,"/v5/market/instruments-info",{"category":"linear","symbol":symbol},60,"instrument metadata")
                metadata[symbol]=parse_instrument(info["list"][0])
            snapshots={}
            for s in chosen:
                snapshots[s]=snapshot_fetch(runtime,s,now_fn())
            decision_now=now_fn()
            if winner_evidence is None:
                candidate=select_candidate(state,snapshots,decision_now,plans)
            else:
                candidate=winner_evidence["symbol"]; touch=winner_evidence["touchTs"]; trigger=winner_evidence["triggerTs"]
                m15_ts=winner_evidence["triggerM15Ts"]; m15_close=winner_evidence["triggerM15Close"]
                if chosen!=[candidate]: raise PermanentError("winner evidence symbol mismatch")
                state["candidate"]=candidate
                if (not touch < trigger <= decision_now or trigger-touch > 15*60 or decision_now-trigger > 15*60):
                    raise PrewriteRejected("stale or invalid winner chronology")
                if not m15_ts <= trigger < m15_ts+900 or m15_ts+900 > decision_now:
                    raise PrewriteRejected("invalid winner trigger M15 evidence")
                current=snapshots[candidate]
                scan_snapshot=document.get("provenance",{}).get("scan_snapshots",{}).get(candidate)
                diagnostic=final_entry_diagnostic(plans[candidate],current,decision_now,scan_snapshot,m15_close)
                state.update(finalObservations=diagnostic["observations"],finalGate={k:diagnostic[k] for k in ("ok","code","message","gate","observed","operator","threshold")},
                             snapshots={candidate:_final_observations(current,decision_now,scan_snapshot)})
                if not diagnostic["ok"]:
                    save_state(state,state_path); raise PrewriteRejected(diagnostic["message"])
                state.update(candidate=candidate,trigger_ts=trigger,touched={candidate:touch},winnerEvidence=dict(winner_evidence))
            if candidate is None: raise PermanentError("exactly one candidate required")
            link="basket-"+approval["nonce"].removeprefix("a")+"-"+candidate
            body=build_payload(candidate,maker_entry(candidate,snapshots[candidate],plans),metadata[candidate],link,
                position_index(mode,plans[candidate].side),snapshots[candidate]["bid"],snapshots[candidate]["ask"],plans)
            state["instrumentMetadata"]=metadata[candidate]
            wallet=read_response_fresh(runtime,"/v5/account/wallet-balance",{"accountType":"UNIFIED"},30,"wallet balance",True)
            check_margin(body,float(wallet["list"][0]["totalAvailableBalance"]))
            result=submit_once(state,state_path,body,lambda b:_create_if_account_clear(runtime,b),
                               lambda l:_one_fresh(runtime,"/v5/order/realtime",{"category":"linear","symbol":candidate,"orderLinkId":l,"openOnly":0},30,"exact order lookup"),plans=plans)
            notify("submitted:"+candidate)
            def lookup(l):
                pending=state.pop("pendingOrder",None)
                return pending or _one(runtime,"/v5/order/realtime",{"category":"linear","symbol":candidate,"orderLinkId":l,"openOnly":0})
            getpos=lambda:_get_exact_position(runtime,candidate,int(body["positionIdx"]),body["side"])
            final_order=lambda:_one_fresh(runtime,"/v5/order/realtime",{"category":"linear","symbol":candidate,
                "orderLinkId":state["orderLinkId"],"openOnly":0},30,"final order authorization")
            status=poll_order(state,state_path,lookup,getpos,runtime.write,notify,now=now_fn,sleep=sleep,timeout=timeout,
                position_idx=body["positionIdx"],metadata=metadata[candidate],
                ownership_check=lambda qty: _verify_owned(runtime,candidate,body,qty,plans),plans=plans,
                cancel_lookup=final_order,
                protection_lookup=lambda:(final_order(),_get_exact_position_fresh(runtime,candidate,int(body["positionIdx"]),body["side"])))
            return {"mode":"live","status":status,"candidate":candidate,"orderId":result.get("orderId")}
        except (AmbiguousOrder,TransientError,TimeoutError,ConnectionError,urllib.error.URLError,
                TypeError,ValueError,KeyError) as e:
            state.update(status="reconciling", exposure_possible=True)
            state.setdefault("errors",{})[state.get("winner") or "unknown"]=str(e)
            save_state(state,state_path)
            try: notify("reconciling:"+str(e))
            except Exception: pass
            if isinstance(e,AmbiguousOrder): raise
            raise AmbiguousOrder("recovery read unresolved") from e
        except Exception as e:
            if not state.get("orderLinkId"):
                state["exposure_possible"] = False
                if isinstance(e,PrewriteRejected) and state.get("candidate"):
                    state["winner"]=state["candidate"]
                _fatal(state,state_path,e,notify,plans); raise
            if isinstance(e,PrewriteRejected):
                state.update(exposure_possible=False,winner=state.get("winner") or state.get("candidate"))
                _fatal(state,state_path,e,notify,plans); raise
            try:
                active_positions,active_orders=_account_exposure(runtime)
                _require_entry_clear(active_positions,active_orders,state.get("winner"))
            except PrewriteRejected:
                state.update(status="reconciling", exposure_possible=True)
                save_state(state,state_path)
                raise AmbiguousOrder("fatal condition with candidate exposure") from e
            except Exception:
                state.update(status="reconciling", exposure_possible=True)
                save_state(state,state_path)
                raise AmbiguousOrder("fatal condition with bot or ambiguous exposure") from e
            state["exposure_possible"] = False
            _fatal(state,state_path,e,notify,plans); raise


def runtime_notifier(message):
    print(message,flush=True)
    token,chat=os.environ.get("TELEGRAM_BOT_TOKEN"),os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat: return
    try:
        data=urllib.parse.urlencode({"chat_id":chat,"text":message}).encode()
        urllib.request.urlopen(urllib.request.Request("https://api.telegram.org/bot"+token+"/sendMessage",data=data),timeout=5).close()
    except Exception: print("notifier_error:telegram delivery failed",flush=True)


def runtime_loop(runtime, state, cycles=1, interval=5, **kwargs):
    results = []
    for index in range(cycles):
        results.append(run_cycle(runtime, state, int(time.time()), **kwargs))
        if index + 1 < cycles: time.sleep(interval)
    return results


def record_failure(state, symbol, error, max_transient=3):
    state["errors"][symbol] = str(error); state["candidate"] = None; state["status"] = "watching"
    if isinstance(error, PermanentError):
        state["permanent_attempts"][symbol] += 1; state["ineligible"].append(symbol)
    elif isinstance(error, TransientError):
        state["transient_attempts"][symbol] += 1
        if state["transient_attempts"][symbol] >= max_transient: state["ineligible"].append(symbol)
    else: raise error


def api(path, params=None, method="GET", body=None, private=False):
    params=params or {}; query=urllib.parse.urlencode(sorted(params.items())); data=json.dumps(body,separators=(",",":")).encode() if body else None
    def request(offset=0):
        headers={"Content-Type":"application/json"}
        if private:
            key,secret=os.environ["BYBIT_API_KEY"],os.environ["BYBIT_API_SECRET"]
            ts,recv=str(int((time.time()+offset)*1000)),"5000"; payload=data.decode() if body else query
            sig=hmac.new(secret.encode(),(ts+key+recv+payload).encode(),hashlib.sha256).hexdigest()
            headers.update({"X-BAPI-API-KEY":key,"X-BAPI-TIMESTAMP":ts,"X-BAPI-RECV-WINDOW":recv,"X-BAPI-SIGN":sig})
        req=urllib.request.Request(BASE+path+("?"+query if query else ""),data=data,headers=headers,method=method)
        with urllib.request.urlopen(req,timeout=15) as response: return json.loads(response.read())
    result=request()
    code=result.get("retCode")
    if code == 10002:
        if method != "GET" or not private: raise AmbiguousOrder(f"{code}: {result.get('retMsg')}")
        with urllib.request.urlopen(urllib.request.Request(BASE+"/v5/market/time"),timeout=15) as response: synced=json.loads(response.read())
        try: offset=float(synced["result"]["timeSecond"])-time.time()
        except (KeyError,TypeError,ValueError) as e: raise TransientError("10002: time sync failed") from e
        result=request(offset); code=result.get("retCode")
    if code != 0:
        exc = TransientError if code in {10000, 10002, 10006, 10016} else PermanentError
        raise exc(f"{code}: {result.get('retMsg')}")
    payload = result["result"]
    if not isinstance(payload, dict): raise PermanentError("invalid API result")
    payload["_response_ts"] = time.time()
    return payload


def main():
    mode = os.environ.get("BASKET_EXECUTION_MODE", "dry-run")
    if mode not in {"dry-run", "live"}: raise SystemExit("invalid BASKET_EXECUTION_MODE")
    approval = None; plan_document = None; confirm = None; recovering = False
    if mode == "live" and Path(STATE_PATH).exists():
        try:
            durable_state=load_state(STATE_PATH)
            recovering=bool(durable_state.get("status") in RECOVERY_STATUSES or
                            (durable_state.get("status") == "fatal" and durable_state.get("exposure_possible")))
            if recovering:
                durable=durable_state.get("planDocument"); source=durable_state.get("executorSourceSha256")
                fingerprint=durable_state.get("planFingerprint"); durable_approval=durable_state.get("approved")
                if not durable or not source or not fingerprint or not isinstance(durable_approval,dict):
                    raise PermanentError("missing durable plan metadata")
                validate_plan_document(durable,_utc_timestamp(durable["generated_at"]))
                if fingerprint != plan_fingerprint(durable,source,durable_approval):
                    raise PermanentError("durable plan fingerprint mismatch")
        except (OSError,json.JSONDecodeError,TypeError,KeyError,PermanentError) as e:
            raise SystemExit("invalid durable plan recovery state") from e
    plan_path=os.environ.get("BASKET_PLAN_PATH")
    if not recovering and plan_path:
        try: plan_document=json.loads(Path(plan_path).read_text())
        except (OSError,json.JSONDecodeError) as e: raise SystemExit("invalid BASKET_PLAN_PATH") from e
    if mode == "live" and not recovering:
        raw = os.environ.get("BASKET_APPROVAL_JSON")
        confirm = os.environ.get("BASKET_LIVE_CONFIRM")
        if not raw or not confirm or not plan_path:
            raise SystemExit("live requires BASKET_APPROVAL_JSON, BASKET_LIVE_CONFIRM, and BASKET_PLAN_PATH")
        try: approval = json.loads(raw)
        except json.JSONDecodeError as e: raise SystemExit("invalid BASKET_APPROVAL_JSON") from e
    now = int(time.time()); runtime = Runtime(api, mode)
    try:
        if mode == "live":
            result = run_live_session(runtime, approval, confirm, now, notify=runtime_notifier,
                                      plan_document=plan_document)
            print(json.dumps({"cycle":result,"warning":RISK_WARNING},sort_keys=True))
        else:
            preflight = preflight_readonly(runtime, mode, approval, confirm, now)
            state = new_state(approval)
            result = run_cycle(runtime,state,now)
            print(json.dumps({"preflight":preflight,"cycle":result,"warning":RISK_WARNING},sort_keys=True))
    except (PermanentError, TransientError, AmbiguousOrder, LockBusy, KeyError, OSError) as e:
        raise SystemExit(str(e)) from e


if __name__ == "__main__": main()
