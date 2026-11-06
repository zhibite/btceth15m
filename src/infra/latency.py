"""Latency measurement for the RTDS feed and entry decisions.

Segments recorded per RTDS update:
  internal = RTDS publish timestamp - Chainlink data timestamp
  client   = local recv timestamp   - RTDS publish timestamp   (network + parse + clock skew)
  e2e      = local recv timestamp   - Chainlink data timestamp

`client` includes the clock offset between this host and the RTDS server; the
minimum client over a window is therefore an upper bound on network + parse +
clock skew. The DB stores per-minute p50/p95/max buckets, not every message.

Entry decisions record:
  data_age_ms = decision time - latest RTDS message receipt
  book_age_ms = decision time - latest CLOB book update
  fill_ms     = paper/live fill time - decision time
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any, Optional

from src.infra.logging import get_logger
from src.persistence.db import Database

log = get_logger(__name__)


def _pct(values: list[float], q: float) -> Optional[float]:
    if not values:
        return None
    vals = sorted(values)
    idx = int(round((q / 100.0) * (len(vals) - 1)))
    idx = max(0, min(len(vals) - 1, idx))
    return float(vals[idx])


class LatencyTracker:
    def __init__(self, db: Optional[Database] = None, retention_s: int = 1800) -> None:
        self._db = db
        self.retention_s = int(retention_s)
        self._samples: dict[tuple[str, str], deque] = defaultdict(
            lambda: deque(maxlen=self.retention_s + 120)
        )
        self._flushed: set[tuple[str, str, int]] = set()
        self._last_flush = 0.0
        self._last_prune = time.time()

    # ── RTDS samples ─────────────────────────────────────────────────────
    def record_rtds(
        self,
        asset: str,
        topic: str,
        data_ts_ms: float,
        publish_ts_ms: float,
        recv_ts: float,
    ) -> None:
        try:
            data_ts_ms = float(data_ts_ms)
            publish_ts_ms = float(publish_ts_ms)
            recv_ts = float(recv_ts)
        except (TypeError, ValueError):
            return
        if data_ts_ms <= 0 or publish_ts_ms <= 0 or recv_ts <= 0:
            return
        internal = publish_ts_ms - data_ts_ms
        client = recv_ts * 1000.0 - publish_ts_ms
        e2e = recv_ts * 1000.0 - data_ts_ms
        self._samples[(str(asset).upper(), str(topic))].append(
            (recv_ts, internal, client, e2e)
        )
        self._maybe_flush(recv_ts)

    def _maybe_flush(self, now: float) -> None:
        if now - self._last_flush < 5.0:
            return
        self.flush(now)

    def flush(self, now: Optional[float] = None, force: bool = False) -> None:
        now = float(now if now is not None else time.time())
        current_min = int(now // 60 * 60)
        for (asset, topic), dq in list(self._samples.items()):
            cutoff = now - self.retention_s
            while dq and dq[0][0] < cutoff:
                dq.popleft()
            groups: dict[int, list[tuple[float, float, float, float]]] = {}
            for sample in dq:
                bucket = int(sample[0] // 60 * 60)
                if not force and bucket >= current_min:
                    continue
                key = (asset, topic, bucket)
                if key in self._flushed:
                    continue
                groups.setdefault(bucket, []).append(sample)
            for bucket, samples in groups.items():
                self._write_bucket(asset, topic, bucket, samples)
                self._flushed.add((asset, topic, bucket))
        pruned = int((now - 7200) // 60 * 60)
        self._flushed = {k for k in self._flushed if k[2] >= pruned}
        self._last_flush = now
        if self._db is not None and now - self._last_prune >= 86400:
            try:
                self._db.prune_latency()
            except Exception as exc:  # noqa: BLE001
                log.warning("latency.prune_failed", error=str(exc))
            self._last_prune = now

    def _write_bucket(self, asset: str, topic: str, bucket: int, samples: list) -> None:
        if self._db is None or not samples:
            return
        internal = [s[1] for s in samples]
        client = [s[2] for s in samples]
        e2e = [s[3] for s in samples]
        try:
            self._db.upsert_latency_bucket(
                bucket_ts=bucket,
                asset=asset,
                topic=topic,
                n=len(samples),
                internal_p50=_pct(internal, 50),
                internal_p95=_pct(internal, 95),
                internal_max=max(internal),
                client_p50=_pct(client, 50),
                client_p95=_pct(client, 95),
                client_max=max(client),
                e2e_p50=_pct(e2e, 50),
                e2e_p95=_pct(e2e, 95),
                e2e_max=max(e2e),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("latency.bucket_write_failed", error=str(exc))

    # ── decision events ──────────────────────────────────────────────────
    def record_decision(
        self,
        *,
        ts: float,
        asset: str,
        window_start: int,
        data_age_ms: Optional[float],
        book_age_ms: Optional[float],
        fill_ms: Optional[float],
        note: str = "",
    ) -> None:
        if self._db is None:
            return
        try:
            self._db.insert_latency_event(
                ts=float(ts),
                asset=str(asset).upper(),
                window_start=int(window_start),
                data_age_ms=data_age_ms,
                book_age_ms=book_age_ms,
                fill_ms=fill_ms,
                note=note,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("latency.event_write_failed", error=str(exc))

    # ── summaries for the dashboard ──────────────────────────────────────
    def summary(self, window_s: int = 900, now: Optional[float] = None) -> dict[str, Any]:
        now = float(now if now is not None else time.time())
        out: dict[str, Any] = {"window_s": int(window_s), "keys": {}}
        for (asset, topic), dq in self._samples.items():
            samples = [s for s in dq if s[0] >= now - window_s]
            if not samples:
                continue
            internal = [s[1] for s in samples]
            client = [s[2] for s in samples]
            e2e = [s[3] for s in samples]
            out["keys"].setdefault(asset, {})[topic] = {
                "n": len(samples),
                "internal": {
                    "p50": _pct(internal, 50),
                    "p95": _pct(internal, 95),
                    "p99": _pct(internal, 99),
                },
                "client": {
                    "p50": _pct(client, 50),
                    "p95": _pct(client, 95),
                    "p99": _pct(client, 99),
                    "min": float(min(client)),
                },
                "e2e": {
                    "p50": _pct(e2e, 50),
                    "p95": _pct(e2e, 95),
                    "p99": _pct(e2e, 99),
                },
            }
        return out

    def decision_summary(self, window_s: int = 3600, now: Optional[float] = None) -> dict[str, Any]:
        now = float(now if now is not None else time.time())
        if self._db is None:
            return {"window_s": int(window_s), "n": 0}
        rows = self._db.latency_events_since(now - window_s)
        out: dict[str, Any] = {"window_s": int(window_s), "n": len(rows)}
        for key in ("data_age_ms", "book_age_ms", "fill_ms"):
            vals = [float(r[key]) for r in rows if r.get(key) is not None]
            out[key] = {
                "p50": _pct(vals, 50),
                "p95": _pct(vals, 95),
                "p99": _pct(vals, 99),
                "n": len(vals),
            }
        return out
