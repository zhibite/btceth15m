"""Mode router: paper by default, live only when explicitly enabled.

The global ``PAPER_TRADING`` value is the default for assets whose mode is
``inherit``. An asset explicitly set to ``live`` needs ``LIVE_ENABLED=true``
plus working credentials; otherwise it falls back to paper and the dashboard
shows the effective mode.
"""
from __future__ import annotations

from typing import Optional

from src.config import AppSettings, RuntimeConfig
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
        runtime: RuntimeConfig | None = None,
    ) -> None:
        self.settings = settings
        self.paper = paper
        self.live = live
        self.runtime = runtime

    @property
    def default_mode(self) -> str:
        if self.settings.paper_trading:
            return "paper"
        return "live" if self.settings.live_enabled else "paper"

    def configured_mode_for(self, asset: str | None) -> str:
        """Mode saved on the config page (may be ``inherit``)."""
        if asset is not None and self.runtime is not None:
            return self.runtime.mode_for(asset)
        return "inherit"

    def mode_for(self, asset: str | None) -> str:
        """Effective execution mode for one asset."""
        mode = self.configured_mode_for(asset)
        if mode == "inherit":
            mode = self.default_mode
        if mode != "live":
            return "paper"
        if not self.settings.live_enabled:
            return "paper"
        if self.live is not None and not self.live.available:
            return "paper"
        return "live" if self.live is not None else "paper"

    @property
    def mode(self) -> str:
        """Summary for logs/status: paper, live or mixed."""
        assets = self.settings.assets_list
        if self.runtime is not None:
            enabled = self.runtime.enabled_assets(assets)
            if not enabled:
                return "paper"
            assets = enabled
        modes = {self.mode_for(asset) for asset in assets}
        if modes == {"live"}:
            return "live"
        if modes == {"paper"}:
            return "paper"
        return "mixed"

    def modes_by_asset(self) -> dict[str, dict[str, str]]:
        assets = self.settings.assets_list
        enabled = (
            self.runtime.enabled_assets(assets)
            if self.runtime is not None
            else list(assets)
        )
        return {
            asset: {
                "configured": self.configured_mode_for(asset),
                "effective": self.mode_for(asset),
                "enabled": asset in enabled,
            }
            for asset in assets
        }

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
        if self.mode_for(market.asset) == "paper":
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
