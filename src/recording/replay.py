"""Offline replay of the recorder's JSONL streams.

The replay deliberately imports the *production* pieces:

  * ``src.strategy.fair_value.fair_probability`` for the TWAP model, and
  * ``src.execution.paper.PaperBroker.preview_buy`` for the executable ask,
    per-level fee, shares and notional.

That keeps the analysis from drifting away from the live bot.  For every
window it walks the recorded 1 Hz book snapshots and, for each entry bucket,
keeps the first signal that would have passed the live gates.  The realized
edge per share is ``(1 if the side won else 0) - effective_price`` where
``effective_price`` already includes the taker fee.
"""
from __future__ import annotations

import csv
import gzip
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

import numpy as np

from src.config import StrategyParams
from src.execution.paper import PaperBroker
from src.infra.logging import get_logger
from src.strategy.fair_value import fair_probability

log = get_logger(__name__)

STREAMS = ("rtds", "book", "trade", "clob", "window")

# Counterfactual entry buckets.  The live per-asset config starts at 630s and
# stops at 870s; 540-629 is included so the earlier zone can be compared.  The
# last bucket stops 30s before settlement on purpose (latency / final prints).
DEFAULT_BUCKETS: tuple[tuple[int, int], ...] = (
    (540, 629),
    (630, 719),
    (720, 779),
    (780, 839),
    (840, 870),
)


def parse_buckets(text: str | None) -> tuple[tuple[int, int], ...]:
    if not text:
        return DEFAULT_BUCKETS
    out: list[tuple[int, int]] = []
    for part in str(text).split(","):
        piece = part.strip()
        if not piece:
            continue
        lo_text, _, hi_text = piece.partition("-")
        try:
            lo, hi = int(lo_text), int(hi_text)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid bucket {piece!r}; expected LO-HI") from exc
        if lo > hi:
            lo, hi = hi, lo
        out.append((lo, hi))
    return tuple(out) or DEFAULT_BUCKETS


def bucket_for(rel: int, buckets: Sequence[tuple[int, int]]) -> Optional[str]:
    for lo, hi in buckets:
        if lo <= rel <= hi:
            return f"{lo}-{hi}"
    return None


def _float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class WindowData:
    asset: str
    start_ts: int
    end_ts: int
    slug: str = ""
    up_token: str = ""
    down_token: str = ""
    p0: Optional[float] = None
    final_price: Optional[float] = None
    outcome: Optional[str] = None
    volume: float = 0.0
    twap_lookback: int = 60
    p0_source: str = ""

    @property
    def window_sec(self) -> int:
        return max(1, self.end_ts - self.start_ts)


@dataclass
class BookSnapshot:
    ts: float
    updated_ts: float
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]
    best_bid: Optional[float]
    best_ask: Optional[float]
    spread: Optional[float]


@dataclass
class SignalRow:
    asset: str
    start_ts: int
    rel: int
    bucket: str
    side: str
    fair: float
    spot: float
    sigma: float
    eff_price: float
    model_edge: float
    fee_per_share: float
    shares: float
    cost: float
    best_bid: Optional[float]
    best_ask: Optional[float]
    spread: Optional[float]
    book_age: Optional[float]
    last_trade: Optional[float]
    ask_minus_last: Optional[float]
    outcome: str
    outcome_source: str
    win: bool
    realized_edge: float
    pnl: float
    is_first: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset": self.asset,
            "start_ts": self.start_ts,
            "rel": self.rel,
            "bucket": self.bucket,
            "side": self.side,
            "fair": self.fair,
            "spot": self.spot,
            "sigma": self.sigma,
            "eff_price": self.eff_price,
            "model_edge": self.model_edge,
            "fee_per_share": self.fee_per_share,
            "shares": self.shares,
            "cost": self.cost,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "spread": self.spread,
            "book_age": self.book_age,
            "last_trade": self.last_trade,
            "ask_minus_last": self.ask_minus_last,
            "outcome": self.outcome,
            "outcome_source": self.outcome_source,
            "win": self.win,
            "realized_edge": self.realized_edge,
            "pnl": self.pnl,
            "is_first": self.is_first,
        }


