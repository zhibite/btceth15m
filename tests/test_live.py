"""Live broker: order placement, response parsing and persistence.

The exchange client is injected, so these tests never touch the network.
"""
from types import SimpleNamespace

import pytest

from src.config import AppSettings, RuntimeConfig
from src.execution.live import LiveBroker
from src.execution.router import BrokerRouter
from src.market.models import WindowMarket
from src.persistence.db import Database


class _FakeDB:
    def __init__(self):
        self.data = {}

    def get_setting(self, key):
        return self.data.get(key)

    def set_setting(self, key, value):
        self.data[key] = value


class _FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def create_and_post_market_order(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _settings(**overrides):
    data = {
        "live_enabled": True,
        "paper_trading": True,
        "wallet_private_key": "0x" + "11" * 32,
        "assets": "BTC,ETH",
    }
    data.update(overrides)
    return AppSettings(**data)


def _market() -> WindowMarket:
    return WindowMarket(
        asset="BTC",
        start_ts=1000,
        end_ts=1900,
        slug="btc-updown-15m-1000",
        up_token="up",
        down_token="down",
        price_to_beat=100.0,
    )


async def test_live_buy_persists_exchange_fill(tmp_path):
    db = Database(tmp_path / "live.db")
    client = _FakeClient(
        {
            "success": True,
            "orderID": "0xorder",
            "takingAmount": "10",
            "makingAmount": "5",
            "status": "matched",
        }
    )
    broker = LiveBroker(
        _settings(),
        db,
        RuntimeConfig(_FakeDB()),
        client_factory=lambda: client,
        order_factory=lambda token_id, amount: {
            "token_id": token_id,
            "amount": amount,
        },
    )

    pos = await broker.buy(
        market=_market(),
        side="UP",
        token_id="up",
        notional=5.0,
        asks=[(0.50, 100.0)],
        price_to_beat=100.0,
        reason="test",
    )

    assert pos is not None
    assert pos.mode == "live"
    assert abs(pos.size - 10.0) < 1e-9
    assert abs(pos.avg_price - 0.5) < 1e-9
    assert abs(pos.fees - 0.175) < 1e-9
    assert abs(pos.cost - 5.175) < 1e-9
    assert len(client.calls) == 1
    assert client.calls[0]["order_args"]["amount"] == 5.0

    order = db.recent_orders(1)[0]
    assert order["mode"] == "live"
    assert order["status"] == "filled"
    assert order["id"] == "0xorder"
    fill = db.recent_fills(1)[0]
    assert fill["mode"] == "live"
    assert abs(float(fill["size"]) - 10.0) < 1e-9
    positions = db.recent_positions(1)
    assert positions[0]["mode"] == "live"
    db.close()


async def test_live_rejected_order_is_recorded(tmp_path):
    db = Database(tmp_path / "live.db")
    client = _FakeClient(
        {"success": False, "orderID": "0xrejected", "errorMsg": "not enough balance"}
    )
    broker = LiveBroker(
        _settings(),
        db,
        RuntimeConfig(_FakeDB()),
        client_factory=lambda: client,
        order_factory=lambda token_id, amount: {"token_id": token_id},
    )

    pos = await broker.buy(
        market=_market(),
        side="UP",
        token_id="up",
        notional=5.0,
        asks=[(0.50, 100.0)],
        price_to_beat=100.0,
    )

    assert pos is None
    order = db.recent_orders(1)[0]
    assert order["id"] == "0xrejected"
    assert order["status"] == "rejected"
    assert "not enough balance" in order["reason"]
    assert db.recent_positions(1) == []
    db.close()


async def test_unmatched_fok_is_not_booked_as_a_fill(tmp_path):
    db = Database(tmp_path / "live.db")
    client = _FakeClient(
        {
            "success": True,
            "orderID": "0xunmatched",
            "status": "unmatched",
            "takingAmount": "",
            "makingAmount": "",
        }
    )
    broker = LiveBroker(
        _settings(),
        db,
        RuntimeConfig(_FakeDB()),
        client_factory=lambda: client,
        order_factory=lambda token_id, amount: {"token_id": token_id},
    )

    pos = await broker.buy(
        market=_market(),
        side="UP",
        token_id="up",
        notional=5.0,
        asks=[(0.50, 100.0)],
        price_to_beat=100.0,
    )

    assert pos is None
    order = db.recent_orders(1)[0]
    assert order["status"] == "rejected"
    assert "unmatched" in order["reason"]
    assert db.recent_fills(1) == []
    assert db.recent_positions(1) == []
    db.close()


async def test_live_disabled_raises(tmp_path):
    db = Database(tmp_path / "live.db")
    broker = LiveBroker(
        _settings(live_enabled=False),
        db,
        RuntimeConfig(_FakeDB()),
        client_factory=lambda: _FakeClient({}),
    )
    with pytest.raises(RuntimeError):
        await broker.buy(
            market=_market(),
            side="UP",
            token_id="up",
            notional=5.0,
            asks=[(0.50, 100.0)],
            price_to_beat=100.0,
        )
    db.close()


def test_live_availability_requires_credentials():
    assert not LiveBroker(_settings(wallet_private_key="")).available
    assert not LiveBroker(_settings(live_enabled=False)).available


def test_router_routes_per_asset_mode():
    runtime = RuntimeConfig(_FakeDB())
    runtime.set_asset_state("BTC", mode="live")
    settings = SimpleNamespace(
        paper_trading=True,
        live_enabled=True,
        assets_list=["BTC", "ETH"],
    )
    runtime.set_asset_state("ETH", mode="inherit")
    paper = SimpleNamespace()
    live = SimpleNamespace(available=True)
    router = BrokerRouter(settings, paper, live, runtime)

    assert router.mode_for("BTC") == "live"
    assert router.mode_for("ETH") == "paper"
    assert router.mode == "mixed"

    live.available = False
    assert router.mode_for("BTC") == "paper"
    assert router.mode == "paper"
