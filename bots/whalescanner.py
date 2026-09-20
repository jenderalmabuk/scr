"""
whalescanner.py — Multi-chain EVM whale transfer monitor.
Async WebSocket per chain, 266 token watchlist, exchange wallet labels.

Architecture:
  asyncio event loop → N WebSocket connections (1 per chain)
  Each WS: subscribe event Transfer for watchlist tokens
  Filter: value > $threshold, classify, dump JSONL

Usage:
  python3 whalescanner.py                          # full run
  python3 whalescanner.py --once                   # single scan only

Output:
  runtime/whales/{chain}_events.jsonl (append)
  runtime/whales/latest_whale_{symbol}.json
"""
import asyncio, json, os, re, time, traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from pathlib import Path

# F-14: move websockets import to module level
import websockets

# ── Config ─────────────────────────────────────────────────────
# Docker-compatible: use env var or fallback to repo root
BASE_DIR = Path(os.getenv("WHALE_RUNTIME_DIR", "/app/runtime/whales"))
BASE_DIR.mkdir(parents=True, exist_ok=True)

# Token watchlist: loaded from token_chain_map.json
TOKEN_MAP_PATH = BASE_DIR / "token_chain_map.json"
EXCHANGE_LABELS_PATH = BASE_DIR / "exchange_labels.json"

# Thresholds
WHALE_THRESHOLDS = {
    "small": 100_000,
    "medium": 500_000,
    "large": 1_000_000,
    "extreme": 10_000_000,
}

# Public RPC endpoints (free, no auth)
RPC_ENDPOINTS: Dict[str, str] = {
    "ethereum": "wss://ethereum-rpc.publicnode.com",
    "binance-smart-chain": "wss://bsc-rpc.publicnode.com",
    "base": "wss://base-rpc.publicnode.com",
    "arbitrum-one": "wss://arbitrum-one-rpc.publicnode.com",
    "optimism": "wss://optimism-rpc.publicnode.com",
    "polygon-pos": "wss://polygon-bor-rpc.publicnode.com",
    "avalanche": "wss://avalanche-c-chain-rpc.publicnode.com",
    "berachain": "wss://berachain-rpc.publicnode.com",
}

# ERC-20 Transfer event signature
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

CHAIN_NAMES: Dict[str, str] = {
    "ethereum": "Ethereum",
    "binance-smart-chain": "BSC",
    "base": "Base",
    "arbitrum-one": "Arbitrum",
    "optimism": "Optimism",
    "polygon-pos": "Polygon",
    "avalanche": "Avalanche",
    "berachain": "Berachain",
}

PRICE_CACHE: Dict[str, float] = {}  # symbol -> USD price


# ── Data models ────────────────────────────────────────────────

def load_token_map() -> Dict[str, Any]:
    """Load token -> chain mapping from file."""
    if not TOKEN_MAP_PATH.exists():
        print(f"[ERROR] Token map not found: {TOKEN_MAP_PATH}")
        return {}
    with open(TOKEN_MAP_PATH) as f:
        return json.load(f)


def load_exchange_labels() -> Dict[str, str]:
    """Load exchange wallet labels."""
    if not EXCHANGE_LABELS_PATH.exists():
        return _get_default_exchange_labels()
    with open(EXCHANGE_LABELS_PATH) as f:
        return json.load(f)


