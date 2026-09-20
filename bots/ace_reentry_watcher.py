#!/usr/bin/env python3
import json
import os
import time
from datetime import datetime, timezone

import requests

SYMBOL = "ACEUSDT"
CHAT_ID = os.getenv("ACE_ALERT_CHAT_ID", "578305627")
TOKEN = os.getenv("ANALYST_TELEGRAM_BOT_TOKEN", "")
STATE_PATH = "/home/fusion_omega/fusion_omega_nexus/runtime/ace_reentry_watcher_state.json"
INTERVAL_S = int(os.getenv("ACE_WATCH_INTERVAL_S", "60"))


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def get_json(url, params=None, retries=3):
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=10)
            if r.status_code == 429:
                wait = min(2 ** attempt, 10)  # exponential backoff, max 10s
                if attempt < retries - 1:
                    time.sleep(wait)
                    continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429 and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
    raise Exception(f"Failed after {retries} attempts")


def market():
    ticker = get_json("https://fapi.binance.com/fapi/v1/ticker/24hr", {"symbol": SYMBOL})
    funding = get_json("https://fapi.binance.com/fapi/v1/fundingRate", {"symbol": SYMBOL, "limit": 1})[0]
    oi = get_json("https://fapi.binance.com/fapi/v1/openInterest", {"symbol": SYMBOL})
    k5 = get_json("https://fapi.binance.com/fapi/v1/klines", {"symbol": SYMBOL, "interval": "5m", "limit": 24})
    k4h = get_json("https://fapi.binance.com/fapi/v1/klines", {"symbol": SYMBOL, "interval": "4h", "limit": 3})

    price = float(ticker["lastPrice"])
    lows = [float(x[3]) for x in k5]
    highs = [float(x[2]) for x in k5]
    closes = [float(x[4]) for x in k5]
    vols = [float(x[7]) for x in k5]
    last_closed_4h = k4h[-2]

    return {
        "price": price,
        "change_24h": float(ticker["priceChangePercent"]),
        "high_24h": float(ticker["highPrice"]),
        "low_24h": float(ticker["lowPrice"]),
        "funding_pct": float(funding["fundingRate"]) * 100,
        "oi_usd": float(oi["openInterest"]) * price,
        "m5_2h_low": min(lows),
        "m5_2h_high": max(highs),
        "m5_last_close": closes[-1],
        "m5_prev_close": closes[-2],
        "m5_quote_volume_1h": sum(vols[-12:]),
        "closed_4h_close": float(last_closed_4h[4]),
        "closed_4h_time": datetime.fromtimestamp(last_closed_4h[6] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


def load_state():
    if not os.path.exists(STATE_PATH):
        return {"sent": {}, "started": now()}
    with open(STATE_PATH) as f:
        return json.load(f)


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def send(msg):
    print(msg.replace("\n", " | "), flush=True)
    if not TOKEN:
        return
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
        timeout=10,
    )


def once(key, state, msg):
    if state["sent"].get(key):
        return
    state["sent"][key] = now()
    send(msg)


def evaluate(d, state):
    p = d["price"]
    f = d["funding_pct"]
    c4 = d["closed_4h_close"]

    # Early warnings
    if p > 0.11000:
        once("bounce_110", state, f"🟢 <b>ACE bounce watch</b>\nPrice ${p:.5f} reclaimed 0.11000.\nWait 4h close > 0.11500 before long.\nFunding {f:.4f}%")

    if p < 0.10000:
        once("break_100_spot", state, f"🔴 <b>ACE breakdown watch</b>\nPrice ${p:.5f} below 0.10000.\nDo NOT chase short. Wait retest fail from below.\nFunding {f:.4f}%")

    # Long re-entry confirmation
    if c4 > 0.11500 and f > -0.05:
        once("long_confirm", state, f"🚀 <b>ACE LONG RE-ENTRY VALID</b>\n4h close ${c4:.5f} > 0.11500 ({d['closed_4h_time']})\nFunding {f:.4f}% normalized.\nPlan: long 25-50%, SL 0.10800, TP 0.12000-0.12500.")

    # Short setup confirmation: 4h below 0.10000 and price retest fails under 0.10000
    retest_fail = d["m5_prev_close"] > 0.10000 and d["m5_last_close"] < 0.10000
    if c4 < 0.10000 and retest_fail:
        once("short_confirm", state, f"⚠️ <b>ACE SHORT SETUP POSSIBLE</b>\n4h close ${c4:.5f} < 0.10000 and M5 retest failed.\nPrice ${p:.5f}, funding {f:.4f}%.\nPlan: short small 25%, SL 0.10450, TP1 0.09500, TP2 0.08800. Avoid if spread/slippage bad.")

    # No-trade zone reminder once per run
    if 0.10000 <= p <= 0.11000:
        state.setdefault("last_zone", "chop")


def main():
    state = load_state()
    send("📡 <b>ACE watcher started</b>\nAlerts: long re-entry, breakdown, short setup.\nDefault: no trade inside 0.10000-0.11000.")
    consecutive_errors = 0
    while True:
        try:
            d = market()
            state["last_check"] = {"time": now(), **d}
            evaluate(d, state)
            save_state(state)
            print(f"[{now()}] ACE ${d['price']:.5f} 24h {d['change_24h']:+.2f}% fund {d['funding_pct']:.4f}% 4h_close {d['closed_4h_close']:.5f}", flush=True)
            consecutive_errors = 0  # reset on success
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                consecutive_errors += 1
                wait = min(60 * consecutive_errors, 300)  # 1min, 2min, 3min... max 5min
                print(f"[{now()}] Rate limit hit, sleeping {wait}s (error #{consecutive_errors})", flush=True)
                time.sleep(wait)
                continue
            send(f"⚠️ <b>ACE watcher error</b>\n{e}")
            time.sleep(60)
        except Exception as e:
            consecutive_errors += 1
            print(f"[{now()}] Error: {e}", flush=True)
            if consecutive_errors >= 3:
                send(f"⚠️ <b>ACE watcher error</b>\n{e}")
            time.sleep(60)
        else:
            time.sleep(INTERVAL_S)


if __name__ == "__main__":
    main()
