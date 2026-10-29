#!/usr/bin/env python3
"""
Unit test for end-to-end lifecycle provenance contract:
OrderIntent -> open position -> partial close -> final close -> trade_history.json
Designed to run with pure Python standard library (unittest) without requiring third-party packages.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Add project root to sys.path
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Ensure test runs in standard library environment without failing on external aiohttp dependency
if "aiohttp" not in sys.modules:
    try:
        import aiohttp
    except ImportError:
        mock_aiohttp = MagicMock()
        mock_session = MagicMock()
        mock_session.close = AsyncMock()
        mock_session.get = MagicMock()
        mock_aiohttp.ClientSession = MagicMock(return_value=mock_session)
        mock_aiohttp.ClientTimeout = MagicMock()
        sys.modules["aiohttp"] = mock_aiohttp

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

from gateway.order_intent import OrderIntent
from gateway.service import ExecutionGateway
from execution.paper_mainnet_trader import PaperMainnetTrader


class FakeRiskManager:
    max_notional_pct = 3.5

    def __init__(self):
        self.released = False
        self.committed = False
        self.vip_risk_used = 0.0

    def get_current_equity(self):
        return 1000.0

    def get_max_running_positions(self):
        return 10

    def _position_count(self):
        return 0

    def check_risk_limits(self, **kwargs):
        return {"can_trade": True}

    async def reserve_open_risk_details(self, symbol, risk_amount):
        return {"reserved": True, "code": "RESERVED"}

    async def release_open_risk(self, symbol):
        self.released = True

    async def commit_open_trade(self, symbol, risk_amount=0.0, is_vip=False):
        self.committed = True


class TestProvenanceLifecycle(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="nexus_test_prov_")
        self.journal_dir = Path(self.temp_dir) / "journal"
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        
        self.patch_env = patch.dict(os.environ, {
            "BOT_JOURNAL_DIR": str(self.journal_dir),
            "FQ_PAPER_POSITION_STATE": str(Path(self.temp_dir) / "open_positions.json"),
            "FQ_PAPER_EQUITY_STATE": str(Path(self.temp_dir) / "paper_equity.json"),
            "GATEWAY_TRADER_MODE": "paper",
            "GATEWAY_PAPER_MAINNET": "true",
            "SIGNALCOPY_USE_GOLDEN_SCALP": "true",
            "SIGNALCOPY_RISK_PER_TRADE_USD": "2.0",
        })
        self.patch_env.start()

        import utils.persistence_paths
        import execution.trade_journal
        import execution.paper_mainnet_trader
        self.patchers = [
            patch.object(utils.persistence_paths, "JOURNAL_DIR", self.journal_dir),
            patch.object(utils.persistence_paths, "TRADE_HISTORY_JSON_PATH", self.journal_dir / "trade_history.json"),
            patch.object(utils.persistence_paths, "TRADE_HISTORY_CSV_PATH", self.journal_dir / "trade_history.csv"),
            patch.object(execution.trade_journal, "JOURNAL_DIR", self.journal_dir),
            patch.object(execution.paper_mainnet_trader, "PAPER_STATE_PATH", Path(self.temp_dir) / "open_positions.json"),
            patch.object(execution.paper_mainnet_trader, "PAPER_EQUITY_PATH", Path(self.temp_dir) / "paper_equity.json"),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        if hasattr(self, "patchers"):
            for p in reversed(self.patchers):
                p.stop()
        self.patch_env.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_lifecycle_provenance_in_trade_history(self):
        """Test full provenance preservation from OrderIntent to trade_history.json."""
        async def run_lifecycle():
            risk_mgr = FakeRiskManager()
            trader = PaperMainnetTrader(nexus_api="http://127.0.0.1:8000")
            
            # Mock mark prices and fresh quotes
            entry_ref = 0.21464
            sl_ref = 0.20761
            tp1_ref = 0.21676
            tp2_ref = 0.22225
            
            trader._get_fresh_open_quote = AsyncMock(return_value={
                "price": entry_ref,
                "price_fresh": True,
                "price_source": "test_feed",
                "price_observed_at": datetime.now(timezone.utc).isoformat(),
            })
            trader._get_completed_bars = AsyncMock(return_value=[])
            
            # Start trader and journal
            await trader.start()
            
            gateway = ExecutionGateway(trader, risk_mgr)
            
            intent = OrderIntent(
                source="AUTONOMOUS_SCANNER",
                symbol="ENAUSDT",
                side="LONG",
                entry_price=entry_ref,
                sl_price=sl_ref,
                tps=[tp1_ref, tp2_ref],
                notional=175.42,
                regime="TRENDING",
                tag="sig_ena_17930001",
                adv_snapshot={
                    "strategy": "MTF_IFVG_PRO",
                    "signal_id": "sig_ena_17930001",
                    "target_risk_usd": 2.0,
                }
            )
            
            # 1. Gateway Execute -> Open Position
            res = await gateway.execute(intent)
            self.assertTrue(res.ok, f"Gateway execute failed: {res.reason}")
            self.assertIn("ENAUSDT", trader.positions)
            
            # 2. Simulate TP1 Hit -> Partial Close (75% closed under Golden Scalp)
            await trader._apply_take_profits("ENAUSDT", tp1_ref)
            
            # 3. Simulate Final Exit -> Final Close (Remaining 25% closed)
            await trader._close("ENAUSDT", tp2_ref, "TP2_RUNNER_FULL")
            
            # Stop trader and flush journal
            await trader.stop()
            
            # Read trade_history.json
            history_file = self.journal_dir / "trade_history.json"
            self.assertTrue(history_file.exists(), "trade_history.json was not created")
            
            with open(history_file, "r", encoding="utf-8") as f:
                history = json.load(f)
                
            self.assertTrue(len(history) > 0, "No records written to trade_history.json")
            closed_trade = history[-1]
            return closed_trade, intent

        closed_trade, intent = asyncio.run(run_lifecycle())
        
        # REQUIRED FIELDS PER CONTRACT SPECIFICATION
        required_fields = [
            "schema_version",
            "mode",
            "source",
            "strategy",
            "signal_id",
            "intent_id",
            "order_id",
            "position_id",
            "signal_entry_price",
            "fill_entry_price",
            "raw_sl_price",
            "active_sl_price",
            "target_risk_usd",
            "actual_risk_usd",
            "actual_rr_ladder",
            "qty_opened",
            "qty_closed",
            "qty_remaining"
        ]
        
        # Verify presence of all required fields
        for field in required_fields:
            self.assertIn(
                field, 
                closed_trade, 
                f"Missing required contract field '{field}' in trade_history record: {json.dumps(closed_trade, indent=2)}"
            )

        # Verify provenance values match
        self.assertEqual(closed_trade.get("schema_version"), 1)
        self.assertEqual(closed_trade.get("mode"), "PAPER")
        self.assertEqual(closed_trade.get("source"), intent.source)
        self.assertEqual(closed_trade.get("strategy"), "MTF_IFVG_PRO")
        self.assertEqual(closed_trade.get("intent_id"), intent.intent_id)
        self.assertEqual(closed_trade.get("target_risk_usd"), 2.0)
        self.assertEqual(closed_trade.get("qty_remaining"), 0.0)


if __name__ == "__main__":
    unittest.main()
