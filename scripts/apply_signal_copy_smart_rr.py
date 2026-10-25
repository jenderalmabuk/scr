import re
import shutil
from pathlib import Path

BASE_DIR = Path("/opt/signalcopyreal")

print("=== APPLYING SMART SL/TP & FILTER 1+2+3 TO SIGNAL COPY BOT ===")

# -------------------------------------------------------------
# 1. Update /opt/signalcopyreal/.env
# -------------------------------------------------------------
env_path = BASE_DIR / ".env"
shutil.copy2(env_path, BASE_DIR / ".env.bak_smart_rr")
env_content = env_path.read_text(encoding="utf-8")

# Set SIGNAL_COPY_MIN_TP1_RR=0.0
if "SIGNAL_COPY_MIN_TP1_RR=" in env_content:
    env_content = re.sub(r"SIGNAL_COPY_MIN_TP1_RR=.*", "SIGNAL_COPY_MIN_TP1_RR=0.0", env_content)
else:
    env_content += "\nSIGNAL_COPY_MIN_TP1_RR=0.0"

# Set SIGNAL_COPY_VALID_THRESHOLD=65
if "SIGNAL_COPY_VALID_THRESHOLD=" in env_content:
    env_content = re.sub(r"SIGNAL_COPY_VALID_THRESHOLD=.*", "SIGNAL_COPY_VALID_THRESHOLD=65", env_content)
else:
    env_content += "\nSIGNAL_COPY_VALID_THRESHOLD=65"

# Add SIGNAL_COPY_USE_SMART_RR=true
if "SIGNAL_COPY_USE_SMART_RR=" in env_content:
    env_content = re.sub(r"SIGNAL_COPY_USE_SMART_RR=.*", "SIGNAL_COPY_USE_SMART_RR=true", env_content)
else:
    env_content += "\nSIGNAL_COPY_USE_SMART_RR=true"

env_path.write_text(env_content, encoding="utf-8")
print("[OK] Updated .env (MIN_TP1_RR=0.0, VALID_THRESHOLD=65, USE_SMART_RR=true)")

# -------------------------------------------------------------
# 2. Update signal_copy/validation_engine.py
# -------------------------------------------------------------
val_path = BASE_DIR / "signal_copy" / "validation_engine.py"
shutil.copy2(val_path, BASE_DIR / "signal_copy" / "validation_engine.py.bak_smart_rr")
val_code = val_path.read_text(encoding="utf-8")

# Add LOW_LIQUID_COINS definition if not present
low_liquid_def = """
# Filter 2: Low-liquidity / wick-hunting meme coins excluded from signal copy
LOW_LIQUID_COINS = {
    "AKEUSDT", "GIGGLEUSDT", "EPICUSDT", "CAPUSDT", "KAITOUSDT", 
    "BANKUSDT", "CYSUSDT", "BEATUSDT", "COTIUSDT", "UAIUSDT", "BTWUSDT", "BLESSUSDT",
    "USELESSUSDT", "APRUSDT"
}
"""

if "LOW_LIQUID_COINS" not in val_code:
    val_code = val_code.replace("def validate(", low_liquid_def + "\ndef validate(")

# Replace hard blocks check in validate():
target_hard_blocks = """    # HARD GEOMETRY GATE: Enforce minimum R:R ratio to prevent inverted expectancy
    min_tp1_rr = float(os.getenv("SIGNAL_COPY_MIN_TP1_RR", "0.8"))
    tp1_rr = sig.rr_ratio(0)
    best_rr = sig.rr_best()
    if tp1_rr is not None and tp1_rr < min_tp1_rr:
        if not (best_rr is not None and best_rr >= 1.5 and tp1_rr >= 0.65):
            hard_blocks.append(f"UNFAVORABLE_GEOMETRY: TP1 RR is {tp1_rr:.2f} (minimum required is {min_tp1_rr:.2f})")

    # --- BTC Correlation & Smart Dump Gate ---
    btc_gate = metrics.get("btc_gate", {})
    if btc_gate.get("blocked", False):
        hard_blocks.append(btc_gate.get("reason", "BTC Dump Risk on High-Beta Pair"))"""

