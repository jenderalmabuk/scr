import asyncio
import json
import sys
import types

from execution.bybit_mainnet_trader import BybitMainnetTrader


class FakeBybitClient:
    def __init__(self, mark):
        self.mark = mark
        self.orders = []

    def get_instrument_info(self, symbol):
        return {
            "lotSizeFilter": {"qtyStep": "0.001"},
            "priceFilter": {"tickSize": "0.1"},
        }

    def create_order(self, **kwargs):
        self.orders.append(kwargs)
        return {"orderId": "order-1"}

    def get_positions(self, symbol=None):
        if not self.orders:
            return []
        qty = float(self.orders[-1]["qty"])
        return [{
            "symbol": symbol or self.orders[-1]["symbol"],
            "side": self.orders[-1]["side"],
            "size": str(qty),
            "avgPrice": str(self.mark),
            "positionValue": str(qty * self.mark),
        }]


def _trader(mark):
    trader = object.__new__(BybitMainnetTrader)
    trader.client = FakeBybitClient(mark)
    trader.positions = {}
    trader.max_concurrent = 5
    trader.max_leverage = 10
    trader.risk_per_trade = 2.0
    trader._save_positions = lambda: None

    async def fake_quote(symbol):
        return {"mark": mark, "bid": mark - 0.01, "ask": mark + 0.01, "timestamp": 1.0}

    trader._fetch_fresh_quote = fake_quote
    return trader


def test_dynamic_exit_audit_writes_top_level_fields(tmp_path, monkeypatch):
    audit_file = tmp_path / "scratch_exit_audit.jsonl"
    monkeypatch.setenv("SCRATCH_EXIT_AUDIT_JOURNAL", str(audit_file))
    trader = object.__new__(BybitMainnetTrader)
    trader.journal_path = tmp_path
    pos = {"side": "LONG", "entry_price": 100.0, "sl_price": 95.0}
    decision = {
        "should_exit": False,
        "should_partial_exit": True,
        "time_due": True,
        "action": "PARTIAL_EXIT",
        "reason": "DAMAGE_MFE_GIVEBACK_PARTIAL",
        "hold_minutes": 60.0,
        "current_r": -0.2,
        "max_favorable_r": 0.8,
        "max_adverse_r": -0.4,
        "tp1_progress": 0.3,
        "setup_context": {"weak_setup": True},
        "manual_structure_profile": {"active": False},
    }

    trader._append_scratch_audit("BTCUSDT", pos, decision, 99.0, layer="DAMAGE_REDUCER")

    row = json.loads(audit_file.read_text().splitlines()[0])
    assert row["layer"] == "DAMAGE_REDUCER"
    assert row["hold_minutes"] == 60.0
    assert row["current_r"] == -0.2
    assert row["max_favorable_r"] == 0.8
    assert row["max_adverse_r"] == -0.4
    assert row["tp1_progress"] == 0.3
    assert row["manual_profile"] == {"active": False}
    assert row["setup_context"] == {"weak_setup": True}
    assert row["decision_reason"] == "DAMAGE_MFE_GIVEBACK_PARTIAL"



def test_outside_entry_zone_rejects_without_live_limit_order():
    trader = _trader(mark=102.0)
    result = asyncio.run(trader.open_position(
        symbol="BTCUSDT",
        side="LONG",
        entry_price=100.0,
        sl_price=95.0,
        tp_prices=[110.0],
        requested_notional=100.0,
        requested_risk_amount=10.0,
    ))
    assert result["ok"] is False
    assert result["position_confirmed"] is False
    assert result["code"] == "NOT_IN_ENTRY_ZONE"
    assert trader.client.orders == []


def test_market_fill_returns_confirmed_position(monkeypatch):
    async def fake_send_open_trade(payload):
        return True

    monkeypatch.setitem(
        sys.modules,
        "notifications.telegram_notifier",
        types.SimpleNamespace(send_open_trade=fake_send_open_trade),
    )

    trader = _trader(mark=100.1)
    result = asyncio.run(trader.open_position(
        symbol="ETHUSDT",
        side="LONG",
        entry_price=100.0,
        sl_price=95.0,
        tp_prices=[110.0],
        requested_notional=100.0,
        requested_risk_amount=10.0,
    ))
    assert result["ok"] is True
    assert result["position_confirmed"] is True
    assert result["notional"] > 0
    assert result["qty"] > 0
    assert trader.client.orders[0]["order_type"] == "Market"
    assert trader.client.orders[0]["price"] is None
