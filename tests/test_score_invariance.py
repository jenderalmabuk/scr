import inspect
import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from gateway.order_intent import OrderIntent
from gateway.service import ExecutionGateway, _classify_reject_layer
from execution.paper_mainnet_trader import PaperMainnetTrader


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

        # Assert all results are strictly identical down to 8 decimal places
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

        # Total adverse risk fraction = sl_frac (0.02) + 2*fee (0.0010) + slip (0.0005) + funding (0.0001) = 0.0216
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
        # Matching payload -> No error
        matching_notify = dict(journal_entry)
        matching_notify["symbol"] = "ETHUSDT"
        with self.assertLogs("gateway.paper", level="ERROR") as cm:
            # Trigger mismatched payload
            mismatched_notify = dict(matching_notify)
            mismatched_notify["pnl_usd"] = -2.50  # Divergence!
            trader._verify_raw_parity_assertions(journal_entry, mismatched_notify)
            self.assertTrue(any("NOTIFICATION_PARITY_MISMATCH" in msg for msg in cm.output))


if __name__ == "__main__":
    unittest.main()
