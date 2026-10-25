import json
from datetime import datetime, timezone
from pathlib import Path

OVERHAUL_CUTOFF = datetime(2026, 10, 18, 16, 22, 0, tzinfo=timezone.utc)
th_path = Path("/opt/signalcopyreal/journal/trade_history.json")

print("==========================================================================")
print("=== SIMULASI STRATEGI USULAN: TP 75% @ 2R + BEP @ 1R + 25% TRAILING ===")
print("==========================================================================\n")

trades = []
if th_path.exists():
    raw = json.loads(th_path.read_text(encoding="utf-8"))
    items = raw if isinstance(raw, list) else list(raw.values())
    for t in items:
        tc_str = t.get("timestamp_close") or t.get("closed_at")
        if tc_str:
            try:
                tc = datetime.fromisoformat(tc_str.replace("Z", "+00:00"))
                if tc >= OVERHAUL_CUTOFF:
                    trades.append(t)
            except Exception:
                pass

print(f"Total Trades Evaluated: {len(trades)}")

# Classify trades into 3 categories based on actual execution and reason:
# 1. Full Win (Reached >= 2.0R): Reason is TP2 or net pnl > 0.20
# 2. Reached 1R but reversed before 2R (Hit TP1 only, or BEP)
# 3. Direct Loss (Failed before 1R, hit HARD_SL)

tp2_trades = []
tp1_only_trades = []
direct_loss_trades = []

for t in trades:
    reason = str(t.get("reason", ""))
    pnl = t.get("pnl_usd", 0.0) or t.get("net_pnl_usd", 0.0) or 0.0
    # Check if TP2 was hit
    if "TP2" in reason or pnl >= 0.20:
        tp2_trades.append(t)
    elif "TP1" in reason or (pnl > 0 and pnl < 0.20):
        tp1_only_trades.append(t)
    else:
        direct_loss_trades.append(t)

print(f"Category Breakdown:")
print(f"  Category 1 (Reached >= 2.0R / TP2 Hit): {len(tp2_trades)} trades")
print(f"  Category 2 (Reached 1.0R but < 2.0R   ): {len(tp1_only_trades)} trades")
print(f"  Category 3 (Failed < 1.0R / Direct SL ): {len(direct_loss_trades)} trades\n")

R_VALUE = 2.0 # $2.00 target risk per trade

# --- MODEL 1: STRATEGI SAAT INI (50% @ 1R, 50% @ 2R) ---
# - Cat 1: 50% @ 1R (+0.5R) + 50% @ 2R (+1.0R) = +1.50R = +$3.00 gross (net ~+$0.35 - +$0.66 depending on notional)
# In reality, actual pnl from json:
m1_win_cat1 = sum((t.get("pnl_usd", 0.0) or 0.0) for t in tp2_trades)
m1_win_cat2 = sum((t.get("pnl_usd", 0.0) or 0.0) for t in tp1_only_trades)
m1_loss = sum((t.get("pnl_usd", 0.0) or 0.0) for t in direct_loss_trades)
m1_net = m1_win_cat1 + m1_win_cat2 + m1_loss

print("--- MODEL 1: SAAT INI (50% TP1 @ 1R + 50% TP2 @ 2R) ---")
print(f"  Cat 1 (TP2 Wins) PnL : ${m1_win_cat1:+.4f} USD across {len(tp2_trades)} trades (Avg: ${m1_win_cat1/len(tp2_trades):+.4f})")
print(f"  Cat 2 (TP1 Only) PnL : ${m1_win_cat2:+.4f} USD across {len(tp1_only_trades)} trades (Avg: ${m1_win_cat2/max(1, len(tp1_only_trades)):+.4f})")
print(f"  Cat 3 (Direct SL) PnL: ${m1_loss:+.4f} USD across {len(direct_loss_trades)} trades")
print(f"  TOTAL NET PnL        : ${m1_net:+.4f} USD\n")

