"""Load-related behavior with several assets enabled.

Covers the things that grow with the asset count:
  * CLOB token/books pruning as windows roll over,
  * exponential backoff for markets Gamma never returns,
  * disabled assets unsubscribing from CLOB books.
"""
import json
from types import SimpleNamespace

from src.config import RuntimeConfig
from src.feeds.clob_ws import ClobWsFeed
from src.market.models import WindowMarket
from src.market.window_manager import WindowManager
from src.strategy.engine import StrategyEngine


class _FakeDB:
    def __init__(self):
        self.data = {}

    def get_setting(self, key):
        return self.data.get(key)

    def set_setting(self, key, value):
        self.data[key] = value


def _snapshot(token: str, price: float) -> str:
    return json.dumps(
        {
            "asset_id": token,
            "bids": [{"price": str(price - 0.01), "size": "10"}],
            "asks": [{"price": str(price), "size": "10"}],
        }
    )


def test_clob_prunes_expired_tokens_and_ignores_late_messages():
    feed = ClobWsFeed(["BTC", "ETH"], "wss://example.invalid")
    feed.set_tokens("BTC", ["btc-old-up", "btc-old-down"])
    feed.set_tokens("ETH", ["eth-old-up", "eth-old-down"])
    assert feed.health()["active_tokens"] == 4

    feed._handle(_snapshot("btc-old-up", 0.50))
    assert feed.book("btc-old-up") is not None

    # New 15m window: old BTC tokens are no longer subscribed.
    feed.set_tokens("BTC", ["btc-new-up", "btc-new-down"])
    assert feed.book("btc-old-up") is None
    assert feed.book("btc-new-up") is not None
    assert feed.health()["active_tokens"] == 4

    # An in-flight message for the old token must not re-add it.
    feed._handle(_snapshot("btc-old-up", 0.60))
    assert feed.book("btc-old-up") is None

    # Disabling an asset drops its tokens too.
    feed.set_tokens("ETH", [])
    assert feed.book("eth-old-up") is None
    assert feed.health()["active_tokens"] == 2


class _MissingGamma:
    def __init__(self, succeed_after=None):
        self.calls = []
        self.succeed_after = succeed_after

    async def get_window(self, asset, start_ts):
        self.calls.append((asset, int(start_ts)))
        if self.succeed_after is not None and len(self.calls) >= self.succeed_after:
            return WindowMarket(
                asset=asset,
                start_ts=int(start_ts),
                end_ts=int(start_ts) + 900,
                up_token="up",
                down_token="down",
                price_to_beat=100.0,
            )
        return None


async def test_missing_market_backoff_grows_and_resets():
    gamma = _MissingGamma(succeed_after=4)
    wm = WindowManager(gamma, ["HYPE"], window_sec=900)

    await wm._discover(1000.0, 900)
    assert len(gamma.calls) == 1
    assert wm._missing_delay["HYPE"] == 6.0

    await wm._discover(1001.0, 900)  # inside the 6s wait
    assert len(gamma.calls) == 1

    await wm._discover(1006.0, 900)  # 6s elapsed -> retry, delay doubles
    assert len(gamma.calls) == 2
    assert wm._missing_delay["HYPE"] == 12.0

    await wm._discover(1018.0, 900)  # 12s elapsed -> retry, still missing
    assert len(gamma.calls) == 3
    assert wm._missing_delay["HYPE"] == 24.0

    await wm._discover(1042.0, 900)  # 24s elapsed -> success
    assert len(gamma.calls) == 4
    assert "HYPE" not in wm._missing_delay
    assert wm.current_window("HYPE") is not None


class _FakeBooks:
    def __init__(self):
        self.tokens = {}

    def set_tokens(self, asset, tokens):
        self.tokens[asset] = list(tokens)

    def book(self, token):
        return None


def _market(asset: str) -> WindowMarket:
    return WindowMarket(
        asset=asset,
        start_ts=900,
        end_ts=1800,
        up_token=f"{asset.lower()}-up",
        down_token=f"{asset.lower()}-down",
        price_to_beat=100.0,
    )


async def test_disabled_asset_unsubscribes_from_clob_books():
    runtime = RuntimeConfig(_FakeDB())
    runtime.set_asset_state("BTC", enabled=False)
    engine = StrategyEngine.__new__(StrategyEngine)
    engine.settings = SimpleNamespace(assets_list=["BTC"], window_sec=900)
    engine.runtime = runtime
    engine.windows = SimpleNamespace(current_window=lambda asset: _market(asset))
    engine.books = _FakeBooks()
    engine.positions = {}
    engine._entry_cooldown = {}
    engine._last_near_miss = {}
    engine.latest = {}
    engine.router = SimpleNamespace(mode_for=lambda asset: "paper", mode="paper")
    engine.kill = SimpleNamespace(to_dict=lambda: {"active": False})
    engine.risk = SimpleNamespace(daily_pnl=lambda asset: 0.0)

    await engine.tick()

    assert engine.books.tokens["BTC"] == []
    assert engine.latest["BTC"]["status"] == "disabled"
    assert engine.latest["BTC"]["enabled"] is False
