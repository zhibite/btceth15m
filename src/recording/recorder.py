"""Record live feed data for offline book replay.

The bot already keeps everything needed for a replay in memory, but throws it
away when the process exits.  This recorder taps the same feeds and writes
hourly, gzipped newline-delimited JSON under ``data/recordings``:

    rtds_BTC_YYYYMMDD_HH.jsonl.gz     raw RTDS Chainlink prints
    book_BTC_YYYYMMDD_HH.jsonl.gz     1 Hz top-N book snapshots per token
    trade_BTC_YYYYMMDD_HH.jsonl.gz    every CLOB last_trade_price event
    clob_ALL_YYYYMMDD_HH.jsonl.gz     optional raw CLOB messages
    window_BTC_YYYYMMDD_HH.jsonl.gz   window metadata + official outcome

``book`` snapshots include the token -> (asset, window, side) mapping, so a
replay does not need the database.  ``window`` records are written on discovery
and again once Gamma publishes ``finalPrice``; the latter also contains the
locally computed 60s TWAP as a cross-check.

The hot-path callbacks only enqueue a dict.  Serialization and gzip happen in
background writer tasks, so a slow disk or a full queue cannot stall the
strategy.  Queues are bounded and per-stream: dropping raw CLOB messages never
drops the much more important RTDS/book stream.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import shutil
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from src.infra.logging import get_logger

log = get_logger(__name__)

_STREAMS = ("rtds", "book", "trade", "clob", "window")
_QUEUE_MAX = {
    "rtds": 50_000,
    "book": 50_000,
    "trade": 50_000,
    "clob": 200_000,
    "window": 5_000,
}
# Label retries after a window ends.  Gamma usually has finalPrice within a few
# seconds; the later retries cover slow / overloaded periods.
_LABEL_RETRY_S = (5.0, 20.0, 60.0, 180.0, 600.0)
_LABEL_GIVE_UP_S = 900.0


class Recorder:
    """Tee live feed events to hourly gzipped JSONL files."""

    def __init__(self, settings, *, windows, rtds, books) -> None:
        self.settings = settings
        self.windows = windows
        self.rtds = rtds
        self.books = books

        self.enabled = bool(getattr(settings, "recording_enabled", False))
        self.dir = Path(
            getattr(settings, "recording_path", None)
            or Path(settings.data_dir) / "recordings"
        )
        self.snapshot_hz = max(0.2, float(getattr(settings, "recording_snapshot_hz", 1.0)))
        self.top_levels = max(1, int(getattr(settings, "recording_top_levels", 10)))
        self.raw_clob = bool(getattr(settings, "recording_raw_clob", False))
        self.retention_days = max(1, int(getattr(settings, "recording_retention_days", 30)))
        self.min_free_gb = max(0.0, float(getattr(settings, "recording_min_free_gb", 2.0)))

        self._queues: dict[str, asyncio.Queue] = {
            name: asyncio.Queue(maxsize=_QUEUE_MAX[name]) for name in _STREAMS
        }
        self._writers: list[asyncio.Task] = []
        self._snapshot_task: Optional[asyncio.Task] = None
        self._label_task: Optional[asyncio.Task] = None
        self._running = False

        self._dropped: dict[str, int] = defaultdict(int)
        self._bytes = 0
        self._disk_low = False
        self._free_gb = 0.0
        self._last_disk_check = 0.0
        self._last_disk_log = 0.0
        self._last_cleanup = 0.0

        # token id -> (asset, window start_ts, UP/DOWN)
        self._token_map: dict[str, tuple[str, int, str]] = {}
        # (asset, start_ts) -> last window record we wrote
        self._window_meta: dict[tuple[str, int], dict[str, Any]] = {}
        self._window_seen: set[tuple[str, int]] = set()
        self._label_retry: dict[tuple[str, int], int] = {}
        self._labeled: set[tuple[str, int]] = set()

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        if not self.enabled or self._running:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # Recording must never prevent the trading bot from starting.
            log.error("recorder.dir_unavailable", dir=str(self.dir), error=str(exc))
            self.enabled = False
            return
        self._running = True
        self._writers = [
            asyncio.create_task(self._writer(name), name=f"recorder-{name}")
            for name in _STREAMS
        ]
        self._snapshot_task = asyncio.create_task(
            self._snapshot_loop(), name="recorder-snapshot"
        )
        self._label_task = asyncio.create_task(
            self._label_loop(), name="recorder-labels"
        )
        log.info(
            "recorder.started",
            dir=str(self.dir),
            snapshot_hz=self.snapshot_hz,
            top_levels=self.top_levels,
            raw_clob=self.raw_clob,
            retention_days=self.retention_days,
        )

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        for task in (self._snapshot_task, self._label_task):
            if task is not None:
                task.cancel()
        for task in (self._snapshot_task, self._label_task):
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        # Flush what is already queued before closing the files.
        for queue in self._queues.values():
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                try:
                    await asyncio.wait_for(queue.put(None), timeout=5.0)
                except asyncio.TimeoutError:
                    log.warning("recorder.stop_queue_full")
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._writers, return_exceptions=True),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            log.warning("recorder.stop_timeout")
        self._writers.clear()
        log.info(
            "recorder.stopped",
            bytes=self._bytes,
            dropped={k: v for k, v in self._dropped.items() if v},
        )

    # ── feed hooks (must stay non-blocking) ──────────────────────────────
    def on_rtds(self, asset: str, payload: Any, recv_ts: float) -> None:
        if not self.enabled:
            return
        self._put(
            "rtds",
            {"recv_ts": recv_ts, "asset": str(asset).upper(), "payload": payload},
        )

    def on_clob_event(self, payload: Any, recv_ts: float) -> None:
        if not self.enabled or not self.raw_clob:
            return
        self._put("clob", {"recv_ts": recv_ts, "payload": payload})

    def on_trade(
        self,
        token_id: str,
        price: float,
        size: float,
        exchange_ts: Optional[float],
        recv_ts: float,
    ) -> None:
        if not self.enabled:
            return
        token = str(token_id)
        asset, start_ts, side = self._token_map.get(token, ("", None, ""))
        self._put(
            "trade",
            {
                "recv_ts": recv_ts,
                # Some trade messages omit the exchange timestamp; the local
                # receive time is a usable fallback for the last-trade proxy.
                "ts": exchange_ts if exchange_ts is not None else recv_ts,
                "token_id": token,
                "asset": asset,
                "start_ts": start_ts,
                "side": side,
                "price": float(price),
                "size": float(size),
            },
        )

    # ── snapshot loop ────────────────────────────────────────────────────
    async def _snapshot_loop(self) -> None:
        interval = 1.0 / self.snapshot_hz
        while self._running:
            started = time.time()
            try:
                self._snapshot_once(started)
                self._check_disk(started)
                self._maybe_cleanup(started)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("recorder.snapshot_failed", error=str(exc))
            await asyncio.sleep(max(0.0, interval - (time.time() - started)))

    def _snapshot_once(self, now: float) -> None:
        window_sec = int(getattr(self.settings, "window_sec", 900))
        for asset, market in list(self.windows.current.items()):
            if not market.up_token or not market.down_token:
                continue
            key = (str(asset).upper(), int(market.start_ts))
            if key not in self._window_seen:
                self._window_seen.add(key)
                self._record_window(now, "discovered", market)
            else:
                meta = self._window_meta.get(key)
                if (
                    meta is not None
                    and market.price_to_beat is not None
                    and meta.get("price_to_beat") != market.price_to_beat
                ):
                    self._record_window(now, "p0", market)

            for side, token in (("UP", market.up_token), ("DOWN", market.down_token)):
                if not token:
                    continue
                self._token_map[str(token)] = (key[0], key[1], side)
                self._record_book(now, market, side, str(token))

            # The previous window just ended; queue it for the official label.
            previous = (key[0], key[1] - window_sec)
            if previous not in self._labeled and previous not in self._label_retry:
                self._label_retry[previous] = 0

    def _record_book(self, now: float, market, side: str, token: str) -> None:
        book = self.books.book(token) if self.books is not None else None
        present = bool(book is not None and (book.bids or book.asks))
        bids: list[list[float]] = []
        asks: list[list[float]] = []
        best_bid = best_ask = spread = None
        updated_ts = None
        age_s = None
        if present:
            bid_levels = book.bids_descending()[: self.top_levels]
            ask_levels = book.asks_ascending()[: self.top_levels]
            bids = [[round(float(p), 6), round(float(s), 4)] for p, s in bid_levels]
            asks = [[round(float(p), 6), round(float(s), 4)] for p, s in ask_levels]
            best_bid = book.best_bid()
            best_ask = book.best_ask()
            spread = book.spread()
            updated_ts = float(book.updated_ts or 0.0) or None
            if updated_ts is not None:
                age_s = max(0.0, now - updated_ts)
        self._put(
            "book",
            {
                "recv_ts": now,
                "ts": now,
                "asset": str(market.asset).upper(),
                "start_ts": int(market.start_ts),
                "slug": market.slug,
                "token_id": token,
                "side": side,
                "present": present,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": spread,
                "bids": bids,
                "asks": asks,
                "updated_ts": updated_ts,
                "age_s": age_s,
            },
        )

    def _record_window(
        self,
        now: float,
        event: str,
        market,
        *,
        rtds_twap: Optional[float] = None,
    ) -> None:
        record = {
            "recv_ts": now,
            "ts": now,
            "event": event,
            **market.to_dict(),
        }
        if rtds_twap is not None:
            record["rtds_twap"] = rtds_twap
            if market.price_to_beat is not None:
                record["rtds_outcome"] = (
                    "UP" if rtds_twap >= market.price_to_beat else "DOWN"
                )
        key = (str(market.asset).upper(), int(market.start_ts))
        self._window_meta[key] = record
        self._put("window", record)

    # ── official labels ──────────────────────────────────────────────────
    async def _label_loop(self) -> None:
        while self._running:
            now = time.time()
            for key in list(self._label_retry):
                asset, start_ts = key
                end_ts = start_ts + int(getattr(self.settings, "window_sec", 900))
                if now < end_ts:
                    continue
                retry = self._label_retry.get(key, 0)
                if retry >= len(_LABEL_RETRY_S) or now > end_ts + _LABEL_GIVE_UP_S:
                    self._label_retry.pop(key, None)
                    self._labeled.add(key)
                    self._record_label_timeout(now, key)
                    continue
                if now < end_ts + _LABEL_RETRY_S[retry]:
                    continue
                self._label_retry[key] = retry + 1
                try:
                    market = await self.windows.fetch_window(asset, start_ts)
                except Exception as exc:  # noqa: BLE001
                    log.debug("recorder.label_fetch_failed", asset=asset, error=str(exc))
                    continue
                if market is None:
                    continue
                if market.price_to_beat is not None and market.final_price is not None:
                    rtds_twap = self._rtds_twap(asset, end_ts)
                    self._record_window(now, "labeled", market, rtds_twap=rtds_twap)
                    self._label_retry.pop(key, None)
                    self._labeled.add(key)
                elif market.price_to_beat is not None:
                    meta = self._window_meta.get(key)
                    if meta is None or meta.get("price_to_beat") is None:
                        self._record_window(now, "p0", market)
            await asyncio.sleep(2.0)

    def _rtds_twap(self, asset: str, end_ts: float) -> Optional[float]:
        if self.rtds is None:
            return None
        buffer = self.rtds.buffer(asset)
        if buffer is None:
            return None
        window = float(getattr(self.settings, "twap_window_s", 60) or 60)
        return buffer.twap_ending_at(end_ts, window)

    def _record_label_timeout(self, now: float, key: tuple[str, int]) -> None:
        meta = self._window_meta.get(key)
        if meta is None:
            return
        record = dict(meta)
        record.update({"recv_ts": now, "ts": now, "event": "label_timeout"})
        self._window_meta[key] = record
        self._put("window", record)

    # ── disk / retention ─────────────────────────────────────────────────
    def _check_disk(self, now: float) -> None:
        if now - self._last_disk_check < 30.0:
            return
        self._last_disk_check = now
        try:
            self._free_gb = shutil.disk_usage(self.dir).free / 1e9
        except OSError:
            self._free_gb = 0.0
            return
        if self.min_free_gb <= 0:
            self._disk_low = False
            return
        was_low = self._disk_low
        self._disk_low = self._free_gb < self.min_free_gb
        if self._disk_low and (not was_low or now - self._last_disk_log > 300.0):
            self._last_disk_log = now
            log.warning(
                "recorder.disk_low",
                free_gb=round(self._free_gb, 2),
                min_free_gb=self.min_free_gb,
                note="recording paused until free space recovers",
            )

    def _maybe_cleanup(self, now: float) -> None:
        if now - self._last_cleanup < 3600.0:
            return
        self._last_cleanup = now
        keep_days = max(1, self.retention_days)
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=keep_days - 1)
        try:
            day_dirs = list(self.dir.iterdir())
        except OSError:
            return
        for path in day_dirs:
            if not path.is_dir():
                continue
            try:
                day = datetime.strptime(path.name, "%Y%m%d").date()
            except ValueError:
                continue
            if day >= cutoff:
                continue
            try:
                shutil.rmtree(path)
                log.info("recorder.retention_removed", path=str(path))
            except OSError as exc:
                log.warning("recorder.retention_failed", path=str(path), error=str(exc))

    # ── queue + writer ───────────────────────────────────────────────────
    def _put(self, stream: str, record: dict[str, Any]) -> None:
        if not self._running or self._disk_low:
            if self._running:
                self._dropped[stream] += 1
            return
        try:
            self._queues[stream].put_nowait(record)
        except asyncio.QueueFull:
            self._dropped[stream] += 1

    async def _writer(self, stream: str) -> None:
        handles: dict[Path, gzip.GzipFile] = {}
        state: dict[str, Optional[str]] = {"hour": None}
        try:
            while True:
                record = await self._queues[stream].get()
                if record is None:
                    break
                batch = [record]
                stop = False
                while len(batch) < 500:
                    try:
                        nxt = self._queues[stream].get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if nxt is None:
                        stop = True
                        break
                    batch.append(nxt)
                await asyncio.to_thread(
                    self._write_batch, stream, handles, state, batch
                )
                if stop:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("recorder.writer_failed", stream=stream, error=str(exc))
        finally:
            await asyncio.to_thread(self._close_handles, handles)

    def _write_batch(
        self,
        stream: str,
        handles: dict[Path, gzip.GzipFile],
        state: dict[str, Optional[str]],
        batch: list[dict[str, Any]],
    ) -> None:
        for record in batch:
            if not isinstance(record, dict):
                continue
            # Group window records by the window's start time, not by when the
            # label arrived: a finalPrice written after the boundary must land
            # in the same day file as the window it describes.
            ts_value = record.get("recv_ts") or record.get("ts") or time.time()
            if stream == "window" and record.get("start_ts"):
                ts_value = record["start_ts"]
            ts = float(ts_value)
            moment = datetime.fromtimestamp(ts, timezone.utc)
            day = moment.strftime("%Y%m%d")
            hour = moment.strftime("%Y%m%d_%H")
            if state["hour"] != hour:
                # Close the previous hour's per-asset handles.  Within one
                # hour we keep one handle per asset open (seven at most for
                # the 15m universe), so interleaved assets do not thrash.
                for old_handle in handles.values():
                    try:
                        old_handle.close()
                    except OSError:
                        pass
                handles.clear()
                state["hour"] = hour
            asset = str(record.get("asset") or "all").upper()
            path = self.dir / day / f"{stream}_{asset}_{hour}.jsonl.gz"
            handle = handles.get(path)
            if handle is None:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    handle = gzip.open(path, "at", compresslevel=3)
                except OSError as exc:
                    log.warning(
                        "recorder.open_failed",
                        path=str(path),
                        error=str(exc),
                        stream=stream,
                    )
                    continue
                handles[path] = handle
            line = json.dumps(record, separators=(",", ":"), default=str) + "\n"
            try:
                handle.write(line)
                self._bytes += len(line)
            except OSError as exc:
                log.warning(
                    "recorder.write_failed",
                    path=str(path),
                    error=str(exc),
                    stream=stream,
                )
        for handle in handles.values():
            try:
                handle.flush()
            except OSError:
                pass

    @staticmethod
    def _close_handles(handles: dict[Path, gzip.GzipFile]) -> None:
        for handle in handles.values():
            try:
                handle.close()
            except OSError:
                pass
        handles.clear()

    # ── status ───────────────────────────────────────────────────────────
    def health(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "running": self._running,
            "dir": str(self.dir),
            "snapshot_hz": self.snapshot_hz,
            "queue": {name: queue.qsize() for name, queue in self._queues.items()},
            "dropped": {k: v for k, v in self._dropped.items() if v},
            "bytes": self._bytes,
            "disk_low": self._disk_low,
            "free_gb": round(self._free_gb, 2) if self._free_gb else None,
        }
