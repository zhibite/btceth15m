"""Polymarket CLOB market WebSocket: order books and last trades.

The feed subscribes to the union of the current window tokens for every
asset. A token-set change (new window) bumps a version and forces a clean
reconnect so no stale subscription lingers.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

import websockets

from src.infra.logging import get_logger
from src.market.models import OrderBook

log = get_logger(__name__)


class ClobWsFeed:
    def __init__(
        self,
        assets: list[str],
        url: str,
        reconnect_base_s: float = 1.0,
        reconnect_max_s: float = 30.0,
    ) -> None:
        self.assets = [a.upper() for a in assets]
        self.url = url
        self.reconnect_base_s = float(reconnect_base_s)
        self.reconnect_max_s = float(reconnect_max_s)
        self.books: dict[str, OrderBook] = {}
        self.last_trade: dict[str, tuple[float, float, float]] = {}
        self._tokens: dict[str, list[str]] = {a: [] for a in self.assets}
        self._active_tokens: set[str] = set()
        self._version = 0
        self._running = False
        self._task: asyncio.Task | None = None
        self.connected = False
        self.last_error = ""
        self.last_msg_ts = 0.0

    def set_tokens(self, asset: str, tokens: list[str]) -> None:
        asset = asset.upper()
        clean = [str(t) for t in tokens if t]
        if self._tokens.get(asset) == clean:
            return
        self._tokens[asset] = clean
        active = {t for ts in self._tokens.values() for t in ts}
        self._active_tokens = active
        for token in clean:
            self.books.setdefault(token, OrderBook(token))
        # Tokens from expired windows must not accumulate forever. Keeping only
        # the currently subscribed set also stops in-flight messages for old
        # tokens from re-adding them after the prune.
        for token in list(self.books):
            if token not in active:
                self.books.pop(token, None)
                self.last_trade.pop(token, None)
        self._version += 1
        log.info(
            "clob.tokens",
            asset=asset,
            tokens=[t[:10] for t in clean],
            active=len(active),
        )

    def book(self, token_id: str) -> Optional[OrderBook]:
        return self.books.get(str(token_id))

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run(), name="clob-ws")
        log.info("clob.started", url=self.url)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def health(self) -> dict:
        return {
            "connected": self.connected,
            "last_error": self.last_error,
            "last_msg_ts": self.last_msg_ts,
            "tokens": {a: len(t) for a, t in self._tokens.items()},
            "active_tokens": len(self._active_tokens),
        }

    async def _run(self) -> None:
        delay = self.reconnect_base_s
        while self._running:
            version = self._version
            tokens = sorted({t for ts in self._tokens.values() for t in ts})
            if not tokens:
                await asyncio.sleep(0.5)
                continue
            try:
                async with websockets.connect(
                    self.url,
                    ping_interval=None,
                    open_timeout=15,
                    close_timeout=5,
                ) as ws:
                    await ws.send(json.dumps({"assets_ids": tokens, "type": "market"}))
                    self.connected = True
                    self.last_error = ""
                    delay = self.reconnect_base_s
                    log.info("clob.connected", tokens=len(tokens))
                    while self._running and self._version == version:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=10.0)
                        except asyncio.TimeoutError:
                            continue
                        self._handle(msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.connected = False
                self.last_error = repr(exc)
                log.warning("clob.reconnect", error=repr(exc), delay_s=delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, self.reconnect_max_s)

    def _handle(self, msg) -> None:
        try:
            data = json.loads(msg)
        except (TypeError, ValueError):
            return
        self.last_msg_ts = time.time()
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            event_type = item.get("event_type")
            if event_type == "last_trade_price" or (
                event_type is None
                and "price" in item
                and "size" in item
                and "asset_id" in item
            ):
                self._on_trade(item)
                continue
            if item.get("asset_id") and "bids" in item and "asks" in item:
                self._on_snapshot(item)
                continue
            for change in item.get("price_changes") or []:
                self._on_change(change)

    def _on_snapshot(self, item: dict) -> None:
        token = str(item.get("asset_id") or "")
        if not token or token not in self._active_tokens:
            return
        book = self.books.setdefault(token, OrderBook(token))
        book.apply_snapshot(item.get("bids"), item.get("asks"), time.time())

    def _on_change(self, change: dict) -> None:
        token = str(change.get("asset_id") or "")
        if not token or token not in self._active_tokens:
            return
        book = self.books.setdefault(token, OrderBook(token))
        try:
            price = float(change.get("price"))
            size = float(change.get("size") or 0.0)
        except (TypeError, ValueError):
            return
        book.apply_price_change(price, size, str(change.get("side") or ""), time.time())

    def _on_trade(self, item: dict) -> None:
        token = str(item.get("asset_id") or "")
        if not token or token not in self._active_tokens:
            return
        try:
            price = float(item.get("price"))
            size = float(item.get("size") or 0.0)
        except (TypeError, ValueError):
            return
        self.last_trade[token] = (price, size, time.time())
