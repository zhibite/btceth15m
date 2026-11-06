from src.config import RuntimeConfig


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