class RtdsSeries:
    """Per-asset Chainlink prints, rebuilt like ``PriceBuffer``.

    ``PriceBuffer.add`` drops a point whose exchange timestamp is not strictly
    newer than the previous one.  We reproduce that rule in arrival order, then
    precompute per-second normalized returns for the sigma calculation.
    """

    def __init__(self, asset: str, points: Iterable[tuple[float, float, float]]) -> None:
        self.asset = str(asset).upper()
        raw = [
            (float(point_ts), float(recv_ts), float(price))
            for point_ts, recv_ts, price in points
            if price is not None
            and math.isfinite(float(price))
            and float(price) > 0
            and math.isfinite(float(point_ts))
            and math.isfinite(float(recv_ts))
        ]
        raw.sort(key=lambda x: x[1])
        accepted: list[tuple[float, float, float]] = []
        last_ts: Optional[float] = None
        for point_ts, recv_ts, price in raw:
            if last_ts is not None and point_ts <= last_ts:
                continue
            accepted.append((point_ts, recv_ts, price))
            last_ts = point_ts
        accepted.sort(key=lambda x: x[0])
        self.point_ts = np.asarray([x[0] for x in accepted], dtype=float)
        self.recv_ts = np.asarray([x[1] for x in accepted], dtype=float)
        self.price = np.asarray([x[2] for x in accepted], dtype=float)
        self._build_returns()

    def _build_returns(self) -> None:
        if len(self.point_ts) < 2:
            self.ret_ts = np.asarray([], dtype=float)
            self.ret = np.asarray([], dtype=float)
            return
        dt = np.diff(self.point_ts)
        mask = (dt > 0.0) & (dt <= 3.0)
        safe_dt = np.where(mask, dt, 1.0)
        with np.errstate(divide="ignore", invalid="ignore"):
            ret = np.log(self.price[1:] / self.price[:-1]) / np.sqrt(safe_dt)
        self.ret_ts = self.point_ts[1:][mask]
        self.ret = ret[mask]

    def __len__(self) -> int:
        return int(len(self.point_ts))

    def latest_at(self, t: float) -> Optional[dict[str, float]]:
        idx = int(np.searchsorted(self.recv_ts, float(t), side="right")) - 1
        if idx < 0:
            return None
        return {
            "point_ts": float(self.point_ts[idx]),
            "recv_ts": float(self.recv_ts[idx]),
            "price": float(self.price[idx]),
            "age": float(t) - float(self.recv_ts[idx]),
        }

    def _range(self, a: float, b: float) -> tuple[int, int]:
        lo = int(np.searchsorted(self.point_ts, float(a), side="left"))
        hi = int(np.searchsorted(self.point_ts, float(b), side="left"))
        return lo, hi

    def count_between(self, a: float, b: float) -> int:
        lo, hi = self._range(a, b)
        return max(0, hi - lo)

    def mean_between(self, a: float, b: float) -> Optional[float]:
        lo, hi = self._range(a, b)
        if hi <= lo:
            return None
        return float(np.mean(self.price[lo:hi]))

    def sigma_at(self, t: float, lookback_s: float, min_samples: int) -> Optional[float]:
        idx = int(np.searchsorted(self.recv_ts, float(t), side="right")) - 1
        if idx < 0:
            return None
        last_pt = float(self.point_ts[idx])
        start_pt = last_pt - float(lookback_s)
        point_lo = int(np.searchsorted(self.point_ts, start_pt, side="left"))
        if idx - point_lo + 1 < int(min_samples):
            return None
        lo = int(np.searchsorted(self.ret_ts, start_pt, side="left"))
        hi = int(np.searchsorted(self.ret_ts, last_pt, side="right"))
        returns = self.ret[lo:hi]
        if len(returns) < 30:
            return None
        value = float(np.std(returns, ddof=1))
        if not math.isfinite(value) or value <= 0:
            return None
        return value


class BookSeries:
    def __init__(self, token_id: str, snapshots: Sequence[BookSnapshot]) -> None:
        self.token_id = str(token_id)
        self.snaps = list(snapshots)
        self.ts = np.asarray([s.ts for s in self.snaps], dtype=float)

    def __len__(self) -> int:
        return len(self.snaps)

    def latest_at(
        self, t: float, max_age: float
    ) -> Optional[tuple[BookSnapshot, float]]:
        idx = int(np.searchsorted(self.ts, float(t), side="right")) - 1
        if idx < 0:
            return None
        snap = self.snaps[idx]
        if not snap.updated_ts:
            return None
        age = float(t) - float(snap.updated_ts)
        if age < 0.0 or age > float(max_age):
            return None
        return snap, age


