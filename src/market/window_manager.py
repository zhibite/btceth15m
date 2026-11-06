"""Market window lifecycle (15m by default).

The manager polls Gamma once per second:
  * at each window boundary it discovers the new window for every asset
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
        window_sec: int = 900,
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
        # One missing-market retry every few seconds instead of one request per
        # second per asset: with seven assets Gamma can otherwise see a
        # constant 7 req/s stream when one market is not listed yet.
        self._last_discovery: dict[str, float] = {}
        self._missing_delay: dict[str, float] = {}
        self._discovery_attempts: dict[str, tuple[int, int]] = {}
        self._missing_retry_s = 3.0
        self._missing_retry_max_s = 60.0
        # priceToBeat usually arrives with the event; if it does not, poll
        # aggressively for a few seconds and then back off to 30s.
        self._refresh_delay: dict[str, float] = {}
        self._refresh_base_s = 2.0
        self._refresh_max_s = 30.0

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
            await self._discover(now, start)
            await self._refresh_late_metadata(now, start)
            await asyncio.sleep(1.0)

    async def _discover(self, now: float, start: int) -> None:
        pending: list[tuple[str, asyncio.Task, float]] = []
        for asset in self.assets:
            cur = self.current.get(asset)
            if cur is not None and cur.start_ts == start:
                continue
            # A market that has never appeared (wrong slug / not listed) backs
            # off exponentially. A market that merely lags the boundary gets a
            # few 1s retries, then also backs off, so a market that stopped
            # being listed cannot cost 1 req/s forever.
            attempts = 0
            previous = self._discovery_attempts.get(asset)
            if previous is not None and previous[0] == start:
                attempts = previous[1]
            delay = (
                self._missing_delay.get(asset, self._missing_retry_s)
                if cur is None
                else 0.0
            )
            if attempts >= 3:
                delay = max(
                    delay,
                    min(2.0 ** (attempts - 2), self._missing_retry_max_s),
                )
            last = self._last_discovery.get(asset, 0.0)
            if now - last < delay:
                continue
            self._last_discovery[asset] = now
            self._discovery_attempts[asset] = (start, attempts + 1)
            pending.append(
                (
                    asset,
                    asyncio.create_task(self.gamma.get_window(asset, start)),
                    delay,
                )
            )
        if not pending:
            return
        # Fetch concurrently: seven serial 5s timeouts would delay the whole
        # universe at a boundary.
        results = await asyncio.gather(
            *(task for _, task, _ in pending), return_exceptions=True
        )
        for (asset, _task, delay), market in zip(pending, results):
            if isinstance(market, BaseException) or market is None:
                if delay > 0:
                    self._missing_delay[asset] = min(
                        max(delay * 2.0, self._missing_retry_s),
                        self._missing_retry_max_s,
                    )
                continue
            self._missing_delay.pop(asset, None)
            self._discovery_attempts.pop(asset, None)
            self.current[asset] = market
            # Do not let a late refresh from the previous window throttle the
            # first priceToBeat fetch of this one.
            self._last_refresh.pop(asset, None)
            self._refresh_delay.pop(asset, None)
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

    async def _refresh_late_metadata(self, now: float, start: int) -> None:
        # priceToBeat may be null for a few seconds after the boundary while
        # Gamma catches up. finalPrice only exists once the window has ended
        # and the engine records it during settlement, so polling for the whole
        # window would burn a Gamma request every 2s for nothing.
        pending: list[tuple[str, asyncio.Task, float]] = []
        for asset in self.assets:
            cur = self.current.get(asset)
            if cur is None or cur.start_ts != start:
                continue
            if cur.price_to_beat is not None:
                self._refresh_delay.pop(asset, None)
                continue
            delay = self._refresh_delay.get(asset, self._refresh_base_s)
            if now - self._last_refresh.get(asset, 0.0) < delay:
                continue
            self._last_refresh[asset] = now
            pending.append(
                (
                    asset,
                    asyncio.create_task(self.gamma.get_window(asset, start)),
                    delay,
                )
            )
        if not pending:
            return
        results = await asyncio.gather(
            *(task for _, task, _ in pending), return_exceptions=True
        )
        for (asset, _task, delay), fresh in zip(pending, results):
            cur = self.current.get(asset)
            if cur is None:
                continue
            if isinstance(fresh, BaseException) or fresh is None:
                self._refresh_delay[asset] = min(
                    max(delay * 2.0, self._refresh_base_s), self._refresh_max_s
                )
                continue
            if fresh.price_to_beat is not None:
                cur.price_to_beat = fresh.price_to_beat
                cur.p0_source = "gamma"
                self._refresh_delay.pop(asset, None)
                log.info(
                    "window.p0_late",
                    asset=asset,
                    slug=cur.slug,
                    p0=cur.price_to_beat,
                )
            else:
                self._refresh_delay[asset] = min(
                    max(delay * 2.0, self._refresh_base_s), self._refresh_max_s
                )
            if fresh.final_price is not None:
                cur.final_price = fresh.final_price
                cur.outcome = fresh.outcome
                cur.closed = fresh.closed
            cur.volume = fresh.volume or cur.volume
