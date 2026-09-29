import os
import shutil
import subprocess

print("Applying entry policy & config fixes...")

# 1. Update /opt/signalcopyreal/.env
env_path = "/opt/signalcopyreal/.env"
shutil.copy2(env_path, f"{env_path}.bak_entry_fix")

with open(env_path, "r") as f:
    lines = f.readlines()

new_lines = []
env_updates = {
    "SIGNAL_COPY_AUTO_MARKET_MIN_SCORE": "60",
    "SIGNAL_COPY_FLOW_NO_TRADE_MARKET_BLOCK": "false",
    "SIGNAL_COPY_MAX_CHASE_R": "0.50",
    "SIGNAL_COPY_MIN_REMAINING_RR": "0.30",
    "SIGNAL_COPY_SINGLE_PRICE_ZONE_BUFFER_PCT": "0.005",
}
seen_keys = set()

for line in lines:
    stripped = line.strip()
    updated = False
    for k, v in env_updates.items():
        if stripped.startswith(f"{k}="):
            new_lines.append(f"{k}={v}\n")
            seen_keys.add(k)
            updated = True
            print(f"Updated .env: {k}={v}")
            break
    if not updated:
        new_lines.append(line)

for k, v in env_updates.items():
    if k not in seen_keys:
        new_lines.append(f"{k}={v}\n")
        print(f"Added to .env: {k}={v}")

with open(env_path, "w") as f:
    f.writelines(new_lines)

# 2. Update validation_config.py
vc_path = "/opt/signalcopyreal/signal_copy/validation_config.py"
shutil.copy2(vc_path, f"{vc_path}.bak_entry_fix")

with open(vc_path, "r") as f:
    vc_content = f.read()

# Replace defaults in validation_config.py
vc_content = vc_content.replace(
    'AUTO_MARKET_MIN_SCORE = _float_env("SIGNAL_COPY_AUTO_MARKET_MIN_SCORE", 65.0)',
    'AUTO_MARKET_MIN_SCORE = _float_env("SIGNAL_COPY_AUTO_MARKET_MIN_SCORE", 60.0)'
)
vc_content = vc_content.replace(
    'FLOW_NO_TRADE_MARKET_BLOCK = _bool_env("SIGNAL_COPY_FLOW_NO_TRADE_MARKET_BLOCK", True)',
    'FLOW_NO_TRADE_MARKET_BLOCK = _bool_env("SIGNAL_COPY_FLOW_NO_TRADE_MARKET_BLOCK", False)'
)
vc_content = vc_content.replace(
    'MAX_CHASE_R = _float_env("SIGNAL_COPY_MAX_CHASE_R", 0.35)',
    'MAX_CHASE_R = _float_env("SIGNAL_COPY_MAX_CHASE_R", 0.50)'
)
vc_content = vc_content.replace(
    'MIN_REMAINING_RR = _float_env("SIGNAL_COPY_MIN_REMAINING_RR", 0.70)',
    'MIN_REMAINING_RR = _float_env("SIGNAL_COPY_MIN_REMAINING_RR", 0.30)'
)

if "SINGLE_PRICE_ZONE_BUFFER_PCT" not in vc_content:
    vc_content += '\nSINGLE_PRICE_ZONE_BUFFER_PCT = _float_env("SIGNAL_COPY_SINGLE_PRICE_ZONE_BUFFER_PCT", 0.005)\n'

with open(vc_path, "w") as f:
    f.write(vc_content)
print("Updated validation_config.py")

# 3. Update entry_policy.py
ep_path = "/opt/signalcopyreal/signal_copy/entry_policy.py"
shutil.copy2(ep_path, f"{ep_path}.bak_entry_fix")

with open(ep_path, "r") as f:
    ep_content = f.read()

old_block = """    low, high = float(sig.entry_low), float(sig.entry_high)
    boundary = low if price < low else high
    
    # Check if price is in discount (cheaper than entry zone) or chase (past entry zone in profit)
    is_discount = price < low if sig.is_long else price > high
    is_chase = price > high if sig.is_long else price < low

    if low <= price <= high:
        entry, action, code = price, EntryAction.MARKET, "PRICE_INSIDE_ENTRY_ZONE"
    elif is_discount:
        # DISCOUNT ZONE: price is cheaper than recommended entry!
        orig_risk = abs(boundary - sl) if sl > 0 else boundary * 0.01
        discount_r = abs(boundary - price) / orig_risk if orig_risk > 0 else 0.0
        current_risk = abs(price - sl)
        max_discount_r = _f(getattr(vc, "MAX_DISCOUNT_R", 0.60), 0.60)
        discount_enabled = bool(getattr(vc, "DISCOUNT_FILL_ENABLED", True))
        
        # Guard: must not be sitting right on top of SL (at least 35% of original risk buffer remains)
        sl_buffer_ok = current_risk >= orig_risk * 0.35
        
        if discount_enabled and discount_r <= max_discount_r and sl_buffer_ok:
            entry, action, code = price, EntryAction.MARKET, "DISCOUNT_ENTRY_ZONE"
        else:
            entry, action, code = boundary, EntryAction.LIMIT, "DISCOUNT_TOO_DEEP_LIMIT"
    elif is_chase:
        # CHASE / MOMENTUM ZONE: price has already run in the profit direction
        orig_risk = abs(boundary - sl) if sl > 0 else boundary * 0.01
        drift_r = abs(price - boundary) / orig_risk if orig_risk > 0 else float("inf")
        max_chase_r = _f(getattr(vc, "MAX_CHASE_R", 0.35), 0.35)
        min_remaining_rr = _f(getattr(vc, "MIN_REMAINING_RR", 0.70), 0.70)
        
        # Check TP1
        tp1 = float(sig.take_profits[0]) if sig.take_profits else 0.0
        tp1_hit = (price >= tp1 if sig.is_long else price <= tp1) if tp1 > 0 else False
        
        # Remaining RR to TP1
        current_risk = abs(price - sl)
        remaining_profit_tp1 = abs(tp1 - price) if tp1 > 0 else 0.0
        remaining_rr_tp1 = remaining_profit_tp1 / current_risk if current_risk > 0 else 0.0
        
        # RSI exhaustion check
        rsi = _f(metrics.get("rsi"), 50.0)
        rsi_exhausted = (sig.is_long and rsi > 72.0) or (not sig.is_long and rsi < 28.0) if rsi > 0 else False
        
        if not tp1_hit and drift_r <= max_chase_r and remaining_rr_tp1 >= min_remaining_rr and not rsi_exhausted:
            entry, action, code = price, EntryAction.MARKET, "MOMENTUM_CHASE_ALLOWED"
        else:
            entry, action, code = boundary, EntryAction.LIMIT, "PRICE_OUTSIDE_ENTRY_ZONE" """