class TradeSeries:
    def __init__(self, token_id: str, trades: Sequence[dict[str, Any]]) -> None:
        self.token_id = str(token_id)
        self.trades = list(trades)
        self.ts = np.asarray([t["ts"] for t in self.trades], dtype=float)

    def last_at(self, t: float, max_age: float) -> Optional[dict[str, Any]]:
        idx = int(np.searchsorted(self.ts, float(t), side="right")) - 1
        if idx < 0:
            return None
        trade = self.trades[idx]
        if float(t) - float(trade["ts"]) > float(max_age):
            return None
        return trade


# ── loading ────────────────────────────────────────────────────────────────
def available_days(root: str | Path) -> list[date]:
    base = Path(root)
    out: list[date] = []
    if not base.exists():
        return out
    for path in sorted(base.iterdir()):
        if not path.is_dir():
            continue
        try:
            out.append(datetime.strptime(path.name, "%Y%m%d").date())
        except ValueError:
            continue
    return out


def recording_files(
    root: str | Path,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
) -> dict[str, list[Path]]:
    base = Path(root)
    out: dict[str, list[Path]] = {stream: [] for stream in STREAMS}
    if not base.exists():
        return out
    for day_dir in sorted(base.iterdir()):
        if not day_dir.is_dir():
            continue
        try:
            day = datetime.strptime(day_dir.name, "%Y%m%d").date()
        except ValueError:
            continue
        if date_from is not None and day < date_from:
            continue
        if date_to is not None and day > date_to:
            continue
        for stream in STREAMS:
            out[stream].extend(sorted(day_dir.glob(f"{stream}_*.jsonl.gz")))
    return out


def iter_jsonl(paths: Iterable[Path]) -> Iterator[dict[str, Any]]:
    for path in paths:
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(record, dict):
                        yield record
        except (OSError, EOFError) as exc:
            log.warning("replay.read_failed", path=str(path), error=str(exc))


def load_windows(
    paths: Iterable[Path], assets: Optional[Iterable[str]] = None
) -> dict[tuple[str, int], WindowData]:
    wanted = {str(a).upper() for a in assets} if assets else None
    out: dict[tuple[str, int], WindowData] = {}
    for rec in iter_jsonl(paths):
        asset = str(rec.get("asset") or "").upper()
        if not asset or (wanted is not None and asset not in wanted):
            continue
        try:
            start_ts = int(rec.get("start_ts"))
        except (TypeError, ValueError):
            continue
        end_ts = int(rec.get("end_ts") or 0) or start_ts + 900
        key = (asset, start_ts)
        window = out.get(key) or WindowData(asset=asset, start_ts=start_ts, end_ts=end_ts)
        window.end_ts = end_ts
        for name in ("slug", "up_token", "down_token", "p0_source"):
            value = rec.get(name)
            if value:
                setattr(window, name, str(value))
        p0 = _float(rec.get("price_to_beat"))
        if p0 is not None:
            window.p0 = p0
        final = _float(rec.get("final_price"))
        if final is not None:
            window.final_price = final
        outcome = str(rec.get("outcome") or "").upper()
        if outcome in ("UP", "DOWN"):
            window.outcome = outcome
        volume = _float(rec.get("volume"))
        if volume is not None:
            window.volume = volume
        try:
            twap = int(rec.get("twap_lookback") or 0)
        except (TypeError, ValueError):
            twap = 0
        if twap > 0:
            window.twap_lookback = twap
        out[key] = window
    return out


def load_rtds(
    paths: Iterable[Path], assets: Optional[Iterable[str]] = None
) -> dict[str, RtdsSeries]:
    wanted = {str(a).upper() for a in assets} if assets else None
    raw: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    for rec in iter_jsonl(paths):
        asset = str(rec.get("asset") or "").upper()
        if not asset or (wanted is not None and asset not in wanted):
            continue
        payload = rec.get("payload")
        if not isinstance(payload, dict):
            continue
        topic = payload.get("topic")
        if topic and topic != "crypto_prices_chainlink":
            continue
        inner = payload.get("payload")
        if not isinstance(inner, dict):
            continue
        items = inner.get("data") if isinstance(inner.get("data"), list) else [inner]
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                point_ts = float(item.get("timestamp")) / 1000.0
                price = float(item.get("value"))
            except (TypeError, ValueError):
                continue
            try:
                recv_ts = float(rec.get("recv_ts") or point_ts)
            except (TypeError, ValueError):
                recv_ts = point_ts
            raw[asset].append((point_ts, recv_ts, price))
    return {
        asset: series
        for asset, points in raw.items()
        if (series := RtdsSeries(asset, points))
    }


