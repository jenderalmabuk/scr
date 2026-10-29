import os
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, AsyncMock, patch

from signal_copy.telegram_formatter import format_price


class TestExecutionParityAndDeviationGuard(unittest.TestCase):
    def setUp(self):
        # Mock persist to prevent modifying disk state during tests
        self.persist_patcher = patch("execution.paper_mainnet_trader._persist_positions")
        self.mock_persist = self.persist_patcher.start()

    def tearDown(self):
        self.persist_patcher.stop()

    def test_format_price_preserves_decimals_above_100(self):
        """Verify format_price doesn't truncate prices >= 100 to whole integers."""
        # AAVE #1 prices
        self.assertEqual(format_price(130.084925), "130.0849")
        self.assertEqual(format_price(131.655795), "131.6558")
        self.assertEqual(format_price(131.5626), "131.5626")
        
        # AAVE #2 prices
        self.assertEqual(format_price(131.484225), "131.4842")
        self.assertEqual(format_price(131.795865), "131.7959")
        self.assertEqual(format_price(131.6939), "131.6939")
        
        # Small prices (ONDO)
        self.assertEqual(format_price(0.407996), "0.4080")
        self.assertEqual(format_price(0.413006), "0.4130")
        
        # Zero
        self.assertEqual(format_price(0), "0")

    def test_entry_deviation_guard_short_chase(self):
        """Verify SHORT is rejected if price drifted up > 0.25% from requested entry."""
        from execution.paper_mainnet_trader import PaperMainnetTrader
        
        trader = PaperMainnetTrader()
        trader.positions = {}
        trader.risk_mgr = MagicMock()
        trader.risk_mgr.get_current_equity.return_value = 100.0
        
        # Setup: SHORT at 130.39, but current market mark has drifted to 131.48 (+0.836% higher)
        params = {
            "symbol": "AAVE_SHORT_TEST",
            "side": "SHORT",
            "entry_price": 130.39,
            "sl_price": 131.69,
            "notional": 200.0,
            "execution_price_quote": {
                "price": 131.48,
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

    def test_entry_deviation_guard_long_chase(self):
        """Verify LONG is rejected if price drifted down > 0.25% from requested entry."""
        from execution.paper_mainnet_trader import PaperMainnetTrader
        
        trader = PaperMainnetTrader()
        trader.positions = {}
        trader.risk_mgr = MagicMock()
        trader.risk_mgr.get_current_equity.return_value = 100.0
        
        # Setup: LONG at 100.0, but market drifted to 99.50 (-0.50% lower, toward SL)
        params = {
            "symbol": "ETH_LONG_TEST",
            "side": "LONG",
            "entry_price": 100.0,
            "sl_price": 98.0,
            "notional": 100.0,
            "execution_price_quote": {
                "price": 99.50,
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

    def test_sl_too_close_to_fill_guard(self):
        """Verify order is rejected if distance from fill to SL is less than 0.50%."""
        from execution.paper_mainnet_trader import PaperMainnetTrader
        
        trader = PaperMainnetTrader()
        trader.positions = {}
        trader.risk_mgr = MagicMock()
        trader.risk_mgr.get_current_equity.return_value = 100.0
        
        # Setup: SHORT at 131.48, SL at 131.69 (dist = 0.21 / 131.48 = 0.16% < 0.50%)
        # Even if requested entry equals mark (no drift), SL distance is too tight
        params = {
            "symbol": "TIGHT_SL_TEST",
            "side": "SHORT",
            "entry_price": 131.48,
            "sl_price": 131.69,
            "notional": 200.0,
            "execution_price_quote": {
                "price": 131.48,
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
        self.assertEqual(res.get("code"), "SL_TOO_CLOSE_TO_FILL")

    def test_unified_fixed_risk_sizing(self):
        """Verify paper_mainnet_trader sizes risk to target_risk_usd ($2.00) without hardcoded 1% ($1.00) penalty."""
        from execution.paper_mainnet_trader import PaperMainnetTrader
        
        trader = PaperMainnetTrader()
        trader.positions = {}
        trader.risk_mgr = MagicMock()
        trader.risk_mgr.get_current_equity.return_value = 100.0  # $100 equity
        
        # With $2.00 target risk, on a 1.0% stop setup, notional should be ~$170-$190 (giving ~$2.00 loss with fees)
        params = {
            "symbol": "SIZING_TEST",
            "side": "SHORT",
            "entry_price": 130.0,
            "sl_price": 131.30,  # 1.0% stop
            "planned_risk_amount": 2.0,
            "notional": 200.0,
            "tp1": 128.05,
            "tp_ladder": [128.05, 126.10],
            "execution_price_quote": {
                "price": 130.0,
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
        self.assertTrue(res.get("ok", False) or res.get("status") == "OPEN" or "SIZING_TEST" in trader.positions)
        pos = trader.positions.get("SIZING_TEST")
        self.assertIsNotNone(pos)
        # Actual loss at fill must be capped at <= $2.00, but NOT arbitrarily clamped to $1.00!
        self.assertGreater(pos["notional"], 120.0, f"Notional {pos['notional']} was unduly crushed below $120")
        self.assertLessEqual(pos["actual_risk_usd"], 2.05, f"Risk {pos['actual_risk_usd']} exceeded $2.00 cap")


if __name__ == "__main__":
    unittest.main()
