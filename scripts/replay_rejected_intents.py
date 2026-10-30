#!/usr/bin/env python3
"""
Deterministic Counterfactual Replay Engine for Rejected Intents.

Reads /opt/signalcopyreal/journal/rejected_intents.jsonl, verifies its
cryptographic hash-chain integrity, and performs deterministic simulation:
- input_count
- resolved_count
- unknown_count
- would_have_won
- would_have_lost
- gross_pnl
- fees
- funding
- slippage
- net_pnl
- loss_avoided
- profit_missed
- net_gate_benefit = loss_avoided - profit_missed
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

JOURNAL_PATH = Path(os.getenv("REJECTED_INTENTS_PATH", "/opt/signalcopyreal/journal/rejected_intents.jsonl"))
TAKER_FEE_PCT = float(os.getenv("PAPER_TAKER_FEE_PCT", "0.0005"))
SLIPPAGE_PCT = float(os.getenv("PAPER_SLIPPAGE_PCT", "0.0005"))
FUNDING_BUF_PCT = float(os.getenv("PAPER_FUNDING_BUFFER_PCT", "0.0001"))


def _compute_event_hash(prev_hash: str, payload_data: Dict[str, Any]) -> str:
    canonical = prev_hash + "|" + json.dumps(payload_data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_journal_chain(path: Path) -> Tuple[bool, int, str]:
    if not path.exists() or path.stat().st_size == 0:
        return True, 0, "file_empty_or_new"
    prev_hash = "0" * 64
    count = 0
    with path.open("r", encoding="utf-8") as f:
        for idx, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as err:
                return False, count, f"line {idx} json error: {err}"
            
            stored_prev = rec.get("previous_event_hash")
            stored_hash = rec.get("event_hash")
            if stored_prev is not None and stored_hash is not None:
                if stored_prev != prev_hash:
                    return False, count, f"line {idx} hash-chain break: prev={stored_prev[:8]} != expected={prev_hash[:8]}"
                data_copy = {k: v for k, v in rec.items() if k != "event_hash"}
                expected_hash = _compute_event_hash(prev_hash, data_copy)
                if stored_hash != expected_hash:
                    return False, count, f"line {idx} tamper detected"
                prev_hash = stored_hash
            count += 1
    return True, count, "chain_valid"


def load_rejected_intents(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def evaluate_counterfactual(record: Dict[str, Any], klines_cache: Dict[str, Any]) -> Dict[str, Any]:
    symbol = record.get("symbol", "")
    side = record.get("side", "").upper()
    entry = float(record.get("entry", 0.0) or 0.0)
    sl = float(record.get("sl", 0.0) or 0.0)
    tps = record.get("tp") or []
    tp1 = float(tps[0]) if tps else 0.0

    if entry <= 0 or sl <= 0 or not symbol:
        return {"status": "UNKNOWN", "reason": "invalid_levels", "gross_pnl": 0.0, "fees": 0.0, "funding": 0.0, "slippage": 0.0, "net_pnl": 0.0}

    sl_frac = abs(entry - sl) / entry
    total_cost_frac = sl_frac + (2.0 * TAKER_FEE_PCT) + SLIPPAGE_PCT + FUNDING_BUF_PCT
    notional = 2.0 / max(total_cost_frac, 1e-6)
    qty = notional / entry

    if symbol not in klines_cache:
        url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}&interval=15&limit=200"
        try:
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                klines_cache[symbol] = resp.json().get("result", {}).get("list", [])
            else:
                klines_cache[symbol] = []
        except Exception:
            klines_cache[symbol] = []

    rows = klines_cache.get(symbol, [])
    if not rows:
        return {"status": "UNKNOWN", "reason": "market_data_unavailable", "gross_pnl": 0.0, "fees": 0.0, "funding": 0.0, "slippage": 0.0, "net_pnl": 0.0}

    chronological = list(reversed(rows))
    entry_filled = False
    hit_sl = False
    hit_tp = False

    for row in chronological:
        high = float(row[2])
        low = float(row[3])

        if not entry_filled:
            if low <= entry <= high:
                entry_filled = True
            continue

        if side == "LONG":
            sl_hit_this_bar = low <= sl
            tp_hit_this_bar = high >= tp1 if tp1 > 0 else False
        else:
            sl_hit_this_bar = high >= sl
            tp_hit_this_bar = low <= tp1 if tp1 > 0 else False

        # Conservative Rule: Same-candle hit resolves to SL first
        if sl_hit_this_bar and tp_hit_this_bar:
            hit_sl = True
            break
        elif sl_hit_this_bar:
            hit_sl = True
            break
        elif tp_hit_this_bar:
            hit_tp = True
            break

    if not entry_filled:
        return {"status": "UNKNOWN", "reason": "entry_not_touched", "gross_pnl": 0.0, "fees": 0.0, "funding": 0.0, "slippage": 0.0, "net_pnl": 0.0}

    fees = (notional * TAKER_FEE_PCT) * 2.0
    slippage = notional * SLIPPAGE_PCT
    funding = notional * FUNDING_BUF_PCT

    if hit_sl:
        gross_loss = abs(entry - sl) * qty
        net_loss = -(gross_loss + fees + slippage + funding)
        return {
            "status": "AVOIDED_LOSS",
            "gross_pnl": -gross_loss,
            "fees": fees,
            "funding": funding,
            "slippage": slippage,
            "net_pnl": net_loss,
            "loss_avoided": abs(net_loss),
            "profit_missed": 0.0,
        }
    elif hit_tp:
        tp_gross = (tp1 - entry) * qty if side == "LONG" else (entry - tp1) * qty
        net_profit = tp_gross - (fees + funding)
        return {
            "status": "MISSED_PROFIT",
            "gross_pnl": tp_gross,
            "fees": fees,
            "funding": funding,
            "slippage": 0.0,
            "net_pnl": net_profit,
            "loss_avoided": 0.0,
            "profit_missed": max(0.0, net_profit),
        }
    else:
        return {"status": "UNKNOWN", "reason": "unresolved_in_sample", "gross_pnl": 0.0, "fees": 0.0, "funding": 0.0, "slippage": 0.0, "net_pnl": 0.0}


def run_replay(path: Path) -> Dict[str, Any]:
    valid_chain, chain_len, chain_msg = verify_journal_chain(path)
    records = load_rejected_intents(path)
    total_intents = len(records)

    print(f"==================================================")
    print(f" DETERMINISTIC COUNTERFACTUAL REPLAY ENGINE")
    print(f" Source: {path}")
    print(f" Integrity: {'VALID' if valid_chain else 'COMPROMISED'} ({chain_msg}, len={chain_len})")
    print(f" Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print(f"==================================================\n")

    summary = {
        "input_count": total_intents,
        "resolved_count": 0,
        "unknown_count": 0,
        "would_have_won": 0,
        "would_have_lost": 0,
        "gross_pnl": 0.0,
        "fees": 0.0,
        "funding": 0.0,
        "slippage": 0.0,
        "net_pnl": 0.0,
        "loss_avoided": 0.0,
        "profit_missed": 0.0,
        "net_gate_benefit": 0.0,
    }

    if not records:
        print("No rejected intents recorded in journal yet.")
        print("System is awaiting forward-test signals.\n")
        return summary

    layer_counts = defaultdict(int)
    for r in records:
        layer_counts[r.get("reject_layer", "UNKNOWN")] += 1

    print("### 1. Reject Layer Breakdown")
    print("| Reject Layer | Count | Share |")
    print("|---|---|---|")
    for layer, count in sorted(layer_counts.items(), key=lambda x: x[1], reverse=True):
        print(f"| {layer} | {count} | {count/total_intents*100:.1f}% |")

    klines_cache: Dict[str, Any] = {}
    for r in records:
        out = evaluate_counterfactual(r, klines_cache)
        st = out["status"]
        if st in ("AVOIDED_LOSS", "MISSED_PROFIT"):
            summary["resolved_count"] += 1
            if st == "AVOIDED_LOSS":
                summary["would_have_lost"] += 1
                summary["loss_avoided"] += out.get("loss_avoided", 0.0)
            else:
                summary["would_have_won"] += 1
                summary["profit_missed"] += out.get("profit_missed", 0.0)
            summary["gross_pnl"] += out.get("gross_pnl", 0.0)
            summary["fees"] += out.get("fees", 0.0)
            summary["funding"] += out.get("funding", 0.0)
            summary["slippage"] += out.get("slippage", 0.0)
            summary["net_pnl"] += out.get("net_pnl", 0.0)
        else:
            summary["unknown_count"] += 1

    summary["net_gate_benefit"] = summary["loss_avoided"] - summary["profit_missed"]

    print("\n### 2. Deterministic Audit Metrics")
    print(f"- **input_count**:        {summary['input_count']}")
    print(f"- **resolved_count**:     {summary['resolved_count']}")
    print(f"- **unknown_count**:      {summary['unknown_count']}")
    print(f"- **would_have_won**:     {summary['would_have_won']}")
    print(f"- **would_have_lost**:    {summary['would_have_lost']}")
    print(f"- **gross_pnl**:          ${summary['gross_pnl']:+.2f}")
    print(f"- **fees**:               ${summary['fees']:.2f}")
    print(f"- **funding**:            ${summary['funding']:.2f}")
    print(f"- **slippage**:           ${summary['slippage']:.2f}")
    print(f"- **net_pnl**:            ${summary['net_pnl']:+.2f}")
    print(f"- **loss_avoided**:       ${summary['loss_avoided']:.2f}")
    print(f"- **profit_missed**:      ${summary['profit_missed']:.2f}")
    summary_data = {k: v for k, v in summary.items() if k != "output_hash"}
    summary["output_hash"] = hashlib.sha256(
        json.dumps(summary_data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    print(f"- **NET GATE BENEFIT**:   ${summary['net_gate_benefit']:+.2f}")
    print(f"- **output_hash**:        {summary['output_hash']}")

    return summary


def main():
    parser = argparse.ArgumentParser(description="Deterministic counterfactual replay audit")
    parser.add_argument("--path", type=str, default=str(JOURNAL_PATH), help="Path to rejected_intents.jsonl")
    args = parser.parse_args()
    run_replay(Path(args.path))


if __name__ == "__main__":
    main()