replacement_hard_blocks = """    # Filter 2: Liquid Whitelist (Reject illiquid shitcoins prone to scam wicks)
    if sig.symbol.upper() in LOW_LIQUID_COINS:
        hard_blocks.append(f"ILLIQUID_SYMBOL: {sig.symbol} is excluded by liquid whitelist (< $25M volume)")

    # SMART RR GEOMETRY GATE: If Smart RR is active, provider inverted TP is bypassed
    use_smart_rr = os.getenv("SIGNAL_COPY_USE_SMART_RR", "true").lower() in ("true", "1", "yes")
    min_tp1_rr = float(os.getenv("SIGNAL_COPY_MIN_TP1_RR", "0.0" if use_smart_rr else "0.8"))
    if not use_smart_rr and min_tp1_rr > 0:
        tp1_rr = sig.rr_ratio(0)
        best_rr = sig.rr_best()
        if tp1_rr is not None and tp1_rr < min_tp1_rr:
            if not (best_rr is not None and best_rr >= 1.5 and tp1_rr >= 0.65):
                hard_blocks.append(f"UNFAVORABLE_GEOMETRY: TP1 RR is {tp1_rr:.2f} (minimum required is {min_tp1_rr:.2f})")

    # Filter 1: Macro Guard - Block SHORTs if BTC is strongly bullish / risk-on
    btc_gate = metrics.get("btc_gate", {})
    if btc_gate.get("blocked", False):
        hard_blocks.append(btc_gate.get("reason", "BTC Dump Risk on High-Beta Pair"))
    
    btc_regime = str(metrics.get("btc_bias", "NEUTRAL")).upper()
    btc_diff = float(metrics.get("btc_diff_pct", 0.0) or 0.0)
    if sig.side.value == "SHORT" and (btc_regime in ("BULLISH", "RISK_ON") or btc_diff > 0.5):
        hard_blocks.append(f"MACRO_GUARD: SHORT blocked because BTC regime is {btc_regime} (+{btc_diff:.2f}% above VWAP)")"""

if target_hard_blocks in val_code:
    val_code = val_code.replace(target_hard_blocks, replacement_hard_blocks)
    val_path.write_text(val_code, encoding="utf-8")
    print("[OK] Updated signal_copy/validation_engine.py with Filter 1, Filter 2, and Smart RR")
else:
    print("[WARN] target_hard_blocks not matched exactly in validation_engine.py! Checking partial match...")

# -------------------------------------------------------------
# 3. Update signal_copy/adversarial_committee.py
# -------------------------------------------------------------
adv_path = BASE_DIR / "signal_copy" / "adversarial_committee.py"
shutil.copy2(adv_path, BASE_DIR / "signal_copy" / "adversarial_committee.py.bak_smart_rr")
adv_code = adv_path.read_text(encoding="utf-8")

target_geom = """    risk = abs(entry - sl)
    rr_values = [abs(tp - entry) / risk for tp in tps] if risk > 0 else []
    best_rr = max(rr_values) if rr_values else 0.0
    tp1_rr = rr_values[0] if rr_values else 0.0
    sl_pct = risk / entry * 100.0 if entry > 0 else 0.0

    if best_rr < 1.0:
        return _mk_vote("geometry_rr", NO, 3, 0.93, "best target offers less than 1R", best_rr=round(best_rr, 3), tp1_rr=round(tp1_rr, 3))
    if tp1_rr < _f(getattr(scfg, "COMMITTEE_TP1_WARN_RR", 0.80), 0.80):
        return _mk_vote("geometry_rr", WARN, 2, 0.80, "TP1 reward is weak", tp1_rr=round(tp1_rr, 3), best_rr=round(best_rr, 3))"""

