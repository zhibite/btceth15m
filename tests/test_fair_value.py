import math

from src.strategy.fair_value import fair_probability, norm_cdf, per_share_fee


def test_norm_cdf_symmetry():
    assert abs(norm_cdf(0.0) - 0.5) < 1e-12
    assert abs(norm_cdf(1.0) + norm_cdf(-1.0) - 1.0) < 1e-12


def test_fee_matches_official_examples():
    # 100 shares at 0.50 -> 1.75 USDC
    assert abs(per_share_fee(0.07, 0.50) * 100 - 1.75) < 1e-12
    # 100 shares at 0.75 -> 1.3125 USDC
    assert abs(per_share_fee(0.07, 0.75) * 100 - 1.3125) < 1e-12
    # 100 shares at 0.90 -> 0.63 USDC
    assert abs(per_share_fee(0.07, 0.90) * 100 - 0.63) < 1e-12


def test_fair_value_before_settlement_window():
    result = fair_probability(
        p0=100.0,
        spot=100.05,
        sigma=1e-4,
        now=940.0,
        end_ts=1000.0,
        twap_window=60.0,
        mean_between=lambda a, b: 100.05,
    )
    assert result is not None
    assert 0.80 < result["fair"] < 0.95


def test_fair_value_sharpens_inside_settlement_window():
    result = fair_probability(
        p0=100.0,
        spot=100.05,
        sigma=1e-4,
        now=970.0,
        end_ts=1000.0,
        twap_window=60.0,
        mean_between=lambda a, b: 100.05,
    )
    assert result is not None
    assert result["fair"] > 0.95
    assert result["remaining_s"] == 30.0


def test_expired_returns_none():
    assert (
        fair_probability(
            p0=100.0,
            spot=100.05,
            sigma=1e-4,
            now=1000.0,
            end_ts=1000.0,
            twap_window=60.0,
            mean_between=lambda a, b: 100.0,
        )
        is None
    )


def test_missing_observation_returns_none():
    assert (
        fair_probability(
            p0=100.0,
            spot=100.05,
            sigma=1e-4,
            now=970.0,
            end_ts=1000.0,
            twap_window=60.0,
            mean_between=lambda a, b: None,
        )
        is None
    )


def test_flat_price_is_coin_flip():
    result = fair_probability(
        p0=100.0,
        spot=100.0,
        sigma=1e-4,
        now=940.0,
        end_ts=1000.0,
        twap_window=60.0,
        mean_between=lambda a, b: 100.0,
    )
    assert result is not None
    assert abs(result["fair"] - 0.5) < 1e-12
