import os
import shutil

base_dir = "/opt/signalcopyreal/signal_copy"

# 1. Patch validation_config.py
vc_path = f"{base_dir}/validation_config.py"
shutil.copy2(vc_path, f"{vc_path}.bak_btc_gate")

with open(vc_path, "r") as f:
    vc_content = f.read()

if "BTC_GATE_ENABLED" not in vc_content:
    btc_config = """
# --- BTC regime & correlation gate ---
BTC_GATE_ENABLED = _bool_env("SIGNAL_COPY_BTC_GATE_ENABLED", True)
BTC_DUMP_THRESHOLD_PCT = _float_env("SIGNAL_COPY_BTC_DUMP_THRESHOLD_PCT", -0.35)
BTC_PUMP_THRESHOLD_PCT = _float_env("SIGNAL_COPY_BTC_PUMP_THRESHOLD_PCT", 0.35)
BTC_HIGH_BETA_CORR = _float_env("SIGNAL_COPY_BTC_HIGH_BETA_CORR", 0.50)
BTC_LOW_BETA_CORR = _float_env("SIGNAL_COPY_BTC_LOW_BETA_CORR", 0.40)
"""
    vc_content += btc_config
    with open(vc_path, "w") as f:
        f.write(vc_content)
    print("Patched validation_config.py")
else:
    print("validation_config.py already has BTC_GATE_ENABLED")

# 2. Patch orchestrator.py
orch_path = f"{base_dir}/orchestrator.py"
shutil.copy2(orch_path, f"{orch_path}.bak_btc_gate")

with open(orch_path, "r") as f:
    orch_content = f.read()

target_orch = """        # --- TradingView real-time indicator confluence ---
        try:
            from .tradingview_factor import TradingViewFactor
            tv = TradingViewFactor()
            tv_data = await tv.fetch(sig.symbol)
            if tv_data and tv_data.get("_ta"):
                tv_score = tv.compute_confluence(tv_data, sig.side.value)
                metrics["tradingview"] = tv_score
                logger.info("[TV] %s side=%s tv_score=%.1f",
                           sig.symbol, sig.side.value, tv_score.get("score", 0))
            else:
                logger.warning("[TV] no data returned for %s (rate limited?)", sig.symbol)
        except Exception as exc:
            logger.warning("[TV] fetch failed for %s: %s", sig.symbol, exc)"""

replacement_orch = target_orch + """

        # --- BTC Regime & Correlation Gate (Market Health Check) ---
        try:
            from .btc_regime_gate import get_btc_regime_gate
            gate = get_btc_regime_gate()
            btc_gate_res = await gate.evaluate_gate(sig.symbol, sig.side.value)
            metrics["btc_gate"] = btc_gate_res
            metrics["btc_correlation"] = btc_gate_res.get("correlation", 0.0)
            metrics["btc_bias"] = btc_gate_res.get("btc_regime", "NEUTRAL")
            metrics["btc_diff_pct"] = btc_gate_res.get("btc_diff_pct", 0.0)
            metrics["btc_vwap"] = btc_gate_res.get("btc_vwap", 0.0)
            logger.info("[BTC_GATE] %s %s -> allowed=%s (regime=%s, diff=%.2f%%, corr=%.2f, low_beta=%s)",
                       sig.symbol, sig.side.value, btc_gate_res.get("allowed"),
                       btc_gate_res.get("btc_regime"), btc_gate_res.get("btc_diff_pct", 0.0),
                       btc_gate_res.get("correlation", 0.0), btc_gate_res.get("is_low_beta"))
        except Exception as exc:
            logger.warning("[BTC_GATE] evaluation failed for %s: %s", sig.symbol, exc)"""

if "from .btc_regime_gate import get_btc_regime_gate" not in orch_content:
    if target_orch in orch_content:
        orch_content = orch_content.replace(target_orch, replacement_orch, 1)
        with open(orch_path, "w") as f:
            f.write(orch_content)
        print("Patched orchestrator.py")
    else:
        print("ERROR: target block not found in orchestrator.py")
