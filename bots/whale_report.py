#!/usr/bin/env python3
"""whale_report.py — Telegram whale accumulation/distribution reporter.

Reads the append-only event logs produced by bots/whalescanner.py
(runtime/whales/*_events.jsonl) and delivers two things to Telegram:

  1. PERIODIC REPORT every WHALE_REPORT_INTERVAL seconds (default 1800 = 30 min):
     aggregates net exchange-flow per coin over the window and ranks
     🚀 AKUMULASI (net withdrawal from CEX = bullish) vs
     🔴 DISTRIBUSI (net deposit to CEX = bearish) + ⚡ MEGA MOVE (neutral).

  2. INSTANT ALERT the moment a single event crosses a value threshold:
     🚀🌕 potensi PUMP (BULLISH / exchange withdrawal),
     🔴📉 potensi DUMP (BEARISH / exchange deposit),
     ⚡🐋 mega transfer (large NEUTRAL wallet-to-wallet).

Bias semantics come straight from whalescanner.classify_event():
    EXCHANGE_WITHDRAWAL -> BULLISH  (coin leaving exchange, sell-pressure down)
    EXCHANGE_DEPOSIT    -> BEARISH  (coin entering exchange, potential dump)
    EXCHANGE_TO_EXCHANGE / WHALE_TRANSFER -> NEUTRAL

Config via env (safe defaults; feature is opt-in via ENABLED):
    WHALE_REPORT_BOT_TOKEN   telegram bot token            (required)
    WHALE_REPORT_CHAT_ID     telegram chat id              (required)
    WHALE_REPORT_ENABLED     master switch, default "true"
    WHALE_REPORT_INTERVAL    periodic report seconds, default 1800
    WHALE_REPORT_ALERT_USD   directional instant-alert usd, default 1000000
    WHALE_REPORT_MEGA_USD    neutral instant-alert usd,     default 5000000
    WHALE_REPORT_MIN_USD     min usd to include in report,  default 100000
    WHALE_REPORT_POLL        alert poll seconds,            default 15
    WHALE_RUNTIME_DIR        dir holding *_events.jsonl,    default <repo>/runtime/whales

Run modes:
    python3 bots/whale_report.py            # persistent watcher (report + alerts)
    python3 bots/whale_report.py --test     # send ONE report from last window, exit
"""
import collections
import glob
import html
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# ---------------- config ----------------
BOT = os.getenv("WHALE_REPORT_BOT_TOKEN", "")
CHAT = os.getenv("WHALE_REPORT_CHAT_ID", "")
DISCORD_WEBHOOK = os.getenv("WHALE_REPORT_DISCORD_WEBHOOK", "")
ENABLED = os.getenv("WHALE_REPORT_ENABLED", "true").lower() == "true"
REPORT_INTERVAL = int(os.getenv("WHALE_REPORT_INTERVAL", "1800"))
ALERT_USD = float(os.getenv("WHALE_REPORT_ALERT_USD", "1000000"))
MEGA_USD = float(os.getenv("WHALE_REPORT_MEGA_USD", "5000000"))
REPORT_MIN = float(os.getenv("WHALE_REPORT_MIN_USD", "100000"))
POLL = int(os.getenv("WHALE_REPORT_POLL", "15"))
_default_runtime = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "runtime", "whales"))
RUNTIME = os.path.abspath(os.getenv("WHALE_RUNTIME_DIR", _default_runtime))
WIB = timezone(timedelta(hours=7))  # user is Indonesian (WIB = UTC+7)

EXPLORERS = {
    "ethereum": "https://etherscan.io/tx/",
    "bsc": "https://bscscan.com/tx/",
    "binance": "https://bscscan.com/tx/",
    "arbitrum": "https://arbiscan.io/tx/",
    "optimism": "https://optimistic.etherscan.io/tx/",
    "base": "https://basescan.org/tx/",
    "polygon": "https://polygonscan.com/tx/",
    "avalanche": "https://snowtrace.io/tx/",
}


def log(m):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [whale-report] {m}", flush=True)


# ---------------- helpers ----------------
def fmt_usd(v):
    v = float(v)
    if v >= 1e9:
        return f"${v / 1e9:.2f}B"
    if v >= 1e6:
        return f"${v / 1e6:.2f}M"
    if v >= 1e3:
        return f"${v / 1e3:.1f}K"
    return f"${v:.0f}"


def short(a):
    return (a[:6] + "…" + a[-4:]) if a and len(a) > 12 else (a or "?")


