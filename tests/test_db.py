from src.market.models import PaperPosition, WindowMarket
from src.persistence.db import Database


def test_window_and_position_roundtrip(tmp_path):
    db = Database(tmp_path / "test.db")
    market = WindowMarket(
        asset="BTC",
        start_ts=1000,
        end_ts=1300,
        slug="btc-updown-5m-1000",
        condition_id="0xabc",
        up_token="up",
        down_token="down",
        price_to_beat=100.0,
    )
    db.upsert_window(market)
    rows = db.recent_windows(1)
    assert rows and rows[0]["asset"] == "BTC"

    pos = PaperPosition(
        id=0,
        asset="BTC",
        start_ts=1000,
        end_ts=1300,
        token_id="up",
        side="UP",
        size=10.0,
        avg_price=0.5,
        cost=5.1,
        fees=0.1,
        entry_ts=1001.0,
        price_to_beat=100.0,
    )
    pid = db.insert_position(pos)
    assert pid > 0
    assert len(db.open_positions()) == 1

    db.settle_position(pid, "UP", payout=10.0, pnl=4.9)
    assert db.open_positions() == []
    settled = db.recent_positions(1)[0]
    assert settled["status"] == "settled"
    assert abs(float(settled["pnl"]) - 4.9) < 1e-9
    total = db.total_realized()
    assert abs(float(total["pnl"]) - 4.9) < 1e-9
    assert int(total["wins"]) == 1
    db.close()


def test_settings_roundtrip(tmp_path):
    db = Database(tmp_path / "settings.db")
    assert db.get_setting("x") is None
    db.set_setting("x", "1")
    assert db.get_setting("x") == "1"
    db.close()
