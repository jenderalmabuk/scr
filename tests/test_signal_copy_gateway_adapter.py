import asyncio

from gateway.adapters.signal_copy_adapter import GatewayTraderShim


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.intent = None

    async def execute(self, intent):
        self.intent = intent
        return self.response


def test_adapter_uses_gateway_top_level_confirmation():
    client = FakeClient({
        "ok": True,
        "position_confirmed": True,
        "entry_price": 100.0,
        "notional": 75.0,
        "trader_response": {"entry_price": 100.0},
    })
    result = asyncio.run(GatewayTraderShim(client).submit_open(
        symbol="BTCUSDT",
        side="LONG",
        entry_price=100.0,
        sl_price=95.0,
        tp1=110.0,
        notional=75.0,
    ))
    assert result["position_confirmed"] is True
    assert result["notional"] == 75.0


def test_adapter_falls_back_to_positive_fill_values():
    client = FakeClient({
        "ok": True,
        "notional": 50.0,
        "trader_response": {"entry_price": 100.0},
    })
    result = asyncio.run(GatewayTraderShim(client).submit_open(
        symbol="ETHUSDT",
        side="SHORT",
        entry_price=100.0,
        sl_price=105.0,
        tp1=90.0,
        notional=50.0,
    ))
    assert result["position_confirmed"] is True
    assert client.intent.symbol == "ETHUSDT"
