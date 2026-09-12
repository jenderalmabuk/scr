"""
NexusDataBridge — data layer for signal-copy pipeline.

Replaces AdvancedDataEngine (956 lines of Binance REST calls) with a lightweight
adapter that reads Nexus scanner cache (OHLCV parquet + OI JSON).

Interface compatibility: drop-in replacement for AdvancedDataEngine.get_advanced_metrics()
"""
from __future__ import annotations

import glob
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp
import pandas as pd
import numpy as np

# Max scans to keep in memory for OI delta calculation
_HISTORY_SCAN_COUNT = 90  # 90 scans = ~90 min at 60s intervals


def _canonical_supported(symbol: str) -> bool:
    paths = [
        Path("/app/runtime/revo/canonical_universe.json"),
        Path(__file__).resolve().parents[1] / "runtime" / "revo" / "canonical_universe.json",
    ]
    for path in paths:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
            pairs = data.get("pairs", data)
            return symbol.upper() in {str(p).upper() for p in pairs}
        except Exception:
            continue
    return True


def price_metadata(source: str, age_sec: float) -> Dict[str, Any]:
    """Execution-safe price provenance."""
    source = str(source or "unknown")
    age = max(0.0, float(age_sec or 0.0))
    return {
        "price_source": source,
        "price_age_sec": age,
        "price_fresh": source in {"binance_rest", "bybit_api"} or age <= 1800.0,
    }


