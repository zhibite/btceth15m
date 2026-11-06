import pytest

from src.config import (
    AppSettings,
    RuntimeConfig,
    StrategyParams,
    interval_label,
    interval_seconds,
)


class _FakeDB:
    def __init__(self):
        self.data = {}

    def get_setting(self, key):
        return self.data.get(key)

    def set_setting(self, key, value):
        self.data[key] = value


def test_strategy_defaults_are_sane():
    params = StrategyParams()
    assert 0 < params.min_edge < 1
    assert 0 < params.model_buffer < 1
    assert params.max_position_usd > 0
    assert params.entry_min_rel_s < params.entry_max_rel_s
    # The maker-shadow module and its knobs were removed.
    assert not hasattr(params, "maker_shadow_enabled")
    assert not hasattr(params, "maker_buffer")


def test_interval_helpers():
    assert interval_seconds("15m") == 900
    assert interval_seconds("5m") == 300
    assert interval_seconds("1h") == 3600
    assert interval_label(900) == "15m"
    assert interval_label(300) == "5m"
    with pytest.raises(ValueError):
        interval_seconds("15")


def test_app_settings_15m_slug():
    settings = AppSettings(market_interval="15m", window_sec=900)
    assert settings.slug_tag == "updown-15m"


def test_app_settings_rejects_interval_mismatch():
    with pytest.raises(ValueError):
        AppSettings(market_interval="15m", window_sec=300)


def test_runtime_config_persists_and_resets():
    db = _FakeDB()
    runtime = RuntimeConfig(db)
    original = runtime.params.min_edge
    runtime.update({"min_edge": 0.08, "max_position_usd": 7.0})
    assert runtime.params.min_edge == 0.08
    assert runtime.params.max_position_usd == 7.0

    reloaded = RuntimeConfig(db)
    assert reloaded.params.min_edge == 0.08

    reloaded.reset()
    assert reloaded.params.min_edge == original


def test_runtime_config_ignores_unknown_keys():
    db = _FakeDB()
    runtime = RuntimeConfig(db)
    runtime.update({"not_a_field": 123})
    assert runtime.params.min_edge > 0
