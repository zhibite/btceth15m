"""Ended windows are settled even when the bot never traded them."""
from types import SimpleNamespace

from src.market.models import WindowMarket
from src.persistence.db import Database
from src.strategy.engine import StrategyEngine


class _FakeWindows:
    def __init__(self, results=None):
        self.results = results or {}
        self.calls = []

    async def fetch_window(self, asset, start_ts):
        self.calls.append((str(asset), int(start_ts)))
        return self.results.get((str(asset), int(start_ts)))


class _Buffer:
    def __init__(self, twap):
        self.twap = twap

    def twap_ending_at(self, end_ts, window_s=60.0):
        return self.twap


class _FakeRtds:
    def __init__(self, twaps=None):
        self.twaps = twaps or {}

    def buffer(self, asset):
        value = self.twaps.get(str(asset))
        return _Buffer(value) if value is not None else None


def _db_with_window(tmp_path, *, start_ts=1000, end_ts=1900, p0=100.0):
    db = Database(tmp_path / "windows.db")
    db.upsert_window(
        WindowMarket(
            asset="XRP",
            start_ts=start_ts,
            end_ts=end_ts,
            slug=f"xrp-updown-15m-{start_ts}",
            up_token="up",
            down_token="down",
            price_to_beat=p0,
        )
    )
    return db


def _engine(db, windows, rtds):
    engine = StrategyEngine.__new__(StrategyEngine)
    engine.db = db
    engine.windows = windows
    engine.rtds = rtds
    engine.settings = SimpleNamespace(twap_window_s=60)
    engine.positions = {}
    engine._window_settle_next = {}
    engine._window_settle_attempts = {}
    engine._settle_first_seen = {}
    return engine


async def test_settle_windows_uses_official_gamma_result(tmp_path, monkeypatch):
    db = _db_with_window(tmp_path)
    gamma = _FakeWindows(
        {
            ("XRP", 1000): WindowMarket(
                asset="XRP",
                start_ts=1000,
                end_ts=1900,
                up_token="up",
                down_token="down",
                price_to_beat=100.0,
                final_price=101.0,
            )
        }
    )
    engine = _engine(db, gamma, _FakeRtds())
    monkeypatch.setattr("src.strategy.engine.time.time", lambda: 1901.0)

    await engine.settle_windows()

    row = db.recent_windows(1)[0]
    assert row["status"] == "settled"
    assert row["outcome"] == "UP"
    assert row["final_price"] == 101.0
    assert row["price_to_beat"] == 100.0
    assert row["p0_source"] == "gamma"
    db.close()


async def test_settle_windows_falls_back_to_local_twap(tmp_path, monkeypatch):
    db = _db_with_window(tmp_path)
    # Gamma knows priceToBeat but has not published finalPrice yet.
    gamma = _FakeWindows(
        {
            ("XRP", 1000): WindowMarket(
                asset="XRP",
                start_ts=1000,
                end_ts=1900,
                up_token="up",
                down_token="down",
                price_to_beat=100.0,
            )
        }
    )
    engine = _engine(db, gamma, _FakeRtds({"XRP": 99.5}))
    # Wait for the official result first; the local TWAP only takes over
    # after _WINDOW_SETTLE_TWAP_WAIT_S.
    monkeypatch.setattr("src.strategy.engine.time.time", lambda: 1925.0)

    await engine.settle_windows()

    row = db.recent_windows(1)[0]
    assert row["status"] == "settled"
    assert row["outcome"] == "DOWN"
    assert row["final_price"] == 99.5
    db.close()


