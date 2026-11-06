"""Mode router: paper by default, live only when explicitly enabled."""
from __future__ import annotations

from typing import Optional

from src.config import AppSettings
from src.execution.live import LiveBroker
from src.execution.paper import PaperBroker
from src.infra.logging import get_logger
from src.market.models import PaperPosition, WindowMarket

log = get_logger(__name__)


class BrokerRouter:
    def __init__(
        self,
        settings: AppSettings,
        paper: PaperBroker,
        live: Optional[LiveBroker] = None,
    ) -> None:
        self.settings = settings
        self.paper = paper
        self.live = live

    @property
    def mode(self) -> str:
        return "paper" if self.settings.paper_trading else "live"

    async def buy(
        self,
        *,
        market: WindowMarket,
        side: str,
        token_id: str,
        notional: float,
        asks: list[tuple[float, float]],
        price_to_beat: float,
        reason: str = "",
    ) -> Optional[PaperPosition]:
        if self.settings.paper_trading:
            return self.paper.buy(
                market=market,
                side=side,
                token_id=token_id,
                notional=notional,
                asks=asks,
                price_to_beat=price_to_beat,
                reason=reason,
            )
        if self.live is None:
            raise RuntimeError("live broker is not configured")
        return await self.live.buy(
            market=market,
            side=side,
            token_id=token_id,
            notional=notional,
            asks=asks,
            price_to_beat=price_to_beat,
            reason=reason,
        )