else:
    print("orchestrator.py already has btc_regime_gate")

# 3. Patch validation_engine.py
ve_path = f"{base_dir}/validation_engine.py"
shutil.copy2(ve_path, f"{ve_path}.bak_btc_gate")

with open(ve_path, "r") as f:
    ve_content = f.read()

target_ve_block = """    sl_pct = sig.sl_distance_pct()
    if sl_pct is not None and sl_pct > vc.SAFETY_MAX_SL_DISTANCE_PCT:
        hard_blocks.append(f"SL distance {sl_pct:.1f}% exceeds safety cap {vc.SAFETY_MAX_SL_DISTANCE_PCT}%")"""

replacement_ve_block = target_ve_block + """

    # --- BTC Correlation & Smart Dump Gate ---
    btc_gate = metrics.get("btc_gate", {})
    if btc_gate.get("blocked", False):
        hard_blocks.append(btc_gate.get("reason", "BTC Dump Risk on High-Beta Pair"))"""

target_ve_snapshot = """                "flow_lookup_status", "flow_symbol_found", "mtf_alignment",
                "tradingview", "chart_vision",
            )"""

replacement_ve_snapshot = """                "flow_lookup_status", "flow_symbol_found", "mtf_alignment",
                "tradingview", "chart_vision",
                "btc_gate", "btc_correlation", "btc_bias", "btc_diff_pct", "btc_vwap",
            )"""

if "btc_gate = metrics.get(\"btc_gate\", {})" not in ve_content:
    if target_ve_block in ve_content and target_ve_snapshot in ve_content:
        ve_content = ve_content.replace(target_ve_block, replacement_ve_block, 1)
        ve_content = ve_content.replace(target_ve_snapshot, replacement_ve_snapshot, 1)
        with open(ve_path, "w") as f:
            f.write(ve_content)
        print("Patched validation_engine.py")
    else:
        print("ERROR: target blocks not found in validation_engine.py")
else:
    print("validation_engine.py already has btc_gate")

# 4. Patch telegram_formatter.py
tf_path = f"{base_dir}/telegram_formatter.py"
shutil.copy2(tf_path, f"{tf_path}.bak_btc_gate")

with open(tf_path, "r") as f:
    tf_content = f.read()

target_tf = """    if btc_bias and btc_bias != "NEUTRAL":
        lines.append(f"   BTC Bias: {btc_bias} | Corr: {btc_corr:.2f}")
    elif btc_corr:
        lines.append(f"   BTC Corr: {btc_corr:.2f}")"""

replacement_tf = """    btc_gate = metrics.get("btc_gate", {})
    btc_diff = metrics.get("btc_diff_pct")
    btc_vwap = metrics.get("btc_vwap")
    
    if btc_gate.get("is_low_beta"):
        lines.append(f"   BTC Corr: {btc_corr:+.2f} (Low-Beta / Momentum Mandiri 🟢)")
    elif btc_bias and btc_bias != "NEUTRAL":
        dump_icon = "🔴" if btc_bias in ("DUMP", "BEARISH") else "🟢"
        diff_str = f"vs VWAP {btc_diff:+.2f}%" if btc_diff is not None else ""
        lines.append(f"   BTC 15m: {btc_bias} {dump_icon} ({diff_str}) │ Corr: {btc_corr:.2f}")
    elif btc_corr:
        diff_str = f" │ VWAP Diff: {btc_diff:+.2f}%" if btc_diff is not None else ""
        lines.append(f"   BTC Corr: {btc_corr:.2f}{diff_str}")"""

if "btc_gate = metrics.get(\"btc_gate\", {})" not in tf_content:
    if target_tf in tf_content:
        tf_content = tf_content.replace(target_tf, replacement_tf, 1)
        with open(tf_path, "w") as f:
            f.write(tf_content)
        print("Patched telegram_formatter.py")
    else:
        print("ERROR: target block not found in telegram_formatter.py")
else:
    print("telegram_formatter.py already has btc_gate display")