async def test_settle_windows_waits_before_twap_fallback(tmp_path, monkeypatch):
    db = _db_with_window(tmp_path)
    gamma = _FakeWindows(
        {
            ("XRP", 1000): WindowMarket(
                asset="XRP",
                start_ts=1000,
                end_ts=1900,
                up_token="up",
                down_token="down",
                price_to_beat=100.0,
            )
        }
    )
    engine = _engine(db, gamma, _FakeRtds({"XRP": 99.5}))
    monkeypatch.setattr("src.strategy.engine.time.time", lambda: 1910.0)

    await engine.settle_windows()

    assert db.recent_windows(1)[0]["status"] == "open"
    assert engine._window_settle_next[("XRP", 1000)] == 1915.0
    db.close()


async def test_settle_windows_leaves_windows_with_open_positions(tmp_path, monkeypatch):
    db = _db_with_window(tmp_path)
    gamma = _FakeWindows()
    engine = _engine(db, gamma, _FakeRtds())
    engine.positions = {("XRP", 1000): object()}
    monkeypatch.setattr("src.strategy.engine.time.time", lambda: 1901.0)

    await engine.settle_windows()

    assert gamma.calls == []
    assert db.recent_windows(1)[0]["status"] == "open"
    db.close()


async def test_settle_windows_retries_with_backoff(tmp_path, monkeypatch):
    db = _db_with_window(tmp_path)
    gamma = _FakeWindows()
    engine = _engine(db, gamma, _FakeRtds())
    clock = {"now": 1901.0}
    monkeypatch.setattr("src.strategy.engine.time.time", lambda: clock["now"])

    await engine.settle_windows()
    assert gamma.calls == [("XRP", 1000)]
    assert engine._window_settle_next[("XRP", 1000)] == 1906.0

    # Not due yet -> no new request.
    clock["now"] = 1905.0
    await engine.settle_windows()
    assert len(gamma.calls) == 1

    clock["now"] = 1906.0
    await engine.settle_windows()
    assert len(gamma.calls) == 2
    assert engine._window_settle_next[("XRP", 1000)] == 1926.0
    assert db.recent_windows(1)[0]["status"] == "open"
    db.close()


async def test_settle_windows_gives_up_after_horizon(tmp_path, monkeypatch):
    db = _db_with_window(tmp_path)
    gamma = _FakeWindows()
    engine = _engine(db, gamma, _FakeRtds())
    monkeypatch.setattr("src.strategy.engine.time.time", lambda: 2801.0)

    await engine.settle_windows()

    # One last attempt, then no more Gamma traffic for this window.
    assert gamma.calls == [("XRP", 1000)]
    assert engine._window_settle_next[("XRP", 1000)] == float("inf")
    assert db.recent_windows(1)[0]["status"] == "open"
    db.close()


def test_unsettled_windows_filters_ended_open_rows(tmp_path):
    db = Database(tmp_path / "query.db")

    def add(asset, start_ts, end_ts):
        db.upsert_window(
            WindowMarket(
                asset=asset,
                start_ts=start_ts,
                end_ts=end_ts,
                up_token="up",
                down_token="down",
                price_to_beat=100.0,
            )
        )

    add("BTC", 1000, 1900)  # ended, still open -> returned
    add("BTC", 2000, 2900)  # not ended before the cutoff -> excluded
    add("ETH", 400, 1300)  # ended but already settled -> excluded
    db.set_window_final("ETH", 400, 101.0, "UP")

    rows = db.unsettled_windows(since_end_ts=0, until_end_ts=2500, limit=10)
    assert [(r["asset"], r["start_ts"]) for r in rows] == [("BTC", 1000)]
    db.close()


def test_set_window_final_updates_official_price_to_beat(tmp_path):
    db = Database(tmp_path / "p0.db")
    db.upsert_window(
        WindowMarket(
            asset="BTC",
            start_ts=1000,
            end_ts=1900,
            up_token="up",
            down_token="down",
            price_to_beat=100.0,
        )
    )

    db.set_window_final("BTC", 1000, 101.0, "UP", price_to_beat=99.0)

    row = db.recent_windows(1)[0]
    assert row["status"] == "settled"
    assert row["price_to_beat"] == 99.0
    assert row["p0_source"] == "gamma"
    db.close()