def _levels(raw: Any) -> tuple[tuple[float, float], ...]:
    out: list[tuple[float, float]] = []
    for item in raw or []:
        try:
            price = float(item[0])
            size = float(item[1])
        except (IndexError, KeyError, TypeError, ValueError):
            continue
        if price > 0 and size > 0:
            out.append((price, size))
    return tuple(out)


def load_books(
    paths: Iterable[Path],
    *,
    tokens: Optional[Iterable[str]] = None,
    assets: Optional[Iterable[str]] = None,
) -> dict[str, BookSeries]:
    wanted_tokens = {str(t) for t in tokens} if tokens else None
    wanted_assets = {str(a).upper() for a in assets} if assets else None
    raw: dict[str, list[BookSnapshot]] = defaultdict(list)
    for rec in iter_jsonl(paths):
        asset = str(rec.get("asset") or "").upper()
        if wanted_assets is not None and asset not in wanted_assets:
            continue
        token = str(rec.get("token_id") or "")
        if not token or (wanted_tokens is not None and token not in wanted_tokens):
            continue
        try:
            ts = float(rec.get("ts") or rec.get("recv_ts") or 0.0)
        except (TypeError, ValueError):
            continue
        if ts <= 0:
            continue
        updated = _float(rec.get("updated_ts")) or 0.0
        bids = _levels(rec.get("bids"))
        asks = _levels(rec.get("asks"))
        best_bid = max((p for p, _ in bids), default=None)
        best_ask = min((p for p, _ in asks), default=None)
        spread = (
            best_ask - best_bid
            if best_bid is not None and best_ask is not None
            else None
        )
        raw[token].append(
            BookSnapshot(
                ts=ts,
                updated_ts=updated,
                bids=bids,
                asks=asks,
                best_bid=best_bid,
                best_ask=best_ask,
                spread=spread,
            )
        )
    out: dict[str, BookSeries] = {}
    for token, snaps in raw.items():
        snaps.sort(key=lambda s: s.ts)
        deduped: list[BookSnapshot] = []
        for snap in snaps:
            if deduped and abs(snap.ts - deduped[-1].ts) < 1e-9:
                deduped[-1] = snap
            else:
                deduped.append(snap)
        out[token] = BookSeries(token, deduped)
    return out


def load_trades(
    paths: Iterable[Path],
    *,
    tokens: Optional[Iterable[str]] = None,
    assets: Optional[Iterable[str]] = None,
) -> dict[str, TradeSeries]:
    wanted_tokens = {str(t) for t in tokens} if tokens else None
    wanted_assets = {str(a).upper() for a in assets} if assets else None
    raw: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in iter_jsonl(paths):
        asset = str(rec.get("asset") or "").upper()
        if wanted_assets is not None and asset not in wanted_assets:
            continue
        token = str(rec.get("token_id") or "")
        if not token or (wanted_tokens is not None and token not in wanted_tokens):
            continue
        try:
            ts = float(rec.get("ts") or 0.0)
            price = float(rec.get("price"))
        except (TypeError, ValueError):
            continue
        if ts <= 0 or price <= 0:
            continue
        raw[token].append(
            {
                "ts": ts,
                "price": price,
                "size": _float(rec.get("size")) or 0.0,
            }
        )
    out: dict[str, TradeSeries] = {}
    for token, trades in raw.items():
        trades.sort(key=lambda t: t["ts"])
        out[token] = TradeSeries(token, trades)
    return out