def parse_ts(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def explorer(chain, tx):
    if not tx:
        return ""
    c = (chain or "").lower()
    for k, base in EXPLORERS.items():
        if k in c:
            return base + tx
    return ""


def tg(text):
    if not BOT or not CHAT:
        log("missing bot token / chat id — cannot send")
        return False
    url = f"https://api.telegram.org/bot{BOT}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": CHAT, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        req = urllib.request.Request(url, data=data,
                                     headers={"User-Agent": "whale-report/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or b"{}").get("ok", False)
    except Exception as e:
        log(f"tg send failed: {e}")
        return False


def discord(text):
    """Send to Discord webhook. Convert HTML to plain Markdown."""
    if not DISCORD_WEBHOOK:
        return False
    # Strip HTML tags: <b>x</b> → **x**, <i>x</i> → *x*, <a href=...>x</a> → [x](url)
    import re
    text = re.sub(r'<b>(.*?)</b>', r'**\1**', text)
    text = re.sub(r'<i>(.*?)</i>', r'*\1*', text)
    text = re.sub(r'<a href="(.*?)">(.*?)</a>', r'[\2](\1)', text)
    text = html.unescape(text)  # &lt; → <
    
    payload = json.dumps({"content": text[:2000]}).encode()  # Discord limit 2000 char
    try:
        req = urllib.request.Request(DISCORD_WEBHOOK, data=payload,
                                     headers={"Content-Type": "application/json", "User-Agent": "whale-report/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 204
    except Exception as e:
        log(f"discord send failed: {e}")
        return False


def notify(text):
    """Send to all enabled destinations."""
    tg_ok = tg(text) if BOT and CHAT else None
    dc_ok = discord(text) if DISCORD_WEBHOOK else None
    return tg_ok or dc_ok


def classify(e):
    """Return (emoji, label, note) from event bias."""
    b = e.get("bias", "NEUTRAL")
    if b == "BULLISH":
        return ("🚀🌕", "AKUMULASI / POTENSI PUMP",
                "Whale menarik coin dari bursa → suplai jual turun (bullish)")
    if b == "BEARISH":
        return ("🔴📉", "DISTRIBUSI / POTENSI DUMP",
                "Whale mengirim coin ke bursa → potensi tekanan jual (bearish)")
    return ("⚡🐋", "MEGA TRANSFER",
            "Perpindahan whale besar antar-wallet (netral)")


# ---------------- data reading ----------------
def event_files():
    return glob.glob(os.path.join(RUNTIME, "*_events.jsonl"))


def read_window(seconds):
    """All events across all chains within the last `seconds` (bounded read)."""
    cutoff = time.time() - seconds
    out = []
    for f in event_files():
        try:
            with open(f) as fh:
                lines = fh.read().splitlines()
        except Exception:
            continue
        for ln in lines[-5000:]:  # bound: recent tail only
            try:
                e = json.loads(ln)
            except Exception:
                continue
            t = parse_ts(e.get("timestamp", ""))
            if t and t >= cutoff:
                out.append(e)
    return out


def scanner_health():
    """Return health and age; stale scanner must never look like quiet market."""
    path = os.path.join(RUNTIME, "whalescanner_health.json")
    try:
        with open(path) as fh:
            health = json.load(fh)
        age = time.time() - parse_ts(health.get("timestamp", ""))
        return health, age
    except Exception:
        return None, None


# ---------------- instant alerts (tail) ----------------
_offsets = {}
_SEEN_MAX = 5000
_seen = collections.deque(maxlen=_SEEN_MAX)
_seen_set = set()


def seed_offsets():
    """Skip existing history on startup so we only alert on NEW events."""
    for f in event_files():
        try:
            _offsets[f] = os.path.getsize(f)
        except Exception:
            _offsets[f] = 0


def poll_alerts():
    for f in event_files():
        try:
            sz = os.path.getsize(f)
        except Exception:
            continue
        off = _offsets.get(f, sz)  # new file -> start at end (no history spam)
        if sz < off:               # rotated/truncated
            off = 0
        if sz == off:
            continue
        try:
            with open(f) as fh:
                fh.seek(off)
                chunk = fh.read()
                _offsets[f] = fh.tell()
        except Exception:
            continue
        for ln in chunk.splitlines():
            try:
                e = json.loads(ln)
            except Exception:
                continue
            tx = e.get("tx_hash", "")
            if tx and tx in _seen_set:
                continue
            if tx:
                if len(_seen) >= _SEEN_MAX:
                    _seen_set.discard(_seen.popleft())
                _seen.append(tx)
                _seen_set.add(tx)
            v = float(e.get("value_usd", 0) or 0)
            bias = e.get("bias", "NEUTRAL")
            thresh = ALERT_USD if bias in ("BULLISH", "BEARISH") else MEGA_USD
            if v >= thresh:
                send_alert(e)


def send_alert(e):
    emoji, label, note = classify(e)
    v = float(e.get("value_usd", 0) or 0)
    sym = e.get("symbol", "?")
    chain = e.get("chain", "")
    et = e.get("event_type", "")
    tier = (e.get("tier", "") or "").upper()
    frm = e.get("from_label") or short(e.get("from_addr", ""))
    to = e.get("to_label") or short(e.get("to_addr", ""))
    link = explorer(chain, e.get("tx_hash", ""))
    lines = [
        f"{emoji} <b>{label}</b> — <b>{html.escape(sym)}</b>",
        f"💰 {fmt_usd(v)} · {html.escape(chain)} · {html.escape(tier)}",
        f"🔁 {html.escape(et)}: {html.escape(str(frm))} → {html.escape(str(to))}",
        f"ℹ️ {note}",
    ]
    if link:
        lines.append(f'🔗 <a href="{link}">lihat tx</a>')
    notify("\n".join(lines))
    log(f"ALERT {label} {sym} {fmt_usd(v)} ({chain})")


# ---------------- periodic report ----------------
def build_report():
    health, health_age = scanner_health()
    events = [e for e in read_window(REPORT_INTERVAL)
              if float(e.get("value_usd", 0) or 0) >= REPORT_MIN]
    now = datetime.now(WIB)
    start = now - timedelta(seconds=REPORT_INTERVAL)
    hdr = (f"🐋 <b>WHALE FLOW REPORT</b> · {REPORT_INTERVAL // 60} menit\n"
           f"🕐 {start.strftime('%H:%M')}–{now.strftime('%H:%M')} WIB")
    if not health or health_age is None or health_age > max(120, POLL * 4):
        age_text = "tidak tersedia" if health_age is None else f"{health_age / 60:.0f} menit"
        return (hdr + "\n\n🚨 <b>SCANNER OFFLINE / STALE</b>\n"
                f"Health terakhir: {age_text} lalu. Data window tidak dapat dipercaya.")
    if not events:
        return hdr + "\n\n😴 Tidak ada aktivitas whale signifikan pada window ini."

    agg = defaultdict(lambda: {"accum": 0.0, "dist": 0.0, "neutral": 0.0,
                               "n": 0, "chains": set()})
    tot = 0.0
    for e in events:
        s = e.get("symbol", "?")
        v = float(e.get("value_usd", 0) or 0)
        b = e.get("bias", "NEUTRAL")
        a = agg[s]
        a["n"] += 1
        a["chains"].add(e.get("chain", ""))
        tot += v
        if b == "BULLISH":
            a["accum"] += v
        elif b == "BEARISH":
            a["dist"] += v
        else:
            a["neutral"] += v

    rows = [(s, a["accum"] - a["dist"], a) for s, a in agg.items()]
    accums = sorted([r for r in rows if r[1] > 0], key=lambda x: -x[1])[:8]
    dists = sorted([r for r in rows if r[1] < 0], key=lambda x: x[1])[:8]
    megas = sorted([(s, a) for s, net, a in rows if a["neutral"] > 0],
                   key=lambda x: -x[1]["neutral"])[:5]

    P = [hdr, f"\n📊 {len(events)} event · {fmt_usd(tot)} volume · {len(agg)} coin"]
    if accums:
        P.append("\n🚀 <b>AKUMULASI</b> <i>(net tarik dari bursa → bullish)</i>")
        for s, net, a in accums:
            P.append(f" • <b>{html.escape(s)}</b>  +{fmt_usd(net)}  ({a['n']}x)")
    if dists:
        P.append("\n🔴 <b>DISTRIBUSI</b> <i>(net masuk bursa → bearish)</i>")
        for s, net, a in dists:
            P.append(f" • <b>{html.escape(s)}</b>  −{fmt_usd(-net)}  ({a['n']}x)")
    if megas:
        P.append("\n⚡ <b>MEGA MOVE</b> <i>(netral / antar-wallet)</i>")
        for s, a in megas:
            P.append(f" • <b>{html.escape(s)}</b>  {fmt_usd(a['neutral'])}  ({a['n']}x)")
    return "\n".join(P)


# ---------------- main ----------------
def main():
    if not ENABLED:
        log("WHALE_REPORT_ENABLED != true — exiting")
        return
    if not BOT or not CHAT:
        log("missing WHALE_REPORT_BOT_TOKEN / WHALE_REPORT_CHAT_ID — exiting")
        return

    if "--test" in sys.argv:
        ok = notify(build_report())
        log(f"test report sent ok={ok}")
        return

    log(f"start · runtime={RUNTIME} · report/{REPORT_INTERVAL}s · "
        f"alert≥{fmt_usd(ALERT_USD)} · mega≥{fmt_usd(MEGA_USD)}")
    seed_offsets()
    notify("🐋 <b>Whale reporter aktif</b>\n"
       f"Report tiap {REPORT_INTERVAL // 60} menit · "
       f"alert instan ≥ {fmt_usd(ALERT_USD)} (mega ≥ {fmt_usd(MEGA_USD)})")
    last_report = time.time()
    while True:
        try:
            poll_alerts()
            if time.time() - last_report >= REPORT_INTERVAL:
                notify(build_report())
                last_report = time.time()
                log("periodic report sent")
        except Exception as e:
            log(f"loop error: {e}")
        time.sleep(POLL)


if __name__ == "__main__":
    main()
