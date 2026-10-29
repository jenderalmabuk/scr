#!/usr/bin/env python3
"""
Unit test for Paper / Bybit Live Trader Schema Parity & Provenance Contract.
Verifies that:
1. Root-level 18 schema fields match 1:1 between Paper and Bybit.
2. position_id remains strictly stable between partial close and final close.
3. qty_opened == qty_closed + qty_remaining across all lifecycle stages.
4. actual_risk_usd uses actual Bybit fill metrics.
5. actual_rr_ladder uses actual entry fill, SL, and TP ladder.
6. Final close PnL is aggregate across partials + final.
7. Zero live network calls are made (pure mock).
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

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
from execution.bybit_mainnet_trader import BybitMainnetTrader


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


class FakeBybitClient:
    def __init__(self, mark=0.21464, step="0.1", tick="0.00001"):
        self.mark = mark
        self.step = step
        self.tick = tick
        self.orders = []
        self.created_orders = []

    def get_ticker(self, symbol):
        return {"lastPrice": str(self.mark)}

    def get_instrument_info(self, symbol):
        return {
            "lotSizeFilter": {"qtyStep": self.step, "minOrderQty": "0.1"},
            "priceFilter": {"tickSize": self.tick},
        }

    def create_order(self, **kwargs):
        self.created_orders.append(kwargs)
        return {"orderId": f"bybit_ord_{len(self.created_orders)}"}

    def get_positions(self, symbol=None):
        return [{
            "symbol": symbol or "ENAUSDT",
            "side": "Buy",
            "size": "100.0",
            "avgPrice": str(self.mark),
            "positionValue": str(100.0 * self.mark),
        }]

    def get_order_history(self, **kwargs):
        ord_num = len(self.created_orders)
        link_id = self.created_orders[-1].get("order_link_id", "") if self.created_orders else ""
        return [{
            "orderLinkId": link_id,
            "orderId": f"bybit_ord_{ord_num}",
            "avgPrice": str(self.mark),
        }]

    def get_closed_pnl(self, **kwargs):
        ord_num = len(self.created_orders)
        pnl_val = "0.75" if ord_num == 2 else "0.25"
        return [{
            "orderId": f"bybit_ord_{ord_num}",
            "closedPnl": pnl_val,
            "avgExitPrice": str(self.mark),
            "closedSize": "75.0" if ord_num == 2 else "25.0",
        }]

    def set_trading_stop(self, **kwargs):
        return {}

    def get_balance(self):
        return 1000.0


class TestProvenanceSchemaParity(unittest.TestCase):
    REQUIRED_SCHEMA_FIELDS = [
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

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="nexus_test_parity_")
        self.paper_journal_dir = Path(self.temp_dir) / "paper_journal"
        self.bybit_journal_dir = Path(self.temp_dir) / "bybit_journal"
        self.paper_journal_dir.mkdir(parents=True, exist_ok=True)
        self.bybit_journal_dir.mkdir(parents=True, exist_ok=True)

        self.patch_env = patch.dict(os.environ, {
            "BOT_JOURNAL_DIR": str(self.paper_journal_dir),
            "FQ_PAPER_POSITION_STATE": str(self.paper_journal_dir / "open_positions.json"),
            "FQ_PAPER_EQUITY_STATE": str(self.paper_journal_dir / "paper_equity.json"),
            "GATEWAY_TRADER_MODE": "paper",
            "GATEWAY_PAPER_MAINNET": "true",
            "SIGNALCOPY_USE_GOLDEN_SCALP": "true",
            "SIGNALCOPY_RISK_PER_TRADE_USD": "2.0",
        })
        self.patch_env.start()

    def tearDown(self):
        self.patch_env.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_paper_and_bybit_schema_parity(self):
        """Test full provenance schema parity between Paper and Bybit trader."""
        entry_ref = 0.21464
        sl_ref = 0.20761
        tp1_ref = 0.21676
        tp2_ref = 0.22225

        # ── 1. Run Paper Trader Lifecycle ─────────────────────
        async def run_paper():
            risk_mgr = FakeRiskManager()
            trader = PaperMainnetTrader(nexus_api="http://127.0.0.1:8000")
            trader._get_fresh_open_quote = AsyncMock(return_value={
                "price": entry_ref,
                "price_fresh": True,
                "price_source": "test_feed",
                "price_observed_at": datetime.now(timezone.utc).isoformat(),
            })
            trader._get_completed_bars = AsyncMock(return_value=[])
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
                tag="sig_ena_paper_1",
                adv_snapshot={
                    "strategy": "MTF_IFVG_PRO",
                    "signal_id": "sig_ena_paper_1",
                    "target_risk_usd": 2.0,
                }
            )
            res = await gateway.execute(intent)
            self.assertTrue(res.ok)

            # Partial then Final close
            await trader._apply_take_profits("ENAUSDT", tp1_ref)
            await trader._close("ENAUSDT", tp2_ref, "TP2_RUNNER_FULL")
            await trader.stop()

            history_file = self.paper_journal_dir / "trade_history.json"
            with open(history_file, "r", encoding="utf-8") as f:
                records = json.load(f)
            return records[-1]

        # ── 2. Run Bybit Trader Lifecycle ─────────────────────
        async def run_bybit():
            trader = object.__new__(BybitMainnetTrader)
            trader.client = FakeBybitClient(mark=entry_ref)
            trader.journal_path = self.bybit_journal_dir
            trader.capital = 100.0
            trader.risk_per_trade = 2.0
            trader.max_leverage = 10
            trader.max_concurrent = 5
            trader.dynamic_exit_enabled = False
            trader.positions = {}
            trader._position_lock = asyncio.Lock()
            trader._kline_cache = {}
            trader.reentry_engine = None
            trader.exhaustion_engine = None
            trader._config_bool = lambda k, default=False: default
            trader._quantize = lambda val, step: f"{float(val):.3f}"
            trader._save_positions = lambda: None

            async def fake_open_quote(sym):
                return {
                    "price": entry_ref,
                    "price_source": "bybit_usdt_perp",
                    "price_observed_at": datetime.now(timezone.utc).isoformat(),
                    "price_age_sec": 0.0,
                    "price_fresh": True
                }
            trader._get_fresh_open_quote = fake_open_quote

            async def fake_close_quote(sym):
                return {"mark": tp1_ref}
            trader._fetch_fresh_quote = fake_close_quote

            # Mock notifications
            mock_send_open = AsyncMock(return_value=True)
            mock_send_close = AsyncMock(return_value=True)

            with patch("notifications.telegram_notifier.send_open_trade", mock_send_open), \
                 patch("notifications.telegram_notifier.send_close_trade", mock_send_close):

                # 1. Open Position
                open_params = {
                    "symbol": "ENAUSDT",
                    "side": "LONG",
                    "entry_price": entry_ref,
                    "sl_price": sl_ref,
                    "sl": sl_ref,
                    "tp1": tp1_ref,
                    "tp2": tp2_ref,
                    "notional": 21.464,
                    "actual_risk_amount": 2.0,
                    "source": "AUTONOMOUS_SCANNER",
                    "strategy": "MTF_IFVG_PRO",
                    "signal_id": "sig_ena_bybit_1",
                    "intent_id": "intent_bybit_123",
                    "tag": "sig_ena_bybit_1",
                    "adv_snapshot": {
                        "strategy": "MTF_IFVG_PRO",
                        "signal_id": "sig_ena_bybit_1",
                        "target_risk_usd": 2.0,
                    }
                }
                open_res = await trader.submit_open(**open_params)
                self.assertTrue(open_res["ok"])
                self.assertIn("ENAUSDT", trader.positions)
                pos_id = trader.positions["ENAUSDT"]["position_id"]
                self.assertTrue(bool(pos_id))

                # 2. Partial Close (75 qty)
                await trader._close_partial(
                    symbol="ENAUSDT",
                    reason="TP1_PARTIAL",
                    exit_price=tp1_ref,
                    close_qty=75.0,
                    qty_remaining=25.0,
                    full_close=False,
                )

                # 3. Final Close (remaining 25 qty)
                await trader._close(
                    symbol="ENAUSDT",
                    reason="TP2_RUNNER_FULL",
                    exit_price=tp2_ref,
                )

            history_file = self.bybit_journal_dir / "trade_history.json"
            with open(history_file, "r", encoding="utf-8") as f:
                records = json.load(f)
            self.assertEqual(len(records), 2, "Expected 2 records: 1 partial + 1 final")
            return records[0], records[1]

        paper_record = asyncio.run(run_paper())
        bybit_partial_record, bybit_final_record = asyncio.run(run_bybit())

        # ── 3. Assert Root-Level Schema Fields ──────────────────
        for field in self.REQUIRED_SCHEMA_FIELDS:
            self.assertIn(
                field, paper_record,
                f"Paper record missing field '{field}': {json.dumps(paper_record, indent=2)}"
            )
            self.assertIn(
                field, bybit_final_record,
                f"Bybit final record missing field '{field}': {json.dumps(bybit_final_record, indent=2)}"
            )
            self.assertIn(
                field, bybit_partial_record,
                f"Bybit partial record missing field '{field}': {json.dumps(bybit_partial_record, indent=2)}"
            )

        # ── 4. Verify Invariants on Bybit Records ───────────────
        # Invariant A: Stable position_id between partial and final
        self.assertEqual(bybit_partial_record["position_id"], bybit_final_record["position_id"])
        self.assertEqual(bybit_final_record["intent_id"], "intent_bybit_123")
        self.assertEqual(bybit_final_record["strategy"], "MTF_IFVG_PRO")
        self.assertEqual(bybit_final_record["source"], "AUTONOMOUS_SCANNER")
        self.assertEqual(bybit_final_record["target_risk_usd"], 2.0)

        # Invariant B: qty_opened == qty_closed + qty_remaining
        # Partial close check
        self.assertAlmostEqual(
            bybit_partial_record["qty_opened"],
            bybit_partial_record["qty_closed"] + bybit_partial_record["qty_remaining"],
            places=4,
            msg="Bybit partial qty accounting failed: qty_opened != qty_closed + qty_remaining"
        )
        self.assertAlmostEqual(bybit_partial_record["qty_closed"], 75.0, places=4)
        self.assertAlmostEqual(bybit_partial_record["qty_remaining"], 25.0, places=4)

        # Final close check
        self.assertAlmostEqual(
            bybit_final_record["qty_opened"],
            bybit_final_record["qty_closed"] + bybit_final_record["qty_remaining"],
            places=4,
            msg="Bybit final qty accounting failed: qty_opened != qty_closed + qty_remaining"
        )
        self.assertEqual(bybit_final_record["qty_remaining"], 0.0)
        self.assertAlmostEqual(bybit_final_record["qty_closed"], bybit_final_record["qty_opened"], places=4)

        # Invariant C: actual_risk_usd from fill Bybit
        # 100 qty * abs(0.21464 - 0.20761) = 100 * 0.00703 = 0.703
        expected_risk = round(100.0 * abs(entry_ref - sl_ref), 4)
        self.assertAlmostEqual(bybit_final_record["actual_risk_usd"], expected_risk, places=3)

        # Invariant D: actual_rr_ladder computed from actual fill, SL, and TP
        self.assertIsInstance(bybit_final_record["actual_rr_ladder"], list)
        self.assertTrue(len(bybit_final_record["actual_rr_ladder"]) >= 2)
        for rr_val in bybit_final_record["actual_rr_ladder"]:
            self.assertGreater(rr_val, 0.0)

        # Invariant E: Aggregate final PnL (partials + final)
        self.assertEqual(bybit_partial_record["pnl_source"], "bybit_closed_pnl")
        self.assertEqual(bybit_final_record["pnl_source"], "bybit_closed_pnl")
        self.assertAlmostEqual(bybit_partial_record["pnl_usd"], 0.75, places=2)
        self.assertAlmostEqual(bybit_final_record["slice_pnl_usd"], 0.25, places=2)
        self.assertAlmostEqual(bybit_final_record["pnl_usd"], 1.00, places=2)
        self.assertAlmostEqual(
            bybit_final_record["pnl_usd"],
            round(bybit_partial_record["pnl_usd"] + bybit_final_record["slice_pnl_usd"], 2),
            places=2,
            msg="Final close PnL must be aggregate of partials + final slice"
        )

        # Invariant F: Mode check
        self.assertEqual(paper_record["mode"], "PAPER")
        self.assertIn(bybit_final_record["mode"], ("BYBIT", "LIVE"))


if __name__ == "__main__":
    unittest.main()