class NexusDataBridge:
    """
    Lightweight data adapter: reads scanner cache, computes derived metrics.
    
    Replaces 956-line AdvancedDataEngine with ~50 lines of cache reads.
    """

    def __init__(self, cache_dir: str = None, api_url: str = None):
        # Default to runtime/whales inside container (mounted from host)
        self.cache_dir = Path(cache_dir or Path(__file__).parent.parent / "runtime" / "whales")
        if not self.cache_dir.exists():
            # Fallback: try /app/data (legacy)
            fallback = Path("/app/data")
            if fallback.exists():
                self.cache_dir = fallback
            else:
                # Try the host's data directory
                host_data = Path(__file__).parent.parent / "data"
                if host_data.exists():
                    self.cache_dir = host_data
                else:
                    raise FileNotFoundError(f"Scanner cache dir not found: {self.cache_dir}")

        # FastAPI URL for klines (reads from TimescaleDB)
        # Try fastapi hostname first (works in container), fallback to localhost (works on host)
        default_api = os.getenv("NEXUS_API_URL", "http://localhost:8000")
        self.api_url = api_url or default_api

        # Cache latest scan metadata in memory (TTL 60s)
        self._scan_cache: Optional[Dict] = None
        self._scan_cache_ts: float = 0.0
        self._scan_cache_ttl: float = 60.0

        # Historical OI snapshots for delta calc (lazy-loaded)
        self._oi_history: Optional[Dict[str, Dict[int, float]]] = None
        self._flow_cache: Optional[Dict[str, Dict[str, Any]]] = None
        self._flow_cache_ts: float = 0.0
        self._flow_cache_ttl: float = 30.0

    def _load_revo_flow_context(self) -> Dict[str, Dict[str, Any]]:
        """Load fresh Revo scanner flow context keyed by plain symbol."""
        now = time.time()
        if self._flow_cache and (now - self._flow_cache_ts) < self._flow_cache_ttl:
            return self._flow_cache

        candidates = [
            Path("/app/runtime/revo/revo_flow_context_canonical.json"),
            Path(__file__).parent.parent / "runtime" / "revo" / "revo_flow_context_canonical.json",
            Path("/app/runtime/revo/revo_flow_context.json"),
            Path(__file__).parent.parent / "runtime" / "revo" / "revo_flow_context.json",
        ]
        for path in candidates:
            if not path.exists():
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            out: Dict[str, Dict[str, Any]] = {}
            rows = raw.get("pairs", raw) if isinstance(raw, dict) else {}
            if isinstance(rows, dict):
                for key, row in rows.items():
                    if isinstance(row, dict):
                        symbol = row.get("symbol") or key
                        if symbol:
                            out[str(symbol).upper()] = row
            if not out:
                continue
            self._flow_cache = out
            self._flow_cache_ts = now
            return out
        self._flow_cache = {}
        self._flow_cache_ts = now
        return self._flow_cache

    async def _fetch_flow_on_demand(self, symbol: str) -> Dict[str, Any]:
        """Fetch fresh symbol flow when batch artifact does not cover it."""
        url = f"{self.api_url}/flow/{symbol.upper()}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, params={"exchange": "bybit"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        return {"flow_lookup_status": f"HTTP_{resp.status}", "flow_symbol_found": False}
                    row = await resp.json()
        except Exception as exc:
            return {"flow_lookup_status": f"FETCH_ERROR:{type(exc).__name__}", "flow_symbol_found": False}
        ready = bool(row.get("data_ready")) and not bool(row.get("data_stale"))
        row["flow_lookup_status"] = "ON_DEMAND_OK" if ready else "ON_DEMAND_NOT_READY"
        row["flow_symbol_found"] = ready
        row["flow_lookup_source"] = "nexus_api_bybit"
        return row

    def _load_oi_5m_raw(self, symbol: str) -> Dict[str, Any]:
        """Derive OI 5m/15m/1h from sidecar raw 5m Bybit snapshots when available.

        If the shared snapshot universe does not cover the symbol, fall back to a
        direct Bybit public fetch for this one symbol so signal-copy can still
        classify the trade.
        """
        symbol = symbol.upper()
        paths = [
            Path("/app/runtime/revo/oi_5m_raw_bybit.jsonl"),
            Path(__file__).parent.parent / "runtime" / "revo" / "oi_5m_raw_bybit.jsonl",
        ]
        for path in paths:
            if not path.exists():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()[-20:]
            except Exception:
                continue
            rows = []
            for line in lines:
                try:
                    snap = json.loads(line)
                    rec = (snap.get("records") or {}).get(symbol)
                    if rec and rec.get("oi_value"):
                        rows.append((snap.get("ts"), float(rec["oi_value"])))
                except Exception:
                    continue
            if rows:
                latest_ts, latest_val = rows[-1]
                out: Dict[str, Any] = {"oi_now_5m_raw": latest_val, "oi_5m_raw_ts": latest_ts}
                for key, back in (("oi_change_5m_pct", 1), ("oi_change_15m_pct", 3), ("oi_change_1h_pct", 12)):
                    if len(rows) > back:
                        old_val = rows[-1 - back][1]
                        if old_val > 0:
                            out[key] = round(((latest_val - old_val) / old_val) * 100.0, 4)
                if any(k in out for k in ("oi_change_5m_pct", "oi_change_15m_pct", "oi_change_1h_pct")):
                    out["oi_source"] = "bybit_raw_5m"
                return out

        if not _canonical_supported(symbol):
            return {"oi_source": "unsupported_canonical"}

        try:
            import requests
            url = "https://api.bybit.com/v5/market/open-interest"
            params = {"category": "linear", "symbol": symbol, "intervalTime": "5min", "limit": 4}
            resp = requests.get(url, params=params, timeout=8)
            resp.raise_for_status()
            data = resp.json()
            rows = (data.get("result", {}) or {}).get("list", []) or []
            vals = []
            for row in rows:
                ts = row.get("timestamp") or row.get("time") or row.get("ts")
                oi = row.get("openInterest") or row.get("oi")
                if oi is None:
                    continue
                try:
                    vals.append((str(ts), float(oi)))
                except Exception:
                    continue
            if not vals:
                return {}
            latest_ts, latest_val = vals[-1]
            out = {"oi_now_5m_raw": latest_val, "oi_5m_raw_ts": latest_ts, "oi_source": "bybit_direct_5m"}
            for key, back in (("oi_change_5m_pct", 1), ("oi_change_15m_pct", 3), ("oi_change_1h_pct", 12)):
                if len(vals) > back:
                    old_val = vals[-1 - back][1]
                    if old_val > 0:
                        out[key] = round(((latest_val - old_val) / old_val) * 100.0, 4)
            return out
        except Exception:
            return {}

    async def _fetch_oi_15m_db(self, symbol: str, limit: int = 5) -> Dict[str, Any]:
        """Fetch Bybit 15m OI rows from FastAPI; derive 1h when enough rows exist."""
        try:
            url = f"{self.api_url}/oi/bybit/{symbol}"
            params = {"tf": "15m", "limit": limit}
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status != 200:
                        return {}
                    payload = await resp.json()
        except Exception:
            return {}

        rows = payload.get("data") or []
        if not rows:
            return {}
        latest = rows[-1]
        out: Dict[str, Any] = {
            "oi_now": latest.get("oi_value"),
            "oi_change_15m_pct": latest.get("oi_delta_pct"),
            "oi_15m_ts": latest.get("timestamp"),
            "oi_source": "bybit_db_15m",
        }
        if len(rows) >= 5:
            old = rows[-5]
            old_val = float(old.get("oi_value") or 0.0)
            now_val = float(latest.get("oi_value") or 0.0)
            if old_val > 0:
                out["oi_change_1h_pct"] = round(((now_val - old_val) / old_val) * 100.0, 4)
                out["oi_1h_derived"] = True
        return out

    def _load_latest_scan(self) -> Dict:
        """Load latest scanner run metadata (OI + timestamp)."""
        now = time.time()
        if self._scan_cache and (now - self._scan_cache_ts) < self._scan_cache_ttl:
            return self._scan_cache

        # Find most recent scan JSON — try cache_dir, then data/
        jsons = sorted(glob.glob(str(self.cache_dir / "latest_scan_*.json")), reverse=True)
        if not jsons:
            # Fallback: data/ directory (host data)
            data_dir = Path(__file__).parent.parent / "data"
            jsons = sorted(glob.glob(str(data_dir / "latest_scan_*.json")), reverse=True)
        if not jsons:
            return {}

        with open(jsons[0], "r") as f:
            data = json.load(f)

        self._scan_cache = data
        self._scan_cache_ts = now
        return data

    async def _fetch_klines_from_api(self, symbol: str, tf: str, limit: int = 100) -> Optional[pd.DataFrame]:
        """Fetch OHLCV from FastAPI (TimescaleDB)."""
        try:
            url = f"{self.api_url}/klines/binance/{symbol}"
            params = {"tf": tf, "limit": limit}
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("data"):
                            df = pd.DataFrame(data["data"])
                            # Rename columns to match expected format
                            df = df.rename(columns={
                                "open_time": "timestamp",
                                "open": "open",
                                "high": "high",
                                "low": "low",
                                "close": "close",
                                "volume": "volume",
                            })
                            return df.tail(limit).copy()
        except Exception as e:
            pass
        return None

    def _load_klines(self, symbol: str, tf: str, limit: int = 100) -> Optional[pd.DataFrame]:
        """Load OHLCV from parquet cache or FastAPI."""
        # Try parquet cache first (runtime/whales/tf)
        tf_dir = self.cache_dir / tf
        if tf_dir.exists():
            parquets = sorted(glob.glob(str(tf_dir / f"{symbol}_*.parquet")), reverse=True)
            if parquets:
                df = pd.read_parquet(parquets[0])
                if not df.empty:
                    return df.tail(limit).copy()
        # Fallback: try data/tf (host data directory)
        host_tf_dir = Path(__file__).parent.parent / "data" / tf
        if host_tf_dir.exists():
            parquets = sorted(glob.glob(str(host_tf_dir / f"{symbol}_*.parquet")), reverse=True)
            if parquets:
                df = pd.read_parquet(parquets[0])
                if not df.empty:
                    return df.tail(limit).copy()
        # Fallback to FastAPI (runs in async context via get_advanced_metrics)
        return None

    def _calc_rsi(self, closes: pd.Series, period: int = 14) -> float:
        """Simple RSI calculation."""
        if len(closes) < period + 1:
            return 50.0

        delta = closes.diff()
        gain = delta.where(delta > 0, 0.0).rolling(window=period).mean()
        loss = -delta.where(delta < 0, 0.0).rolling(window=period).mean()

        rs = gain / loss.replace(0, 1e-10)
        rsi = 100 - (100 / (1 + rs))
        return float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50.0

    def _calc_cvd_proxy(self, df: pd.DataFrame, source: str = "nexus_cache") -> Dict[str, float]:
        """Proxy CVD z-score from volume distribution. Returns dict with zscore and source tag."""
        if df.empty or len(df) < 20:
            return {"cvd_zscore": 0.0, "cvd_source": source}

        df = df.copy()
        df['directional_vol'] = df['volume'] * np.where(df['close'] > df['open'], 1, -1)
        cvd = df['directional_vol'].cumsum()

        mean = cvd.tail(20).mean()
        std = cvd.tail(20).std()
        if std == 0:
            return {"cvd_zscore": 0.0, "cvd_source": source}

        z = (cvd.iloc[-1] - mean) / std
        return {"cvd_zscore": float(z), "cvd_source": source}

    def _build_oi_history(self) -> Dict[str, Dict[int, float]]:
        """Build in-memory OI history from last N scans for all symbols."""
        if self._oi_history is not None:
            return self._oi_history

        jsons = sorted(glob.glob(str(self.cache_dir / "latest_scan_*.json")))
        if not jsons:
            data_dir = Path(__file__).parent.parent / "data"
            jsons = sorted(glob.glob(str(data_dir / "latest_scan_*.json")))
        if len(jsons) < 2:
            self._oi_history = {}
            return self._oi_history

        # Use last ~90 scans (at 60s intervals = 90 min history)
        recent = jsons[-_HISTORY_SCAN_COUNT:]
        history: Dict[str, Dict[int, float]] = {}

        for path in recent:
            try:
                ts = int(path.split("_")[-1].replace(".json", ""))
                with open(path, "r") as f:
                    data = json.load(f)
                oi_map = data.get("oi", {})
                for sym, exchs in oi_map.items():
                    # Keep one exchange basis stable; summing Binance+Bybit when one
                    # intermittently disappears creates fake OI jumps.
                    total_oi = exchs.get("bybit") if exchs.get("bybit") is not None else exchs.get("binance")
                    if total_oi is None:
                        continue
                    total_oi = float(total_oi)
                    if sym not in history:
                        history[sym] = {}
                    history[sym][ts] = total_oi
            except (json.JSONDecodeError, ValueError, KeyError):
                continue

        self._oi_history = history
        return history

    def _calc_oi_changes(self, symbol: str, scan_data: Dict) -> Dict[str, float]:
        """Calculate OI changes from historical scans.

        Uses in-memory OI history built from last ~90 scanner snapshots.
        Returns 0.0 deltas when no history is available.
        """
        history = self._build_oi_history()
        sym_history = history.get(symbol, {})
        if len(sym_history) < 2:
            return {"oi_change_5m_pct": 0.0, "oi_change_15m_pct": 0.0, "oi_change_1h_pct": 0.0}

        # Current OI from latest scan — same stable basis as history.
        exchs = scan_data.get("oi", {}).get(symbol, {})
        oi_now_raw = exchs.get("bybit") if exchs.get("bybit") is not None else exchs.get("binance")
        oi_now = float(oi_now_raw or 0.0)
        if oi_now <= 0:
            return {"oi_change_5m_pct": 0.0, "oi_change_15m_pct": 0.0, "oi_change_1h_pct": 0.0}

        timestamps = sorted(sym_history.keys())
        latest_ts = timestamps[-1]

        # Find closest snapshot to 5m/15m/1h ago (±2 min)
        target_5m = latest_ts - 300
        target_15m = latest_ts - 900
        target_1h = latest_ts - 3600

        def _closest(target: int) -> Optional[int]:
            best = None
            best_diff = float("inf")
            for ts in timestamps:
                diff = abs(ts - target)
                if diff < best_diff and diff < 120:  # within 2 min tolerance
                    best_diff = diff
                    best = ts
            return best

        ts_5m = _closest(target_5m)
        ts_15m = _closest(target_15m)
        ts_1h = _closest(target_1h)

        def _pct(ts: Optional[int]) -> float:
            if not ts:
                return 0.0
            oi_old = sym_history[ts]
            if oi_old <= 0:
                return 0.0
            return ((oi_now - oi_old) / oi_old) * 100.0

        return {
            "oi_change_5m_pct": round(_pct(ts_5m), 4),
            "oi_change_15m_pct": round(_pct(ts_15m), 4),
            "oi_change_1h_pct": round(_pct(ts_1h), 4),
        }

    async def get_advanced_metrics(self, symbol: str) -> Dict[str, Any]:
        """Drop-in replacement for AdvancedDataEngine.get_advanced_metrics()."""
        scan_data = self._load_latest_scan()

        # Load 5m klines (most recent bars for price + RSI) - try cache first, then API
        df_5m = self._load_klines(symbol, "5m", limit=100)
        if df_5m is None or df_5m.empty:
            df_5m = await self._fetch_klines_from_api(symbol, "5m", limit=100)
        if df_5m is None or df_5m.empty:
            return {
                "symbol": symbol,
                "price": 0.0,
                "source": "nexus_cache",
                "cache_age_sec": 0.0,
            }

        # Price provenance is mandatory: validation may use stale enrichment,
        # execution must require price_fresh.
        price_source, price_age_sec = "nexus_cache", 0.0
        if 'timestamp' in df_5m.columns or 'open_time' in df_5m.columns:
            ts_col = 'timestamp' if 'timestamp' in df_5m.columns else 'open_time'
            last_ts = pd.to_datetime(df_5m[ts_col].iloc[-1])
            age_sec = (pd.Timestamp.now(tz='UTC') - last_ts).total_seconds()
            price_age_sec = max(0.0, age_sec)

            if age_sec > 1800:  # Data older than 30min
                price = None
                # Try Binance REST first
                try:
                    async with aiohttp.ClientSession() as session:
                        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                price = float(data['price'])
                                price_source, price_age_sec = "binance_rest", 0.0
                except Exception:
                    pass
                
                # Fallback to Bybit if Binance failed (symbol not on Binance or other error)
                if price is None:
                    try:
                        df_bybit = await self._fetch_klines_from_api(symbol, "5m", limit=1)
                        if df_bybit is not None and not df_bybit.empty:
                            price = float(df_bybit['close'].iloc[-1])
                            price_source, price_age_sec = "bybit_api", 0.0
                    except Exception:
                        pass
                
                # Last resort is enrichment-only; provenance prevents execution.
                if price is None:
                    price = float(df_5m['close'].iloc[-1])
                    price_source = "stale_cache"
            else:
                price = float(df_5m['close'].iloc[-1])
        else:
            price = float(df_5m['close'].iloc[-1])
        rsi = self._calc_rsi(df_5m['close'], period=14)
        cvd_result = self._calc_cvd_proxy(df_5m, source="nexus_cache")
        cvd_zscore = cvd_result["cvd_zscore"]
        cvd_source = cvd_result["cvd_source"]
        imbalance = cvd_zscore / 3.0

        # Prefer batch scanner flow; fetch symbol directly when batch misses it.
        flow_row = self._load_revo_flow_context().get(symbol.upper(), {})
        if flow_row:
            flow_row = dict(flow_row)
            flow_row.setdefault("flow_lookup_status", "BATCH_OK")
            flow_row.setdefault("flow_symbol_found", True)
            flow_row.setdefault("flow_lookup_source", "scanner_batch")
        else:
            flow_row = await self._fetch_flow_on_demand(symbol)
        oi_metrics = {
            "oi_change_5m_pct": None,
            "oi_change_15m_pct": flow_row.get("oi_delta_pct_15m"),
            "oi_change_1h_pct": None,
            "oi_source": "revo_flow_context_15m" if flow_row else "missing",
        }
        db_oi = await self._fetch_oi_15m_db(symbol.upper(), limit=5)
        oi_metrics.update({k: v for k, v in db_oi.items() if v is not None})
        raw_oi = self._load_oi_5m_raw(symbol.upper())
        oi_metrics.update({k: v for k, v in raw_oi.items() if v is not None})
        if oi_metrics.get("oi_change_15m_pct") is None:
            oi_metrics.update(self._calc_oi_changes(symbol, scan_data))

        # Price change 15m
        if len(df_5m) >= 4:
            p_15m_ago = float(df_5m['close'].iloc[-4])
            price_change_15m_pct = ((price - p_15m_ago) / p_15m_ago) * 100.0
        else:
            price_change_15m_pct = 0.0

        # Regime detection
        if len(df_5m) >= 20:
            df_5m['hl'] = df_5m['high'] - df_5m['low']
            atr = df_5m['hl'].tail(20).mean()
            atr_pct = (atr / price) * 100.0

            if atr_pct > 1.5:
                regime = "HIGH_VOL"
            elif abs(price_change_15m_pct) > 0.8:
                regime = "TRENDING"
            else:
                regime = "RANGING"
        else:
            regime = "UNKNOWN"

        # Funding rate: extract from scan if available, else neutral
        funding_rate = float(scan_data.get("funding", {}).get(symbol, 0.0)) or 0.0001

        # Cache age
        scan_ts = scan_data.get("ts", 0.0)
        cache_age_sec = time.time() - scan_ts if scan_ts else 999.0

        if flow_row:
            cvd_zscore = float(flow_row.get("cvd_zscore_15m", cvd_zscore) or 0.0)
            cvd_source = flow_row.get("cvd_source", "revo_flow_context")
            funding_rate = float(flow_row.get("funding_rate", funding_rate) or 0.0)

        return {
            "symbol": symbol,
            "price": price,
            "rsi": rsi,
            "cvd_zscore": cvd_zscore,
            "cvd_source": cvd_source,
            "imbalance": cvd_zscore / 3.0,
            "funding_rate": funding_rate,
            "funding_zscore": flow_row.get("funding_zscore"),
            "price_change_15m_pct": price_change_15m_pct,
            "regime_label": regime,
            "flow_direction": flow_row.get("flow_direction"),
            "qvol_5m": flow_row.get("qvol_5m"),
            "volume_zscore_15m": flow_row.get("volume_zscore_15m"),
            "data_ready": flow_row.get("data_ready"),
            "data_stale": flow_row.get("data_stale"),
            "data_quality": flow_row.get("data_quality"),
            "flow_source": flow_row.get("source"),
            "flow_ts": flow_row.get("ts"),
            "flow_lookup_status": flow_row.get("flow_lookup_status"),
            "flow_symbol_found": bool(flow_row.get("flow_symbol_found")),
            "flow_lookup_source": flow_row.get("flow_lookup_source"),
            "source": "revo_flow_context" if flow_row.get("flow_symbol_found") else "nexus_cache",
            "cache_age_sec": cache_age_sec,
            **price_metadata(price_source, price_age_sec),
            **oi_metrics,
        }


# Singleton for orchestrator injection
_bridge_instance: Optional[NexusDataBridge] = None


def get_data_bridge(cache_dir: str = None) -> NexusDataBridge:
    """Get or create singleton bridge instance."""
    global _bridge_instance
    if _bridge_instance is None:
        _bridge_instance = NexusDataBridge(cache_dir)
    return _bridge_instance
