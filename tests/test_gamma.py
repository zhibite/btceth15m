import json

import httpx

from src.market.gamma import GammaClient


def _event(slug: str) -> dict:
    return {
        "slug": slug,
        "volume": "1234.5",
        "eventMetadata": {"priceToBeat": "100.0", "finalPrice": "101.0"},
        "markets": [
            {
                "clobTokenIds": json.dumps(["up-token", "down-token"]),
                "conditionId": "0xabc",
                "orderPriceMinTickSize": "0.01",
                "cryptoMarketConfig": {"twapLookbackSeconds": 60},
            }
        ],
    }


async def test_15m_window_slug_and_duration():
    start = 1793957400
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["slug"] = request.url.params.get("slug")
        return httpx.Response(200, json=[_event(f"btc-updown-15m-{start}")])

    client = GammaClient(
        "https://gamma.example",
        slug_tag="updown-15m",
        window_sec=900,
        transport=httpx.MockTransport(handler),
    )
    try:
        market = await client.get_window("BTC", start)
    finally:
        await client.close()

    assert seen["slug"] == f"btc-updown-15m-{start}"
    assert market is not None
    assert market.start_ts == start
    assert market.end_ts == start + 900
    assert market.twap_lookback == 60
    assert market.price_to_beat == 100.0
    assert market.final_price == 101.0
    assert market.outcome == "UP"
