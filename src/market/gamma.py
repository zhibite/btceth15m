"""Gamma REST client for Up/Down window metadata (15m by default)."""
from __future__ import annotations

import json
from typing import Optional

import httpx

from src.infra.logging import get_logger
from src.market.models import WindowMarket

log = get_logger(__name__)


def _f(value) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


class GammaClient:
    def __init__(
        self,
        base_url: str,
        *,
        slug_tag: str = "updown-15m",
        window_sec: int = 900,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.slug_tag = str(slug_tag)
        self.window_sec = int(window_sec)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(5.0),
            headers={"User-Agent": "polymarket-paper/0.2"},
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    def slug_for(self, asset: str, start_ts: int) -> str:
        return f"{asset.lower()}-{self.slug_tag}-{int(start_ts)}"

    async def get_window(self, asset: str, start_ts: int) -> Optional[WindowMarket]:
        asset = asset.upper()
        start_ts = int(start_ts)
        slug = self.slug_for(asset, start_ts)
        try:
            resp = await self._client.get(f"{self.base_url}/events", params={"slug": slug})
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            log.debug("gamma.fetch_failed", slug=slug, error=str(exc))
            return None
        if not data:
            return None
        ev = data[0]
        markets = ev.get("markets") or []
        if not markets:
            return None
        m = markets[0]
        try:
            tokens = json.loads(m.get("clobTokenIds") or "[]")
        except (TypeError, ValueError):
            tokens = []
        if len(tokens) < 2:
            return None

        md = ev.get("eventMetadata") or {}
        p0 = _f(md.get("priceToBeat"))
        final = _f(md.get("finalPrice"))
        cfg = m.get("cryptoMarketConfig") or {}
        outcome = None
        if p0 is not None and final is not None:
            outcome = "UP" if final >= p0 else "DOWN"
        else:
            try:
                prices = m.get("outcomePrices")
                op = json.loads(prices) if isinstance(prices, str) else prices
                if op and len(op) == 2:
                    up = float(op[0])
                    if up >= 0.999:
                        outcome = "UP"
                    elif up <= 0.001:
                        outcome = "DOWN"
            except (TypeError, ValueError):
                pass

        return WindowMarket(
            asset=asset,
            start_ts=start_ts,
            end_ts=start_ts + self.window_sec,
            slug=slug,
            condition_id=str(m.get("conditionId") or ""),
            up_token=str(tokens[0]),
            down_token=str(tokens[1]),
            price_to_beat=p0,
            final_price=final,
            outcome=outcome,
            volume=_f(ev.get("volume")) or 0.0,
            tick_size=_f(m.get("orderPriceMinTickSize")) or 0.01,
            twap_lookback=int(cfg.get("twapLookbackSeconds") or 60),
            closed=bool(ev.get("closed") or m.get("closed")),
            p0_source="gamma" if p0 is not None else "pending",
        )