# ── replay ─────────────────────────────────────────────────────────────────
def scan_window(
    window: WindowData,
    rtds: RtdsSeries,
    books: dict[str, BookSeries],
    trades: dict[str, TradeSeries],
    params: StrategyParams,
    *,
    step: int = 1,
    buckets: Sequence[tuple[int, int]] = DEFAULT_BUCKETS,
    max_spot_age: float = 5.0,
    max_trade_age: float = 5.0,
    fair_cap: Optional[float] = None,
) -> list[SignalRow]:
    if not window.up_token or not window.down_token:
        return []
    twap_window = float(window.twap_lookback or 60)
    min_twap_points = max(15, int(twap_window * 0.6))

    p0 = window.p0
    if p0 is None:
        start_a = window.start_ts - twap_window
        if rtds.count_between(start_a, window.start_ts) < min_twap_points:
            return []
        p0 = rtds.mean_between(start_a, window.start_ts)
        if p0 is None:
            return []
    outcome = window.outcome
    outcome_source = "gamma"
    if outcome not in ("UP", "DOWN"):
        end_a = window.end_ts - twap_window
        if rtds.count_between(end_a, window.end_ts) < min_twap_points:
            return []
        close = rtds.mean_between(end_a, window.end_ts)
        if close is None:
            return []
        outcome = "UP" if close >= p0 else "DOWN"
        outcome_source = "rtds"

    scan_lo = min(lo for lo, _ in buckets)
    scan_hi = max(hi for _, hi in buckets)
    rows: list[SignalRow] = []
    seen_buckets: set[str] = set()
    first_live: Optional[SignalRow] = None

    for rel in range(int(scan_lo), int(scan_hi) + 1, max(1, int(step))):
        t = window.start_ts + rel
        spot = rtds.latest_at(t)
        if spot is None or spot["age"] > max_spot_age:
            continue
        sigma = rtds.sigma_at(t, params.sigma_lookback_s, params.min_sigma_samples)
        if sigma is None:
            continue
        fv = fair_probability(
            p0=p0,
            spot=spot["price"],
            sigma=sigma,
            now=t,
            end_ts=window.end_ts,
            twap_window=twap_window,
            mean_between=rtds.mean_between,
        )
        if fv is None:
            continue
        fair_up = float(fv["fair"])

        candidates: list[dict[str, Any]] = []
        for side, token, fair in (
            ("UP", window.up_token, fair_up),
            ("DOWN", window.down_token, 1.0 - fair_up),
        ):
            fair = float(fair)
            if fair_cap is not None:
                # The tail buckets are systematically overconfident; cap the
                # model probability before the edge/gates, not after.
                fair = min(fair, float(fair_cap))
            if fair < params.min_fair:
                continue
            series = books.get(token)
            if series is None:
                continue
            hit = series.latest_at(t, params.max_stale_book_s)
            if hit is None:
                continue
            snap, age = hit
            preview = PaperBroker.preview_buy(
                list(snap.asks),
                params.max_position_usd,
                params.taker_fee_rate,
            )
            if preview is None:
                continue
            if preview.effective_price < params.min_entry_price:
                continue
            candidates.append(
                {
                    "side": side,
                    "token": token,
                    "fair": float(fair),
                    "preview": preview,
                    "snap": snap,
                    "age": age,
                }
            )
        if not candidates:
            continue
        best = max(candidates, key=lambda c: c["fair"] - c["preview"].effective_price)
        snap = best["snap"]
        preview = best["preview"]
        if snap.spread is None or snap.spread > params.max_spread:
            continue
        if best["age"] > params.max_stale_book_s:
            continue
        if preview.best_size < params.min_ask_shares:
            continue
        edge = float(best["fair"] - preview.effective_price)
        if edge < params.min_edge + params.model_buffer:
            continue

        bucket = bucket_for(rel, buckets)
        if bucket is None:
            continue
        if bucket in seen_buckets:
            continue
        seen_buckets.add(bucket)

        side = str(best["side"])
        eff = float(preview.effective_price)
        shares = float(preview.shares)
        cost = float(preview.cost)
        win = side == outcome
        realized_edge = (1.0 if win else 0.0) - eff
        pnl = shares * realized_edge
        last = _last_trade(trades, str(best["token"]), t, max_trade_age)
        last_price = _float(last.get("price")) if last is not None else None
        ask_minus_last = (
            snap.best_ask - last_price
            if last_price is not None and snap.best_ask is not None
            else None
        )
        row = SignalRow(
            asset=window.asset,
            start_ts=window.start_ts,
            rel=rel,
            bucket=bucket,
            side=side,
            fair=float(best["fair"]),
            spot=float(spot["price"]),
            sigma=float(sigma),
            eff_price=eff,
            model_edge=edge,
            fee_per_share=float(preview.fee_per_share),
            shares=shares,
            cost=cost,
            best_bid=snap.best_bid,
            best_ask=snap.best_ask,
            spread=snap.spread,
            book_age=float(best["age"]),
            last_trade=last_price,
            ask_minus_last=ask_minus_last,
            outcome=outcome,
            outcome_source=outcome_source,
            win=win,
            realized_edge=realized_edge,
            pnl=pnl,
        )
        rows.append(row)
        if (
            first_live is None
            and params.entry_min_rel_s <= rel <= params.entry_max_rel_s
        ):
            first_live = row
        if len(seen_buckets) == len(buckets):
            break

    if first_live is not None:
        first_live.is_first = True
    return rows