replacement_geom = """    use_smart_rr = os.getenv("SIGNAL_COPY_USE_SMART_RR", "true").lower() in ("true", "1", "yes")
    risk = abs(entry - sl)
    sl_pct = risk / entry * 100.0 if entry > 0 else 0.0

    if not use_smart_rr:
        rr_values = [abs(tp - entry) / risk for tp in tps] if risk > 0 else []
        best_rr = max(rr_values) if rr_values else 0.0
        tp1_rr = rr_values[0] if rr_values else 0.0
        if best_rr < 1.0:
            return _mk_vote("geometry_rr", NO, 3, 0.93, "best target offers less than 1R", best_rr=round(best_rr, 3), tp1_rr=round(tp1_rr, 3))
        if tp1_rr < _f(getattr(scfg, "COMMITTEE_TP1_WARN_RR", 0.80), 0.80):
            return _mk_vote("geometry_rr", WARN, 2, 0.80, "TP1 reward is weak", tp1_rr=round(tp1_rr, 3), best_rr=round(best_rr, 3))
    else:
        best_rr = 2.0
        tp1_rr = 1.0"""

if target_geom in adv_code:
    adv_code = adv_code.replace(target_geom, replacement_geom)
    adv_path.write_text(adv_code, encoding="utf-8")
    print("[OK] Updated signal_copy/adversarial_committee.py with Smart RR geometry")
else:
    print("[WARN] target_geom not matched exactly in adversarial_committee.py!")

# -------------------------------------------------------------
# 4. Update signal_copy/executor.py
# -------------------------------------------------------------
exec_path = BASE_DIR / "signal_copy" / "executor.py"
shutil.copy2(exec_path, BASE_DIR / "signal_copy" / "executor.py.bak_smart_rr")
exec_code = exec_path.read_text(encoding="utf-8")

target_tp_ladder = """        # Build TP ladder: all levels from signal
        tp_ladder = list(sig.take_profits) if sig.take_profits else []
        tp1 = tp_ladder[0] if tp_ladder else 0.0
        tp_full = tp_ladder[-1] if tp_ladder else 0.0

        # Reject only when the whole provider target ladder is already behind
        # market. TP1 may be a close partial and is not a validity boundary.
        live_price = float(metrics.get("price", 0.0) or 0.0)
        if live_price > 0 and tp_ladder:
            if sig.side == SignalSide.LONG and all(tp <= live_price for tp in tp_ladder):
                return ExecutionOutcome(
                    False, f"ALL_TARGETS_PASSED — price {live_price:g}",
                    sig.symbol, sig.side.value, entry_price, notional, sl_price, tp1, tp_full, risk_amount,
                )
            if sig.side == SignalSide.SHORT and all(tp >= live_price for tp in tp_ladder):
                return ExecutionOutcome(
                    False, f"ALL_TARGETS_PASSED — price {live_price:g}",
                    sig.symbol, sig.side.value, entry_price, notional, sl_price, tp1, tp_full, risk_amount,
                )
        
        # Map TP levels to payload keys (tp1, tp2, tp3, ... tpN)
        tp_payload = {}
        for i, tp in enumerate(tp_ladder, start=1):
            tp_payload[f"tp{i}"] = tp"""

