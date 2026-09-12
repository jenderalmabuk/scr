#!/usr/bin/env python3
"""Nexus scanner — replaces revo scanner loop F2C.

Queries Nexus FastAPI endpoints and writes Revo-compatible JSON files
to a runtime directory. RevoAdaptiveStrategy reads these files unchanged.

Output files (compatible with Revo strategy):
  - revo_flow_context.json      (flow per pair)
  - btc_context_v135.json       (BTC regime)
  - pair_universe_remote.json   (top universe)
  - freqtrade_pairlist.json     (pairlist for Freqtrade)
  - revo_execution_context.json (metadata)
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

NEXUS_API = os.getenv("NEXUS_API_URL", "http://localhost:8000")
RUNTIME_DIR = Path(os.getenv("REVO_RUNTIME_DIR", "./runtime/revo"))
TOP_N = int(os.getenv("REVO_TOP_N", "530"))
MIN_VOLUME = float(os.getenv("REVO_MIN_VOLUME", "600000"))
INTERVAL = int(os.getenv("REVO_SCANNER_INTERVAL", "300"))
FRESH_KLINE_MAX_AGE_SEC = int(os.getenv("REVO_FRESH_KLINE_MAX_AGE_SEC", "900"))
HTTP_TIMEOUT_SEC = int(os.getenv("REVO_SCANNER_HTTP_TIMEOUT_SEC", "120"))


def fetch_json(client: httpx.Client, path: str, **params) -> dict:
    r = client.get(f"{NEXUS_API}{path}", params=params, timeout=HTTP_TIMEOUT_SEC)
    r.raise_for_status()
    return r.json()


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.rename(path)


def ft_pair(symbol: str) -> str:
    if symbol.endswith("USDT"):
        return f"{symbol[:-4]}/USDT:USDT"
    return symbol


def raw_symbol(pair: str) -> str:
    p = str(pair).upper()
    if "/" in p:
        base = p.split("/", 1)[0]
        quote = p.split("/", 1)[1].split(":", 1)[0]
        return base + quote
    return p.replace(":USDT", "")


def has_fresh_kline(client: httpx.Client, symbol: str) -> bool:
    """Fresh on either exchange. Keeps pairlist from carrying stale zombie rows."""
    now = datetime.now(timezone.utc)
    for exchange in ("binance", "bybit"):
        try:
            data = fetch_json(client, f"/klines/{exchange}/{symbol}", tf="5m", limit=1)
            rows = data.get("data") or []
            if not rows:
                continue
            ts = rows[-1].get("open_time") or rows[-1].get("timestamp")
            if not ts:
                continue
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if (now - dt).total_seconds() <= FRESH_KLINE_MAX_AGE_SEC:
                return True
        except Exception:
            continue
    return False


def load_runtime_symbols(rt: Path, names: list[str]) -> list[str]:
    out, seen = [], set()
    for name in names:
        path = rt / name
        try:
            lines = path.read_text().splitlines()
        except Exception:
            continue
        for line in lines:
            sym = raw_symbol(line.strip())
            if sym and sym not in seen:
                seen.add(sym)
                out.append(sym)
    return out


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.rename(path)


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    keys = sorted({k for row in rows for k in row}) if rows else ["empty"]
    with tmp.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)
    tmp.rename(path)


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row, default=str) + "\n")


def flow_permission(rec: dict) -> tuple[str, list[str], list[str]]:
    reasons, blockers = [], []
    if not rec.get("data_ready"):
        blockers.append("NO_FLOW")
    if rec.get("data_stale"):
        blockers.append("STALE_FLOW")
    direction = str(rec.get("flow_direction", "NO_TRADE")).upper()
    if direction in ("LONG_ONLY", "BOTH_ALLOWED"):
        reasons.append(f"FLOW_{direction}")
    else:
        blockers.append("FLOW_HOSTILE")
    if float(rec.get("funding_rate") or 0) >= 0.0003 or float(rec.get("funding_zscore") or 0) >= 2:
        blockers.append("FUNDING_CROWDED_LONG")
    elif float(rec.get("funding_rate") or 0) <= 0:
        reasons.append("FUNDING_CONTRARIAN")
    if float(rec.get("volume_zscore_15m") or 0) >= 1:
        reasons.append("VOLUME_CONFIRM")
    return direction, reasons, blockers


def build_contexts(*, cycle: str, btc: dict, remote: dict, revo_flow: dict) -> tuple[dict, dict, dict, str]:
    now = datetime.now(timezone.utc).isoformat()
    btc_regime = btc.get("btc_regime", "unknown")
    regime_pairs, candidates = {}, {}
    blocker_counts: dict[str, int] = {}
    qvol_by_pair = {p["pair"]: float(p.get("quote_volume") or 0) for p in remote.get("pairs", [])}

    for pair, rec in revo_flow.items():
        direction, reasons, blockers = flow_permission(rec)
        vol_z = float(rec.get("volume_zscore_15m") or 0)
        funding_rate = float(rec.get("funding_rate") or 0)
        funding_z = float(rec.get("funding_zscore") or 0)
        risk_modifier = 1.0 if btc_regime == "risk_on" else 0.75 if btc_regime == "neutral" else 0.5 if btc_regime == "risk_off" else 0.0
        funding_state = "crowded_long" if "FUNDING_CROWDED_LONG" in blockers else "contrarian_long" if funding_rate <= 0 else "neutral"
        vol_state = "hot" if vol_z >= 2 else "normal" if vol_z > -1 else "quiet"
        regime_pairs[pair] = {
            "pair_regime": "flow_uptrend_pullback_watch" if direction in ("LONG_ONLY", "BOTH_ALLOWED") else "hostile",
            "efficiency_ratio_48": None,
            "atr_pct": None,
            "volatility_state": vol_state,
            "funding_state": funding_state,
            "risk_modifier": risk_modifier,
        }
        score = 0
        score += 2 if direction == "LONG_ONLY" else 1 if direction == "BOTH_ALLOWED" else 0
        score += 2 if float(rec.get("cvd_zscore_15m") or 0) > 0 else 0
        score += 1 if float(rec.get("oi_delta_pct_15m") or 0) > 0 else 0
        score += 2 if funding_state == "contrarian_long" else -2 if funding_state == "crowded_long" else 0
        score += 1 if vol_z >= 1 else 0
        score += 1 if btc_regime in ("risk_on", "neutral") else -1 if btc_regime == "risk_off" else -3
        if qvol_by_pair.get(pair, 0) < MIN_VOLUME:
            blockers.append("LOW_VOLUME")
        if btc_regime == "panic":
            blockers.append("BTC_PANIC")
        for b in blockers:
            blocker_counts[b] = blocker_counts.get(b, 0) + 1
        dyn_min = 8 if btc_regime == "risk_on" else 9 if btc_regime == "neutral" else 10
        permission = "ENTRY_READY" if not blockers and score >= dyn_min else "WATCH" if not blockers else "NO_TRADE"
        candidates[pair] = {
            "pair": pair,
            "permission": permission,
            "direction": "LONG",
            "score": score,
            "dynamic_min_score": dyn_min,
            "stake_modifier": risk_modifier,
            "reasons": reasons,
            "blockers": blockers,
        }

    summary = {
        "scanner_in": len(remote.get("pairs", [])),
        "flow_rows": len(revo_flow),
        "flow_long": sum(1 for r in revo_flow.values() if r.get("flow_direction") == "LONG_ONLY"),
        "hybrid_eligible": sum(1 for c in candidates.values() if c["permission"] in ("ENTRY_READY", "WATCH")),
        "entry_ready": sum(1 for c in candidates.values() if c["permission"] == "ENTRY_READY"),
        "watch": sum(1 for c in candidates.values() if c["permission"] == "WATCH"),
        "no_trade": sum(1 for c in candidates.values() if c["permission"] == "NO_TRADE"),
        **{f"blocked_by_{k.lower()}": v for k, v in sorted(blocker_counts.items())},
    }
    regime = {"schema_version": "nexus.revo.regime.v1", "timestamp": now, "btc": btc, "pairs": regime_pairs}
    candidate = {"schema_version": "nexus.revo.candidate.v1", "timestamp": now, "profile": "nexus_blueprint_v1", "pairs": candidates, "summary": summary}
    blocker = {"schema_version": "nexus.revo.blocker.v1", "timestamp": now, "cycle": cycle, "summary": summary, "blockers": blocker_counts}
    txt = "\n".join([
        f"cycle={cycle}", f"btc_regime={btc_regime}", f"scanner_in={summary['scanner_in']}",
        f"flow_rows={summary['flow_rows']}", f"flow_long={summary['flow_long']}",
        f"hybrid_eligible={summary['hybrid_eligible']}", f"entry_ready={summary['entry_ready']}",
        f"watch={summary['watch']}", f"no_trade={summary['no_trade']}",
        "blockers:", *[f"- {k}: {v}" for k, v in sorted(blocker_counts.items(), key=lambda x: x[1], reverse=True)], "",
    ])
    return regime, candidate, blocker, txt


def run_cycle(client: httpx.Client) -> dict:
    cycle = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rt = RUNTIME_DIR
    rt.mkdir(parents=True, exist_ok=True)
    rc = 0

    # Initialize variables to avoid UnboundLocalError
    btc = {"btc_regime": "unknown", "btc_price": 0}
    pairs = []
    ft_pairs = []
    remote = {"pairs": []}
    total = 0
    tradeable = 0

    # Step 1: BTC regime
    try:
        btc = fetch_json(client, "/btc_regime")
        write_json(rt / "btc_context_v135.json", btc)
        print(f"[nexus-scanner] BTC regime: {btc.get('btc_regime')} price={btc.get('btc_price')}")
    except Exception as e:
        print(f"[nexus-scanner] BTC regime ERROR: {e}")
        rc = 1

    # Step 2: Universe top N
    try:
        uni = fetch_json(client, "/universe/top", n=TOP_N, min_volume=MIN_VOLUME, sort_by="abs_change")
        pairs = uni.get("pairs", [])
        remote = {"pairs": [{"pair": ft_pair(p["pair"]), "symbol": p["pair"], "quote_volume": p["quote_volume"],
                             "price_change_pct": p["price_change_pct"]} for p in pairs]}
        write_json(rt / "pair_universe_remote.json", remote)
        write_json(rt / "pair_universe_top100.json", remote)
        write_json(rt / "pair_universe_all.json", remote)
        write_json(rt / "pair_universe_stage15.json", remote)
        write_csv(rt / "pair_universe_all.csv", remote["pairs"])
        write_csv(rt / "pair_universe_stage15.csv", remote["pairs"])
        write_csv(rt / "pair_universe_top100.csv", remote["pairs"][:100])

        # Freqtrade pairlist (FALLBACK: universe order; upgraded to flow-anchored
        # in Step 3 when flow data is available)
        ft_pairs = [ft_pair(p["pair"]) for p in pairs[:TOP_N]]
        ft_out = {"pairs": ft_pairs}
        write_json(rt / "pair_universe_freqtrade.json", ft_out)
        write_json(rt / "freqtrade_pairlist.json", ft_out)
        print(f"[nexus-scanner] Universe: {len(pairs)} pairs, freqtrade(fallback): {len(ft_pairs)}")
    except Exception as e:
        print(f"[nexus-scanner] Universe ERROR: {e}")
        rc = 1

    # Step 3: Flow context for all universe pairs
    try:
        flow_all = fetch_json(client, "/flow/all", exchange="bybit",
                              max_age_sec=FRESH_KLINE_MAX_AGE_SEC)
        raw_flow = flow_all.get("pairs", {})
        revo_flow = {}
        for symbol, rec in raw_flow.items():
            pair = ft_pair(symbol)
            rec = dict(rec)
            rec["pair"] = pair
            rec["symbol"] = symbol
            rec["cvd_source"] = rec.get("source")
            rec["data_quality"] = "OK" if rec.get("data_ready") else "BAD"
            revo_flow[pair] = rec
        # Write as old Revo format: {"BTC/USDT:USDT": {...}}
        write_json(rt / "revo_flow_context.json", revo_flow)

        # Upgrade pairlist: anchor to flow-covered pairs so every traded pair has
        # REAL flow data (no proxy fallback). Keep universe ranking (movers first),
        # then append any remaining flow-covered pairs, capped at TOP_N.
        flow_keys = set(revo_flow.keys())
        if flow_keys:
            ranked = [ft_pair(p["pair"]) for p in pairs]
            runtime_pairs = [ft_pair(s) for s in load_runtime_symbols(rt, ["binance_top100.txt", "bybit_all_691.txt"])]
            seen = set(ranked)
            candidates = ranked + [p for p in runtime_pairs if p not in seen]
            ft_pairs = [p for p in candidates if has_fresh_kline(client, raw_symbol(p))]
            ft_out = {"pairs": ft_pairs}
            # Keep published universe artifacts fresh-only. Flow may cover fewer
            # pairs than OHLCV; pairlist freshness is based on 5m klines from
            # Binance or Bybit, not only flow coverage.
            remote = {"pairs": [
                {
                    "pair": pair,
                    "symbol": raw_symbol(pair),
                    "quote_volume": revo_flow.get(pair, {}).get("qvol_5m", 0),
                    "price_change_pct": None,
                }
                for pair in ft_pairs
            ]}
            write_json(rt / "pair_universe_remote.json", remote)
            write_json(rt / "pair_universe_top100.json", remote)
            write_json(rt / "pair_universe_all.json", remote)
            write_json(rt / "pair_universe_stage15.json", remote)
            write_csv(rt / "pair_universe_all.csv", remote["pairs"])
            write_csv(rt / "pair_universe_stage15.csv", remote["pairs"])
            write_csv(rt / "pair_universe_top100.csv", remote["pairs"][:100])
            write_json(rt / "pair_universe_freqtrade.json", ft_out)
            write_json(rt / "freqtrade_pairlist.json", ft_out)
            print(f"[nexus-scanner] Pairlist fresh-5m: {len(ft_pairs)} pairs "
                  f"(stale/missing excluded)")
        # Audit-friendly raw collector dumps.
        write_json(rt / "revo_flow_context_collector.json", revo_flow)
        write_csv(rt / "revo_flow_context_collector.csv", list(revo_flow.values()))
        # Canonical copy keeps Nexus envelope
        write_json(rt / "revo_flow_context_canonical.json", flow_all)
        tradeable = flow_all.get("summary", {}).get("tradeable", 0)
        total = flow_all.get("summary", {}).get("total", 0)

        regime, candidate, blocker, blocker_txt = build_contexts(cycle=cycle, btc=btc, remote=remote, revo_flow=revo_flow)
        write_json(rt / "regime_context.json", regime)
        write_json(rt / "regime_context_summary.json", {"schema_version": regime["schema_version"], "timestamp": regime["timestamp"], "btc_regime": btc.get("btc_regime"), "total": len(regime["pairs"])})
        write_json(rt / "candidate_context.json", candidate)
        write_json(rt / "candidate_context_summary.json", {"schema_version": candidate["schema_version"], "timestamp": candidate["timestamp"], **candidate["summary"]})
        write_json(rt / "blocker_matrix.json", blocker)
        write_text(rt / "blocker_matrix.txt", blocker_txt)
        write_text(rt / "UNIVERSE_SCANNER_COMPACT.txt", f"cycle={cycle}\nuniverse={len(remote.get('pairs', []))}\npublished_freqtrade={len(ft_pairs)}\n")
        write_text(rt / "TOP100_FLOW_ENGINE_COMPACT.txt", f"cycle={cycle}\nflow_total={total}\nflow_tradeable={tradeable}\nentry_ready={candidate['summary']['entry_ready']}\n")
        write_text(rt / "BTC_MODE_ROUTER_COMPACT.txt", f"cycle={cycle}\nbtc_regime={btc.get('btc_regime')}\nbtc_price={btc.get('btc_price')}\n")
        print(f"[nexus-scanner] Flow: {total} pairs, {tradeable} tradeable, entry_ready={candidate['summary']['entry_ready']}")
    except Exception as e:
        print(f"[nexus-scanner] Flow ERROR: {e}")
        rc = 1

    # Step 4: Execution context
    exec_ctx = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "contract_status": "OK" if rc == 0 else "PARTIAL",
        "remote_pair_count": len(ft_pairs),
        "execution_pair_count": len(ft_pairs),
        "source": "nexus_scanner",
        "cycle": cycle,
        "nexus_api": NEXUS_API,
    }
    write_json(rt / "revo_execution_context.json", exec_ctx)

    # Step 5: Compact summary
    compact = {
        "NEXUS_SCANNER_CYCLE": cycle,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runtime": str(rt),
        "nexus_api": NEXUS_API,
        "last_rc": rc,
        "btc_regime": btc.get("btc_regime"),
        "universe_count": len(remote.get('pairs', [])),
        "flow_total": total,
        "flow_tradeable": tradeable,
    }
    write_json(rt / "NEXUS_SCANNER_COMPACT.json", compact)
    heartbeat = {"schema_version": "nexus.revo.heartbeat.v1", **compact, "status": "OK" if rc == 0 else "PARTIAL"}
    write_json(rt / "NEXUS_SCANNER_HEARTBEAT_LATEST.json", heartbeat)
    append_jsonl(rt / "NEXUS_SCANNER_HEARTBEAT.jsonl", heartbeat)
    write_json(rt / "bybit_flow_collector_heartbeat_latest.json", heartbeat)
    append_jsonl(rt / "bybit_flow_collector_heartbeat.jsonl", heartbeat)
    write_text(rt / "BYBIT_FLOW_COLLECTOR_HEARTBEAT_COMPACT.txt", "\n".join([
        f"cycle={cycle}", f"status={heartbeat['status']}", f"flow_total={compact.get('flow_total', 0)}", f"flow_tradeable={compact.get('flow_tradeable', 0)}", "",
    ]))
    print(f"[nexus-scanner] Cycle {cycle} rc={rc}")
    return {"rc": rc, "cycle": cycle}


def main():
    print(f"[nexus-scanner] START runtime={RUNTIME_DIR} api={NEXUS_API} interval={INTERVAL}s")
    with httpx.Client(timeout=30) as client:
        if "--once" in sys.argv:
            run_cycle(client)
            return
        while True:
            try:
                run_cycle(client)
            except Exception as e:
                print(f"[nexus-scanner] Cycle ERROR: {e}")
            time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