new_block = """    low, high = float(sig.entry_low), float(sig.entry_high)
    boundary = low if price < low else high

    # Smart entry zone buffer for single-price or narrow zone (<0.3% wide)
    zone_width_pct = (high - low) / low if low > 0 else 0.0
    buffer_pct = _f(getattr(vc, "SINGLE_PRICE_ZONE_BUFFER_PCT", 0.005), 0.005)
    if zone_width_pct < 0.003:
        orig_risk = abs(boundary - sl) if sl > 0 else boundary * 0.01
        buf = max(orig_risk * 0.15, boundary * buffer_pct)
        eff_low = low - buf
        eff_high = high + buf
    else:
        eff_low = low
        eff_high = high

    # Check if price is in discount (cheaper than entry zone) or chase (past entry zone in profit)
    is_discount = price < eff_low if sig.is_long else price > eff_high
    is_chase = price > eff_high if sig.is_long else price < eff_low

    if eff_low <= price <= eff_high:
        entry, action, code = price, EntryAction.MARKET, "PRICE_INSIDE_ENTRY_ZONE"
    elif is_discount:
        # DISCOUNT ZONE: price is cheaper than recommended entry!
        orig_risk = abs(boundary - sl) if sl > 0 else boundary * 0.01
        discount_r = abs(boundary - price) / orig_risk if orig_risk > 0 else 0.0
        current_risk = abs(price - sl)
        max_discount_r = _f(getattr(vc, "MAX_DISCOUNT_R", 0.60), 0.60)
        discount_enabled = bool(getattr(vc, "DISCOUNT_FILL_ENABLED", True))

        # Guard: must not be sitting right on top of SL (at least 35% of original risk buffer remains)
        sl_buffer_ok = current_risk >= orig_risk * 0.35

        if discount_enabled and discount_r <= max_discount_r and sl_buffer_ok:
            entry, action, code = price, EntryAction.MARKET, "DISCOUNT_ENTRY_ZONE"
        else:
            entry, action, code = boundary, EntryAction.LIMIT, "DISCOUNT_TOO_DEEP_LIMIT"
    elif is_chase:
        # CHASE / MOMENTUM ZONE: price has already run in the profit direction
        orig_risk = abs(boundary - sl) if sl > 0 else boundary * 0.01
        drift_r = abs(price - boundary) / orig_risk if orig_risk > 0 else float("inf")
        max_chase_r = _f(getattr(vc, "MAX_CHASE_R", 0.50), 0.50)
        min_remaining_rr = _f(getattr(vc, "MIN_REMAINING_RR", 0.30), 0.30)

        # Check TP1
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
                has_attractive_runner = True

        # RSI exhaustion check
        rsi = _f(metrics.get("rsi"), 50.0)
        rsi_exhausted = (sig.is_long and rsi > 74.0) or (not sig.is_long and rsi < 26.0) if rsi > 0 else False

        if not tp1_hit and drift_r <= max_chase_r and (remaining_rr_tp1 >= min_remaining_rr or has_attractive_runner) and not rsi_exhausted:
            entry, action, code = price, EntryAction.MARKET, "MOMENTUM_CHASE_ALLOWED"
        else:
            entry, action, code = boundary, EntryAction.LIMIT, "PRICE_OUTSIDE_ENTRY_ZONE" """

# Strip trailing spaces on each line for reliable matching
old_lines = [l.rstrip() for l in old_block.strip().splitlines()]
ep_lines = [l.rstrip() for l in ep_content.splitlines()]

# Find start index
start_idx = -1
for i in range(len(ep_lines) - len(old_lines) + 1):
    match = True
    for j in range(len(old_lines)):
        if ep_lines[i+j] != old_lines[j]:
            match = False
            break
    if match:
        start_idx = i
        break

if start_idx != -1:
    before = "\n".join(ep_lines[:start_idx])
    after = "\n".join(ep_lines[start_idx + len(old_lines):])
    ep_content = before + "\n" + new_block.strip() + "\n" + after
    with open(ep_path, "w") as f:
        f.write(ep_content)
    print(f"Patched entry_policy.py at line {start_idx+1} successfully!")
else:
    print("WARNING: Could not find exact matching block in entry_policy.py!")