def _get_default_exchange_labels() -> Dict[str, str]:
    """Known exchange wallet addresses (Ethereum mainnet)."""
    return {
        # Binance
        "0x28c6c06298d514db089934071355e5743bf21d60": "Binance 14",
        "0x21a31ee1afc51d94c2efccaa2092ad1028285549": "Binance 15",
        "0xdfd5293d8e347dfe59e90efd55b2956a1343963d": "Coinbase 10",
        "0xbe0eb53f46cd790cd13851d5eff43d12404d33e8": "Binance 7",
        "0xf977814e90da44bfa03b6295a0616a897441acec": "Binance 8",
        "0x1e6f4d34d2d11621d7653e3bd7516bec8b1e5b60": "Binance 9",
        "0x3ddfa8ec3052539b6c9549f12ae2c44c0d0a1f9a": "Binance 10",
        # Bybit
        "0x1db92e2eebc8e0c075a02bea49a2935bcd2dfcf4": "Bybit 1",
        "0x8b8d0e90d0e1e7a39dc1e4b0f0713b8c3a3e6f9a": "Bybit 2",
        # OKX
        "0x6cc5f688a315f3dc28a7781717a9a798a59fda7b": "OKX 1",
        "0x5041ed759dd4afc3a72b8192c143f72f4721f1fc": "OKX 2",
        # Kraken
        "0x291c3f5f06e5b0b3f7a1f0b5f0a0b0c0d0e0f0a0": "Kraken 1",
        "0x0a869d79a7052c7f1b55a8ebabbea3420f0d1e13": "Kraken 2",
        # KuCoin
        "0x2b5634c42055806a59e9107ed44d43c426e58258": "KuCoin 1",
        "0x689c56aef474df92d44a1b70850f808488f9769c": "KuCoin 2",
        # Bitget
        "0x0639556f03714a74a5feeaf5736a4a64ff70d77d": "Bitget 1",
        # Gate.io
        "0x0d0707963952f2fba59dd06f2b425ace40b492fe": "Gate.io 1",
        # MEXC
        "0x013e7fb304e7d9ec81621c761f4c75e590e8e1b8": "MEXC 1",
    }


def classify_event(
    from_addr: str, to_addr: str,
    addr_labels: Dict[str, str],
    net_exchange_wallets: Optional[List[str]] = None,
) -> Tuple[str, str, str, str]:
    """
    Classify a transfer event.

    Returns:
        event_type: str (EXCHANGE_WITHDRAWAL, EXCHANGE_DEPOSIT, WHALE_TRANSFER, etc)
        bias: str (BULLISH, BEARISH, NEUTRAL)
        from_label: str
        to_label: str
    """
    f_lower = from_addr.lower()
    t_lower = to_addr.lower()

    from_label = addr_labels.get(f_lower, "")
    to_label = addr_labels.get(t_lower, "")

    from_is_exchange = bool(from_label)
    to_is_exchange = bool(to_label)

    if from_is_exchange and not to_is_exchange:
        return "EXCHANGE_WITHDRAWAL", "BULLISH", from_label, to_label
    if to_is_exchange and not from_is_exchange:
        return "EXCHANGE_DEPOSIT", "BEARISH", from_label, to_label
    if from_is_exchange and to_is_exchange:
        return "EXCHANGE_TO_EXCHANGE", "NEUTRAL", from_label, to_label

    return "WHALE_TRANSFER", "NEUTRAL", from_label, to_label


def format_value(value_usd: float) -> str:
    """Format USD value for display."""
    if value_usd >= 1_000_000:
        return f"${value_usd/1_000_000:.2f}M"
    return f"${value_usd:,.0f}"


def get_threshold_tier(value_usd: float) -> str:
    """Get tier label for USD value."""
    if value_usd >= WHALE_THRESHOLDS["extreme"]:
        return "EXTREME"
    if value_usd >= WHALE_THRESHOLDS["large"]:
        return "LARGE"
    if value_usd >= WHALE_THRESHOLDS["medium"]:
        return "MEDIUM"
    if value_usd >= WHALE_THRESHOLDS["small"]:
        return "SMALL"
    return "MINOR"


# ── Price helpers ──────────────────────────────────────────────

def get_prices(symbols: List[str]) -> Dict[str, float]:
    """Fetch current USD prices for symbols via Binance API."""
    prices = {}
    for sym in symbols:
        try:
            pair = f"{sym}USDT" if not sym.endswith("USDT") else sym
            r = __import__("requests").get(
                f"https://api.binance.com/api/v3/ticker/price?symbol={pair}",
                timeout=5,
            )
            if r.status_code == 200:
                d = r.json()
                prices[sym] = float(d["price"])
        except Exception:
            pass
    return prices


# ── WebSocket scanner ──────────────────────────────────────────

