import pytest

from src.market.models import WindowMarket
from src.market.window_manager import WindowManager


class _DummyGamma:
    async def get_window(self, asset, start_ts):
        return None


class _Stop(Exception):
    pass


def test_boundary_floors_to_5m():
    wm = WindowManager(_DummyGamma(), ["BTC"], window_sec=300)
    assert wm.boundary(1000.0) == 900
    assert wm.boundary(1199.9) == 900
    assert wm.boundary(1200.0) == 1200
    assert wm.boundary(1499.0) == 1200


def test_boundary_floors_to_15m():
    wm = WindowManager(_DummyGamma(), ["BTC"], window_sec=900)
    assert wm.boundary(1793957999.0) == 1793957400
    assert wm.boundary(1793958300.0) == 1793958300


async def test_gamma_not_polled_while_price_to_beat_is_known(monkeypatch):
    """finalPrice is null for the whole window; that must not trigger polls."""

    class _Gamma:
        def __init__(self):
            self.calls = []

        async def get_window(self, asset, start_ts):
            self.calls.append((asset, int(start_ts)))
            return WindowMarket(
                asset=asset,
                start_ts=int(start_ts),
                end_ts=int(start_ts) + 300,
                up_token="up",
                down_token="down",
                price_to_beat=100.0,
            )

    gamma = _Gamma()
    wm = WindowManager(gamma, ["BTC"], window_sec=300)
    wm._running = True  # bypass start() so the loop runs without a task
    monkeypatch.setattr("src.market.window_manager.time.time", lambda: 1100.0)

    sleeps = 0

    async def fake_sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 3:
            raise _Stop

    monkeypatch.setattr("src.market.window_manager.asyncio.sleep", fake_sleep)

    with pytest.raises(_Stop):
        await wm._run()

    # One discovery fetch only: the window already has priceToBeat.
    assert gamma.calls == [("BTC", 900)]
