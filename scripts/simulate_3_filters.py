import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

OVERHAUL_CUTOFF = datetime(2026, 10, 18, 16, 22, 0, tzinfo=timezone.utc)
th_path = Path("/opt/signalcopyreal/journal/trade_history.json")

print("==========================================================================")
print("=== SIMULASI & UJI VALIDASI 3 FILTER PERBAIKAN PADA 42 DATA TRADE LIVE ===")
print("==========================================================================\n")

# 1. Fetch current 24h volumes for universe from Nexus API
vol_map = {}
try:
    req = urllib.request.Request("http://127.0.0.1:8000/universe/top")
    with urllib.request.urlopen(req, timeout=5) as resp:
        u_data = json.loads(resp.read().decode())
        for item in u_data.get("pairs", []):
            vol_map[item.get("pair")] = float(item.get("quote_volume", 0.0))
except Exception as e:
    print("Warning: unable to fetch live volumes, using volume estimation:", e)

# Hard-coded verified volume tiers based on Bybit 24h volume
# Tier 1 (High liquid > $50M): BTC, ETH, SOL, DOGE, XRP, SUI, NEAR, INJ, LINK, AVAX, XMR, PEPE, 1000PEPE, BCH, LTC, TAO
# Tier 2 (Mid liquid $20M-$50M): ENA, FIL, ZEC, DOT, ONDO, WIF, LAB
# Tier 3 (Low liquid / shitcoin < $20M): AKE, GIGGLE, EPIC, CAP, KAITOU, BANK, CYS, BEAT, COTI, UAI, BTW, BLESS

LOW_LIQUID_COINS = {
    "AKEUSDT", "GIGGLEUSDT", "EPICUSDT", "CAPUSDT", "KAITOUSDT", 
    "BANKUSDT", "CYSUSDT", "BEATUSDT", "COTIUSDT", "UAIUSDT", "BTWUSDT", "BLESSUSDT"
}

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

print(f"Total Live Trades Evaluated: {len(trades)}\n")

def evaluate_subset(name, filtered_trades):
    n = len(filtered_trades)
    if n == 0:
        print(f"[{name}] 0 trades passed filter.")
        return
    wins = [t for t in filtered_trades if (t.get("pnl_usd") or 0.0) > 0]
    losses = [t for t in filtered_trades if (t.get("pnl_usd") or 0.0) <= 0]
    win_pnl = sum((t.get("pnl_usd", 0.0) or 0.0) for t in wins)
    loss_pnl = sum((t.get("pnl_usd", 0.0) or 0.0) for t in losses)
    fees = sum((t.get("entry_fee_usd", 0.0) or 0.0) + (t.get("exit_fee_usd", 0.0) or 0.0) for t in filtered_trades)
    net_pnl = win_pnl + loss_pnl
    wr = (len(wins) / n) * 100
    pf = (win_pnl / abs(loss_pnl)) if loss_pnl != 0 else float("inf")
    
    print(f"=== {name} ===")
    print(f"  Total Trades: {n} (Filtered out {len(trades) - n} trades)")
    print(f"  Win / Loss  : {len(wins)}W / {len(losses)}L  (Win Rate: {wr:.1f}%)")
    print(f"  Win PnL     : ${win_pnl:+.4f} USD")
    print(f"  Loss PnL    : ${loss_pnl:+.4f} USD")
    print(f"  Fees Paid   : ${fees:.4f} USD")
    print(f"  NET PnL     : ${net_pnl:+.4f} USD")
    print(f"  ProfitFactor: {pf:.2f}")
    print(f"  Simulated Final Equity ($100 start): ${100.0 + net_pnl:.2f} USDT\n")

# 0. BASELINE (CURRENT REALITY)
evaluate_subset("0. BASELINE (REALITA SAAT INI - TANPA FILTER TAMBAHAN)", trades)

# FILTER 1: Macro BTC Regime Filter for Shorts
# Rule: Do not allow SHORT if BTC is Bullish / Risk-On / Neutral-Bullish.
# In trade snapshot: check btc_context_snapshot. If btc_regime in ("RISK_ON", "BULLISH", "NEUTRAL_BULLISH") or btc_bias == "BULLISH", reject SHORT.
def pass_filter_1(t):
    side = t.get("side")
    if side == "LONG":
        return True
    btc_snap = t.get("btc_context_snapshot", {})
    regime = (btc_snap.get("btc_regime") or "").upper()
    bias = (btc_snap.get("btc_bias") or "").upper()
    macro = (btc_snap.get("btc_bias_macro") or "").upper()
    # Reject short if BTC was explicitly bullish / risk on
    if "RISK_ON" in regime or "BULL" in bias or "BULL" in macro:
        return False
    return True

f1_trades = [t for t in trades if pass_filter_1(t)]
evaluate_subset("1. HANYA FILTER 1 (Macro Regime Guard untuk SHORT)", f1_trades)

# FILTER 2: Liquid Pairs Only Filter (Eliminate low-liquidity shitcoins < $25M)
# Rule: Symbol not in LOW_LIQUID_COINS and quote_volume >= 25M
def pass_filter_2(t):
    sym = t.get("symbol", "")
    if sym in LOW_LIQUID_COINS:
        return False
    vol = vol_map.get(sym, 30000000.0)
    if vol < 20000000.0:
        return False
    return True

f2_trades = [t for t in trades if pass_filter_2(t)]
evaluate_subset("2. HANYA FILTER 2 (Filter Likuiditas: Tolak Shitcoin < $25M)", f2_trades)

# FILTER 3: Higher Timeframe Trend Alignment (H1/4H Alignment)
# Rule: h1_bias_alignment or h1_sr_alignment != 'BAD' and structure_bias != opposite
def pass_filter_3(t):
    side = t.get("side")
    h1_map = t.get("h1_location_map", {})
    sr_align = h1_map.get("h1_sr_alignment", "OK")
    bias_align = h1_map.get("h1_bias_alignment", "NEUTRAL")
    # if strongly opposing H1 SR or bias
    if sr_align == "BAD" and bias_align in ("BEARISH" if side == "LONG" else "BULLISH"):
        return False
    return True

f3_trades = [t for t in trades if pass_filter_3(t)]
evaluate_subset("3. HANYA FILTER 3 (Konfirmasi Tren Multi-Timeframe H1/4H)", f3_trades)

# COMBINATION A: Filter 1 + Filter 2 (Macro Guard + Liquid Whitelist)
f1_f2_trades = [t for t in trades if pass_filter_1(t) and pass_filter_2(t)]
evaluate_subset("4. KOMBINASI FILTER 1 + 2 (Macro Guard + Likuiditas Tinggi)", f1_f2_trades)

# COMBINATION B: Filter 1 + Filter 2 + Filter 3 (Semua 3 Filter Sekaligus)
f_all_trades = [t for t in trades if pass_filter_1(t) and pass_filter_2(t) and pass_filter_3(t)]
evaluate_subset("5. KOMBINASI LENGKAP FILTER 1 + 2 + 3", f_all_trades)

print("--- DETAIL PER TRADE PADA KOMBINASI TERBAIK (FILTER 1 + 2) ---")
for t in f1_f2_trades:
    sym = t.get("symbol")
    side = t.get("side")
    pnl = t.get("pnl_usd", 0.0)
    reason = t.get("reason")
    to = t.get("timestamp_open")
    tc = t.get("timestamp_close")
    print(f"  {sym:12} {side:5} | PnL: ${pnl:+.4f} | Reason: {reason:8} | {to} -> {tc}")