class ChainWorker:
    """Async WebSocket worker for one chain."""

    def __init__(
        self,
        chain_key: str,
        rpc_url: str,
        chain_name: str,
        watchlist: Dict[str, str],  # token_symbol -> contract_address
        exchange_labels: Dict[str, str],
        price_cache_ref: Dict[str, float],
        decimals_map: Dict[str, int],  # symbol -> decimals for this chain
    ):
        self.chain_key = chain_key
        self.rpc_url = rpc_url
        self.chain_name = chain_name
        self.watchlist = watchlist  # symbol -> addr (lowercase)
        self.addr_to_symbol: Dict[str, str] = {v.lower(): k for k, v in watchlist.items()}
        self.labels = exchange_labels
        self.prices = price_cache_ref
        self.decimals_map = decimals_map
        self.event_count = 0
        self.last_reconnect = 0.0
        self._ws: Any = None

    async def connect(self):
        """Open WebSocket connection."""
        self._ws = await websockets.connect(self.rpc_url, max_size=10_485_760, ping_interval=30)
        print(f"[{self.chain_name}] WebSocket connected")

    async def subscribe(self):
        """Subscribe to event logs for all watchlist tokens."""
        if not self._ws:
            return
        # Collect all addresses
        addresses = [addr.lower() for addr in self.watchlist.values()]
        
        # eth_subscribe with multi-address filter (publicnode.com supports this)
        # Max ~100 addresses per subscription to avoid oversized payload
        BATCH = 100
        for i in range(0, len(addresses), BATCH):
            batch_addrs = addresses[i:i + BATCH]
            request = {
                "jsonrpc": "2.0",
                "id": i // BATCH + 1,
                "method": "eth_subscribe",
                "params": ["logs", {
                    "address": batch_addrs,
                    "topics": [TRANSFER_TOPIC]
                }],
            }
            await self._ws.send(json.dumps(request))
        print(f"[{self.chain_name}] Subscribed to {len(addresses)} token addresses")

    async def listen(self):
        """Listen for incoming transfer events."""
        if not self._ws:
            return

        await self.subscribe()

        while True:
            try:
                msg = await asyncio.wait_for(self._ws.recv(), timeout=60)
                data = json.loads(msg)

                # Subscription confirmation
                if "id" in data:
                    continue

                # New event
                params = data.get("params", {})
                result = params.get("result", {})

                if not result:
                    continue

                tx_hash = result.get("transactionHash", "0x")
                address = result.get("address", "").lower()
                topics = result.get("topics", [])
                data_hex = result.get("data", "0x")

                if len(topics) < 3:
                    continue  # not a Transfer event

                from_addr = "0x" + topics[1][26:] if len(topics[1]) > 26 else topics[1]
                to_addr = "0x" + topics[2][26:] if len(topics[2]) > 26 else topics[2]
                value_hex = data_hex

                # Lookup token
                symbol = self.addr_to_symbol.get(address, "UNKNOWN")

                # Get decimals for this symbol on this chain
                decimals = self.decimals_map.get(symbol, 18)

                # Parse value (ERC-20 Transfer value is in data field, uint256 hex)
                try:
                    value_int = int(value_hex, 16)
                except (ValueError, TypeError):
                    continue

                # Skip small transfers
                # ponytail: we need price here but can estimate from cached prices
                price = self.prices.get(symbol, 0.0)
                if price <= 0:
                    continue  # can't evaluate without price

                value_usd = value_int * price / (10 ** decimals)  # use actual decimals

                if value_usd < WHALE_THRESHOLDS["small"]:
                    continue

                # Classify
                event_type, bias, from_label, to_label = classify_event(
                    from_addr, to_addr, self.labels,
                )

                tier = get_threshold_tier(value_usd)
                self.event_count += 1

                event = {
                    "chain": self.chain_name,
                    "symbol": symbol,
                    "event_type": event_type,
                    "tier": tier,
                    "value_usd": round(value_usd, 2),
                    "from_addr": from_addr,
                    "to_addr": to_addr,
                    "from_label": from_label,
                    "to_label": to_label,
                    "bias": bias,
                    "confidence_score": min(100, int(50 + value_usd / WHALE_THRESHOLDS["extreme"] * 50)),
                    "risk_level": "Low" if event_type == "EXCHANGE_WITHDRAWAL" else ("Medium" if event_type == "WHALE_TRANSFER" else "High"),
                    "reason": self._build_reason(event_type, symbol, value_usd, bias, from_label, to_label),
                    "tx_hash": tx_hash,
                    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "block_number": result.get("blockNumber", "0x0"),
                }

                self._save_event(event)
                print(f"  [{self.chain_name}] {event['event_type']} {symbol} {format_value(value_usd)} → {bias}")

            except asyncio.TimeoutError:
                # Heartbeat ping
                if self._ws:
                    try:
                        await self._ws.send(json.dumps({
                            "jsonrpc": "2.0",
                            "id": 9999,
                            "method": "eth_blockNumber",
                            "params": [],
                        }))
                    except Exception:
                        break
            except websockets.exceptions.ConnectionClosed:
                print(f"[{self.chain_name}] Connection closed, reconnecting in 5s...")
                await asyncio.sleep(5)
                break
            except Exception as e:
                print(f"[{self.chain_name}] Error: {e}")
                traceback.print_exc()
                break

    def _build_reason(
        self, event_type: str, symbol: str, value_usd: float,
        bias: str, from_label: str, to_label: str,
    ) -> str:
        """Build a short human-readable reason string."""
        v = format_value(value_usd)
        if event_type == "EXCHANGE_WITHDRAWAL":
            return f"Large withdrawal {v} from {from_label}"
        if event_type == "EXCHANGE_DEPOSIT":
            return f"Large deposit {v} to {to_label}"
        if event_type == "WHALE_TRANSFER":
            if from_label and to_label:
                return f"Transfer {v} from {from_label} to {to_label}"
            return f"Whale transfer {v}"
        return f"{event_type} {v}"

    def _save_event(self, event: Dict[str, Any]):
        """Save event to JSONL file + latest per chain/symbol (atomic write)."""
        # 1. Append to chain events JSONL
        path = BASE_DIR / f"{self.chain_key}_events.jsonl"
        with open(path, "a") as f:
            f.write(json.dumps(event) + "\n")
        
        # 2. Save latest per chain+symbol (atomic: temp + rename)
        latest_name = f"latest_whale_{self.chain_key}_{event['symbol']}.json"
        latest_path = BASE_DIR / latest_name
        tmp_path = latest_path.with_suffix(latest_path.suffix + ".tmp")
        
        # Add age_weight if not present (will be computed by engine, but include for completeness)
        if "age_weight" not in event:
            event["age_weight"] = "NONE"
        
        with open(tmp_path, "w") as f:
            json.dump(event, f, default=str)
        tmp_path.rename(latest_path)

    async def run(self):
        """Main loop: connect, listen, reconnect on disconnect."""
        while True:
            try:
                await self.connect()
                await self.listen()
            except Exception as e:
                print(f"[{self.chain_name}] Worker error: {e}")
                traceback.print_exc()
                await asyncio.sleep(5)


