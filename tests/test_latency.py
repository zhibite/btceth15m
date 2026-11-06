import time

from src.infra.latency import LatencyTracker
from src.persistence.db import Database


def test_latency_buckets_and_decisions(tmp_path):
    db = Database(tmp_path / "latency.db")
    tracker = LatencyTracker(db)
    now = time.time()
    base = int(now // 60 * 60) - 120  # minute-aligned, two minutes ago

    for i in range(60):
        recv = float(base + i)
        tracker.record_rtds(
            "BTC",
            "crypto_prices_chainlink",
            data_ts_ms=recv * 1000.0 - 2000.0,
            publish_ts_ms=recv * 1000.0 - 1000.0,
            recv_ts=recv,
        )
    tracker.flush(now=now)

    rows = db.recent_latency_buckets(24)
    assert len(rows) == 1
    assert int(rows[0]["n"]) == 60
    assert abs(float(rows[0]["internal_p50"]) - 1000.0) < 1e-6
    assert abs(float(rows[0]["client_p50"]) - 1000.0) < 1e-6
    assert abs(float(rows[0]["e2e_p50"]) - 2000.0) < 1e-6

    tracker.record_decision(
        ts=now,
        asset="BTC",
        window_start=base,
        data_age_ms=120.0,
        book_age_ms=40.0,
        fill_ms=2.0,
        note="test",
    )
    events = db.recent_latency_events(10)
    assert len(events) == 1

    summary = tracker.summary(window_s=900, now=now)
    key = summary["keys"]["BTC"]["crypto_prices_chainlink"]
    assert key["n"] == 60
    assert abs(key["internal"]["p50"] - 1000.0) < 1e-6

    decision = tracker.decision_summary(window_s=3600, now=now)
    assert decision["n"] == 1
    assert decision["data_age_ms"]["p50"] == 120.0
    db.close()
