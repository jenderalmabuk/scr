#!/usr/bin/env python3
"""Forensic Filter Audit & Dual-Path Simulation for Nexus Signal Copy.

Investigates historical trade history, detects loss clusters, grades Telegram channels,
mines recent signal logs from docker, and performs a dual-path simulation comparing
the legacy filter against the Nexus Resilience v1.0 candidate filter.

Safety:
- 100% READ-ONLY.
- Does not modify any production configuration, journal, or state.
- Outputs artifacts strictly to reports/ directory.
"""

from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


REPORTS_DIR = Path("/opt/signalcopyreal/reports")
TRADE_HISTORY_FILE = Path("/opt/signalcopyreal/journal/trade_history.json")
CH_PERF_FILE = Path("/opt/signalcopyreal/runtime/state/channel_performance.json")


def load_trade_history() -> List[Dict[str, Any]]:
    if not TRADE_HISTORY_FILE.exists():
        print(f"[WARN] {TRADE_HISTORY_FILE} not found.")
        return []
    try:
        with open(TRADE_HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[ERROR] Failed to load trade history: {e}")
        return []


def load_channel_performance() -> Dict[str, Any]:
    if not CH_PERF_FILE.exists():
        return {}
    try:
        with open(CH_PERF_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def audit_trade_history(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    total_records = len(trades)
    closed = [t for t in trades if t.get("timestamp_close")]
    open_pos = [t for t in trades if not t.get("timestamp_close")]

    total_gross_profit = 0.0
    total_gross_loss = 0.0
    total_pnl = 0.0
    total_fee = 0.0
    wins = 0
    losses = 0
    be = 0

    long_trades = 0
    long_pnl = 0.0
    short_trades = 0
    short_pnl = 0.0

    for t in closed:
        pnl = float(t.get("pnl_usd", 0.0) or t.get("final_exit_net_pnl_usd", 0.0) or 0.0)
        fee = float(t.get("fee_usd", 0.0) or t.get("exit_fee", 0.0) or 0.0)
        side = str(t.get("side", "")).upper()

        total_pnl += pnl
        total_fee += fee

        if pnl > 0.0001:
            wins += 1
            total_gross_profit += pnl
        elif pnl < -0.0001:
            losses += 1
            total_gross_loss += abs(pnl)
        else:
            be += 1

        if side == "LONG":
            long_trades += 1
            long_pnl += pnl
        elif side == "SHORT":
            short_trades += 1
            short_pnl += pnl

    win_rate = (wins / len(closed)) * 100.0 if closed else 0.0
    pf = (total_gross_profit / total_gross_loss) if total_gross_loss > 0 else (99.0 if total_gross_profit > 0 else 0.0)

    # Detect loss clusters (streaks of consecutive losses >= 4)
    # Sort closed trades by close timestamp
    def _parse_ts(t: Dict[str, Any]) -> str:
        return str(t.get("timestamp_close") or t.get("timestamp_open") or "")

    sorted_closed = sorted(closed, key=_parse_ts)
    clusters = []
    current_cluster: List[Dict[str, Any]] = []

    for t in sorted_closed:
        pnl = float(t.get("pnl_usd", 0.0) or t.get("final_exit_net_pnl_usd", 0.0) or 0.0)
        if pnl < -0.0001:
            current_cluster.append(t)
        else:
            if len(current_cluster) >= 4:
                c_loss = sum(float(x.get("pnl_usd", 0.0) or x.get("final_exit_net_pnl_usd", 0.0) or 0.0) for x in current_cluster)
                sides = [str(x.get("side", "")).upper() for x in current_cluster]
                long_count = sides.count("LONG")
                short_count = sides.count("SHORT")
                bias = "HEAVY_LONG" if long_count >= len(sides)*0.75 else ("HEAVY_SHORT" if short_count >= len(sides)*0.75 else "MIXED")
                clusters.append({
                    "streak_length": len(current_cluster),
                    "total_loss_usd": round(c_loss, 2),
                    "start_time": _parse_ts(current_cluster[0]),
                    "end_time": _parse_ts(current_cluster[-1]),
                    "symbols": [x.get("symbol", "") for x in current_cluster],
                    "directional_bias": bias,
                    "long_ratio": round(long_count / len(sides), 2),
                })
            current_cluster = []

    if len(current_cluster) >= 4:
        c_loss = sum(float(x.get("pnl_usd", 0.0) or x.get("final_exit_net_pnl_usd", 0.0) or 0.0) for x in current_cluster)
        sides = [str(x.get("side", "")).upper() for x in current_cluster]
        long_count = sides.count("LONG")
        short_count = sides.count("SHORT")
        bias = "HEAVY_LONG" if long_count >= len(sides)*0.75 else ("HEAVY_SHORT" if short_count >= len(sides)*0.75 else "MIXED")
        clusters.append({
            "streak_length": len(current_cluster),
            "total_loss_usd": round(c_loss, 2),
            "start_time": _parse_ts(current_cluster[0]),
            "end_time": _parse_ts(current_cluster[-1]),
            "symbols": [x.get("symbol", "") for x in current_cluster],
            "directional_bias": bias,
            "long_ratio": round(long_count / len(sides), 2),
        })

    return {
        "total_records": total_records,
        "closed_trades": len(closed),
        "open_trades": len(open_pos),
        "wins": wins,
        "losses": losses,
        "breakeven": be,
        "win_rate_pct": round(win_rate, 2),
        "total_pnl_usd": round(total_pnl, 2),
        "total_gross_profit": round(total_gross_profit, 2),
        "total_gross_loss": round(total_gross_loss, 2),
        "profit_factor": round(pf, 2),
        "total_fee_usd": round(total_fee, 2),
        "long_stats": {"count": long_trades, "pnl_usd": round(long_pnl, 2)},
        "short_stats": {"count": short_trades, "pnl_usd": round(short_pnl, 2)},
        "loss_clusters": sorted(clusters, key=lambda c: c["total_loss_usd"])[:5],
    }


def grade_channels(trades: List[Dict[str, Any]], ch_perf: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    channel_stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "trades": 0, "wins": 0, "losses": 0, "gross_profit": 0.0, "gross_loss": 0.0, "net_pnl": 0.0,
        "source_name": ""
    })

    # Accumulate from trade_history
    for t in trades:
        if not t.get("timestamp_close"):
            continue
        adv = t.get("adv_snapshot") or {}
        ch_id = str(adv.get("source_chat_id") or adv.get("channel") or t.get("source") or "UNKNOWN")
        pnl = float(t.get("pnl_usd", 0.0) or t.get("final_exit_net_pnl_usd", 0.0) or 0.0)

        s = channel_stats[ch_id]
        s["trades"] += 1
        s["net_pnl"] += pnl
        if pnl > 0.0001:
            s["wins"] += 1
            s["gross_profit"] += pnl
        elif pnl < -0.0001:
            s["losses"] += 1
            s["gross_loss"] += abs(pnl)

    # Incorporate extra data from channel_performance.json if available
    for ch_id, data in ch_perf.items():
        if ch_id not in channel_stats:
            pnl = float(data.get("total_pnl", 0.0) or 0.0)
            w = int(data.get("wins", 0) or 0)
            l = int(data.get("losses", 0) or 0)
            channel_stats[ch_id] = {
                "trades": w + l,
                "wins": w,
                "losses": l,
                "gross_profit": pnl if pnl > 0 else 0.0,
                "gross_loss": abs(pnl) if pnl < 0 else 0.0,
                "net_pnl": pnl,
                "source_name": str(data.get("name", ch_id)),
            }
        else:
            if not channel_stats[ch_id]["source_name"] and data.get("name"):
                channel_stats[ch_id]["source_name"] = str(data.get("name"))

    results: Dict[str, Dict[str, Any]] = {}
    for ch_id, s in channel_stats.items():
        n = s["trades"]
        w = s["wins"]
        wr = (w / n * 100.0) if n > 0 else 0.0
        pf = (s["gross_profit"] / s["gross_loss"]) if s["gross_loss"] > 0 else (99.0 if s["gross_profit"] > 0 else 0.0)
        net = s["net_pnl"]

        # Grading logic
        if net < -50.0 or (n >= 10 and wr < 35.0):
            grade = "RETIRE"
            action = "Unsubscribe / Blacklist"
        elif net > 15.0 and wr >= 50.0 and pf >= 1.2 and n >= 8:
            grade = "LIVE"
            action = "Promote to Live"
        elif pf >= 0.8 and net > -15.0:
            grade = "WATCH"
            action = "Passive Observation"
        else:
            grade = "CALIBRATION"
            action = "Strict Calibration Mode"

        results[ch_id] = {
            "source_chat_id": ch_id,
            "source_name": s["source_name"] or ch_id,
            "trades": n,
            "wins": w,
            "losses": s["losses"],
            "win_rate_pct": round(wr, 2),
            "net_pnl_usd": round(net, 2),
            "profit_factor": round(pf, 2),
            "grade": grade,
            "action": action,
        }

    return results


def mine_recent_signals_from_docker() -> List[Dict[str, Any]]:
    """Mine recent signal stream from nexus_signal_copy docker logs."""
    try:
        proc = subprocess.run(
            ["docker", "logs", "--tail", "25000", "nexus_signal_copy"],
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
        logs = proc.stdout + "\n" + proc.stderr
    except Exception as e:
        print(f"[WARN] Failed to fetch docker logs: {e}")
        return []

    signals: List[Dict[str, Any]] = []
    
    # State tracking per signal parsing window
    current_sig: Dict[str, Any] = {}
    
    for line in logs.splitlines():
        # Match parsed trade signal
        # [SIGNAL_COPY] parsed: BNBUSDT LONG | entry 750-750 | TP [768] | SL 737 | lev - | src -1002817127218
        m_parse = re.search(r"\[SIGNAL_COPY\] parsed:\s+([A-Z0-9]+)\s+(LONG|SHORT)\s+\|\s+entry\s+([0-9.-]+)\s+\|\s+TP\s+\[(.*?)\]\s+\|\s+SL\s+([0-9.]+)\s+.*?src\s+(-?[0-9]+)", line)
        if m_parse:
            if current_sig.get("symbol"):
                signals.append(current_sig)
            sym, side, entry_str, tps_str, sl_str, src_str = m_parse.groups()
            tps_clean = []
            for x in tps_str.split(","):
                x = x.strip()
                if x and x != "-":
                    try:
                        tps_clean.append(float(x))
                    except ValueError:
                        pass

            def _clean_val(val_str: str) -> float:
                try:
                    val_str = val_str.split("-")[0].strip()
                    return float(val_str)
                except (ValueError, TypeError):
                    return 0.0

            current_sig = {
                "symbol": sym,
                "side": side,
                "entry_price": _clean_val(entry_str),
                "sl_price": _clean_val(sl_str),
                "tps": tps_clean,
                "source_chat_id": src_str,
                "mtf_score": 50.0,
                "tv_score": 50.0,
                "vision_score": 0.0,
                "btc_regime": "NEUTRAL",
                "btc_diff": 0.0,
                "btc_corr": 0.0,
                "btc_allowed": True,
                "channel_score": 50.0,
                "actual_verdict": "UNKNOWN",
                "rejection_reasons": [],
            }
            continue

        if not current_sig:
            continue

        # Match MTF: [MTF] BNBUSDT side=LONG mtf_score=70.0
        m_mtf = re.search(r"\[MTF\]\s+(\S+)\s+side=\S+\s+mtf_score=([0-9.]+)", line)
        if m_mtf and m_mtf.group(1) == current_sig.get("symbol"):
            current_sig["mtf_score"] = float(m_mtf.group(2))
            continue

        # Match TV: [TV] BNBUSDT side=LONG tv_score=64.0
        m_tv = re.search(r"\[TV\]\s+(\S+)\s+side=\S+\s+tv_score=([0-9.]+)", line)
        if m_tv and m_tv.group(1) == current_sig.get("symbol"):
            current_sig["tv_score"] = float(m_tv.group(2))
            continue

        # Match BTC Gate: [BTC_GATE] BNBUSDT LONG -> allowed=False (regime=DUMP, diff=-0.60%, corr=0.86
        m_btc = re.search(r"\[BTC_GATE\]\s+(\S+)\s+\S+\s+->\s+allowed=(True|False)\s+\(regime=([A-Z_]+),\s+diff=([0-9.-]+)%,\s+corr=([0-9.-]+)", line)
        if m_btc and m_btc.group(1) == current_sig.get("symbol"):
            current_sig["btc_allowed"] = (m_btc.group(2) == "True")
            current_sig["btc_regime"] = m_btc.group(3)
            current_sig["btc_diff"] = float(m_btc.group(4))
            current_sig["btc_corr"] = float(m_btc.group(5))
            continue

        # Match Channel score: [CHANNEL_SCORE] BNBUSDT source=-1002817127218 raw=59.4 adj=+2.0 final=61.4 verdict=REJECT
        m_ch = re.search(r"\[CHANNEL_SCORE\]\s+(\S+)\s+source=\S+\s+raw=[0-9.]+\s+adj=[0-9.+-]+\s+final=([0-9.]+)\s+verdict=([A-Z_]+)", line)
        if m_ch and m_ch.group(1) == current_sig.get("symbol"):
            current_sig["channel_score"] = float(m_ch.group(2))
            current_sig["actual_verdict"] = m_ch.group(3)
            continue

        # Match Verdict: [SIGNAL_COPY] BNBUSDT -> REJECT score=61.4 ... reasons=['...']
        m_ver = re.search(r"\[SIGNAL_COPY\]\s+(\S+)\s+->\s+([A-Z_]+)\s+score=([0-9.]+).*?reasons=(.*)", line)
        if m_ver and m_ver.group(1) == current_sig.get("symbol"):
            current_sig["actual_verdict"] = m_ver.group(2)
            current_sig["channel_score"] = float(m_ver.group(3))
            current_sig["rejection_reasons"] = m_ver.group(4)
            signals.append(current_sig)
            current_sig = {}
            continue

    if current_sig.get("symbol"):
        signals.append(current_sig)

    return signals


def simulate_dual_path(signals: List[Dict[str, Any]], channel_grades: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    path_a_passed = 0
    path_a_rejected = 0
    path_a_reasons = defaultdict(int)

    path_b_passed = 0
    path_b_rejected = 0
    path_b_reasons = defaultdict(int)

    comparisons: List[Dict[str, Any]] = []

    for sig in signals:
        sym = sig.get("symbol", "")
        side = sig.get("side", "")
        mtf = float(sig.get("mtf_score", 50.0))
        tv = float(sig.get("tv_score", 50.0))
        vis = float(sig.get("vision_score", 0.0))
        btc_allowed = bool(sig.get("btc_allowed", True))
        btc_regime = str(sig.get("btc_regime", "NEUTRAL")).upper()
        ch_id = str(sig.get("source_chat_id", ""))
        grade_info = channel_grades.get(ch_id, {})
        grade = grade_info.get("grade", "DEFAULT")

        # --- PATH A: Filter Lama (Legacy) ---
        # Logic: avg >= 55, binary veto if not btc_allowed or regime in (DUMP, RISK_OFF)
        path_a_ok = True
        a_reason = ""
        avg_a = (mtf + tv) / 2.0  # if vision missing
        if avg_a < 55.0:
            path_a_ok = False
            a_reason = f"Adversarial Score Low ({avg_a:.1f} < 55.0)"
        elif not btc_allowed or btc_regime in ("DUMP", "RISK_OFF"):
            path_a_ok = False
            a_reason = f"BTC Binary Veto ({btc_regime})"

        if path_a_ok:
            path_a_passed += 1
        else:
            path_a_rejected += 1
            path_a_reasons[a_reason] += 1

        # --- PATH B: Nexus Resilience Suite v1.0 ---
        # Logic:
        # 1. Anti-Average Trap: avg >= 50 AND min >= 30 across active components
        components = [s for s in [mtf, tv] if s > 0]
        if vis > 0:
            components.append(vis)
        
        avg_b = sum(components) / len(components) if components else 0.0
        min_b = min(components) if components else 0.0

        path_b_ok = True
        b_reason = ""

        # Channel check: If RETIRE -> require strict bar or reject
        if grade == "RETIRE":
            path_b_ok = False
            b_reason = f"Toxic Channel Blacklist ({ch_id})"
        elif avg_b < 50.0 or min_b < 30.0:
            path_b_ok = False
            b_reason = f"Adversarial Failed (avg={avg_b:.1f}<50 or min={min_b:.1f}<30)"
        else:
            # Weighted Penalty
            penalty = 0
            if btc_regime in ("DUMP", "RISK_OFF"):
                penalty += 20
            if sig.get("btc_diff", 0.0) < -0.5 and side == "LONG":
                penalty += 15

            # Hard Veto ONLY for extreme conflict
            if btc_regime in ("DUMP", "RISK_OFF") and sig.get("btc_diff", 0.0) < -1.5 and side == "LONG":
                path_b_ok = False
                b_reason = "Hard Veto: Extreme Dump Conflict"
            else:
                final_b_score = avg_b - penalty
                if final_b_score < 40.0:
                    path_b_ok = False
                    b_reason = f"Penalty Depleted Confluence ({final_b_score:.1f} < 40.0)"

        if path_b_ok:
            path_b_passed += 1
        else:
            path_b_rejected += 1
            path_b_reasons[b_reason] += 1

        comparisons.append({
            "symbol": sym,
            "side": side,
            "channel": ch_id,
            "channel_grade": grade,
            "legacy_result": "PASS" if path_a_ok else f"REJECT ({a_reason})",
            "resilience_result": "PASS" if path_b_ok else f"REJECT ({b_reason})",
            "unblocked_by_resilience": (not path_a_ok and path_b_ok),
            "protected_by_resilience": (path_a_ok and not path_b_ok),
        })

    return {
        "total_signals_analyzed": len(signals),
        "path_a_legacy": {
            "passed": path_a_passed,
            "rejected": path_a_rejected,
            "pass_rate_pct": round((path_a_passed / len(signals)) * 100.0, 2) if signals else 0.0,
            "top_rejection_reasons": sorted(path_a_reasons.items(), key=lambda x: x[1], reverse=True),
        },
        "path_b_resilience": {
            "passed": path_b_passed,
            "rejected": path_b_rejected,
            "pass_rate_pct": round((path_b_passed / len(signals)) * 100.0, 2) if signals else 0.0,
            "top_rejection_reasons": sorted(path_b_reasons.items(), key=lambda x: x[1], reverse=True),
        },
        "net_unblocked_signals": sum(1 for c in comparisons if c["unblocked_by_resilience"]),
        "toxic_prevented_signals": sum(1 for c in comparisons if c["protected_by_resilience"]),
        "sample_comparisons": comparisons[:10],
    }


def export_reports(trade_metrics: Dict[str, Any], channels: Dict[str, Dict[str, Any]], sim: Dict[str, Any]):
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # 1. JSON Export
    json_path = REPORTS_DIR / "forensic_audit.json"
    audit_data = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "trade_metrics": trade_metrics,
        "channel_matrix": channels,
        "filter_simulation": sim,
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(audit_data, f, indent=2)
    print(f"✅ Generated JSON report: {json_path}")

    # 2. CSV Export for Channels
    csv_path = REPORTS_DIR / "channel_grades.csv"
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["source_chat_id", "source_name", "trades", "wins", "losses", "win_rate_pct", "net_pnl_usd", "profit_factor", "grade", "action"])
        for ch in sorted(channels.values(), key=lambda x: x["net_pnl_usd"], reverse=True):
            writer.writerow([
                ch["source_chat_id"], ch["source_name"], ch["trades"], ch["wins"], ch["losses"],
                ch["win_rate_pct"], ch["net_pnl_usd"], ch["profit_factor"], ch["grade"], ch["action"]
            ])
    print(f"✅ Generated CSV grades: {csv_path}")

    # 3. Markdown Summary
    md_path = REPORTS_DIR / "forensic_summary.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# Forensic Filter Audit & Resilience Simulation Report\n\n")
        f.write(f"*Generated At: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}*\n\n")
        
        f.write("## 1. Historical Trade Autopsy (`journal/trade_history.json`)\n\n")
        f.write(f"- **Total Records**: {trade_metrics['total_records']}\n")
        f.write(f"- **Closed Trades**: {trade_metrics['closed_trades']}\n")
        f.write(f"- **Win Rate**: {trade_metrics['win_rate_pct']}%\n")
        f.write(f"- **Total Net PnL**: ${trade_metrics['total_pnl_usd']:.2f}\n")
        f.write(f"- **Profit Factor**: {trade_metrics['profit_factor']}\n")
        f.write(f"- **Total Exchange Fees**: ${trade_metrics['total_fee_usd']:.2f}\n")
        f.write(f"- **Directional Breakdown**: LONG ${trade_metrics['long_stats']['pnl_usd']:.2f} ({trade_metrics['long_stats']['count']} trades) | SHORT ${trade_metrics['short_stats']['pnl_usd']:.2f} ({trade_metrics['short_stats']['count']} trades)\n\n")

        f.write("### Top Loss Clusters (Cluster Death Verification)\n\n")
        for i, c in enumerate(trade_metrics['loss_clusters'], 1):
            f.write(f"{i}. **{c['streak_length']} Consecutive Losses** -> Loss: **${c['total_loss_usd']:.2f}** (Bias: {c['directional_bias']}, Long Ratio: {c['long_ratio']*100:.0f}%)\n")
            f.write(f"   - Window: `{c['start_time'][:19]}` to `{c['end_time'][:19]}`\n")
            f.write(f"   - Symbols: {', '.join(c['symbols'][:6])}...\n\n")

        f.write("## 2. Channel Performance Matrix & Grading (All 41 Channels)\n\n")
        f.write("| Source Chat ID | Name | Trades | Win Rate | Net PnL | PF | Grade | Action |\n")
        f.write("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n")
        for ch in sorted(channels.values(), key=lambda x: x["net_pnl_usd"], reverse=True):
            f.write(f"| `{ch['source_chat_id']}` | {ch['source_name']} | {ch['trades']} | {ch['win_rate_pct']}% | ${ch['net_pnl_usd']:.2f} | {ch['profit_factor']} | **{ch['grade']}** | {ch['action']} |\n")
        f.write("\n")

        f.write("## 3. Dual-Path Filter Simulation (Legacy vs Resilience v1.0)\n\n")
        f.write(f"- **Signals Analyzed**: {sim['total_signals_analyzed']}\n")
        f.write(f"- **Path A (Legacy Filter)**: {sim['path_a_legacy']['passed']} passed ({sim['path_a_legacy']['pass_rate_pct']}%), {sim['path_a_legacy']['rejected']} rejected\n")
        f.write(f"- **Path B (Nexus Resilience v1.0)**: {sim['path_b_resilience']['passed']} passed ({sim['path_b_resilience']['pass_rate_pct']}%), {sim['path_b_resilience']['rejected']} rejected\n")
        f.write(f"- **Signals Rescued from Over-Filtering**: +{sim['net_unblocked_signals']}\n")
        f.write(f"- **Toxic Channel Trades Prevented**: {sim['toxic_prevented_signals']}\n\n")
        
        f.write("### Top Rejection Reasons Comparison\n\n")
        f.write("**Path A (Legacy)**:\n")
        for r, cnt in sim['path_a_legacy']['top_rejection_reasons']:
            f.write(f"- {r}: {cnt} signals\n")
        f.write("\n**Path B (Resilience v1.0)**:\n")
        for r, cnt in sim['path_b_resilience']['top_rejection_reasons']:
            f.write(f"- {r}: {cnt} signals\n")
        f.write("\n")

    print(f"✅ Generated Markdown summary: {md_path}")


def main():
    print("=" * 60)
    print("Nexus Signal Copy — Forensic Filter Audit & Dual-Path Simulation")
    print("=" * 60)

    # 1. Historical Mining
    print("[1/3] Mining trade_history.json...")
    trades = load_trade_history()
    ch_perf = load_channel_performance()
    trade_metrics = audit_trade_history(trades)
    channel_grades = grade_channels(trades, ch_perf)

    # 2. Mine Recent Signals from Docker Logs
    print("[2/3] Mining recent signals from nexus_signal_copy logs...")
    signals = mine_recent_signals_from_docker()
    print(f"Found {len(signals)} parsed trade signal events from container logs.")

    # 3. Simulate Dual-Path
    print("[3/3] Simulating Dual-Path filter...")
    sim = simulate_dual_path(signals, channel_grades)

    # Export
    export_reports(trade_metrics, channel_grades, sim)

    print("\n" + "=" * 60)
    print(f"AUDIT COMPLETE — Closed Trades: {trade_metrics['closed_trades']} | Net PnL: ${trade_metrics['total_pnl_usd']:.2f}")
    print(f"Simulation: Legacy Passed {sim['path_a_legacy']['passed']} -> Resilience Passed {sim['path_b_resilience']['passed']}")
    print("=" * 60)


if __name__ == "__main__":
    main()
