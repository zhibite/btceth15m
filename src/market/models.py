"""Data models shared by feeds, strategy, execution and web."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class WindowMarket:
    asset: str
    start_ts: int
    end_ts: int
    slug: str = ""
    condition_id: str = ""
    up_token: str = ""
    down_token: str = ""
    price_to_beat: Optional[float] = None
    final_price: Optional[float] = None
    outcome: Optional[str] = None  # UP / DOWN
    volume: float = 0.0
    tick_size: float = 0.01
    twap_lookback: int = 60
    closed: bool = False
    p0_source: str = "gamma"

    def rel(self, now: float | None = None) -> float:
        return (now if now is not None else time.time()) - self.start_ts

    def time_left(self, now: float | None = None) -> float:
        return self.end_ts - (now if now is not None else time.time())

    def token_for(self, side: str) -> str:
        return self.up_token if side.upper() == "UP" else self.down_token

    def to_dict(self) -> dict:
        return {
            "asset": self.asset,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "slug": self.slug,
            "condition_id": self.condition_id,
            "up_token": self.up_token,
            "down_token": self.down_token,
            "price_to_beat": self.price_to_beat,
            "final_price": self.final_price,
            "outcome": self.outcome,
            "volume": self.volume,
            "tick_size": self.tick_size,
            "twap_lookback": self.twap_lookback,
            "closed": self.closed,
            "p0_source": self.p0_source,
        }


def _levels(raw) -> dict[float, float]:
    out: dict[float, float] = {}
    for item in raw or []:
        try:
            price = float(item["price"])
            size = float(item["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if size > 0:
            out[price] = size
    return out


@dataclass
class OrderBook:
    token_id: str
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    updated_ts: float = 0.0
    last_trade: Optional[tuple[float, float, float]] = None  # price, size, ts

    def apply_snapshot(self, bids, asks, ts: float | None = None) -> None:
        self.bids = _levels(bids)
        self.asks = _levels(asks)
        self.updated_ts = float(ts if ts is not None else time.time())

    def apply_price_change(self, price: float, size: float, side: str, ts: float | None = None) -> None:
        book = self.bids if str(side).upper() == "BUY" else self.asks
        if size <= 0:
            book.pop(float(price), None)
        else:
            book[float(price)] = float(size)
        self.updated_ts = float(ts if ts is not None else time.time())

    def best_bid(self) -> Optional[float]:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> Optional[float]:
        return min(self.asks) if self.asks else None

    def spread(self) -> Optional[float]:
        bid, ask = self.best_bid(), self.best_ask()
        if bid is None or ask is None:
            return None
        return ask - bid

    def asks_ascending(self) -> list[tuple[float, float]]:
        return sorted(self.asks.items(), key=lambda kv: kv[0])

    def bids_descending(self) -> list[tuple[float, float]]:
        return sorted(self.bids.items(), key=lambda kv: kv[0], reverse=True)

    def ask_depth(self, levels: int = 3) -> float:
        return sum(size for _, size in self.asks_ascending()[:levels])

    def to_dict(self, levels: int = 5) -> dict:
        return {
            "token_id": self.token_id,
            "best_bid": self.best_bid(),
            "best_ask": self.best_ask(),
            "spread": self.spread(),
            "bids": [[p, s] for p, s in self.bids_descending()[:levels]],
            "asks": [[p, s] for p, s in self.asks_ascending()[:levels]],
            "ask_depth3": self.ask_depth(3),
            "updated_ts": self.updated_ts,
            "age_s": max(0.0, time.time() - self.updated_ts) if self.updated_ts else None,
        }


@dataclass
class PaperPosition:
    id: int
    asset: str
    start_ts: int
    end_ts: int
    token_id: str
    side: str  # UP / DOWN
    size: float
    avg_price: float
    cost: float  # notional + fees
    fees: float
    entry_ts: float
    price_to_beat: float
    status: str = "open"
    payout: float = 0.0
    pnl: float = 0.0
    settled_ts: float = 0.0
    mode: str = "paper"  # paper / live

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "asset": self.asset,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "token_id": self.token_id,
            "side": self.side,
            "size": self.size,
            "avg_price": self.avg_price,
            "cost": self.cost,
            "fees": self.fees,
            "entry_ts": self.entry_ts,
            "price_to_beat": self.price_to_beat,
            "status": self.status,
            "payout": self.payout,
            "pnl": self.pnl,
            "settled_ts": self.settled_ts,
            "mode": self.mode,
        }


@dataclass
class SignalSnapshot:
    ts: float
    asset: str
    start_ts: int
    side: str
    fair: float
    ask: float
    edge: float
    fee: float
    shares: float
    reason: str = ""
    acted: bool = False

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "asset": self.asset,
            "start_ts": self.start_ts,
            "side": self.side,
            "fair": self.fair,
            "ask": self.ask,
            "edge": self.edge,
            "fee": self.fee,
            "shares": self.shares,
            "reason": self.reason,
            "acted": self.acted,
        }
