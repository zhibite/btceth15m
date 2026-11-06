import math

from src.feeds.rtds import PriceBuffer


def test_sigma_normalizes_returns_by_sample_interval():
    """A 2s gap must not double the per-second volatility estimate."""
    buf = PriceBuffer(retention_s=3600.0)
    a = 1e-4
    log_price = 0.0
    base_ts = 1000.0
    for i in range(40):
        buf.add(base_ts + 2.0 * i, math.exp(log_price))
        # Alternate +/-2a every 2s => per-second vol is a*sqrt(2).
        log_price += 2.0 * a * (1.0 if i % 2 == 0 else -1.0)

    sigma = buf.sigma(lookback_s=1000.0, min_samples=30)

    assert sigma is not None
    expected = a * math.sqrt(2.0)
    assert abs(sigma - expected) < 0.05 * expected
