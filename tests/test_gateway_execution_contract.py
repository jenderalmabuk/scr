import asyncio
import os

from gateway.order_intent import OrderIntent
from gateway.service import ExecutionGateway, GatewayResult


class FakeRisk:
    max_notional_pct = 1.0

    def __init__(self):
        self.released = False
        self.committed = False

    def get_current_equity(self):
        return 1000.0

    def get_max_running_positions(self):
        return 5

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


class FakeTrader:
    def __init__(self, response):
        self.response = response

    async def _get_fresh_open_quote(self, symbol):
        return {
            "price": 100.0,
            "price_source": "fake",
            "price_observed_at": "2026-09-13T00:00:00+00:00",
            "price_age_sec": 0.0,
            "price_fresh": True,
        }

    async def submit_open(self, **kwargs):
        return self.response


def _intent():
    return OrderIntent(
        source="SIGNAL_COPY",
        symbol="BTCUSDT",
        side="LONG",
        entry_price=100.0,
        sl_price=95.0,
        tps=[110.0],
        notional=100.0,
    )


def test_gateway_result_serializes_position_confirmed():
    result = GatewayResult(ok=True, reason="opened", position_confirmed=True)
    assert result.to_dict()["position_confirmed"] is True


def test_gateway_size_caps_explicit_notional_to_strict_usd_risk():
    old = os.environ.get("SIGNALCOPY_RISK_PER_TRADE_USD")
    os.environ["SIGNALCOPY_RISK_PER_TRADE_USD"] = "2"
    try:
        gateway = ExecutionGateway(FakeTrader({}), FakeRisk())
        notional, risk_amount, err = gateway._size(_intent(), 100.0, 95.0, "LONG")
    finally:
        if old is None:
            os.environ.pop("SIGNALCOPY_RISK_PER_TRADE_USD", None)
        else:
            os.environ["SIGNALCOPY_RISK_PER_TRADE_USD"] = old

    assert err is None
    assert notional == 40.0
    assert risk_amount == 2.0


def test_gateway_size_rejects_when_min_notional_would_break_strict_risk():
    old_risk = os.environ.get("SIGNALCOPY_RISK_PER_TRADE_USD")
    old_min = os.environ.get("GATEWAY_MIN_NOTIONAL_USD")
    os.environ["SIGNALCOPY_RISK_PER_TRADE_USD"] = "2"
    os.environ["GATEWAY_MIN_NOTIONAL_USD"] = "10"
    try:
        gateway = ExecutionGateway(FakeTrader({}), FakeRisk())
        notional, risk_amount, err = gateway._size(_intent(), 100.0, 70.0, "LONG")
    finally:
        if old_risk is None:
            os.environ.pop("SIGNALCOPY_RISK_PER_TRADE_USD", None)
        else:
            os.environ["SIGNALCOPY_RISK_PER_TRADE_USD"] = old_risk
        if old_min is None:
            os.environ.pop("GATEWAY_MIN_NOTIONAL_USD", None)
        else:
            os.environ["GATEWAY_MIN_NOTIONAL_USD"] = old_min

    assert notional == 0.0
    assert risk_amount == 0.0
    assert "strict $2.00 cap" in err


def test_gateway_rejects_unconfirmed_trader_acceptance():
    risk = FakeRisk()
    gateway = ExecutionGateway(FakeTrader({"ok": True, "executed": True}), risk)
    result = asyncio.run(gateway.execute(_intent()))
    assert result.ok is False
    assert result.position_confirmed is False
    assert risk.released is True
    assert risk.committed is False


def test_gateway_confirms_positive_fill_contract():
    risk = FakeRisk()
    gateway = ExecutionGateway(
        FakeTrader({"ok": True, "executed": True, "entry_price": 100.0, "notional": 100.0}),
        risk,
    )
    result = asyncio.run(gateway.execute(_intent()))
    assert result.ok is True
    assert result.position_confirmed is True
    assert result.notional == 100.0
    assert risk.committed is True
