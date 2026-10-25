import re

path = "/opt/signalcopyreal/execution/paper_mainnet_trader.py"
with open(path, "r", encoding="utf-8") as f:
    content = f.read()

# 1. Add source and tp_plan to position dict
target_pos_dict = '"tag": str(params.get("tag", "")),'
replace_pos_dict = '"tag": str(params.get("tag", "")),\n            "source": str(params.get("source", "")),\n            "tp_plan": str(params.get("tp_plan") or _adv_snapshot.get("tp_plan") or ""), '

if target_pos_dict in content and '"source": str(params.get("source"' not in content:
    content = content.replace(target_pos_dict, replace_pos_dict, 1)
    print("Patched submit_open with source and tp_plan!")
else:
    print("submit_open already patched or target not found.")

# 2. Update _check_tp_ladder with Golden Scalp support
target_tp_ladder = """            # --- STAGE 1: Hit TP1 (1.0R) -> Move SL to BEP (Breakeven + 0.1% buffer), 100% lot retained ---
            if idx == 0:
                pos["tp1_hit"] = True
                pos["tp1_hit_at"] = pos.get("tp1_hit_at") or datetime.now(UTC)
                pos["shadow_cohort"] = True
                self._record_shadow_tp(pos, 0, tp)
                entry_p = float(pos["entry_price"])
                side = str(pos["side"]).upper()
                bep_sl = entry_p * 1.001 if side == "LONG" else entry_p * 0.999
                cur_sl = float(pos.get("sl_price") or 0)
                if side == "LONG":
                    pos["sl_price"] = max(cur_sl, bep_sl)
                else:
                    pos["sl_price"] = min(cur_sl, bep_sl) if cur_sl > 0 else bep_sl
                pos["sl_kind"] = "BREAKEVEN_1R"
                pos["next_tp_index"] = 1
                _persist_positions(self.positions)
                logger.info("[1R_REACHED_BEP] %s %s hit 1.0R (%.6g) -> SL moved to BEP (%.6g). 100%% lot retained (Risk-Free)!",
                            symbol, side, tp, pos["sl_price"])
                idx = 1
                continue
            
            # --- STAGE 2: Hit TP2 (2.0R) -> Close 75% of initial position, activate Trailing Stop on 25% runner ---
            if idx == 1:
                pos["tp2_hit"] = True
                pos["tp2_hit_at"] = datetime.now(UTC)
                self._record_shadow_tp(pos, 1, tp)
                initial_qty = float(pos["initial_qty"])
                target_remaining = initial_qty * 0.25
                cur_qty = float(pos.get("qty", 0.0))
                slice_qty = min(cur_qty, max(0.0, cur_qty - target_remaining))
                if slice_qty > 0:
                    pos["qty"] = max(0.0, cur_qty - slice_qty)
                    await self._realize_partial(pos, tp, slice_qty, "TP2_75PCT_PARTIAL")
                pos["next_tp_index"] = 2
                pos["locked_profit"] = True
                pos["trail_active"] = True
                pos["sl_kind"] = "TRAILING_RUNNER"
                _persist_positions(self.positions)
                logger.info("[2R_REACHED_TP2] %s %s hit 2.0R (%.6g) -> Closed 75%% lot! 25%% runner trailing activated.",
                            symbol, side, tp)
                break"""

