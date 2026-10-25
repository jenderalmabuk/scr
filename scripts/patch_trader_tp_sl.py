from pathlib import Path

trader_path = Path("/opt/signalcopyreal/execution/paper_mainnet_trader.py")
content = trader_path.read_text(encoding="utf-8")

print("=== PATCHING PAPER_MAINNET_TRADER (BEP @ 1R, 75% @ 2R, 25% TRAILING) ===")

# 1. Update _apply_take_profits
old_apply_tp = """    async def _apply_take_profits(self, symbol: str, mark: float):
        pos = self.positions.get(symbol)
        if not pos:
            return
        ladder = pos.get("tp_ladder") or [pos.get("tp1_price")]
        idx = int(pos.get("next_tp_index", 0))
        while idx < len(ladder):
            tp = float(ladder[idx] or 0)
            hit = (pos["side"] == "LONG" and mark >= tp) or (pos["side"] == "SHORT" and mark <= tp)
            if not tp or not hit:
                break
            if idx == 0:
                pos["tp1_hit"] = True
                pos["tp1_hit_at"] = pos.get("tp1_hit_at") or datetime.now(UTC)
                pos["shadow_cohort"] = True
            self._record_shadow_tp(pos, idx, tp)
            idx += 1
            if idx == len(ladder):
                await self._close(symbol, tp, f"TP{idx}")
                return
            slice_qty = float(pos["initial_qty"]) / len(ladder)
            pos["qty"] = max(0.0, float(pos["qty"]) - slice_qty)
            pos["next_tp_index"] = idx
            pos["tp1_hit"] = True
            _persist_positions(self.positions)
            await self._realize_partial(pos, tp, slice_qty, f"TP{idx}_PARTIAL")"""

new_apply_tp = """    async def _apply_take_profits(self, symbol: str, mark: float):
        pos = self.positions.get(symbol)
        if not pos:
            return
        ladder = pos.get("tp_ladder") or [pos.get("tp1_price")]
        idx = int(pos.get("next_tp_index", 0))
        while idx < len(ladder):
            tp = float(ladder[idx] or 0)
            hit = (pos["side"] == "LONG" and mark >= tp) or (pos["side"] == "SHORT" and mark <= tp)
            if not tp or not hit:
                break
            
            # --- STAGE 1: Hit TP1 (1.0R) -> Move SL to BEP (Breakeven + 0.1% buffer), 100% lot retained ---
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

if old_apply_tp in content:
    content = content.replace(old_apply_tp, new_apply_tp)
    print("Successfully patched _apply_take_profits in paper_mainnet_trader.py")
else:
    print("Warning: old _apply_take_profits block not found exactly. Checking...")

# 2. Update trailing condition to trigger immediately on trail_active
old_trail_cond = 'if pos.get("locked_profit") and hold_min > 10.0 and _dyn_env_bool("TRAIL_SL_ENABLED"):'
new_trail_cond = 'if (pos.get("trail_active") or (pos.get("locked_profit") and hold_min > 10.0)) and _dyn_env_bool("TRAIL_SL_ENABLED", default=True):'

if old_trail_cond in content:
    content = content.replace(old_trail_cond, new_trail_cond)
    print("Successfully updated trailing condition to support runner trailing")

# 3. Update SL hit exit reason to reflect BREAKEVEN_EXIT or TRAILING_RUNNER_EXIT
old_sl_close = """                    sl = float(pos.get("sl_price") or 0)
                    if sl and self._level_hit(pos, mark, sl, favorable=False):
                        pos["sl_trigger_quote"] = quote
                        await self._close(symbol, mark, "HARD_SL")
                        continue"""

new_sl_close = """                    sl = float(pos.get("sl_price") or 0)
                    if sl and self._level_hit(pos, mark, sl, favorable=False):
                        pos["sl_trigger_quote"] = quote
                        sl_kind = str(pos.get("sl_kind", "HARD_SL"))
                        exit_reason = "BREAKEVEN_EXIT" if "BREAKEVEN" in sl_kind else ("TRAILING_RUNNER_EXIT" if "TRAILING" in sl_kind else "HARD_SL")
                        await self._close(symbol, mark, exit_reason)
                        continue"""

if old_sl_close in content:
    content = content.replace(old_sl_close, new_sl_close)
    print("Successfully updated SL exit reason attribution in paper_mainnet_trader.py")

trader_path.write_text(content, encoding="utf-8")
print("Saved paper_mainnet_trader.py")
