#!/usr/bin/env python3
import hashlib, hmac, json, math, os, time, urllib.parse, urllib.request
from datetime import datetime, timezone

BASE, SYMBOL = "https://api.bybit.com", "SPCXUSDT"
ZONE_LOW, ZONE_HIGH, REVERSAL = 135.10, 135.55, 135.70
STOP, TP1, TP2 = 133.90, 139.40, 141.20
RISK_CAP, EXPIRY_S = 2.0, 4 * 3600
CHAT_ID = "578305627"
TG_TOKEN = os.getenv("ANALYST_TELEGRAM_BOT_TOKEN", "")
STATE = "/home/fusion_omega/fusion_omega_nexus/runtime/spcx_live_executor.json"


def modeled_loss(qty, entry, stop):
    return qty * (entry - stop) + qty * (entry + stop) * 0.00055 + qty * entry * 0.004


def safe_qty(entry, stop, cap, step):
    return math.floor((cap / modeled_loss(1, entry, stop)) / step) * step


def rr(entry, stop, target):
    return (target - entry) / (entry - stop)


def update_trigger(touched, price, m5_close):
    touched = touched or ZONE_LOW <= price <= ZONE_HIGH
    return touched, touched and m5_close > REVERSAL


def market_ok(funding, spread_pct, oi_change_pct, btc_1h_pct):
    return funding <= 0 and spread_pct <= 0.08 and oi_change_pct >= -3 and btc_1h_pct >= -1.5


def invalidated(m5_close, price):
    return m5_close < 134.80 or price > 137.20


def api(path, params=None, method="GET", body=None, private=False):
    params = params or {}
    query = urllib.parse.urlencode(sorted(params.items()))
    url = BASE + path + ("?" + query if query else "")
    data = json.dumps(body, separators=(",", ":")).encode() if body else None
    headers = {"Content-Type": "application/json"}
    if private:
        key, secret = os.environ["BYBIT_API_KEY"], os.environ["BYBIT_API_SECRET"]
        ts, recv = str(int(time.time() * 1000)), "5000"
        payload = data.decode() if body else query
        sig = hmac.new(secret.encode(), (ts + key + recv + payload).encode(), hashlib.sha256).hexdigest()
        headers.update({"X-BAPI-API-KEY": key, "X-BAPI-TIMESTAMP": ts,
                        "X-BAPI-RECV-WINDOW": recv, "X-BAPI-SIGN": sig})
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.loads(response.read())
    if result.get("retCode") != 0:
        raise RuntimeError(f"Bybit {result.get('retCode')}: {result.get('retMsg')}")
    return result["result"]


def notify(text):
    print(text, flush=True)
    if TG_TOKEN:
        data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data), timeout=10).read()


def snapshot():
    ticker = api("/v5/market/tickers", {"category": "linear", "symbol": SYMBOL})["list"][0]
    k5 = api("/v5/market/kline", {"category": "linear", "symbol": SYMBOL,
                                  "interval": "5", "limit": 3})["list"]
    oi = api("/v5/market/open-interest", {"category": "linear", "symbol": SYMBOL,
            "intervalTime": "5min", "limit": 24})["list"][::-1]
    btc = api("/v5/market/kline", {"category": "linear", "symbol": "BTCUSDT",
            "interval": "60", "limit": 3})["list"][::-1]
    price = float(ticker["lastPrice"])
    return {"price": price, "m5_close": float(k5[1][4]),
            "funding": float(ticker["fundingRate"]),
            "spread_pct": (float(ticker["ask1Price"])-float(ticker["bid1Price"]))/price*100,
            "oi_change_pct": (float(oi[-1]["openInterest"])/float(oi[0]["openInterest"])-1)*100,
            "btc_1h_pct": (float(btc[-1][4])/float(btc[-2][4])-1)*100}


def account_clear():
    positions = api("/v5/position/list", {"category": "linear", "symbol": SYMBOL}, private=True)["list"]
    orders = api("/v5/order/realtime", {"category": "linear", "symbol": SYMBOL,
                                           "openOnly": 0}, private=True)["list"]
    return (not any(float(x.get("size") or 0) for x in positions)
            and not any(x.get("orderStatus") in {"New", "PartiallyFilled", "Untriggered"} for x in orders))


def execute(price):
    entry = round(min(price, 135.70), 2)
    qty = safe_qty(entry, STOP, RISK_CAP, 0.01)
    if modeled_loss(qty, entry, STOP) > RISK_CAP or rr(entry, STOP, TP1) < 2:
        raise RuntimeError("risk/RR guard failed")
    body = {"category": "linear", "symbol": SYMBOL, "side": "Buy", "orderType": "Limit",
            "qty": f"{qty:.2f}", "price": f"{entry:.2f}", "timeInForce": "PostOnly",
            "positionIdx": 0, "takeProfit": f"{TP2:.2f}", "stopLoss": f"{STOP:.2f}",
            "tpTriggerBy": "MarkPrice", "slTriggerBy": "MarkPrice",
            "orderLinkId": f"hermes-spcx-{int(time.time())}"}
    result = api("/v5/order/create", method="POST", body=body, private=True)
    return qty, entry, result.get("orderId"), body


def main():
    approved = int(os.environ["SPCX_APPROVED_TS"])
    touched = False
    notify("SPCX watcher ARMED: long reversal only; live risk <= $2; expires 4h")
    while time.time() - approved < EXPIRY_S:
        try:
            d = snapshot()
            print(datetime.now(timezone.utc).isoformat(), d, "touched", touched, flush=True)
            if invalidated(d["m5_close"], d["price"]):
                notify("SPCX plan invalidated; no order sent")
                return
            touched, trigger = update_trigger(touched, d["price"], d["m5_close"])
            if trigger and market_ok(d["funding"], d["spread_pct"], d["oi_change_pct"], d["btc_1h_pct"]):
                if not account_clear():
                    notify("SPCX blocked: existing position/order")
                    return
                qty, entry, order_id, body = execute(d["price"])
                with open(STATE, "w") as f:
                    json.dump({"status": "submitted", "orderId": order_id, "body": body}, f, indent=2)
                notify(f"SPCX LIVE submitted: LONG {qty:.2f} @ {entry:.2f}; SL {STOP}; TP {TP2}; order {order_id}")
                return
        except Exception as error:
            print(f"watch error: {error}", flush=True)
        time.sleep(30)
    notify("SPCX plan expired; no order sent")


if __name__ == "__main__":
    main()