# ── Orchestrator ────────────────────────────────────────────────

class WhaleScannerOrchestrator:
    """Manages all chain workers."""

    def __init__(self):
        self.workers: List[ChainWorker] = []
        self.price_cache: Dict[str, float] = {}
        self.start_time = time.time()
        self.total_events = 0

    def build_watchlists(self) -> Dict[str, Dict[str, str]]:
        """
        Build per-chain watchlist from token_chain_map.json.
        Returns: chain_key -> {symbol: contract_address}
        Also builds chain -> {symbol: decimals} for value conversion.
        """
        token_map = load_token_map()
        watchlists: Dict[str, Dict[str, str]] = {}
        decimals_map: Dict[str, Dict[str, int]] = {}

        for sym, info in token_map.items():
            chains = info.get("chains", {})
            decimals = info.get("decimals", 18)
            for chain_key, contract_addr in chains.items():
                if chain_key not in RPC_ENDPOINTS:
                    continue
                if contract_addr and contract_addr != "0x":
                    if chain_key not in watchlists:
                        watchlists[chain_key] = {}
                        decimals_map[chain_key] = {}
                    watchlists[chain_key][sym] = contract_addr
                    decimals_map[chain_key][sym] = decimals

        print(f"\nWatchlists by chain:")
        for chain, tokens in sorted(watchlists.items(), key=lambda x: -len(x[1])):
            print(f"  {CHAIN_NAMES.get(chain, chain):20s} → {len(tokens)} tokens")
        print(f"  Total watchlist: {sum(len(v) for v in watchlists.values())} entries")

        # Store decimals map for value conversion
        self.decimals_map = decimals_map

        return watchlists

    def refresh_prices(self, symbols: List[str]):
        """Update price cache for all watchlist tokens."""
        prices = get_prices(symbols)
        self.price_cache.update(prices)
        print(f"[PRICES] Cached {len(prices)}/{len(symbols)} prices")
    
    def write_health_check(self):
        """Write health check file for monitoring."""
        health = {
            "status": "running",
            "uptime_seconds": int(time.time() - self.start_time),
            "workers": len(self.workers),
            "total_events": sum(w.event_count for w in self.workers),
            "price_cache_size": len(self.price_cache),
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        health_path = BASE_DIR / "whalescanner_health.json"
        with open(health_path, "w") as f:
            json.dump(health, f, indent=2)

    async def run(self, once: bool = False):
        """Start all chain workers."""
        watchlists = self.build_watchlists()
        if not watchlists:
            print("[ERROR] No watchlists built. Run token_chain_scanner.py first.")
            return

        # Load exchange labels
        labels = load_exchange_labels()
        print(f"[LABELS] Loaded {len(labels)} exchange wallet labels")

        # Collect all symbols for price fetch
        all_symbols = set()
        for tokens in watchlists.values():
            all_symbols.update(tokens.keys())
        all_symbols.discard("UNKNOWN")

        # Initial price refresh
        self.refresh_prices(list(all_symbols))

        # Create workers
        for chain_key, tokens in watchlists.items():
            if not tokens:
                continue
            rpc = RPC_ENDPOINTS.get(chain_key)
            if not rpc:
                print(f"[SKIP] {chain_key}: no RPC endpoint configured")
                continue

            # Build decimals map for this chain from token_map
            token_map = load_token_map()
            decimals_map = {}
            for sym in tokens.keys():
                info = token_map.get(sym, {})
                decimals_map[sym] = info.get("decimals", 18)

            worker = ChainWorker(
                chain_key=chain_key,
                rpc_url=rpc,
                chain_name=CHAIN_NAMES.get(chain_key, chain_key),
                watchlist=tokens,
                exchange_labels=labels,
                price_cache_ref=self.price_cache,
                decimals_map=decimals_map,
            )
            self.workers.append(worker)

        if not self.workers:
            print("[ERROR] No workers created. Check RPC endpoints.")
            return

        print(f"\nStarting {len(self.workers)} chain workers...")
        print("=" * 60)

        if once:
            # One-shot: connect, listen for 30s, disconnect
            tasks = [asyncio.create_task(w.run()) for w in self.workers]
            await asyncio.sleep(30)
            for t in tasks:
                t.cancel()
        else:
            # Continuous run with periodic tasks
            tasks = [asyncio.create_task(w.run()) for w in self.workers]
            
            # Background tasks: price refresh every 5 minutes, health check every 60s
            async def periodic_refresh():
                while True:
                    await asyncio.sleep(300)  # 5 minutes
                    try:
                        self.refresh_prices(list(all_symbols))
                    except Exception as e:
                        print(f"[ERROR] Price refresh failed: {e}")
            
            async def periodic_health():
                while True:
                    await asyncio.sleep(60)  # 1 minute
                    try:
                        self.write_health_check()
                    except Exception as e:
                        print(f"[ERROR] Health check write failed: {e}")
            
            tasks.append(asyncio.create_task(periodic_refresh()))
            tasks.append(asyncio.create_task(periodic_health()))
            
            await asyncio.gather(*tasks)


# ── Main ────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Multi-chain whale transfer scanner")
    parser.add_argument("--once", action="store_true", help="Single scan (30s)")
    args = parser.parse_args()

    orchestrator = WhaleScannerOrchestrator()

    try:
        asyncio.run(orchestrator.run(once=args.once))
    except KeyboardInterrupt:
        print("\n[SHUTDOWN] Stopping all workers...")


if __name__ == "__main__":
    # F-14: websockets already imported at module level
    main()