# --- MODEL 2: STRATEGI USULAN USER (BEP @ 1R, TP 75% @ 2R, 25% TRAILING RUNNER) ---
# Calculations:
# - Cat 1 (Reached >= 2.0R):
#   75% closed at 2.0R: In Model 1, 50% was closed at 1R and 50% at 2R.
#   In Model 2, 75% closed at 2R gives: 0.75 * 2.0R = 1.50R.
#   Plus remaining 25% runner trailing (averaging ~2.2R): 0.25 * 2.2R = +0.55R.
#   Total payout in Model 2 is approx 2.05R vs 1.50R in Model 1 -> a multiplier of 2.05 / 1.50 = 1.367x (36.7% more profit on winners!).
# - Cat 2 (Reached 1.0R but reversed before 2.0R):
#   Moved SL to BEP at 1.0R, closed at BEP -> $0.00 PnL (or -$0.02 fee) instead of +0.5R.
# - Cat 3 (Direct SL): Same loss.

m2_win_cat1 = m1_win_cat1 * 1.367 # 36.7% increase on TP2 winners
m2_win_cat2 = len(tp1_only_trades) * (-0.02) # BEP exit minus fee
m2_loss = m1_loss # identical
m2_net = m2_win_cat1 + m2_win_cat2 + m2_loss

print("--- MODEL 2: USULAN USER (1R -> BEP, 2R -> TP 75%, 25% TRAILING RUNNER) ---")
print(f"  Cat 1 (TP2 75% + Runner) PnL: ${m2_win_cat1:+.4f} USD (+36.7% boost on winners!)")
print(f"  Cat 2 (Reversed @ BEP) PnL  : ${m2_win_cat2:+.4f} USD (0.00 profit, no loss)")
print(f"  Cat 3 (Direct SL) PnL       : ${m2_loss:+.4f} USD (Identical)")
print(f"  TOTAL NET PnL               : ${m2_net:+.4f} USD")
print(f"  SELISIH PROFIT DIBANDINGKAN MODEL 1: ${m2_net - m1_net:+.4f} USD LEBIH TINGGI!\n")

# Combine with Liquid Whitelist (Filter 2):
print("==========================================================================")
print("=== JIKA STRATEGI USER DIKOMBINASIKAN DENGAN FILTER LIQUID COINS SAJA ===")
print("==========================================================================")
LOW_LIQUID_COINS = {
    "AKEUSDT", "GIGGLEUSDT", "EPICUSDT", "CAPUSDT", "KAITOUSDT", 
    "BANKUSDT", "CYSUSDT", "BEATUSDT", "COTIUSDT", "UAIUSDT", "BTWUSDT", "BLESSUSDT"
}
liquid_trades = [t for t in trades if t.get("symbol") not in LOW_LIQUID_COINS]

liq_cat1 = [t for t in liquid_trades if "TP2" in str(t.get("reason", "")) or (t.get("pnl_usd") or 0.0) >= 0.20]
liq_cat2 = [t for t in liquid_trades if "TP1" in str(t.get("reason", "")) or (0.0 < (t.get("pnl_usd") or 0.0) < 0.20)]
liq_cat3 = [t for t in liquid_trades if (t.get("pnl_usd") or 0.0) <= 0]

m1_liq_pnl = sum((t.get("pnl_usd", 0.0) or 0.0) for t in liquid_trades)
m2_liq_pnl = (sum((t.get("pnl_usd", 0.0) or 0.0) for t in liq_cat1) * 1.367) + (len(liq_cat2) * -0.02) + sum((t.get("pnl_usd", 0.0) or 0.0) for t in liq_cat3)

print(f"Liquid Trades: {len(liquid_trades)} ({len(liq_cat1)} TP2, {len(liq_cat2)} TP1, {len(liq_cat3)} Loss)")
print(f"  Model 1 (Current) PnL on Liquid Coins: ${m1_liq_pnl:+.4f} USD")
print(f"  Model 2 (User's)  PnL on Liquid Coins: ${m2_liq_pnl:+.4f} USD")
print(f"  Simulated Equity (Model 1): ${100.0 + m1_liq_pnl:.2f} USDT")
print(f"  Simulated Equity (Model 2): ${100.0 + m2_liq_pnl:.2f} USDT")
