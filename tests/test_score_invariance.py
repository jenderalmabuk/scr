import inspect
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from gateway.order_intent import OrderIntent
from gateway.service import ExecutionGateway, _classify_reject_layer, verify_rejected_intents_journal, _compute_event_hash
from execution.paper_mainnet_trader import PaperMainnetTrader, RISK_OVERRUN_TOLERANCE_USD
from scripts.replay_rejected_intents import run_replay


class TestScoreInvarianceAndNetRisk(unittest.TestCase):
    def setUp(self):
        os.environ["SIGNALCOPY_RISK_PER_TRADE_USD"] = "2.0"
        os.environ["STARTING_BALANCE"] = "100.0"
        os.environ["PAPER_TAKER_FEE_PCT"] = "0.0005"
        os.environ["PAPER_SLIPPAGE_PCT"] = "0.0005"
        os.environ["PAPER_FUNDING_BUFFER_PCT"] = "0.0001"
        os.environ["GATEWAY_MAX_POSITION_LEVERAGE"] = "3.5"
        os.environ["GATEWAY_MAX_NOTIONAL_USD"] = "350.0"

    def test_score_not_in_sizing_signature(self):
        """Verify that _size() signature does not accept score, ensuring structural decoupling."""
        sig = inspect.signature(ExecutionGateway._size)
        params = list(sig.parameters.keys())
        self.assertNotIn("score", params)
        self.assertNotIn("priority_score", params)
        self.assertNotIn("confidence", params)

    def test_sizing_invariance_across_scores(self):
        """Verify that varying score (0 to 100) produces IDENTICAL notional and risk."""
        mock_risk_mgr = MagicMock()
        mock_risk_mgr.get_current_equity.return_value = 100.0
        mock_trader = MagicMock()
        gw = ExecutionGateway(risk_mgr=mock_risk_mgr, trader=mock_trader)

        test_scores = [0.0, 15.0, 50.0, 75.0, 95.0, 100.0]
        results = []

        entry = 100.0
        sl = 98.0  # 2% gross distance

        for sc in test_scores:
            intent = OrderIntent(
                source="SIGNAL_COPY",
                symbol="ETHUSDT",
                side="LONG",
                entry_price=entry,
                sl_price=sl,
                risk_pct=0.02,
                adv_snapshot={"score": sc, "validation_score": sc, "priority_score": sc},
            )
            notional, risk_amount, err = gw._size(intent, entry, sl, "LONG")
            self.assertIsNone(err)
            results.append((notional, risk_amount))

        base_notional, base_risk = results[0]
        for notional, risk in results[1:]:
            self.assertAlmostEqual(notional, base_notional, places=8)
            self.assertAlmostEqual(risk, base_risk, places=8)

    def test_net_risk_cap_bounds_all_costs(self):
        """Verify that risk_amount calculated in _size strictly adheres to <= $2.00 net of fees and slip."""
        mock_risk_mgr = MagicMock()
        mock_risk_mgr.get_current_equity.return_value = 100.0
        mock_trader = MagicMock()
        gw = ExecutionGateway(risk_mgr=mock_risk_mgr, trader=mock_trader)

        entry = 100.0
        sl = 98.0  # 2% distance
        intent = OrderIntent(
            source="SIGNAL_COPY",
            symbol="BTCUSDT",
            side="LONG",
            entry_price=entry,
            sl_price=sl,
            risk_pct=0.05,
        )

        notional, risk_amount, err = gw._size(intent, entry, sl, "LONG")
        self.assertIsNone(err)
        self.assertLessEqual(risk_amount, 2.000001)

        expected_notional = 2.0 / 0.0216
        self.assertAlmostEqual(notional, expected_notional, places=4)

    def test_reject_layer_classification(self):
        """Verify reject reasons map correctly to institutional audit layers."""
        self.assertEqual(_classify_reject_layer("REGIME_FLIPPED_BEFORE_FILL: BTC in PUMP"), "DIRECTIONAL_REGIME_GATE")
        self.assertEqual(_classify_reject_layer("SYMBOL_HARD_SL_COOLDOWN_ACTIVE: AAVE in cooldown"), "COOLDOWN_GUARD")
        self.assertEqual(_classify_reject_layer("MAX_DIRECTIONAL_EXPOSURE_SHORT"), "CORRELATION_GUARD")
        self.assertEqual(_classify_reject_layer("max running positions reached (10)"), "PORTFOLIO_CAP")
        self.assertEqual(_classify_reject_layer("ENTRY_CHASE_EXCEEDED: SHORT drifted +0.84%"), "EXECUTION_PRICE_GUARD")
        self.assertEqual(_classify_reject_layer("SL_TOO_CLOSE_TO_FILL: distance 0.21%"), "SL_DISTANCE_GUARD")
        self.assertEqual(_classify_reject_layer("PRICE_PROVENANCE_REQUIRED"), "PROVENANCE_GUARD")
        self.assertEqual(_classify_reject_layer("blocked by portfolio risk: drawdown"), "RISK_MANAGER")

    def test_paper_trader_raw_parity_assertions(self):
        """Verify that _verify_raw_parity_assertions detects mathematical mismatches and logs alarm."""
        trader = PaperMainnetTrader()
        journal_entry = {
            "entry_price": 100.0,
            "exit_price": 98.0,
            "qty": 1.5,
            "gross_pnl_usd": -3.0,
            "fee_usd": 0.15,
            "pnl_usd": -3.15,
        }
        matching_notify = dict(journal_entry)
        matching_notify["symbol"] = "ETHUSDT"
        with self.assertLogs("gateway.paper", level="ERROR") as cm:
            mismatched_notify = dict(matching_notify)
            mismatched_notify["pnl_usd"] = -2.50
            trader._verify_raw_parity_assertions(journal_entry, mismatched_notify)
            self.assertTrue(any("NOTIFICATION_PARITY_MISMATCH" in msg for msg in cm.output))

    def test_hash_chaining_and_tamper_detection(self):
        """Verify that hash-chained journal validates clean logs and flags tampering immediately."""
        with tempfile.NamedTemporaryFile("w+", delete=False, encoding="utf-8") as tmp:
            tmp_path = Path(tmp.name)

        try:
            prev = "0" * 64
            records = []
            for i in range(3):
                payload = {
                    "schema_version": 2,
                    "intent_id": f"intent_{i}",
                    "symbol": "BTCUSDT",
                    "previous_event_hash": prev,
                }
                h = _compute_event_hash(prev, payload)
                payload["event_hash"] = h
                records.append(payload)
                prev = h
                tmp_path.write_text(
                    "\n".join(json.dumps(r, separators=(",", ":")) for r in records) + "\n",
                    encoding="utf-8",
                )

            valid, count, msg = verify_rejected_intents_journal(tmp_path)
            self.assertTrue(valid)
            self.assertEqual(count, 3)

            # Tamper with record 1
            tampered = list(records)
            tampered[1]["symbol"] = "SOLUSDT"  # Tampered field without rehash
            tmp_path.write_text(
                "\n".join(json.dumps(r, separators=(",", ":")) for r in tampered) + "\n",
                encoding="utf-8",
            )
            valid_t, count_t, msg_t = verify_rejected_intents_journal(tmp_path)
            self.assertFalse(valid_t)
            self.assertIn("tamper detected", msg_t)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def test_duplicate_intent_suppression(self):
        """Verify that duplicate rejected intent_ids are not appended twice to journal."""
        with tempfile.NamedTemporaryFile("w+", delete=False, encoding="utf-8") as tmp:
            tmp_path = Path(tmp.name)

        try:
            with patch("gateway.service.REJECTED_INTENTS_PATH", tmp_path):
                mock_risk_mgr = MagicMock()
                mock_trader = MagicMock()
                gw = ExecutionGateway(risk_mgr=mock_risk_mgr, trader=mock_trader)

                intent = OrderIntent(
                    source="SIGNAL_COPY",
                    symbol="DOGEUSDT",
                    side="LONG",
                    entry_price=0.10,
                    sl_price=0.09,
                    risk_pct=0.01,
                )
                from gateway.service import GatewayResult
                res = GatewayResult(ok=False, reason="TEST_REJECT")

                # First call appends
                gw._append_rejected_intent(intent, res)
                lines_1 = [l for l in tmp_path.read_text(encoding="utf-8").splitlines() if l.strip()]
                self.assertEqual(len(lines_1), 1)

                # Duplicate call is suppressed
                gw._append_rejected_intent(intent, res)
                lines_2 = [l for l in tmp_path.read_text(encoding="utf-8").splitlines() if l.strip()]
                self.assertEqual(len(lines_2), 1)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def test_risk_overrun_tolerance_and_pnl_signs(self):
        """Verify explicit PnL sign edge cases for risk overrun evaluation."""
        planned_max_loss = 2.00
        tolerance_usd = RISK_OVERRUN_TOLERANCE_USD  # default 0.05

        def compute_overrun(net_pnl: float) -> float:
            actual_loss = max(0.0, -net_pnl)
            return max(0.0, actual_loss - (planned_max_loss + tolerance_usd))

        # 1. PnL -1.00 -> no overrun
        self.assertEqual(compute_overrun(-1.00), 0.0)

        # 2. PnL -2.04 -> no overrun
        self.assertEqual(compute_overrun(-2.04), 0.0)

        # 3. PnL -2.06 -> overrun of $0.01
        self.assertAlmostEqual(compute_overrun(-2.06), 0.01, places=6)

        # 4. PnL +1.00 -> no overrun
        self.assertEqual(compute_overrun(1.00), 0.0)

    def test_gateway_restart_rebuilds_dedup_and_tip_hash(self):
        """Verify that on restart, ExecutionGateway preloads seen intent IDs and last hash."""
        with tempfile.NamedTemporaryFile("w+", delete=False, encoding="utf-8") as tmp:
            tmp_path = Path(tmp.name)

        try:
            # Seed 2 historical records
            prev = "0" * 64
            records = []
            for i in range(2):
                payload = {
                    "schema_version": 2,
                    "intent_id": f"restart_intent_{i}",
                    "symbol": "BTCUSDT",
                    "previous_event_hash": prev,
                }
                h = _compute_event_hash(prev, payload)
                payload["event_hash"] = h
                records.append(payload)
                prev = h
            tmp_path.write_text(
                "\n".join(json.dumps(r, separators=(",", ":")) for r in records) + "\n",
                encoding="utf-8",
            )

            # Re-instantiate gateway (simulating daemon restart)
            with patch("gateway.service.REJECTED_INTENTS_PATH", tmp_path):
                mock_risk_mgr = MagicMock()
                mock_trader = MagicMock()
                gw = ExecutionGateway(risk_mgr=mock_risk_mgr, trader=mock_trader)

                # Assert seen intent IDs were reconstructed
                self.assertIn("restart_intent_0", gw._seen_rejected_intent_ids)
                self.assertIn("restart_intent_1", gw._seen_rejected_intent_ids)
                self.assertEqual(gw._last_reject_event_hash, records[1]["event_hash"])

                # Submitting an existing intent is suppressed
                dup_intent = OrderIntent(
                    intent_id="restart_intent_1",
                    source="SIGNAL_COPY",
                    symbol="BTCUSDT",
                    side="LONG",
                    entry_price=50000.0,
                    sl_price=49000.0,
                )
                from gateway.service import GatewayResult
                gw._append_rejected_intent(dup_intent, GatewayResult(ok=False, reason="REGIME_REJECT"))
                lines_after_dup = [l for l in tmp_path.read_text(encoding="utf-8").splitlines() if l.strip()]
                self.assertEqual(len(lines_after_dup), 2)

                # Submitting a new intent appends correctly with continuous hash chain
                new_intent = OrderIntent(
                    intent_id="restart_intent_2",
                    source="SIGNAL_COPY",
                    symbol="ETHUSDT",
                    side="SHORT",
                    entry_price=3000.0,
                    sl_price=3100.0,
                )
                gw._append_rejected_intent(new_intent, GatewayResult(ok=False, reason="REGIME_REJECT"))
                lines_after_new = [l for l in tmp_path.read_text(encoding="utf-8").splitlines() if l.strip()]
                self.assertEqual(len(lines_after_new), 3)

                # Verify chain integrity
                valid, count, msg = verify_rejected_intents_journal(tmp_path)
                self.assertTrue(valid, msg)
                self.assertEqual(count, 3)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def test_replay_audit_determinism(self):
        """Verify that consecutive replay audits produce bit-for-bit identical hashes and metrics."""
        with tempfile.NamedTemporaryFile("w+", delete=False, encoding="utf-8") as tmp:
            tmp_path = Path(tmp.name)

        try:
            prev = "0" * 64
            records = []
            for i in range(3):
                payload = {
                    "schema_version": 2,
                    "intent_id": f"det_intent_{i}",
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "entry": 50000.0,
                    "sl": 49000.0,
                    "tp": [52000.0],
                    "previous_event_hash": prev,
                }
                h = _compute_event_hash(prev, payload)
                payload["event_hash"] = h
                records.append(payload)
                prev = h
            tmp_path.write_text(
                "\n".join(json.dumps(r, separators=(",", ":")) for r in records) + "\n",
                encoding="utf-8",
            )

            # Back-to-back replay runs
            res1 = run_replay(tmp_path)
            res2 = run_replay(tmp_path)

            self.assertEqual(res1["output_hash"], res2["output_hash"])
            for metric in [
                "input_count", "resolved_count", "unknown_count", "would_have_won",
                "would_have_lost", "gross_pnl", "fees", "funding", "slippage",
                "net_pnl", "loss_avoided", "profit_missed", "net_gate_benefit"
            ]:
                self.assertEqual(res1[metric], res2[metric])
        finally:
            if tmp_path.exists():
                tmp_path.unlink()


if __name__ == "__main__":
    unittest.main()
