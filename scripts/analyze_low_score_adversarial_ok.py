import json
from pathlib import Path
from collections import defaultdict
import statistics

outcomes_path = Path("/opt/signalcopyreal/runtime/state/committee_outcomes.jsonl")
decisions_path = Path("/opt/signalcopyreal/runtime/state/committee_decisions.jsonl")

# Load decisions to get legacy_bull_bear if available
decisions = {}
if decisions_path.exists():
    with decisions_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
                sig_id = row.get("signal_id")
                if sig_id:
                    decisions[sig_id] = row
            except Exception:
                pass

records = []
if outcomes_path.exists():
    with outcomes_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
                records.append(row)
            except Exception:
                pass

print(f"Total outcome records loaded: {len(records)}")

for r in records:
    sig_id = r.get("signal_id")
    dec = decisions.get(sig_id, {})
    c = dec.get("committee") or {}
    lbb = c.get("legacy_bull_bear") or {}
    r["legacy_bb_approved"] = lbb.get("approved")
    r["legacy_bb_reason"] = lbb.get("reason")
    if "committee_final_vote" not in r or not r["committee_final_vote"]:
        r["committee_final_vote"] = c.get("final_vote", "UNKNOWN")

def summarize_group(rows, name):
    if not rows:
        print(f"\n--- {name} (N=0) ---")
        return
    pnls = [r.get("pnl_pct", 0.0) for r in rows if r.get("pnl_pct") is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    evens = [p for p in pnls if p == 0]
    win_rate = len(wins) / len(pnls) * 100 if pnls else 0.0
    tot_win = sum(wins)
    tot_loss = abs(sum(losses))
    pf = tot_win / tot_loss if tot_loss > 0 else (float("inf") if tot_win > 0 else 0.0)
    avg_pnl = statistics.mean(pnls) if pnls else 0.0
    median_pnl = statistics.median(pnls) if pnls else 0.0
    tp_exits = sum(1 for r in rows if "TP" in str(r.get("exit_reason", "")))
    sl_exits = sum(1 for r in rows if "SL" in str(r.get("exit_reason", "")))
    exp_exits = sum(1 for r in rows if "EXPIRE" in str(r.get("exit_reason", "")))

    print(f"\n--- {name} (N={len(rows)}) ---")
    print(f"  Win Rate: {win_rate:.1f}% ({len(wins)}W / {len(losses)}L / {len(evens)}E)")
    print(f"  Avg PnL: {avg_pnl:+.2f}% | Median PnL: {median_pnl:+.2f}% | Total Net PnL: {sum(pnls):+.2f}%")
    print(f"  Profit Factor: {pf:.2f}")
    print(f"  Exit Breakdown: TP={tp_exits} ({tp_exits/len(rows)*100:.1f}%), SL={sl_exits} ({sl_exits/len(rows)*100:.1f}%), EXPIRE={exp_exits} ({exp_exits/len(rows)*100:.1f}%)")

low_score = [r for r in records if (r.get("validation_score") or 0) < 60]
mid_score = [r for r in records if 60 <= (r.get("validation_score") or 0) < 75]
high_score = [r for r in records if (r.get("validation_score") or 0) >= 75]

print("==================================================")
print("1. OVERALL SCORE BUCKETS")
print("==================================================")
summarize_group(low_score, "SCORE < 60 (ALL)")
summarize_group(mid_score, "SCORE 60-75 (ALL)")
summarize_group(high_score, "SCORE >= 75 (ALL)")

print("\n==================================================")
print("2. ADVERSARIAL YES / OK ACROSS SCORE BUCKETS")
print("==================================================")
low_adv_yes = [r for r in low_score if r.get("committee_final_vote") == "YES"]
mid_adv_yes = [r for r in mid_score if r.get("committee_final_vote") == "YES"]
high_adv_yes = [r for r in high_score if r.get("committee_final_vote") == "YES"]

summarize_group(low_adv_yes, "LOW SCORE (<60) + COMMITTEE YES")
summarize_group(mid_adv_yes, "MID SCORE (60-75) + COMMITTEE YES")
summarize_group(high_adv_yes, "HIGH SCORE (>=75) + COMMITTEE YES")

low_lbb_yes = [r for r in low_score if r.get("legacy_bb_approved") is True]
mid_lbb_yes = [r for r in mid_score if r.get("legacy_bb_approved") is True]
high_lbb_yes = [r for r in high_score if r.get("legacy_bb_approved") is True]

summarize_group(low_lbb_yes, "LOW SCORE (<60) + LEGACY BB APPROVED")
summarize_group(mid_lbb_yes, "MID SCORE (60-75) + LEGACY BB APPROVED")
summarize_group(high_lbb_yes, "HIGH SCORE (>=75) + LEGACY BB APPROVED")

print("\n==================================================")
print("3. LOW SCORE (<60) BY ADVERSARIAL VOTE BREAKDOWN")
print("==================================================")
low_warn = [r for r in low_score if r.get("committee_final_vote") == "WARN"]
low_no = [r for r in low_score if r.get("committee_final_vote") == "NO"]
summarize_group(low_warn, "LOW SCORE (<60) + COMMITTEE WARN")
summarize_group(low_no, "LOW SCORE (<60) + COMMITTEE NO")

print("\n==================================================")
print("4. FINE-GRAINED SCORE BREAKDOWN FOR ADVERSARIAL YES")
print("==================================================")
for s_min, s_max in [(0, 45), (45, 55), (55, 65), (65, 75), (75, 100)]:
    subset = [r for r in records if s_min <= (r.get("validation_score") or 0) < s_max and (r.get("committee_final_vote") == "YES" or r.get("legacy_bb_approved") is True)]
    summarize_group(subset, f"SCORE {s_min}-{s_max} + ADVERSARIAL OK")
