from types import SimpleNamespace

from src.config import RuntimeConfig, get_asset_strategy_defaults
from src.strategy.engine import StrategyEngine


class _FakeDB:
    def __init__(self):
        self.data = {}

    def get_setting(self, key):
        return self.data.get(key)

    def set_setting(self, key, value):
        self.data[key] = value


def test_asset_override_inherits_global():
    db = _FakeDB()
    runtime = RuntimeConfig(db)
    global_edge = runtime.params.min_edge

    runtime.update({"min_edge": 0.12, "max_position_usd": 7.0}, asset="BTC")

    assert runtime.params_for("BTC").min_edge == 0.12
    assert runtime.params_for("BTC").max_position_usd == 7.0
    assert runtime.params_for("ETH").min_edge == global_edge
    assert runtime.has_override("BTC")
    assert not runtime.has_override("ETH")


def test_asset_override_persists_and_resets():
    db = _FakeDB()
    runtime = RuntimeConfig(db)
    runtime.update({"min_edge": 0.12}, asset="BTC")

    reloaded = RuntimeConfig(db)
    assert reloaded.params_for("BTC").min_edge == 0.12
    assert reloaded.has_override("BTC")

    reloaded.reset("BTC")
    assert not reloaded.has_override("BTC")
    assert reloaded.params_for("BTC").min_edge == reloaded.params.min_edge


def test_global_update_does_not_change_existing_asset_override():
    db = _FakeDB()
    runtime = RuntimeConfig(db)
    runtime.update({"min_edge": 0.12}, asset="BTC")
    runtime.update({"min_edge": 0.20})

    assert runtime.params.min_edge == 0.20
    assert runtime.params_for("BTC").min_edge == 0.12
    assert runtime.params_for("ETH").min_edge == 0.20


def test_unknown_asset_uses_global():
    db = _FakeDB()
    runtime = RuntimeConfig(db)
    runtime.update({"min_edge": 0.11})
    assert runtime.params_for("SOL").min_edge == 0.11
    assert not runtime.has_override("SOL")


def test_shipped_btc_defaults_are_loaded_from_toml():
    """config.toml table names are upper-case; the loader must still find them."""
    btc = get_asset_strategy_defaults().get("BTC", {})
    assert btc["entry_min_rel_s"] == 180
    assert btc["entry_max_rel_s"] == 270
    assert btc["model_buffer"] == 0.05
    assert btc["min_fair"] == 0.65
    assert btc["min_entry_price"] == 0.55
    assert "ETH" not in get_asset_strategy_defaults()


def test_asset_defaults_merge_over_global_and_reset():
    db = _FakeDB()
    runtime = RuntimeConfig(
        db,
        asset_defaults={"BTC": {"min_fair": 0.65, "entry_min_rel_s": 180}},
    )
    btc = runtime.params_for("BTC")
    assert btc.min_fair == 0.65
    assert btc.entry_min_rel_s == 180
    # Fields not listed inherit the current global value.
    assert btc.model_buffer == runtime.params.model_buffer
    assert runtime.source_for("BTC") == "asset_default"
    assert runtime.source_for("ETH") == "global"
    assert not runtime.has_override("BTC")

    # Global edits still flow into the unspecified BTC fields.
    runtime.update({"model_buffer": 0.04})
    assert runtime.params_for("BTC").model_buffer == 0.04

    # Saving a dashboard override takes precedence over the config defaults.
    runtime.update({"min_fair": 0.70}, asset="BTC")
    assert runtime.source_for("BTC") == "override"
    assert runtime.params_for("BTC").min_fair == 0.70

    # Reset removes the override and returns to the BTC config default.
    runtime.reset("BTC")
    assert runtime.source_for("BTC") == "asset_default"
    assert runtime.params_for("BTC").min_fair == 0.65


def test_tick_interval_honours_fastest_asset_override():
    db = _FakeDB()
    runtime = RuntimeConfig(db)
    runtime.update({"engine_tick_s": 1.0}, asset="BTC")
    runtime.update({"engine_tick_s": 0.2}, asset="ETH")
    engine = SimpleNamespace(
        settings=SimpleNamespace(assets_list=["BTC", "ETH"]),
        runtime=runtime,
    )
    assert StrategyEngine._tick_interval(engine) == 0.2