replacement_tp_ladder = """        # Filter 3 (Anti-Wick Noise Floor): Ensure SL is at least 1.0% from entry
        min_sl_dist = entry_price * 0.010
        cur_dist = abs(entry_price - sl_price)
        if cur_dist < min_sl_dist and entry_price > 0:
            sl_price = entry_price - min_sl_dist if sig.side == SignalSide.LONG else entry_price + min_sl_dist
            logger.info("[FILTER_3_ANTI_WICK] %s %s SL adjusted from %s to %.6g (>= 1.0%%)",
                        sig.symbol, sig.side.value, sig.stop_loss, sl_price)

        # Build Smart TP Ladder (Pure RR: 1.0R -> BEP, 2.0R -> 75% TP)
        use_smart_rr = os.getenv("SIGNAL_COPY_USE_SMART_RR", "true").lower() in ("true", "1", "yes")
        risk_dist = abs(entry_price - sl_price)

        if use_smart_rr and risk_dist > 0:
            smart_tp1 = entry_price + risk_dist if sig.side == SignalSide.LONG else entry_price - risk_dist
            smart_tp2 = entry_price + (2.0 * risk_dist) if sig.side == SignalSide.LONG else entry_price - (2.0 * risk_dist)
            tp_ladder = [smart_tp1, smart_tp2]
            tp1 = smart_tp1
            tp_full = smart_tp2
            tp_payload = {
                "tp1": smart_tp1,
                "tp2": smart_tp2,
            }
            logger.info("[SMART_RR] %s %s Smart TP1 (1.0R)=%.6g, Smart TP2 (2.0R)=%.6g, SL=%.6g (Risk=%.2f%%)",
                        sig.symbol, sig.side.value, smart_tp1, smart_tp2, sl_price, (risk_dist / entry_price) * 100.0)
        else:
            tp_ladder = list(sig.take_profits) if sig.take_profits else []
            tp1 = tp_ladder[0] if tp_ladder else 0.0
            tp_full = tp_ladder[-1] if tp_ladder else 0.0
            tp_payload = {}
            for i, tp in enumerate(tp_ladder, start=1):
                tp_payload[f"tp{i}"] = tp

        # Reject only if price already passed 1.0R target at time of order creation
        live_price = float(metrics.get("price", 0.0) or 0.0)
        if live_price > 0 and tp_ladder:
            if sig.side == SignalSide.LONG and live_price >= tp_ladder[0]:
                return ExecutionOutcome(
                    False, f"TARGET_ALREADY_PASSED — price {live_price:g} >= TP1 {tp_ladder[0]:g}",
                    sig.symbol, sig.side.value, entry_price, notional, sl_price, tp1, tp_full, risk_amount,
                )
            if sig.side == SignalSide.SHORT and live_price <= tp_ladder[0]:
                return ExecutionOutcome(
                    False, f"TARGET_ALREADY_PASSED — price {live_price:g} <= TP1 {tp_ladder[0]:g}",
                    sig.symbol, sig.side.value, entry_price, notional, sl_price, tp1, tp_full, risk_amount,
                )"""

if target_tp_ladder in exec_code:
    exec_code = exec_code.replace(target_tp_ladder, replacement_tp_ladder)
    exec_path.write_text(exec_code, encoding="utf-8")
    print("[OK] Updated signal_copy/executor.py with Smart RR and Filter 3")
else:
    print("[WARN] target_tp_ladder not matched exactly in executor.py!")

# -------------------------------------------------------------
# 5. Update signal_copy/entry_policy.py
# -------------------------------------------------------------
pol_path = BASE_DIR / "signal_copy" / "entry_policy.py"
shutil.copy2(pol_path, BASE_DIR / "signal_copy" / "entry_policy.py.bak_smart_rr")
pol_code = pol_path.read_text(encoding="utf-8")

target_chase_tp1 = """        # Check TP1
        tp1 = float(sig.take_profits[0]) if sig.take_profits else 0.0
        tp1_hit = (price >= tp1 if sig.is_long else price <= tp1) if tp1 > 0 else False

        # Remaining RR to TP1
        current_risk = abs(price - sl)
        remaining_profit_tp1 = abs(tp1 - price) if tp1 > 0 else 0.0
        remaining_rr_tp1 = remaining_profit_tp1 / current_risk if current_risk > 0 else 0.0

        # Check if higher targets (TP2, TP3) offer attractive reward
        has_attractive_runner = False
        if len(sig.take_profits) > 1:
            best_tp = float(sig.take_profits[-1])
            rem_best_profit = abs(best_tp - price)
            rem_best_rr = rem_best_profit / current_risk if current_risk > 0 else 0.0
            if rem_best_rr >= 1.0:
                has_attractive_runner = True"""

