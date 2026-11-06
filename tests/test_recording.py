import asyncio
import gzip
import json
import math
import time
from types import SimpleNamespace

import numpy as np
import pytest

from src.config import StrategyParams
from src.market.models import OrderBook, WindowMarket
from src.recording.recorder import Recorder
from src.recording.replay import (
    BookSeries,
    BookSnapshot,
    RtdsSeries,
    WindowData,
    scan_window,
    summarize,
)


def _synthetic_series(start: int, end: int) -> RtdsSeries:
    """A positive-drift random walk with enough history for sigma."""
    rng = np.random.default_rng(7)
    first = start - 700
    count = end - first + 1
    steps = rng.normal(0.0, 1e-4, count)
    logp = 0.00003 * np.arange(count) + np.cumsum(steps)
    points = [
        (
            float(first + i),
            float(first + i) + 0.1,
            float(100.0 * math.exp(logp[i])),
        )
        for i in range(count)
    ]
    return RtdsSeries("BTC", points)


def _synthetic_books(start: int, end: int) -> dict[str, BookSeries]:
    out: dict[str, BookSeries] = {}
    for token, bid, ask in (("up", 0.80, 0.82), ("down", 0.18, 0.20)):
        snaps = [
            BookSnapshot(
                ts=float(ts),
                updated_ts=float(ts) - 0.05,
                bids=((bid, 1000.0),),
                asks=((ask, 1000.0),),
                best_bid=bid,
                best_ask=ask,
                spread=ask - bid,
            )
            for ts in range(start, end + 1)
        ]
        out[token] = BookSeries(token, snaps)
    return out


def _params() -> StrategyParams:
    return StrategyParams(
        min_edge=0.05,
        model_buffer=0.05,
        min_fair=0.65,
        min_entry_price=0.55,
        max_position_usd=5.0,
        min_ask_shares=5.0,
        max_spread=0.03,
        max_stale_book_s=3.0,
        taker_fee_rate=0.07,
        entry_min_rel_s=630,
        entry_max_rel_s=870,
        sigma_lookback_s=600,
        min_sigma_samples=120,
    )


def test_rtds_series_replicates_price_buffer_drop_rule():
    points = [
        (100.0, 10.0, 100.0),
        (101.0, 11.0, 101.0),
        (100.5, 12.0, 99.0),  # out of order: PriceBuffer.add drops it
        (102.0, 13.0, 102.0),
    ]
    series = RtdsSeries("BTC", points)
    assert len(series) == 3
    assert series.point_ts.tolist() == [100.0, 101.0, 102.0]


def test_scan_window_reports_realized_edge_by_bucket():
    start, end = 1_000_000, 1_000_900
    series = _synthetic_series(start, end)
    books = _synthetic_books(start, end)
    window = WindowData(
        asset="BTC",
        start_ts=start,
        end_ts=end,
        up_token="up",
        down_token="down",
        p0=100.0,
        outcome="UP",
        twap_lookback=60,
    )

    rows = scan_window(window, series, books, {}, _params(), step=1)

    assert rows
    assert all(row.side == "UP" for row in rows)
    first = [row for row in rows if row.is_first]
    assert len(first) == 1
    assert first[0].bucket == "630-719"
    assert first[0].eff_price == pytest.approx(0.83033, abs=2e-4)
    assert first[0].realized_edge == pytest.approx(0.16967, abs=2e-4)
    assert first[0].pnl == pytest.approx(first[0].shares * first[0].realized_edge)

    bucket_summary = summarize(rows, first_only=False)
    by_key = {(row["asset"], row["bucket"]): row for row in bucket_summary}
    btc = by_key[("BTC", "630-719")]
    assert btc["n"] == 1
    assert btc["win_rate"] == 1.0
    assert btc["median_realized_edge"] == pytest.approx(0.16967, abs=2e-4)

    first_summary = summarize(rows, first_only=True)
    first_keys = [
        row
        for row in first_summary
        if row["asset"] == "BTC" and row["bucket"] != "ALL"
    ]
    assert len(first_keys) == 1
    assert first_keys[0]["bucket"] == "630-719"


