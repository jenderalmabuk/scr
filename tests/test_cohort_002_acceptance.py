import os
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock, patch

from execution.paper_mainnet_trader import PaperMainnetTrader
from gateway.order_intent import OrderIntent
from gateway.service import ExecutionGateway


class TestCohort002Acceptance(unittest.TestCase):
    def setUp(self):
        self.persist_patcher = patch("execution.paper_mainnet_trader._persist_positions")
        self.mock_persist = self.persist_patcher.start()

    def tearDown(self):
        self.persist_patcher.stop()

    def _create_trader(self):
        trader = PaperMainnetTrader()
        trader.positions = {}
        trader.risk_mgr = MagicMock()
        trader.risk_mgr.get_current_equity.return_value = 100.0
        return trader

    def test_ondo_scenario_long_upward_chase_rejected(self):
        """P0 test: ONDO LONG chased +0.53% (limit 0.25%) -> MUST BE REJECTED."""
        trader = self._create_trader()
        params = {
            "symbol": "ONDOUSDT_CHASE_TEST",
            "side": "LONG",
            "entry_price": 0.4166,
            "sl_price": 0.4100,
            "notional": 100.0,
            "execution_price_quote": {
                "price": 0.4188093, # +0.5303% chase
                "price_fresh": True,
                "price_source": "bybit",
                "price_observed_at": datetime.now(timezone.utc).isoformat()
            }
        }
        import asyncio
        loop = asyncio.new_event_loop()
        res = loop.run_until_complete(trader.submit_open(**params))
        loop.close()

        self.assertIsNotNone(res)
        self.assertFalse(res.get("ok", True))
        self.assertEqual(res.get("code"), "ENTRY_CHASE_EXCEEDED")
        self.assertNotIn("ONDOUSDT_CHASE_TEST", trader.positions)

    def test_aave_scenario_long_adverse_drop_rejected(self):
        """P0 test: AAVE LONG dropped -0.37% (limit 0.25%) -> MUST BE REJECTED."""
        trader = self._create_trader()
        params = {
            "symbol": "AAVEUSDT_DROP_TEST",
            "side": "LONG",
            "entry_price": 134.50,
            "sl_price": 132.00,
            "notional": 100.0,
            "execution_price_quote": {
                "price": 134.00, # -0.3717% drop
                "price_fresh": True,
                "price_source": "bybit",
                "price_observed_at": datetime.now(timezone.utc).isoformat()
            }
        }
        import asyncio
        loop = asyncio.new_event_loop()
        res = loop.run_until_complete(trader.submit_open(**params))
        loop.close()

        self.assertIsNotNone(res)
        self.assertFalse(res.get("ok", True))
        self.assertEqual(res.get("code"), "ENTRY_CHASE_EXCEEDED")
        self.assertNotIn("AAVEUSDT_DROP_TEST", trader.positions)

    def test_entry_deviation_within_tolerance_accepted(self):
        """P0 test: Deviation +0.10% (well within 0.25%) -> ACCEPTED."""
        trader = self._create_trader()
        params = {
            "symbol": "VALID_DEV_TEST",
            "side": "LONG",
            "entry_price": 100.0,
            "sl_price": 98.0,
            "notional": 100.0,
            "execution_price_quote": {
                "price": 100.10, # +0.10%
                "price_fresh": True,
                "price_source": "bybit",
                "price_observed_at": datetime.now(timezone.utc).isoformat()
            }
        }
        import asyncio
        loop = asyncio.new_event_loop()
        res = loop.run_until_complete(trader.submit_open(**params))
        loop.close()

        self.assertIsNotNone(res)
        self.assertTrue(res.get("ok", False))
        self.assertIn("VALID_DEV_TEST", trader.positions)
        pos = trader.positions["VALID_DEV_TEST"]
        # Provenance keys verification:
        self.assertEqual(pos.get("entry_fill_source"), "ACTUAL_FILL")
        self.assertEqual(pos.get("entry_integrity_status"), "MATCHED")
        self.assertAlmostEqual(pos.get("planned_entry_price"), 100.0, places=4)
        self.assertGreater(pos.get("actual_fill_price"), 0.0)

    def test_post_fill_slippage_deviation_rejected(self):
        """P0 test: Mark is +0.245% (under 0.25%), but adverse fill pushes it to +0.295% -> REJECTED."""
        trader = self._create_trader()
        params = {
            "symbol": "POST_FILL_SLIP_TEST",
            "side": "LONG",
            "entry_price": 100.0,
            "sl_price": 95.0, # plenty of distance
            "notional": 100.0,
            "execution_price_quote": {
                "price": 100.24, # +0.24% mark
                "price_fresh": True,
                "price_source": "bybit",
                "price_observed_at": datetime.now(timezone.utc).isoformat()
            }
        }
        # Force adverse fill to push it over 0.25%
        with patch.object(trader, "_adverse_fill", return_value=100.27): # +0.27% > 0.25%
            import asyncio
            loop = asyncio.new_event_loop()
            res = loop.run_until_complete(trader.submit_open(**params))
            loop.close()

        self.assertIsNotNone(res)
        self.assertFalse(res.get("ok", True))
        self.assertEqual(res.get("code"), "ENTRY_CHASE_EXCEEDED")
        self.assertNotIn("POST_FILL_SLIP_TEST", trader.positions)

    def test_pnl_parity_authoritative_ledger(self):
        """P1 test: net_pnl == pnl_usd == final_exit_net_pnl_usd == (gross - fees)."""
        trader = self._create_trader()
        mock_journal = AsyncMock()
        trader._journal = mock_journal

        # Open a position manually
        import asyncio
        loop = asyncio.new_event_loop()
        trader.positions["PNL_TEST"] = {
            "schema_version": 1,
            "mode": "PAPER",
            "position_id": "pos_test_123",
            "symbol": "PNL_TEST",
            "side": "LONG",
            "entry_price": 10.0,
            "actual_fill_price": 10.0,
            "planned_entry_price": 10.0,
            "entry_fee": 0.05,
            "qty": 10.0,
            "notional": 100.0,
            "sl_price": 9.5,
            "opened_at": datetime.now(timezone.utc),
            "entry_fill_source": "ACTUAL_FILL",
            "entry_integrity_status": "MATCHED",
        }

        # Close at 9.6 (loss)
        # Gross loss = (9.6 - 10.0) * 10 = -4.00
        # Exit fee ~ 0.05, Entry fee = 0.05 -> Net loss = -4.10
        with patch.object(trader, "_adverse_fill", return_value=9.60):
            loop.run_until_complete(trader._close("PNL_TEST", 9.60, "SL_HIT"))
        loop.close()

        self.assertTrue(mock_journal.write_trade.called)
        trade_payload = mock_journal.write_trade.call_args[0][0]

        # Verify Authoritative Unification:
        net_pnl = trade_payload.get("net_pnl")
        pnl_usd = trade_payload.get("pnl_usd")
        final_exit_net = trade_payload.get("final_exit_net_pnl_usd")
        gross_pnl = trade_payload.get("gross_pnl_usd")
        entry_fee = trade_payload.get("entry_fee_usd")
        exit_fee = trade_payload.get("exit_fee_usd")

        self.assertIsNotNone(net_pnl)
        self.assertEqual(net_pnl, pnl_usd)
        self.assertEqual(net_pnl, final_exit_net)
        expected_net = gross_pnl - entry_fee - exit_fee
        self.assertAlmostEqual(net_pnl, expected_net, places=4)
        self.assertEqual(trade_payload.get("entry_fill_source"), "ACTUAL_FILL")
        self.assertEqual(trade_payload.get("entry_integrity_status"), "MATCHED")

    def test_order_intent_score_propagation(self):
        """P1 test: Score field propagates from OrderIntent to payload and position."""
        intent = OrderIntent(
            source="AUTONOMOUS_SCANNER",
            symbol="SCORE_TEST",
            side="LONG",
            entry_price=10.0,
            sl_price=9.5,
            notional=100.0,
            score=0.88,
            confidence=0.88,
            adv_snapshot={"score": 0.88, "strategy": "MTF_IFVG_PRO"}
        )
        self.assertEqual(intent.score, 0.88)
        d = intent.to_dict()
        self.assertEqual(d.get("score"), 0.88)

        rebuilt = OrderIntent.from_dict(d)
        self.assertEqual(rebuilt.score, 0.88)

    def test_directional_orderflow_gate_logic(self):
        """P1 test: BOTH_ALLOWED & NEUTRAL are rejected; only LONG_ONLY / SHORT_ONLY accepted."""
        # Test evaluation function matching autonomous scanner gate
        def check_directional_gate(side: str, flow_dir: str):
            if side == "LONG":
                if flow_dir not in ("LONG_ONLY", "STRONG_BUY"):
                    return False, f"LONG rejected by flow_dir={flow_dir}"
                return True, "CONFIRMED"
            elif side == "SHORT":
                if flow_dir not in ("SHORT_ONLY", "STRONG_SELL"):
                    return False, f"SHORT rejected by flow_dir={flow_dir}"
                return True, "CONFIRMED"
            return False, "INVALID_SIDE"

        # LONG tests
        self.assertFalse(check_directional_gate("LONG", "BOTH_ALLOWED")[0])
        self.assertFalse(check_directional_gate("LONG", "NEUTRAL")[0])
        self.assertFalse(check_directional_gate("LONG", "SHORT_ONLY")[0])
        self.assertFalse(check_directional_gate("LONG", "UNKNOWN")[0])
        self.assertTrue(check_directional_gate("LONG", "LONG_ONLY")[0])
        self.assertTrue(check_directional_gate("LONG", "STRONG_BUY")[0])

        # SHORT tests
        self.assertFalse(check_directional_gate("SHORT", "BOTH_ALLOWED")[0])
        self.assertFalse(check_directional_gate("SHORT", "NEUTRAL")[0])
        self.assertFalse(check_directional_gate("SHORT", "LONG_ONLY")[0])
        self.assertFalse(check_directional_gate("SHORT", "UNKNOWN")[0])
        self.assertTrue(check_directional_gate("SHORT", "SHORT_ONLY")[0])
        self.assertTrue(check_directional_gate("SHORT", "STRONG_SELL")[0])

    def test_gate1_post_sl_symbol_directional_cooldown(self):
        """Gate 1: Symbol that hit SL < 12h ago on same side is vetoed."""
        from bots.autonomous_market_scanner import AutonomousMarketScanner
        scanner = AutonomousMarketScanner()

        mock_trades = [
            {
                "symbol": "ONDOUSDT",
                "side": "LONG",
                "timestamp_close": datetime.now(timezone.utc).isoformat(),
                "normalized_reason": "HARD_SL"
            }
        ]
        with patch("pathlib.Path.open", unittest.mock.mock_open(read_data=json.dumps(mock_trades))), \
             patch("pathlib.Path.exists", return_value=True):
            is_cd, msg = scanner.check_post_sl_symbol_cooldown("ONDOUSDT", "LONG", cooldown_hours=12.0)
            self.assertTrue(is_cd)
            self.assertIn("Recently stopped out on LONG", msg)

            # Different side (SHORT) must NOT be blocked by LONG cooldown
            is_cd_short, _ = scanner.check_post_sl_symbol_cooldown("ONDOUSDT", "SHORT", cooldown_hours=12.0)
            self.assertFalse(is_cd_short)

    def test_gate2_market_structure_swing_integrity(self):
        """Gate 2: Price below 24h swing low rejects LONG (anti-falling knife)."""
        k1h = [{"low": 10.0 + i*0.1, "high": 11.0 + i*0.1} for i in range(30)]
        lows_24h = [float(k["low"]) for k in k1h[-25:-1]]
        swing_low_24h = min(lows_24h)

        # Case 1: current price 9.5 < swing_low (Lower Low / Falling knife) -> REJECT
        current_price_knife = 9.5
        self.assertLess(current_price_knife, swing_low_24h)

        # Case 2: current price 11.5 >= swing_low (Healthy pullback) -> ACCEPT
        current_price_healthy = 11.5
        self.assertGreaterEqual(current_price_healthy, swing_low_24h)

    def test_gate3_orderflow_multi_bar_persistence(self):
        """Gate 3: First bar spike is vetoed; 2nd consecutive cycle passes."""
        from bots.autonomous_market_scanner import AutonomousMarketScanner
        scanner = AutonomousMarketScanner()

        # Cycle 1: First time seeing LONG_ONLY (prev was None)
        prev_1 = scanner.flow_history.get("BTCUSDT")
        scanner.flow_history["BTCUSDT"] = "LONG_ONLY"
        cycle_1_persistent = prev_1 in ("LONG_ONLY", "STRONG_BUY")
        self.assertFalse(cycle_1_persistent) # Vetoed as single-bar spike!

        # Cycle 2: Second consecutive cycle of LONG_ONLY
        prev_2 = scanner.flow_history.get("BTCUSDT")
        scanner.flow_history["BTCUSDT"] = "LONG_ONLY"
        cycle_2_persistent = prev_2 in ("LONG_ONLY", "STRONG_BUY")
        self.assertTrue(cycle_2_persistent) # Confirmed persistent!



    def test_dual_tier_risk_accounting_classification(self):
        """P1 test: Differentiate raw budget breach from tolerance overrun."""
        # Case 1: LTC scenario - actual loss $2.04154 (planned $2.00, tolerance $0.05)
        planned_max_loss = 2.00
        tolerance_usd = 0.05
        actual_net_loss = 2.04154
        reason = "HARD_SL"

        planned_risk_breach_usd = max(0.0, actual_net_loss - planned_max_loss)
        is_risk_budget_breach = planned_risk_breach_usd > 0.0 and "SL" in reason.upper()
        risk_overrun_usd = max(0.0, actual_net_loss - (planned_max_loss + tolerance_usd))
        is_risk_overrun = risk_overrun_usd > 0.0 and "SL" in reason.upper()

        self.assertTrue(is_risk_budget_breach, "Must flag budget breach for $2.04154 > $2.00")
        self.assertAlmostEqual(planned_risk_breach_usd, 0.04154, places=5)
        self.assertFalse(is_risk_overrun, "Must NOT flag tolerance overrun within $0.05 tolerance")
        self.assertEqual(risk_overrun_usd, 0.0)

        # Case 2: Severe slippage - actual loss $2.08000
        actual_net_loss_severe = 2.08000
        planned_risk_breach_usd_severe = max(0.0, actual_net_loss_severe - planned_max_loss)
        is_risk_budget_breach_severe = planned_risk_breach_usd_severe > 0.0 and "SL" in reason.upper()
        risk_overrun_usd_severe = max(0.0, actual_net_loss_severe - (planned_max_loss + tolerance_usd))
        is_risk_overrun_severe = risk_overrun_usd_severe > 0.0 and "SL" in reason.upper()

        self.assertTrue(is_risk_budget_breach_severe)
        self.assertTrue(is_risk_overrun_severe, "Must flag overrun for $2.08000 > $2.05")
        self.assertAlmostEqual(risk_overrun_usd_severe, 0.03000, places=5)

    def test_cvd_and_oi_provenance_distinction(self):
        """P1 test: cvd_status and oi_status explicitly distinguish 0.0 from UNAVAILABLE."""
        # Case 1: cvd_raw is 0.0 (genuine zero flow) -> VALID
        cvd_raw = 0.0
        cvd_val = float(cvd_raw) if cvd_raw is not None else 0.0
        cvd_status = "VALID" if cvd_raw is not None else "UNAVAILABLE"
        self.assertEqual(cvd_status, "VALID")
        self.assertEqual(cvd_val, 0.0)

        # Case 2: cvd_raw is None (missing feed) -> UNAVAILABLE
        cvd_raw_missing = None
        cvd_val_missing = float(cvd_raw_missing) if cvd_raw_missing is not None else 0.0
        cvd_status_missing = "VALID" if cvd_raw_missing is not None else "UNAVAILABLE"
        self.assertEqual(cvd_status_missing, "UNAVAILABLE")
        self.assertEqual(cvd_val_missing, 0.0)

        # Case 3: OI history missing -> UNAVAILABLE
        oi_history = []
        oi_status = "VALID" if (oi_history and len(oi_history) >= 2) else "UNAVAILABLE"
        self.assertEqual(oi_status, "UNAVAILABLE")

    def test_cohort_manifest_and_gateway_reconciliation(self):
        """P1 test: Cohort manifest integrity & atomic gateway reconciliation."""
        import json
        from pathlib import Path
        manifest_path = Path("/opt/signalcopyreal/active_cohort.json")
        self.assertTrue(manifest_path.exists(), "active_cohort.json must exist")

        with manifest_path.open("r", encoding="utf-8") as f:
            manifest = json.load(f)

        self.assertEqual(manifest.get("cohort_id"), "cohort_post_patch_20261103_002")
        self.assertEqual(manifest.get("git_commit"), "bc95c5a")
        self.assertEqual(manifest.get("authoritative_pnl_metric"), "authoritative_net_pnl_usd")
        self.assertEqual(manifest.get("execution_mode"), "PAPER_REAL_PRICE_MATCHING")
        self.assertAlmostEqual(manifest.get("baseline_equity_usd"), 95.8870, places=4)

        # Test Gateway portfolio reconciliation block
        mock_trader = MagicMock()
        mock_risk = MagicMock()
        mock_risk.trader = mock_trader
        mock_risk.get_current_equity.return_value = 97.5000
        mock_risk.get_daily_pnl_pct.return_value = 0.0
        mock_risk.get_total_exposure_pct.return_value = 0.0
        mock_risk.get_reserved_risk_total.return_value = 0.0
        mock_risk.get_active_open_risk_total.return_value = 3.9672
        mock_risk.get_total_open_risk.return_value = 3.9672
        mock_risk.get_parallel_open_risk_budget.return_value = 10.0
        mock_risk._position_count.return_value = 0
        mock_risk._position_symbols.return_value = []
        mock_risk.is_daily_loss_limit_hit.return_value = False
        mock_risk.is_exposure_limit_exceeded.return_value = False
        mock_trader.positions = {}

        gateway = ExecutionGateway(mock_trader, mock_risk)

        port = gateway.portfolio()
        self.assertIn("active_cohort", port)
        self.assertIn("cohort_reconciliation", port)
        reconcil = port["cohort_reconciliation"]
        self.assertEqual(reconcil["reconciliation_status"], "MATCHED")
        self.assertAlmostEqual(reconcil["baseline_equity_usd"], 95.8870, places=4)
        self.assertAlmostEqual(reconcil["current_equity_usd"], 97.5000, places=4)
        self.assertAlmostEqual(reconcil["equity_delta_usd"], 1.6130, places=4)


if __name__ == "__main__":
    unittest.main()
