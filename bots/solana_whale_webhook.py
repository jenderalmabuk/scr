#!/usr/bin/env python3
"""solana_whale_webhook.py — Solana whale-transfer monitor via Helius webhook.

Free-tier Helius Enhanced Webhook + Cloudflare quick tunnel (NO inbound port
needed — Azure NSG stays closed). Mirrors bots/whalescanner.py output contract
so fusionnew/clean_core/engine.py reads it UNCHANGED:
    writes runtime/whales/latest_whale_solana_{SYMBOL}.json
engine reads via glob: latest_whale_*_{base_symbol}.json  (engine.py:590)

Flow:
  1. start cloudflared quick tunnel -> local receiver (127.0.0.1:PORT)
  2. parse assigned https://<rand>.trycloudflare.com URL from cloudflared stderr
  3. (re)register Helius enhanced webhook: [20 mints], TRANSFER, authHeader
  4. receive POSTs -> parse tokenTransfers -> filter our mints -> USD -> write files
  5. self-heal: if tunnel dies, restart it, recapture URL, re-register

Env (all optional except HELIUS_API_KEY):
  SOLANA_WHALE_ENABLED   master switch, default "false" (OFF, reversible)
  HELIUS_API_KEY         Helius key (from .env)
  SOLANA_WHALE_PORT      local receiver port (default 8799)
  SOLANA_WHALE_SECRET    shared authHeader secret (default derived, override advised)
  SOLANA_WHALE_MIN_USD   min transfer USD to record (default 100000)
  WHALE_RUNTIME_DIR      output dir (default repo runtime/whales)
  SOLANA_TOKEN_MAP       token map path (default $WHALE_RUNTIME_DIR/solana_token_map.json)
  CLOUDFLARED_BIN        cloudflared path (default "cloudflared")
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

# ── Config ─────────────────────────────────────────────────────
ENABLED = os.getenv("SOLANA_WHALE_ENABLED", "false").lower() == "true"
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "").strip()
PORT = int(os.getenv("SOLANA_WHALE_PORT", "8799"))
SECRET = os.getenv("SOLANA_WHALE_SECRET", "sol-whale-" + (HELIUS_API_KEY[:8] or "nokey"))
MIN_USD = float(os.getenv("SOLANA_WHALE_MIN_USD", "100000"))
CLOUDFLARED_BIN = os.getenv("CLOUDFLARED_BIN", "cloudflared")

_REPO = Path(__file__).resolve().parent.parent
BASE_DIR = Path(os.getenv("WHALE_RUNTIME_DIR", str(_REPO / "runtime" / "whales")))
BASE_DIR.mkdir(parents=True, exist_ok=True)
TOKEN_MAP_PATH = Path(os.getenv("SOLANA_TOKEN_MAP", str(BASE_DIR / "solana_token_map.json")))

HELIUS_WEBHOOK_API = "https://api.helius.xyz/v0/webhooks"

WHALE_THRESHOLDS = {"small": 100_000, "medium": 500_000, "large": 1_000_000, "extreme": 10_000_000}

# mint -> (SYMBOL, name)   and   SYMBOL -> mint
MINT_TO_SYM: dict = {}
SYMBOLS: list = []


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] [sol-whale] {msg}", flush=True)


def load_token_map():
    global MINT_TO_SYM, SYMBOLS
    if not TOKEN_MAP_PATH.exists():
        log(f"FATAL token map not found: {TOKEN_MAP_PATH}")
        sys.exit(2)
    data = json.load(open(TOKEN_MAP_PATH))
    for sym, info in data.items():
        mint = info["mint"] if isinstance(info, dict) else info
        name = info.get("name", sym) if isinstance(info, dict) else sym
        MINT_TO_SYM[mint] = (sym, name)
        SYMBOLS.append(sym)
    log(f"loaded {len(MINT_TO_SYM)} Solana mints: {', '.join(sorted(SYMBOLS))}")


# ── Prices (Bybit linear perps, one call, refreshed) ───────────
PRICE_CACHE: dict = {}   # SYMBOL -> per-unit USD price
_PRICE_LOCK = threading.Lock()


def _http_get_json(url: str, timeout: int = 12):
    req = urllib.request.Request(url, headers={"User-Agent": "sol-whale/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def refresh_prices():
    """Fetch Bybit linear tickers once, resolve per-unit USD price for our symbols.
    Handles 1000x/10000x/1000000x perp prefixes (e.g. 1000PEPEUSDT)."""
    try:
        d = _http_get_json("https://api.bybit.com/v5/market/tickers?category=linear")
        tick = {t["symbol"]: float(t["lastPrice"]) for t in d.get("result", {}).get("list", [])
                if t.get("lastPrice")}
    except Exception as e:
        log(f"price fetch failed: {e}")
        return
    resolved = {}
    for sym in SYMBOLS:
        # direct
        if f"{sym}USDT" in tick:
            resolved[sym] = tick[f"{sym}USDT"]
            continue
        # prefixed (per-N-tokens contract -> per-unit)
        for mult in (1000, 10000, 1000000):
            key = f"{mult}{sym}USDT"
            if key in tick:
                resolved[sym] = tick[key] / mult
                break
    with _PRICE_LOCK:
        PRICE_CACHE.update(resolved)
    log(f"prices: {len(resolved)}/{len(SYMBOLS)} resolved (Bybit linear)")


def price_of(sym: str) -> float:
    with _PRICE_LOCK:
        return PRICE_CACHE.get(sym, 0.0)


# ── Classification (mirror whalescanner.py) ────────────────────
def get_tier(v: float) -> str:
    if v >= WHALE_THRESHOLDS["extreme"]:
        return "EXTREME"
    if v >= WHALE_THRESHOLDS["large"]:
        return "LARGE"
    if v >= WHALE_THRESHOLDS["medium"]:
        return "MEDIUM"
    if v >= WHALE_THRESHOLDS["small"]:
        return "SMALL"
    return "MINOR"


def fmt_value(v: float) -> str:
    if v >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    return f"${v:,.0f}"


def save_event(event: dict):
    """Atomic write: temp + rename. Name matches engine glob latest_whale_*_{sym}.json."""
    latest = BASE_DIR / f"latest_whale_solana_{event['symbol']}.json"
    tmp = latest.with_suffix(latest.suffix + ".tmp")
    # append to JSONL trail too (parity with EVM scanner)
    with open(BASE_DIR / "solana_events.jsonl", "a") as f:
        f.write(json.dumps(event) + "\n")
    with open(tmp, "w") as f:
        json.dump(event, f, default=str)
    tmp.rename(latest)


def handle_transaction(tx: dict) -> int:
    """Parse one Helius enhanced tx; write whale events for our mints. Returns count."""
    written = 0
    sig = tx.get("signature", "")
    slot = tx.get("slot", 0)
    for tr in tx.get("tokenTransfers", []) or []:
        mint = tr.get("mint")
        if mint not in MINT_TO_SYM:
            continue
        sym, _name = MINT_TO_SYM[mint]
        amount = float(tr.get("tokenAmount") or 0)
        price = price_of(sym)
        if price <= 0 or amount <= 0:
            continue
        value_usd = amount * price
        if value_usd < MIN_USD:
            continue
        tier = get_tier(value_usd)
        from_addr = tr.get("fromUserAccount", "") or ""
        to_addr = tr.get("toUserAccount", "") or ""
        event = {
            "chain": "Solana",
            "symbol": sym,
            "event_type": "WHALE_TRANSFER",
            "tier": tier,
            "value_usd": round(value_usd, 2),
            "from_addr": from_addr,
            "to_addr": to_addr,
            "from_label": "",
            "to_label": "",
            "bias": "NEUTRAL",
            "confidence_score": min(100, int(50 + value_usd / WHALE_THRESHOLDS["extreme"] * 50)),
            "risk_level": "Medium",
            "reason": f"Whale transfer {fmt_value(value_usd)}",
            "tx_hash": sig,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "block_number": str(slot),
        }
        save_event(event)
        log(f"  WHALE_TRANSFER {sym} {fmt_value(value_usd)} ({tier})")
        written += 1
    return written


# ── HTTP receiver ──────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence default noisy logging
        pass

    def _reply(self, code: int, body: str = "ok"):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        # health endpoint
        if self.path.startswith("/health"):
            self._reply(200, "sol-whale ok")
        else:
            self._reply(404, "not found")

    def do_POST(self):
        # auth: Helius sends the configured authHeader value in Authorization header
        auth = self.headers.get("Authorization", "")
        if SECRET and auth != SECRET:
            self._reply(401, "unauthorized")
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n) if n else b"[]"
            payload = json.loads(raw or b"[]")
        except Exception as e:
            self._reply(400, f"bad payload: {e}")
            return
        txs = payload if isinstance(payload, list) else [payload]
        total = 0
        for tx in txs:
            try:
                total += handle_transaction(tx)
            except Exception as e:
                log(f"parse error: {e}")
        self._reply(200, f"processed {len(txs)} tx, {total} whales")


def start_receiver():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    log(f"receiver listening 127.0.0.1:{PORT}")
    return srv


# ── Cloudflare quick tunnel ────────────────────────────────────
_TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def start_tunnel() -> tuple:
    """Launch cloudflared quick tunnel -> local receiver. Returns (proc, public_url)."""
    proc = subprocess.Popen(
        [CLOUDFLARED_BIN, "tunnel", "--url", f"http://127.0.0.1:{PORT}",
         "--no-autoupdate", "--retries", "99"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    url = None
    deadline = time.time() + 40
    while time.time() < deadline and proc.stderr is not None:
        line = proc.stderr.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        m = _TUNNEL_RE.search(line)
        if m:
            url = m.group(0)
            break
    if not url:
        log("FAILED to capture tunnel URL")
        try:
            proc.terminate()
        except Exception:
            pass
        return None, None
    log(f"tunnel up: {url}")
    # drain stderr in background so pipe never blocks the tunnel
    threading.Thread(target=lambda: [proc.stderr.readline() for _ in iter(int, 1)],
                     daemon=True).start()
    return proc, url


# ── Helius webhook registration ────────────────────────────────
def _helius(method: str, path: str = "", body: Optional[dict] = None):
    url = f"{HELIUS_WEBHOOK_API}{path}?api-key={HELIUS_API_KEY}"
    data = json.dumps(body).encode() if body is not None else None
    # Helius sits behind Cloudflare WAF which 403s the default Python-urllib UA.
    headers = {"User-Agent": "Mozilla/5.0 sol-whale/1.0"}
    # Only send Content-Type when there IS a body — a bodiless DELETE with
    # Content-Type:application/json makes Helius try to parse "null" -> 400.
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def cleanup_stale_webhooks():
    """Delete any prior webhook we own (trycloudflare URL or our mint set)."""
    try:
        existing = _helius("GET")
    except Exception as e:
        log(f"list webhooks failed: {e}")
        return
    our_mints = set(MINT_TO_SYM.keys())
    for wh in existing or []:
        wurl = wh.get("webhookURL", "")
        addrs = set(wh.get("accountAddresses", []))
        if "trycloudflare.com" in wurl or (addrs & our_mints):
            wid = wh.get("webhookID")
            try:
                _helius("DELETE", f"/{wid}")
                log(f"deleted stale webhook {wid}")
            except Exception as e:
                log(f"delete {wid} failed: {e}")


def register_webhook(public_url: str) -> str:
    cleanup_stale_webhooks()
    body = {
        "webhookURL": f"{public_url}/whale",
        "transactionTypes": ["TRANSFER"],
        "accountAddresses": list(MINT_TO_SYM.keys()),
        "webhookType": "enhanced",
        "authHeader": SECRET,
    }
    resp = _helius("POST", body=body)
    wid = resp.get("webhookID")
    if wid:
        log(f"registered webhook {wid} -> {public_url}/whale ({len(MINT_TO_SYM)} mints)")
    else:
        log(f"register FAILED: {str(resp)[:200]}")
    return wid


# ── Main ───────────────────────────────────────────────────────
def main():
    if not ENABLED:
        log("SOLANA_WHALE_ENABLED != true — exiting (feature OFF).")
        return
    if not HELIUS_API_KEY:
        log("FATAL HELIUS_API_KEY missing.")
        sys.exit(2)
    load_token_map()
    refresh_prices()
    start_receiver()

    # supervised tunnel + webhook, self-healing
    last_price = time.time()
    proc = None
    while True:
        if proc is None or proc.poll() is not None:
            if proc is not None:
                log("tunnel died — restarting")
            proc, url = start_tunnel()
            if not url:
                time.sleep(10)
                continue
            register_webhook(url)
        # refresh prices every 5 min
        if time.time() - last_price > 300:
            refresh_prices()
            last_price = time.time()
        time.sleep(5)


if __name__ == "__main__":
    main()