def test_scan_window_fair_cap_reduces_model_edge():
    start, end = 3_000_000, 3_000_900
    series = _synthetic_series(start, end)
    books = _synthetic_books(start, end)
    window = WindowData(
        asset="BTC",
        start_ts=start,
        end_ts=end,
        up_token="up",
        down_token="down",
        p0=100.0,
        outcome="UP",
        twap_lookback=60,
    )

    rows = scan_window(
        window, series, books, {}, _params(), step=1, fair_cap=0.95
    )

    assert rows
    assert all(row.fair <= 0.9500001 for row in rows)
    first = [row for row in rows if row.is_first][0]
    assert first.model_edge == pytest.approx(0.95 - 0.830332, abs=2e-4)


def test_scan_window_falls_back_to_rtds_outcome():
    start, end = 2_000_000, 2_000_900
    series = _synthetic_series(start, end)
    books = _synthetic_books(start, end)
    window = WindowData(
        asset="BTC",
        start_ts=start,
        end_ts=end,
        up_token="up",
        down_token="down",
        p0=100.0,
        outcome=None,
        twap_lookback=60,
    )

    rows = scan_window(window, series, books, {}, _params(), step=1)

    assert rows
    assert rows[0].outcome_source == "rtds"
    assert rows[0].outcome == "UP"


async def test_recorder_writes_gzipped_streams(tmp_path):
    now = time.time()
    start = int(now // 900) * 900
    market = WindowMarket(
        asset="BTC",
        start_ts=start,
        end_ts=start + 900,
        slug="btc-updown-15m-test",
        up_token="up",
        down_token="down",
        price_to_beat=100.0,
    )

    async def fetch_window(asset, start_ts):
        return None

    windows = SimpleNamespace(current={"BTC": market}, fetch_window=fetch_window)

    up = OrderBook("up")
    up.apply_snapshot([{"price": 0.79, "size": 100}], [{"price": 0.81, "size": 100}], now)
    down = OrderBook("down")
    down.apply_snapshot([{"price": 0.19, "size": 100}], [{"price": 0.21, "size": 100}], now)
    books = SimpleNamespace(
        book=lambda token: {"up": up, "down": down}.get(token)
    )
    fake_buffer = SimpleNamespace(twap_ending_at=lambda end, window: 100.0)
    rtds = SimpleNamespace(buffer=lambda asset: fake_buffer)
    settings = SimpleNamespace(
        recording_enabled=True,
        recording_path=tmp_path,
        data_dir=str(tmp_path),
        recording_snapshot_hz=20.0,
        recording_top_levels=5,
        recording_raw_clob=True,
        recording_retention_days=30,
        recording_min_free_gb=0.0,
        window_sec=900,
        twap_window_s=60,
    )

    recorder = Recorder(settings, windows=windows, rtds=rtds, books=books)
    recorder.start()
    try:
        await asyncio.sleep(0.12)
        recorder.on_rtds(
            "BTC",
            {
                "topic": "crypto_prices_chainlink",
                "payload": {"timestamp": int(now * 1000), "value": 100.1},
            },
            now,
        )
        recorder.on_clob_event({"event_type": "last_trade_price"}, now)
        recorder.on_trade("up", 0.8, 10.0, now, now)
        await asyncio.sleep(0.12)
    finally:
        await recorder.stop()

    files = list(tmp_path.rglob("*.jsonl.gz"))
    assert files
    prefixes = {path.name.split("_")[0] for path in files}
    assert {"rtds", "book", "trade", "clob", "window"} <= prefixes

    def read(prefix: str) -> list[dict]:
        rows: list[dict] = []
        for path in files:
            if not path.name.startswith(f"{prefix}_"):
                continue
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                rows.extend(json.loads(line) for line in handle if line.strip())
        return rows

    rtds_rows = read("rtds")
    assert rtds_rows
    assert rtds_rows[0]["asset"] == "BTC"
    assert rtds_rows[0]["payload"]["topic"] == "crypto_prices_chainlink"

    book_rows = read("book")
    assert any(
        row["asset"] == "BTC"
        and row["token_id"] == "up"
        and row["bids"] == [[0.79, 100.0]]
        and row["asks"] == [[0.81, 100.0]]
        for row in book_rows
    )

    trade_rows = read("trade")
    assert trade_rows
    assert trade_rows[0]["asset"] == "BTC"
    assert trade_rows[0]["side"] == "UP"
    assert trade_rows[0]["price"] == 0.8

    window_rows = read("window")
    assert any(
        row["event"] == "discovered" and row["price_to_beat"] == 100.0
        for row in window_rows
    )
