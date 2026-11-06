from src.execution.paper import PaperBroker


def test_preview_walks_ask_levels():
    preview = PaperBroker.preview_buy([(0.50, 10), (0.60, 10)], 8.0, 0.07)
    assert preview is not None
    assert abs(preview.shares - 15.0) < 1e-9
    assert abs(preview.notional - 8.0) < 1e-9
    assert abs(preview.avg_price - 8.0 / 15.0) < 1e-9
    expected_fee = 0.07 * (0.50 * 0.50 * 10 + 0.60 * 0.40 * 5)
    assert abs(preview.fee - expected_fee) < 1e-9
    assert preview.best_ask == 0.50


def test_preview_handles_empty_book():
    assert PaperBroker.preview_buy([], 10.0, 0.07) is None


def test_preview_skips_invalid_prices():
    preview = PaperBroker.preview_buy([(0.0, 10), (1.0, 10), (0.5, 10)], 2.5, 0.07)
    assert preview is not None
    assert abs(preview.shares - 5.0) < 1e-9
    assert preview.avg_price == 0.5
