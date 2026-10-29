import os
import unittest
from unittest.mock import MagicMock
from gateway.order_intent import OrderIntent
from gateway.service import ExecutionGateway


class TestResilienceGuards(unittest.TestCase):
    def setUp(self):
        os.environ["GATEWAY_MAX_DIRECTIONAL_POSITIONS"] = "2"
        os.environ["STARTING_BALANCE"] = "100.0"
        os.environ["SIGNALCOPY_RISK_PER_TRADE_USD"] = "2.0"

    def test_correlation_guard_blocks_third_directional_position(self):
        # Mock trader with 2 open LONG positions
        mock_trader = MagicMock()
        mock_trader.positions = {
            "BTCUSDT": {"side": "LONG"},
            "ETHUSDT": {"side": "LONG"},
        }
        mock_risk_mgr = MagicMock()
        mock_risk_mgr.get_max_running_positions.return_value = 10
        mock_risk_mgr._position_count.return_value = 2

        gw = ExecutionGateway(risk_mgr=mock_risk_mgr, trader=mock_trader)

        # 3rd LONG should be blocked by Correlation Guard
        intent = OrderIntent(
            source="SIGNAL_COPY",
            symbol="SOLUSDT",
            side="LONG",
            entry_price=150.0,
            sl_price=140.0,
            risk_pct=0.01,
        )

        import asyncio
        res = asyncio.run(gw.execute(intent))

        self.assertFalse(res.ok)
        self.assertIn("MAX_DIRECTIONAL_EXPOSURE_LONG", res.reason)

        # But a SHORT position should NOT be blocked by LONG directional cap!
        short_intent = OrderIntent(
            source="SIGNAL_COPY",
            symbol="SOLUSDT",
            side="SHORT",
            entry_price=150.0,
            sl_price=160.0,
            risk_pct=0.01,
        )
        mock_risk_mgr.check_risk_limits.return_value = {"can_trade": True}
        mock_risk_mgr.get_current_equity.return_value = 100.0
        mock_trader.submit_open = MagicMock()
        
        async def mock_submit(*args, **kwargs):
            return {"position_confirmed": True, "entry_price": 150.0, "notional": 50.0}
        mock_trader.submit_open.side_effect = mock_submit

        res_short = asyncio.run(gw.execute(short_intent))
        # Short should not hit the MAX_DIRECTIONAL_EXPOSURE_LONG reason
        self.assertNotIn("MAX_DIRECTIONAL_EXPOSURE_LONG", res_short.reason)

    def test_adaptive_sizing_during_drawdown(self):
        mock_trader = MagicMock()
        mock_trader.positions = {}
        mock_risk_mgr = MagicMock()
        
        gw = ExecutionGateway(risk_mgr=mock_risk_mgr, trader=mock_trader)

        intent = OrderIntent(
            source="SIGNAL_COPY",
            symbol="SOLUSDT",
            side="LONG",
            entry_price=100.0,
            sl_price=95.0, # 5% sl_frac
            risk_pct=0.02, # 2%
        )

        # Case 1: Normal equity = $100 -> Risk cap $2.00
        mock_risk_mgr.get_current_equity.return_value = 100.0
        notional, risk_amount, err = gw._size(intent, 100.0, 95.0, "LONG")
        self.assertIsNone(err)
        # notional = 2.0 / 0.05 = 40.0
        self.assertEqual(notional, 40.0)

        # Case 2: Drawdown 15% (equity = $85) -> Adaptive risk cap $1.00
        mock_risk_mgr.get_current_equity.return_value = 85.0
        notional, risk_amount, err = gw._size(intent, 100.0, 95.0, "LONG")
        self.assertIsNone(err)
        # notional = 1.0 / 0.05 = 20.0
        self.assertEqual(notional, 20.0)

        # Case 3: Drawdown 25% (equity = $75) -> Adaptive risk cap $0.50
        mock_risk_mgr.get_current_equity.return_value = 75.0
        notional, risk_amount, err = gw._size(intent, 100.0, 95.0, "LONG")
        self.assertIsNone(err)
        # notional = 0.5 / 0.05 = 10.0
        self.assertEqual(notional, 10.0)


if __name__ == "__main__":
    unittest.main()