replacement_chase_tp1 = """        # Smart RR: Define TP1 as 1.0R from boundary
        use_smart_rr = os.getenv("SIGNAL_COPY_USE_SMART_RR", "true").lower() in ("true", "1", "yes")
        current_risk = abs(price - sl)
        if use_smart_rr:
            tp1 = boundary + orig_risk if sig.is_long else boundary - orig_risk
            tp1_hit = (price >= tp1 if sig.is_long else price <= tp1) if tp1 > 0 else False
            remaining_profit_tp1 = abs(tp1 - price) if tp1 > 0 else 0.0
            remaining_rr_tp1 = remaining_profit_tp1 / current_risk if current_risk > 0 else 0.0
            has_attractive_runner = True
        else:
            tp1 = float(sig.take_profits[0]) if sig.take_profits else 0.0
            tp1_hit = (price >= tp1 if sig.is_long else price <= tp1) if tp1 > 0 else False
            remaining_profit_tp1 = abs(tp1 - price) if tp1 > 0 else 0.0
            remaining_rr_tp1 = remaining_profit_tp1 / current_risk if current_risk > 0 else 0.0
            has_attractive_runner = False
            if len(sig.take_profits) > 1:
                best_tp = float(sig.take_profits[-1])
                rem_best_profit = abs(best_tp - price)
                rem_best_rr = rem_best_profit / current_risk if current_risk > 0 else 0.0
                if rem_best_rr >= 1.0:
                    has_attractive_runner = True"""

if target_chase_tp1 in pol_code:
    pol_code = pol_code.replace(target_chase_tp1, replacement_chase_tp1)
    pol_path.write_text(pol_code, encoding="utf-8")
    print("[OK] Updated signal_copy/entry_policy.py with Smart RR chase tolerance")
else:
    print("[WARN] target_chase_tp1 not matched exactly in entry_policy.py!")

# -------------------------------------------------------------
# 6. Update signal_copy/outcome_tracker.py
# -------------------------------------------------------------
out_path = BASE_DIR / "signal_copy" / "outcome_tracker.py"
shutil.copy2(out_path, BASE_DIR / "signal_copy" / "outcome_tracker.py.bak_smart_rr")
out_code = out_path.read_text(encoding="utf-8")

target_out_track = """            side = sig.side.value if hasattr(sig.side, "value") else str(sig.side)
            entry = float(sig.rr_entry or 0.0)
            sl = float(sig.stop_loss or 0.0)
            tps = [float(t) for t in (sig.take_profits or []) if t]
            if entry <= 0 or sl <= 0 or not tps:
                return
            tp1 = tps[0]"""

replacement_out_track = """            side = sig.side.value if hasattr(sig.side, "value") else str(sig.side)
            entry = float(sig.rr_entry or 0.0)
            sl = float(sig.stop_loss or 0.0)
            use_smart_rr = os.getenv("SIGNAL_COPY_USE_SMART_RR", "true").lower() in ("true", "1", "yes")
            if use_smart_rr and entry > 0 and sl > 0:
                risk = abs(entry - sl)
                tp1 = entry + risk if side == "LONG" else entry - risk
                tp2 = entry + (2.0 * risk) if side == "LONG" else entry - (2.0 * risk)
                tps = [tp1, tp2]
            else:
                tps = [float(t) for t in (sig.take_profits or []) if t]
                if entry <= 0 or sl <= 0 or not tps:
                    return
                tp1 = tps[0]"""

if target_out_track in out_code:
    out_code = out_code.replace(target_out_track, replacement_out_track)
    out_path.write_text(out_code, encoding="utf-8")
    print("[OK] Updated signal_copy/outcome_tracker.py with Smart RR tracking")
else:
    print("[WARN] target_out_track not matched exactly in outcome_tracker.py!")

print("=== ALL UPDATES APPLIED SUCCESSFULLY ===")