replacement_tp_ladder = """            is_golden_scalp = (
                str(pos.get("source", "")).upper() == "AUTONOMOUS_SCANNER"
                or str(pos.get("tag", "")).startswith("auto_")
                or pos.get("tp_plan") == "GOLDEN_SCALP"
                or (isinstance(pos.get("adv_snapshot"), dict) and pos["adv_snapshot"].get("tp_plan") == "GOLDEN_SCALP")
            )

            if is_golden_scalp:
                # --- GOLDEN SCALP STAGE 1: Hit TP1 (1.5R) -> Realize 75% partial, Lock SL at +1.0R (Guaranteed Profit), Trail 25% runner ---
                if idx == 0:
                    pos["tp1_hit"] = True
                    pos["tp1_hit_at"] = pos.get("tp1_hit_at") or datetime.now(UTC)
                    pos["shadow_cohort"] = True
                    self._record_shadow_tp(pos, 0, tp)
                    initial_qty = float(pos["initial_qty"])
                    target_remaining = initial_qty * 0.25
                    cur_qty = float(pos.get("qty", 0.0))
                    slice_qty = min(cur_qty, max(0.0, cur_qty - target_remaining))
                    if slice_qty > 0:
                        pos["qty"] = max(0.0, cur_qty - slice_qty)
                        await self._realize_partial(pos, tp, slice_qty, "TP1_75PCT_GOLDEN")
                    
                    entry_p = float(pos["entry_price"])
                    orig_sl = float(pos.get("provider_sl_original") or pos.get("sl_price") or entry_p)
                    risk_per_unit = abs(entry_p - orig_sl)
                    side = str(pos["side"]).upper()
                    lock_1r_sl = entry_p + (1.0 * risk_per_unit) if side == "LONG" else entry_p - (1.0 * risk_per_unit)
                    cur_sl = float(pos.get("sl_price") or 0)
                    if side == "LONG":
                        pos["sl_price"] = max(cur_sl, lock_1r_sl)
                    else:
                        pos["sl_price"] = min(cur_sl, lock_1r_sl) if cur_sl > 0 else lock_1r_sl
                    
                    pos["sl_kind"] = "LOCKED_PROFIT_1R"
                    pos["locked_profit"] = True
                    pos["trail_active"] = True
                    pos["next_tp_index"] = 1
                    _persist_positions(self.positions)
                    logger.info("[1.5R_GOLDEN_TP1] %s %s hit 1.5R (%.6g) -> Closed 75%% lot! SL locked to +1.0R (%.6g). 25%% runner trailing activated!",
                                symbol, side, tp, pos["sl_price"])
                    idx = 1
                    continue

                # --- GOLDEN SCALP STAGE 2: Hit TP2 (3.0R) -> Close remaining 25% runner ---
                if idx == 1:
                    pos["tp2_hit"] = True
                    pos["tp2_hit_at"] = datetime.now(UTC)
                    self._record_shadow_tp(pos, 1, tp)
                    cur_qty = float(pos.get("qty", 0.0))
                    if cur_qty > 0:
                        pos["qty"] = 0.0
                        await self._realize_partial(pos, tp, cur_qty, "TP2_RUNNER_FULL")
                    pos["next_tp_index"] = 2
                    pos["status"] = "CLOSED"
                    _persist_positions(self.positions)
                    logger.info("[3.0R_GOLDEN_TP2] %s %s hit 3.0R (%.6g) -> Runner 25%% full target reached! Position closed.",
                                symbol, side, tp)
                    break
            else:
                # --- STANDARD SIGNAL COPY: Hit TP1 (1.0R) -> Move SL to BEP (Breakeven + 0.1% buffer) ---
                if idx == 0:
                    pos["tp1_hit"] = True
                    pos["tp1_hit_at"] = pos.get("tp1_hit_at") or datetime.now(UTC)
                    pos["shadow_cohort"] = True
                    self._record_shadow_tp(pos, 0, tp)
                    entry_p = float(pos["entry_price"])
                    side = str(pos["side"]).upper()
                    bep_sl = entry_p * 1.001 if side == "LONG" else entry_p * 0.999
                    cur_sl = float(pos.get("sl_price") or 0)
                    if side == "LONG":
                        pos["sl_price"] = max(cur_sl, bep_sl)
                    else:
                        pos["sl_price"] = min(cur_sl, bep_sl) if cur_sl > 0 else bep_sl
                    pos["sl_kind"] = "BREAKEVEN_1R"
                    pos["next_tp_index"] = 1
                    _persist_positions(self.positions)
                    logger.info("[1R_REACHED_BEP] %s %s hit 1.0R (%.6g) -> SL moved to BEP (%.6g). 100%% lot retained (Risk-Free)!",
                                symbol, side, tp, pos["sl_price"])
                    idx = 1
                    continue
                
                # --- STANDARD SIGNAL COPY: Hit TP2 (2.0R) -> Close 75% of initial position, activate Trailing Stop on 25% runner ---
                if idx == 1:
                    pos["tp2_hit"] = True
                    pos["tp2_hit_at"] = datetime.now(UTC)
                    self._record_shadow_tp(pos, 1, tp)
                    initial_qty = float(pos["initial_qty"])
                    target_remaining = initial_qty * 0.25
                    cur_qty = float(pos.get("qty", 0.0))
                    slice_qty = min(cur_qty, max(0.0, cur_qty - target_remaining))
                    if slice_qty > 0:
                        pos["qty"] = max(0.0, cur_qty - slice_qty)
                        await self._realize_partial(pos, tp, slice_qty, "TP2_75PCT_PARTIAL")
                    pos["next_tp_index"] = 2
                    pos["locked_profit"] = True
                    pos["trail_active"] = True
                    pos["sl_kind"] = "TRAILING_RUNNER"
                    _persist_positions(self.positions)
                    logger.info("[2R_REACHED_TP2] %s %s hit 2.0R (%.6g) -> Closed 75%% lot! 25%% runner trailing activated.",
                                symbol, side, tp)
                    break"""

if target_tp_ladder in content:
    content = content.replace(target_tp_ladder, replacement_tp_ladder, 1)
    print("Patched _check_tp_ladder with Golden Scalp support!")
else:
    print("WARNING: target_tp_ladder not found verbatim in paper_mainnet_trader.py!")

with open(path, "w", encoding="utf-8") as f:
    f.write(content)

print("Patching complete.")
