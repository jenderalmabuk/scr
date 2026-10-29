"""
Shadow CVD & 30d Taker Flow Accumulation Module (rebalancedbot / ranker.py integration)
+ Whale Flush Liquidation Shield.

Fetches 30-day klines from Binance Futures and calculates CVD & Momentum from taker flow:
    Net Flow = 2 * Taker_Buy_Volume - Total_Volume
    CVD Ratio = sum(Net Flow) / (sum(|Net Flow|) + 1e-9) in [-1.0, +1.0]

Verdicts:
  - ACCUMULATION: cvd_7d >= +0.05 or (cvd_30d >= +0.05 and cvd_7d >= -0.10)
  - DISTRIBUTION: cvd_7d <= -0.30 or (cvd_30d <= -0.30 and cvd_7d < 0)
  - NEUTRAL: other
  - EXEMPT: unlisted on Binance Futures / Bybit only
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

_cvd_cache: Dict[str, Dict[str, Any]] = {}

def get_symbol_shadow_cvd(symbol: str, ttl_sec: int = 600) -> Dict[str, Any]:
    """Fetch 30-day klines from Binance Futures and calculate Shadow CVD & Momentum.
    Caches results in memory for ttl_sec (default 10 minutes).
    Fallback to Bybit / Exempt if unlisted on Binance Futures.
    """
    if not symbol:
        return {"status": "ERROR", "symbol": "", "verdict": "EMPTY_SYMBOL"}

    clean = symbol.strip().upper().replace("/", "").replace("-", "")
    if not clean.endswith("USDT") and not clean.endswith("USDC"):
        clean = f"{clean}USDT"

    now_ts = time.time()
    cached = _cvd_cache.get(clean)
    if cached and (now_ts - cached.get("cached_at", 0)) < ttl_sec:
        return cached["data"]

    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={clean}&interval=1d&limit=31"
    req = urllib.request.Request(url, headers={"User-Agent": "nexus-shadow-cvd/1.0"})

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if not data or len(data) < 5:
                res = {
                    "status": "EXEMPT",
                    "source": "binance_fapi",
                    "symbol": clean,
                    "verdict": "EXEMPT_SHORT_HISTORY",
                    "cvd_7d": None,
                    "cvd_30d": None,
                    "mom_30d": None,
                }
            else:
                flow_30 = [2.0 * float(bar[9]) - float(bar[5]) for bar in data]
                cvd_30 = sum(flow_30) / (sum(abs(f) for f in flow_30) + 1e-9)

                flow_7 = flow_30[-7:]
                cvd_7 = sum(flow_7) / (sum(abs(f) for f in flow_7) + 1e-9)

                mom_30 = float(data[-1][4]) / float(data[0][4]) - 1.0

                if cvd_7 >= 0.05 or (cvd_30 >= 0.05 and cvd_7 >= -0.10):
                    verdict = "ACCUMULATION"
                elif cvd_7 <= -0.30 or (cvd_30 <= -0.30 and cvd_7 < 0):
                    verdict = "DISTRIBUTION"
                else:
                    verdict = "NEUTRAL"

                res = {
                    "status": "ACTIVE",
                    "source": "binance_fapi",
                    "symbol": clean,
                    "verdict": verdict,
                    "cvd_7d": round(cvd_7, 4),
                    "cvd_30d": round(cvd_30, 4),
                    "mom_30d": round(mom_30, 4),
                }
    except urllib.error.HTTPError as he:
        if he.code == 400:
            res = {
                "status": "EXEMPT",
                "source": "bybit_only",
                "symbol": clean,
                "verdict": "EXEMPT_BYBIT_ONLY",
                "cvd_7d": None,
                "cvd_30d": None,
                "mom_30d": None,
            }
        else:
            res = {
                "status": "ERROR",
                "source": "binance_fapi",
                "symbol": clean,
                "verdict": f"BYPASS_HTTP_{he.code}",
                "cvd_7d": None,
                "cvd_30d": None,
                "mom_30d": None,
            }
    except Exception as e:
        logger.debug("Failed to fetch shadow CVD for %s: %s", clean, e)
        res = {
            "status": "ERROR",
            "source": "binance_fapi",
            "symbol": clean,
            "verdict": "BYPASS_EXCEPTION",
            "cvd_7d": None,
            "cvd_30d": None,
            "mom_30d": None,
        }

    _cvd_cache[clean] = {"data": res, "cached_at": now_ts}
    return res


def check_whale_flush_shield(
    symbol: str, is_long: bool, min_usd: float = 2_000_000.0, max_age_min: int = 20
) -> Tuple[bool, Optional[str]]:
    """Check if ongoing massive liquidation cascade opposes the trade.
    Returns (is_blocked, reason).
    """
    if not symbol:
        return False, None

    clean = symbol.strip().upper().replace("/", "").replace("-", "")
    if not clean.endswith("USDT") and not clean.endswith("USDC"):
        clean = f"{clean}USDT"

    paths_to_check = [
        Path("/app/runtime/whales") / f"latest_cex_whale_{clean}.json",
        Path("/opt/signalcopyreal/runtime/whales") / f"latest_cex_whale_{clean}.json",
        Path("/home/fusion_omega/fusion_omega_nexus/runtime/whales") / f"latest_cex_whale_{clean}.json",
    ]

    for p in paths_to_check:
        try:
            if not p.exists():
                continue
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)

            ts_str = data.get("timestamp_utc", "")
            if not ts_str:
                continue

            dt = datetime.fromisoformat(ts_str)
            age_sec = (datetime.now(timezone.utc) - dt).total_seconds()
            if age_sec > (max_age_min * 60):
                continue

            w_type = str(data.get("type", "")).upper()
            w_bias = str(data.get("bias", "")).upper()
            val_usd = float(data.get("value_usd", 0.0) or 0.0)
            exchange = data.get("exchange", "CEX")

            # Check liquidation cascade against trade
            if w_type == "LIQUIDATION" and val_usd >= min_usd:
                # If LONG, opposed by BEARISH liquidation cascade (longs getting wiped)
                if is_long and w_bias in ("BEARISH", "SELL"):
                    age_min = round(age_sec / 60.0, 1)
                    return True, f"Massive Long Liquidation Cascade (${val_usd:,.0f} {exchange}) detected {age_min}m ago"
                # If SHORT, opposed by BULLISH liquidation cascade (shorts getting squeezed)
                elif (not is_long) and w_bias in ("BULLISH", "BUY"):
                    age_min = round(age_sec / 60.0, 1)
                    return True, f"Massive Short Liquidation Cascade (${val_usd:,.0f} {exchange}) detected {age_min}m ago"
        except Exception as exc:
            logger.debug("Failed checking whale flush shield for %s: %s", clean, exc)

    return False, None
