"""End-to-end checks for per-asset config and per-asset PnL views.

Uses the real FastAPI app with a minimal fake state so the HTTP layer,
query-parameter parsing and RuntimeConfig persistence are all exercised
together.
"""
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.config import RuntimeConfig
from src.market.models import PaperPosition, WindowMarket
from src.persistence.db import Database
from src.web import app as web_app
from src.web import auth


class _FakeDB:
    def __init__(self):
        self.data = {}

    def get_setting(self, key):
        return self.data.get(key)

    def set_setting(self, key, value):
        self.data[key] = value


def _client(tmp_path):
    db = Database(tmp_path / "web.db")
    runtime = RuntimeConfig(_FakeDB())
    state = SimpleNamespace(
        settings=SimpleNamespace(assets_list=["BTC", "ETH"]),
        runtime=runtime,
        db=db,
        router=SimpleNamespace(mode="paper"),
        killswitch=SimpleNamespace(to_dict=lambda: {"active": False}),
        engine=SimpleNamespace(latest={}),
        rtds=SimpleNamespace(health=lambda: {}),
        books=SimpleNamespace(health=lambda: {}),
        started_at=time.time(),
    )
    web_app.set_state(state)
    client = TestClient(web_app.app)
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session("tester"))
    return client, runtime, db


def _settled_position(db, asset, start_ts, pnl):
    db.upsert_window(
        WindowMarket(
            asset=asset,
            start_ts=start_ts,
            end_ts=start_ts + 300,
            slug=f"{asset.lower()}-updown-5m-{start_ts}",
            up_token="up",
            down_token="down",
            price_to_beat=100.0,
        )
    )
    pid = db.insert_position(
        PaperPosition(
            id=0,
            asset=asset,
            start_ts=start_ts,
            end_ts=start_ts + 300,
            token_id="up",
            side="UP",
            size=10.0,
            avg_price=0.5,
            cost=5.0,
            fees=0.0,
            entry_ts=start_ts + 10,
            price_to_beat=100.0,
        )
    )
    db.settle_position(pid, "UP", payout=10.0 if pnl > 0 else 0.0, pnl=pnl)


def test_config_api_per_asset_override(tmp_path):
    client, runtime, _ = _client(tmp_path)

    global_cfg = client.get("/api/config").json()
    assert global_cfg["asset"] == ""
    assert global_cfg["assets"] == ["BTC", "ETH"]
    assert global_cfg["overridden"] is False
    global_edge = global_cfg["params"]["min_edge"]

    btc_before = client.get("/api/config", params={"asset": "btc"}).json()
    assert btc_before["asset"] == "BTC"
    assert btc_before["overridden"] is False
    assert btc_before["params"]["min_edge"] == global_edge

    saved = client.put(
        "/api/config",
        json={
            "asset": "BTC",
            "params": {"min_edge": 0.11, "max_position_usd": 3.0},
        },
    )
    assert saved.status_code == 200
    body = saved.json()
    assert body["overridden"] is True
    assert body["params"]["min_edge"] == 0.11
    assert body["params"]["max_position_usd"] == 3.0

    # ETH keeps inheriting the global values.
    eth = client.get("/api/config", params={"asset": "ETH"}).json()
    assert eth["overridden"] is False
    assert eth["params"]["min_edge"] == global_edge

    # A later global change only affects assets without an override.
    client.put("/api/config", json={"params": {"min_edge": 0.07}})
    assert runtime.params_for("BTC").min_edge == 0.11
    assert runtime.params_for("ETH").min_edge == 0.07

    reset = client.post("/api/config/reset", json={"asset": "BTC"}).json()
    assert reset["overridden"] is False
    assert reset["params"]["min_edge"] == 0.07
    assert not runtime.has_override("BTC")


def test_config_api_rejects_bad_value(tmp_path):
    client, _, _ = _client(tmp_path)
    r = client.put(
        "/api/config",
        json={"asset": "BTC", "params": {"min_edge": 5.0}},
    )
    assert r.status_code == 400
    assert r.json()["detail"]


def test_pnl_api_filters_by_asset(tmp_path):
    client, _, db = _client(tmp_path)
    _settled_position(db, "BTC", 1000, 4.0)
    _settled_position(db, "BTC", 1300, -1.0)
    _settled_position(db, "ETH", 1000, -2.0)

    all_pnl = client.get("/api/pnl").json()["summary"]["total"]
    assert int(all_pnl["n"]) == 3
    assert abs(float(all_pnl["pnl"]) - 1.0) < 1e-9

    btc = client.get("/api/pnl", params={"asset": "btc"}).json()["summary"]["total"]
    assert int(btc["n"]) == 2
    assert abs(float(btc["pnl"]) - 3.0) < 1e-9
    assert int(btc["wins"]) == 1

    eth = client.get("/api/pnl", params={"asset": "ETH"}).json()["summary"]["total"]
    assert int(eth["n"]) == 1
    assert abs(float(eth["pnl"]) + 2.0) < 1e-9

    # Every table endpoint accepts the same filter.
    fills = client.get("/api/fills", params={"asset": "BTC"}).json()
    assert fills["pagination"]["total"] == 0  # no fills inserted in this test
    positions = client.get("/api/positions", params={"asset": "BTC"}).json()
    assert positions["pagination"]["total"] == 2
    db.close()
