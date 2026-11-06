"""5-minute window lifecycle.

The manager polls Gamma once per second:
  * at each 300s boundary it discovers the new window for every asset
  * while a window is missing priceToBeat / finalPrice it refreshes the event
  * it invokes the engine callback exactly once per newly discovered window
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Optional

from src.infra.logging import get_logger
from src.market.gamma import GammaClient
from src.market.models import WindowMarket

log = get_logger(__name__)

WindowCallback = Callable[[str, WindowMarket], Awaitable[None]]


class WindowManager:
    def __init__(
        self,
        gamma: GammaClient,
        assets: list[str],
        window_sec: int = 300,
        on_window: WindowCallback | None = None,
    ) -> None:
        self.gamma = gamma
        self.assets = [a.upper() for a in assets]
        self.window_sec = int(window_sec)
        self.on_window = on_window
        self.current: dict[str, WindowMarket] = {}
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_refresh: dict[str, float] = {}

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run(), name="window-manager")
        log.info("window_manager.started", assets=self.assets, window_s=self.window_sec)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def boundary(self, now: float | None = None) -> int:
        ts = now if now is not None else time.time()
        return int(ts // self.window_sec) * self.window_sec

    def current_window(self, asset: str) -> Optional[WindowMarket]:
        return self.current.get(asset.upper())

    async def fetch_window(self, asset: str, start_ts: int) -> Optional[WindowMarket]:
        return await self.gamma.get_window(asset, start_ts)

    async def _run(self) -> None:
        while self._running:
            now = time.time()
            start = self.boundary(now)
            for asset in self.assets:
                cur = self.current.get(asset)
                if cur is None or cur.start_ts != start:
                    market = await self.gamma.get_window(asset, start)
                    if market is None:
                        # Gamma can lag the boundary by a second or two; retry
                        # on the next tick without blocking the other assets.
                        continue
                    self.current[asset] = market
                    log.info(
                        "window.discovered",
                        asset=asset,
                        slug=market.slug,
                        p0=market.price_to_beat,
                        volume=market.volume,
                    )
                    if self.on_window is not None:
                        try:
                            await self.on_window(asset, market)
                        except Exception as exc:  # noqa: BLE001
                            log.exception(
                                "window.callback_failed", asset=asset, error=str(exc)
                            )
                    continue

                # Late metadata: priceToBeat / finalPrice may be null for a
                # few seconds after the boundary.
                missing = cur.price_to_beat is None or cur.final_price is None
                if missing and now - self._last_refresh.get(asset, 0.0) >= 2.0:
                    self._last_refresh[asset] = now
                    fresh = await self.gamma.get_window(asset, start)
                    if fresh is not None:
                        if cur.price_to_beat is None and fresh.price_to_beat is not None:
                            cur.price_to_beat = fresh.price_to_beat
                            cur.p0_source = "gamma"
                            log.info(
                                "window.p0_late",
                                asset=asset,
                                slug=cur.slug,
                                p0=cur.price_to_beat,
                            )
                        if fresh.final_price is not None:
                            cur.final_price = fresh.final_price
                            cur.outcome = fresh.outcome
                            cur.closed = fresh.closed
                        cur.volume = fresh.volume or cur.volume

            await asyncio.sleep(1.0)
