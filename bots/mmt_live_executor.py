#!/usr/bin/env python3
import hashlib, hmac, json, math, os, time, urllib.parse, urllib.request
from datetime import datetime, timezone

BASE = "https://api.bybit.com"
SYMBOL = "MMTUSDT"
ENTRY_LOW, ENTRY_HIGH = 0.21900, 0.22000
STOP, TARGET = 0.21420, 0.23750
RISK_CAP = 2.0
APPROVED_TS = 1786253980  # 2026-08-09 approval window
EXPIRY_S = 4 * 3600
STATE = "/home/fusion_omega/fusion_omega_nexus/runtime/mmt_live_executor.json"
CHAT_ID = os.getenv("ACE_ALERT_CHAT_ID", "578305627")
TG_TOKEN = os.getenv("ANALYST_TELEGRAM_BOT_TOKEN", "")


def modeled_loss(qty, entry, stop):
    fees = qty * (entry + stop) * 0.00055
    slippage = qty * entry * 0.004
    return qty * (entry - stop) + fees + slippage


def safe_qty(entry, stop, risk_cap, qty_step):
    per_unit = modeled_loss(1, entry, stop)
    return math.floor((risk_cap / per_unit) / qty_step) * qty_step


def rr(entry, stop, target):
    return (target - entry) / (entry - stop)


def plan_active(now_ts, approved_ts, expiry_s):
    return now_ts - approved_ts < expiry_s


def valid_gate(price, m5_close, funding, spread_pct, oi_change_pct, btc_1h_pct):
    return (ENTRY_LOW <= price <= ENTRY_HIGH and m5_close >= 0.21800
            and funding <= 0 and spread_pct <= 0.10
            and oi_change_pct >= -3.0 and btc_1h_pct >= -1.5
            and rr(price, STOP, TARGET) >= 3.0)


def request(path, params=None, method="GET", body=None, private=False):
    params = params or {}
    query = urllib.parse.urlencode(sorted(params.items()))
    url = BASE + path + ("?" + query if query else "")
    data = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if private:
        key, secret = os.environ["BYBIT_API_KEY"], os.environ["BYBIT_API_SECRET"]
        ts, recv = str(int(time.time() * 1000)), "5000"
        payload = data.decode() if body is not None else query
        sig = hmac.new(secret.encode(), (ts + key + recv + payload).encode(), hashlib.sha256).hexdigest()
        headers.update({"X-BAPI-API-KEY": key, "X-BAPI-TIMESTAMP": ts,
                        "X-BAPI-RECV-WINDOW": recv, "X-BAPI-SIGN": sig})
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=15) as response:
        result = json.loads(response.read())
    if result.get("retCode") != 0:
        raise RuntimeError(f"Bybit {result.get('retCode')}: {result.get('retMsg')}")
    return result["result"]


def notify(text):
    print(text, flush=True)
    if not TG_TOKEN:
        return
    payload = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
    urllib.request.urlopen(urllib.request.Request(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=payload), timeout=10).read()


def market():
    ticker = request("/v5/market/tickers", {"category": "linear", "symbol": SYMBOL})["list"][0]
    k5 = request("/v5/market/kline", {"category": "linear", "symbol": SYMBOL,
                                      "interval": "5", "limit": 3})["list"]
    oi = request("/v5/market/open-interest", {"category": "linear", "symbol": SYMBOL,
            "intervalTime": "5min", "limit": 24})["list"][::-1]
    btc = request("/v5/market/kline", {"category": "linear", "symbol": "BTCUSDT",
            "interval": "60", "limit": 3})["list"][::-1]
    price = float(ticker["lastPrice"])
    return {"price": price, "m5_close": float(k5[1][4]),
            "funding": float(ticker["fundingRate"]),
            "spread_pct": (float(ticker["ask1Price"])-float(ticker["bid1Price"]))/price*100,
            "oi_change_pct": (float(oi[-1]["openInterest"])/float(oi[0]["openInterest"])-1)*100,
            "btc_1h_pct": (float(btc[-1][4])/float(btc[-2][4])-1)*100}


def no_existing_position_or_order():
    positions = request("/v5/position/list", {"category": "linear", "symbol": SYMBOL}, private=True)["list"]
    if any(float(p.get("size") or 0) > 0 for p in positions):
        return False, "existing MMT position"
    orders = request("/v5/order/realtime", {"category": "linear", "symbol": SYMBOL,
                                            "openOnly": 0}, private=True)["list"]
    if any(o.get("orderStatus") in {"New", "PartiallyFilled", "Untriggered"} for o in orders):
        return False, "existing MMT order"
    return True, "clear"


def execute(price):
    qty = safe_qty(price, STOP, RISK_CAP, 0.1)
    if qty < 0.1 or modeled_loss(qty, price, STOP) > RISK_CAP:
        raise RuntimeError("unsafe quantity")
    body = {"category": "linear", "symbol": SYMBOL, "side": "Buy", "orderType": "Limit",
            "qty": f"{qty:.1f}", "price": f"{price:.5f}", "timeInForce": "PostOnly",
            "positionIdx": 0, "takeProfit": f"{TARGET:.5f}", "stopLoss": f"{STOP:.5f}",
            "tpTriggerBy": "MarkPrice", "slTriggerBy": "MarkPrice",
            "orderLinkId": f"hermes-mmt-{int(time.time())}"}
    result = request("/v5/order/create", method="POST", body=body, private=True)
    return qty, result.get("orderId"), body


def main():
    approved = int(os.getenv("MMT_APPROVED_TS", str(APPROVED_TS)))
    notify("MMT watcher ARMED: approved plan only; live limit, risk <= $2, expiry 4h")
    while plan_active(time.time(), approved, EXPIRY_S):
        try:
            d = market()
            print(datetime.now(timezone.utc).isoformat(), d, flush=True)
            if valid_gate(**d):
                clear, reason = no_existing_position_or_order()
                if not clear:
                    notify(f"MMT plan blocked: {reason}")
                    return
                qty, order_id, body = execute(d["price"])
                notify(f"MMT LIVE order submitted: LONG {qty:.1f} @ {d['price']:.5f}; SL {STOP}; TP {TARGET}; order {order_id}")
                with open(STATE, "w") as f:
                    json.dump({"status": "submitted", "orderId": order_id, "body": body}, f, indent=2)
                return
        except Exception as e:
            print(f"watch error: {e}", flush=True)
        time.sleep(30)
    notify("MMT plan expired without entry; no order sent")


if __name__ == "__main__":
    main()