def _last_trade(
    trades: dict[str, TradeSeries], token: str, t: float, max_age: float
) -> Optional[dict[str, Any]]:
    series = trades.get(token)
    if series is None:
        return None
    return series.last_at(t, max_age)


# ── aggregation / output ───────────────────────────────────────────────────
def summarize(
    rows: Iterable[SignalRow], *, first_only: bool = False
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[SignalRow]] = defaultdict(list)
    for row in rows:
        if first_only and not row.is_first:
            continue
        groups[(row.asset, row.bucket)].append(row)
        groups[("ALL", row.bucket)].append(row)
        groups[(row.asset, "ALL")].append(row)
        groups[("ALL", "ALL")].append(row)
    out: list[dict[str, Any]] = []
    for (asset, bucket), items in sorted(groups.items()):
        n = len(items)
        if n == 0:
            continue
        wins = sum(1 for row in items if row.win)
        cost = sum(row.cost for row in items)
        pnl = sum(row.pnl for row in items)
        realized = np.asarray([row.realized_edge for row in items], dtype=float)
        model = np.asarray([row.model_edge for row in items], dtype=float)
        book_ages = [row.book_age for row in items if row.book_age is not None]
        ask_diffs = [
            row.ask_minus_last for row in items if row.ask_minus_last is not None
        ]
        out.append(
            {
                "asset": asset,
                "bucket": bucket,
                "n": n,
                "wins": wins,
                "win_rate": wins / n,
                "avg_entry": float(np.mean([row.eff_price for row in items])),
                "avg_model_edge": float(np.mean(model)),
                "mean_realized_edge": float(np.mean(realized)),
                "median_realized_edge": float(np.median(realized)),
                "p10_realized_edge": float(np.percentile(realized, 10)),
                "p90_realized_edge": float(np.percentile(realized, 90)),
                "total_cost": float(cost),
                "total_pnl": float(pnl),
                "roi": float(pnl / cost) if cost > 0 else None,
                "median_book_age": (
                    float(np.median(book_ages)) if book_ages else None
                ),
                "median_ask_minus_last": (
                    float(np.median(ask_diffs)) if ask_diffs else None
                ),
            }
        )
    return out


SUMMARY_FIELDS = (
    "asset",
    "bucket",
    "n",
    "wins",
    "win_rate",
    "avg_entry",
    "avg_model_edge",
    "mean_realized_edge",
    "median_realized_edge",
    "p10_realized_edge",
    "p90_realized_edge",
    "total_cost",
    "total_pnl",
    "roi",
    "median_book_age",
    "median_ask_minus_last",
)

SIGNAL_FIELDS = tuple(SignalRow.__dataclass_fields__.keys())


def write_signals_csv(path: str | Path, rows: Iterable[SignalRow]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(SIGNAL_FIELDS))
        writer.writeheader()
        for row in rows:
            writer.writerow(row.to_dict())


def write_summary_csv(path: str | Path, rows: Iterable[dict[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(SUMMARY_FIELDS))
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in SUMMARY_FIELDS})


def summary_markdown(rows: Sequence[dict[str, Any]], title: str) -> str:
    lines = [f"# {title}", ""]
    if not rows:
        lines.append("_No labelled signals._")
        return "\n".join(lines) + "\n"
    header = (
        "| asset | bucket | n | win% | avg entry | model edge | mean net | "
        "median net | p10 | p90 | ROI |"
    )
    lines.append(header)
    lines.append(
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
    )
    for row in rows:
        win_rate = row.get("win_rate")
        roi = row.get("roi")
        lines.append(
            "| {asset} | {bucket} | {n} | {win} | {entry} | {model} | {mean} | "
            "{median} | {p10} | {p90} | {roi} |".format(
                asset=row.get("asset"),
                bucket=row.get("bucket"),
                n=row.get("n"),
                win=f"{100.0 * win_rate:.1f}" if win_rate is not None else "-",
                entry=_fmt(row.get("avg_entry")),
                model=_fmt(row.get("avg_model_edge")),
                mean=_fmt(row.get("mean_realized_edge")),
                median=_fmt(row.get("median_realized_edge")),
                p10=_fmt(row.get("p10_realized_edge")),
                p90=_fmt(row.get("p90_realized_edge")),
                roi=f"{100.0 * roi:+.1f}%" if roi is not None else "-",
            )
        )
    return "\n".join(lines) + "\n"


def _fmt(value: Any) -> str:
    try:
        if value is None:
            return "-"
        return f"{float(value):+.4f}"
    except (TypeError, ValueError):
        return "-"
