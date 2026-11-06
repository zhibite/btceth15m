"""Paper execution: walk the live CLOB book and book simulated fills.

The broker is deliberately simple and honest:
  * a BUY consumes ask levels from the best ask upward until the requested
    notional is spent (or the book runs out)
  * the fee is the official Polymarket taker fee per level
  * the fill is persisted with the average price and total fee
  * the position is held to settlement (Phase 1)
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Optional

from src.config import RuntimeConfig
from src.infra.logging import get_logger
from src.market.models import PaperPosition, WindowMarket
from src.persistence.db import Database
from src.strategy.fair_value import per_share_fee

log = get_logger(__name__)


@dataclass
class BuyPreview:
    shares: float
    notional: float
    avg_price: float
    fee: float
    best_ask: float
    best_size: float
    levels: list[tuple[float, float]]

    @property
    def cost(self) -> float:
        return self.notional + self.fee

    @property
    def fee_per_share(self) -> float:
        return self.fee / self.shares if self.shares > 0 else 0.0

    @property
    def effective_price(self) -> float:
        return self.cost / self.shares if self.shares > 0 else 0.0


class PaperBroker:
    def __init__(self, db: Database, runtime: RuntimeConfig) -> None:
        self._db = db
        self._runtime = runtime

    @staticmethod
    def preview_buy(
        asks: list[tuple[float, float]], notional: float, fee_rate: float
    ) -> Optional[BuyPreview]:
        if not asks or notional <= 0:
            return None
        remaining = float(notional)
        levels: list[tuple[float, float]] = []
        for price, size in sorted(asks, key=lambda kv: kv[0]):
            if not (0.0 < price < 1.0) or size <= 0:
                continue
            take = min(float(size), remaining / price)
            if take <= 0:
                break
            levels.append((float(price), float(take)))
            remaining -= price * take
            if remaining <= 1e-9:
                break
        if not levels:
            return None
        shares = sum(s for _, s in levels)
        notional_filled = sum(p * s for p, s in levels)
        if shares <= 0 or notional_filled <= 0:
            return None
        avg = notional_filled / shares
        fee = sum(per_share_fee(fee_rate, p) * s for p, s in levels)
        best_ask, best_size = levels[0]
        return BuyPreview(
            shares=shares,
            notional=notional_filled,
            avg_price=avg,
            fee=fee,
            best_ask=best_ask,
            best_size=best_size,
            levels=levels,
        )

    def buy(
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
        params = self._runtime.params_for(market.asset)
        preview = self.preview_buy(asks, notional, params.taker_fee_rate)
        if preview is None:
            return None
        if preview.shares < params.min_order_shares:
            log.info(
                "paper.rejected_min_shares",
                asset=market.asset,
                shares=round(preview.shares, 4),
                min_shares=params.min_order_shares,
            )
            return None
        if preview.shares < params.min_ask_shares:
            log.info(
                "paper.rejected_thin_book",
                asset=market.asset,
                shares=round(preview.shares, 4),
                min_ask_shares=params.min_ask_shares,
            )
            return None

        now = time.time()
        order_id = uuid.uuid4().hex[:16]
        fill_id = uuid.uuid4().hex[:16]
        self._db.insert_order(
            order_id=order_id,
            ts=now,
            asset=market.asset,
            start_ts=market.start_ts,
            token_id=token_id,
            side="BUY",
            price=preview.avg_price,
            size=preview.shares,
            notional=preview.notional,
            status="filled",
            mode="paper",
            reason=f"outcome={side}; {reason}",
        )
        self._db.insert_fill(
            fill_id=fill_id,
            order_id=order_id,
            ts=now,
            asset=market.asset,
            start_ts=market.start_ts,
            token_id=token_id,
            side="BUY",
            price=preview.avg_price,
            size=preview.shares,
            fee=preview.fee,
            role="TAKER",
        )
        pos = PaperPosition(
            id=0,
            asset=market.asset,
            start_ts=market.start_ts,
            end_ts=market.end_ts,
            token_id=token_id,
            side=side.upper(),
            size=preview.shares,
            avg_price=preview.avg_price,
            cost=preview.cost,
            fees=preview.fee,
            entry_ts=now,
            price_to_beat=float(price_to_beat),
        )
        pos.id = self._db.insert_position(pos)
        log.info(
            "paper.filled",
            asset=market.asset,
            window=market.start_ts,
            side=side,
            shares=round(preview.shares, 4),
            avg_price=round(preview.avg_price, 4),
            notional=round(preview.notional, 4),
            fee=round(preview.fee, 5),
            levels=[(round(p, 3), round(s, 2)) for p, s in preview.levels[:5]],
            cost=round(preview.cost, 4),
        )
        return pos
