#!/usr/bin/env python3
"""
Counterfactual Replay Engine for Rejected Intents.

Reads /opt/signalcopyreal/journal/rejected_intents.jsonl and analyzes:
1. Breakdown of rejected intents per gate/layer.
2. Conservative counterfactual replay:
   - Evaluates hypothetical outcomes if the trade had been executed.
   - Includes taker fees (0.05% x 2), adverse slippage (0.05%), and funding buffer (0.01%).
   - Conservative rule: If both SL and TP price levels are within the same range, assume SL hit.
   - Computes:
     * loss_avoided (USD saved by blocking losing signals)
     * profit_missed (USD missed by blocking winning signals)
     * net_gate_benefit = loss_avoided - profit_missed
     * unknown_outcome (unresolved / no market data)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

JOURNAL_PATH = Path(os.getenv("REJECTED_INTENTS_PATH", "/opt/signalcopyreal/journal/rejected_intents.jsonl"))
TAKER_FEE_PCT = float(os.getenv("PAPER_TAKER_FEE_PCT", "0.0005"))
SLIPPAGE_PCT = float(os.getenv("PAPER_SLIPPAGE_PCT", "0.0005"))
FUNDING_BUF_PCT = float(os.getenv("PAPER_FUNDING_BUFFER_PCT", "0.0001"))


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
    """Conservatively evaluate hypothetical trade outcome."""
    symbol = record.get("symbol", "")
    side = record.get("side", "").upper()
    entry = float(record.get("entry", 0.0) or 0.0)
    sl = float(record.get("sl", 0.0) or 0.0)
    tps = record.get("tp") or []
    tp1 = float(tps[0]) if tps else 0.0

    if entry <= 0 or sl <= 0 or not symbol:
        return {"status": "UNKNOWN", "reason": "invalid_levels", "pnl": 0.0}

    # Standard paper fixed risk sizing ($2.00 net)
    sl_frac = abs(entry - sl) / entry
    total_cost_frac = sl_frac + (2.0 * TAKER_FEE_PCT) + SLIPPAGE_PCT + FUNDING_BUF_PCT
    notional = 2.0 / max(total_cost_frac, 1e-6)
    qty = notional / entry

    # Fetch recent candles from Bybit if not cached
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
        return {"status": "UNKNOWN", "reason": "market_data_unavailable", "pnl": 0.0}

    # Reverse rows to chronological order (Bybit returns newest first)
    chronological = list(reversed(rows))
    
    # Track price trajectory
    entry_filled = False
    hit_sl = False
    hit_tp = False

    for row in chronological:
        high = float(row[2])
        low = float(row[3])

        if not entry_filled:
            # Check if entry touched
            if (side == "LONG" and low <= entry <= high) or (side == "SHORT" and low <= entry <= high):
                entry_filled = True
            continue

        # When filled, check SL / TP hits
        if side == "LONG":
            sl_hit_this_bar = low <= sl
            tp_hit_this_bar = high >= tp1 if tp1 > 0 else False
        else:
            sl_hit_this_bar = high >= sl
            tp_hit_this_bar = low <= tp1 if tp1 > 0 else False

        # Conservative rule: If both hit within the same candle, assume SL hit first
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
        return {"status": "UNKNOWN", "reason": "entry_not_touched", "pnl": 0.0}

    if hit_sl:
        # Avoided a loss of ~$2.00
        loss_amount = notional * total_cost_frac
        return {"status": "AVOIDED_LOSS", "pnl": -loss_amount, "notional": notional}
    elif hit_tp:
        # Missed a profit
        tp_gross = (tp1 - entry) * qty if side == "LONG" else (entry - tp1) * qty
        fees = (notional * TAKER_FEE_PCT) + (tp1 * qty * TAKER_FEE_PCT)
        net_profit = tp_gross - fees
        return {"status": "MISSED_PROFIT", "pnl": net_profit, "notional": notional}
    else:
        return {"status": "UNKNOWN", "reason": "unresolved_in_sample", "pnl": 0.0}


def run_replay(path: Path) -> None:
    records = load_rejected_intents(path)
    total_intents = len(records)

    print(f"==================================================")
    print(f" COUNTERFACTUAL REPLAY AUDIT (N={total_intents})")
    print(f" Source: {path}")
    print(f" Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print(f"==================================================\n")

    if not records:
        print("No rejected intents recorded yet.")
        print("All incoming order intents are currently passing gates or system is flat.\n")
        return

    # 1. Gate Breakdown
    layer_counts = defaultdict(int)
    reason_counts = defaultdict(int)
    for r in records:
        layer_counts[r.get("reject_layer", "UNKNOWN")] += 1
        reason_counts[r.get("primary_reason", "UNKNOWN")] += 1

    print("### 1. Rejection Distribution by Layer")
    print("| Reject Layer | Count | Share |")
    print("|---|---|---|")
    for layer, count in sorted(layer_counts.items(), key=lambda x: x[1], reverse=True):
        print(f"| {layer} | {count} | {count/total_intents*100:.1f}% |")

    print("\n### 2. Top Primary Reject Reasons")
    print("| Reason | Count |")
    print("|---|---|")
    for reason, count in sorted(reason_counts.items(), key=lambda x: x[1], reverse=True)[:10]:
        print(f"| {reason[:60]} | {count} |")

    # 2. Counterfactual Simulation
    klines_cache: Dict[str, Any] = {}
    loss_avoided = 0.0
    profit_missed = 0.0
    count_avoided = 0
    count_missed = 0
    count_unknown = 0

    print("\nSimulating market replay for rejected signals...")
    for r in records:
        res = evaluate_counterfactual(r, klines_cache)
        status = res.get("status")
        if status == "AVOIDED_LOSS":
            loss_avoided += abs(res.get("pnl", 0.0))
            count_avoided += 1
        elif status == "MISSED_PROFIT":
            profit_missed += res.get("pnl", 0.0)
            count_missed += 1
        else:
            count_unknown += 1

    net_gate_benefit = loss_avoided - profit_missed

    print("\n### 3. Counterfactual Gate Benefit Summary")
    print(f"- **Loss Avoided (Blocked Losses)**:  ${loss_avoided:.2f} ({count_avoided} signals)")
    print(f"- **Profit Missed (Blocked Wins)**:   ${profit_missed:.2f} ({count_missed} signals)")
    print(f"- **Unknown / Unresolved**:           {count_unknown} signals")
    print(f"- **NET GATE BENEFIT**:               ${net_gate_benefit:+.2f}")

    if net_gate_benefit > 0:
        print("\nVerdict: Gating demonstrates POSITIVE net expectancy (Loss Avoided > Profit Missed).")
    elif net_gate_benefit < 0:
        print("\nVerdict: Gating throttles profit; review gate selectivity.")
    else:
        print("\nVerdict: Neutral gate expectancy.")


def main():
    parser = argparse.ArgumentParser(description="Replay rejected intents counterfactual journal")
    parser.add_argument("--path", type=str, default=str(JOURNAL_PATH), help="Path to rejected_intents.jsonl")
    args = parser.parse_args()
    run_replay(Path(args.path))


if __name__ == "__main__":
    main()
