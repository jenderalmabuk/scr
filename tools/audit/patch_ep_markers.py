ep_path = "/opt/signalcopyreal/signal_copy/entry_policy.py"

with open(ep_path, "r") as f:
    content = f.read()

start_marker = "    low, high = float(sig.entry_low), float(sig.entry_high)"
end_marker = "    conflicts = _market_conflicts(sig, price, metrics, score)"

if start_marker in content and end_marker in content:
    start_pos = content.index(start_marker)
    end_pos = content.index(end_marker)
    
    new_code = """    low, high = float(sig.entry_low), float(sig.entry_high)
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
            entry, action, code = boundary, EntryAction.LIMIT, "PRICE_OUTSIDE_ENTRY_ZONE"
    else:
        entry, action, code = boundary, EntryAction.LIMIT, "PRICE_OUTSIDE_ENTRY_ZONE"

    """
    new_content = content[:start_pos] + new_code + content[end_pos:]
    with open(ep_path, "w") as f:
        f.write(new_content)
    print("Patched entry_policy.py using marker replacement successfully!")
else:
    print("Error: start or end marker not found!")
