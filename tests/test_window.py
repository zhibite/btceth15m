from src.market.window_manager import WindowManager


class _DummyGamma:
    async def get_window(self, asset, start_ts):
        return None


def test_boundary_floors_to_5m():
    wm = WindowManager(_DummyGamma(), ["BTC"], window_sec=300)
    assert wm.boundary(1000.0) == 900
    assert wm.boundary(1199.9) == 900
    assert wm.boundary(1200.0) == 1200
    assert wm.boundary(1499.0) == 1200
