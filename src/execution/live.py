"""Live CLOB execution via ``py-clob-client-v2``.

Live trading is guarded by two independent switches:

  * ``LIVE_ENABLED=true`` — the deployment-level master switch;
  * per-asset mode ``live`` on the config page.

A live entry is a marketable **FOK** buy sized by that asset's
``max_position_usd``. The exchange fill is persisted to
``orders/fills/positions`` with ``mode='live'``. Settlement and PnL use the
same public 60s-TWAP outcome as the paper track; on-chain redemption of
winning shares and wallet-balance reconciliation are deliberately left to the
operator (see docs/README.md).

Paper mode never imports the client package: the import happens lazily on the
first live order.
"""
from __future__ import annotations

import asyncio
import importlib.util
import math
import threading
import time
import uuid
from typing import Any, Callable, Optional

from src.config import AppSettings, RuntimeConfig, StrategyParams
from src.execution.paper import PaperBroker
from src.infra.logging import get_logger
from src.market.models import PaperPosition, WindowMarket
from src.persistence.db import Database
from src.strategy.fair_value import per_share_fee

log = get_logger(__name__)

CLIENT_MODULE = "py_clob_client_v2"


def _num(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(out):
        return default
    return out


def _default_order_args(token_id: str, amount: float) -> Any:
    from py_clob_client_v2 import MarketOrderArgs, OrderType, Side  # type: ignore

    return MarketOrderArgs(
        token_id=str(token_id),
        amount=float(amount),
        side=Side.BUY,
        order_type=OrderType.FOK,
    )


class LiveBroker:
    _client_installed: bool | None = None

    def __init__(
        self,
        settings: AppSettings,
        db: Database | None = None,
        runtime: RuntimeConfig | None = None,
        *,
        client_factory: Callable[[], Any] | None = None,
        order_factory: Callable[[str, float], Any] | None = None,
    ) -> None:
        self.settings = settings
        self._db = db
        self._runtime = runtime
        self._client: Any = None
        self._lock = threading.Lock()
        self._client_factory = client_factory
        self._order_factory = order_factory or _default_order_args

    # ── availability ─────────────────────────────────────────────────────
    @classmethod
    def client_installed(cls) -> bool:
        if cls._client_installed is None:
            cls._client_installed = importlib.util.find_spec(CLIENT_MODULE) is not None
        return cls._client_installed

    @property
    def credentials_configured(self) -> bool:
        return bool(self.settings.wallet_private_key)

    @property
    def available(self) -> bool:
        """Whether live orders can actually be placed right now."""
        if not self.settings.live_enabled:
            return False
        if not self.credentials_configured:
            return False
        if self._client_factory is None and not self.client_installed():
            return False
        return True

    @property
    def unavailable_reason(self) -> str:
        if not self.settings.live_enabled:
            return "LIVE_ENABLED=false"
        if not self.credentials_configured:
            return "WALLET_PRIVATE_KEY 未配置"
        if self._client_factory is None and not self.client_installed():
            return f"{CLIENT_MODULE} 未安装（pip install -e .[live]）"
        return ""

    def _params_for(self, asset: str) -> StrategyParams:
        if self._runtime is not None:
            return self._runtime.params_for(asset)
        return StrategyParams()

    # ── client ───────────────────────────────────────────────────────────
    def _ensure_client(self) -> Any:
        if not self.settings.live_enabled:
            raise RuntimeError("live trading is disabled (set LIVE_ENABLED=true)")
        if not self.settings.wallet_private_key:
            raise RuntimeError("WALLET_PRIVATE_KEY is required for live trading")
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is not None:
                return self._client

            if self._client_factory is not None:
                self._client = self._client_factory()
                return self._client

            try:
                from py_clob_client_v2 import ApiCreds, ClobClient  # type: ignore
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise RuntimeError(
                    "py-clob-client-v2 is not installed; install the [live] extra first"
                ) from exc

            creds = None
            if (
                self.settings.api_key
                and self.settings.api_secret
                and self.settings.api_passphrase
            ):
                creds = ApiCreds(
                    api_key=self.settings.api_key,
                    api_secret=self.settings.api_secret,
                    api_passphrase=self.settings.api_passphrase,
                )
            client = ClobClient(
                host=self.settings.clob_api,
                chain_id=137,
                key=self.settings.wallet_private_key,
                creds=creds,
                signature_type=int(self.settings.signature_type),
                funder=(
                    self.settings.funder_address
                    or self.settings.wallet_address
                    or None
                ),
            )
            if creds is None:
                # L1 auth is enough to create/derive the L2 API credentials.
                creds = client.create_or_derive_api_key()
                client.set_api_creds(creds)
            self._client = client
            log.info("live.client_ready", host=self.settings.clob_api)
            return self._client

    def _place_market_order(
        self, client: Any, token_id: str, notional: float
    ) -> Any:
        args = self._order_factory(token_id, float(notional))
        if hasattr(client, "create_and_post_market_order"):
            return client.create_and_post_market_order(order_args=args)
        # Fallback for client builds without the convenience wrapper.
        from py_clob_client_v2 import OrderType  # type: ignore

        order = client.create_market_order(args)
        return client.post_order(order, OrderType.FOK)

    # ── order placement ──────────────────────────────────────────────────
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
        return await asyncio.to_thread(
            self._buy_sync,
            market=market,
            side=side,
            token_id=token_id,
            notional=notional,
            asks=asks,
            price_to_beat=price_to_beat,
            reason=reason,
        )

    def _buy_sync(
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
        params = self._params_for(market.asset)
        preview = PaperBroker.preview_buy(asks, notional, params.taker_fee_rate)
        local_order_id = uuid.uuid4().hex[:16]
        if preview is None:
            log.info("live.rejected_no_book", asset=market.asset, side=side)
            return None
        if preview.shares < params.min_order_shares:
            log.info(
                "live.rejected_min_shares",
                asset=market.asset,
                shares=round(preview.shares, 4),
                min_shares=params.min_order_shares,
            )
            return None
        if preview.shares < params.min_ask_shares:
            log.info(
                "live.rejected_thin_book",
                asset=market.asset,
                shares=round(preview.shares, 4),
                min_ask_shares=params.min_ask_shares,
            )
            return None

        client = self._ensure_client()
        try:
            response = self._place_market_order(client, token_id, notional)
        except Exception as exc:  # noqa: BLE001
            self._record_order(
                local_order_id,
                market,
                token_id,
                side,
                preview,
                status="error",
                reason=f"outcome={side}; {reason}; {exc}",
            )
            log.exception("live.order_failed", asset=market.asset, error=str(exc))
            raise

        if not isinstance(response, dict):
            self._record_order(
                local_order_id,
                market,
                token_id,
                side,
                preview,
                status="error",
                reason=f"outcome={side}; {reason}; unexpected response type",
            )
            raise RuntimeError(
                f"unexpected live order response: {type(response).__name__}"
            )
        data = response
        raw_order_id = (
            data.get("orderID") or data.get("orderId") or data.get("id") or ""
        )
        order_id = str(raw_order_id or local_order_id)
        success = data.get("success")
        error = str(data.get("errorMsg") or data.get("error") or "")
        if success is None and not raw_order_id and not error:
            self._record_order(
                order_id,
                market,
                token_id,
                side,
                preview,
                status="error",
                reason=f"outcome={side}; {reason}; unrecognized response",
            )
            raise RuntimeError("unrecognized live order response (no order id/status)")
        status_text = str(data.get("status") or "").lower()
        if success is False or (success is None and not raw_order_id and error):
            self._record_order(
                order_id,
                market,
                token_id,
                side,
                preview,
                status="rejected",
                reason=f"outcome={side}; {reason}; {error or 'rejected'}",
            )
            log.warning(
                "live.order_rejected",
                asset=market.asset,
                side=side,
                error=error,
                response=data,
            )
            return None
        if status_text in {"unmatched", "cancelled", "canceled", "invalid", "expired"}:
            self._record_order(
                order_id,
                market,
                token_id,
                side,
                preview,
                status="rejected",
                reason=f"outcome={side}; {reason}; {status_text}",
            )
            log.info(
                "live.order_unfilled",
                asset=market.asset,
                side=side,
                status=status_text,
            )
            return None

        size, spent, avg_price, fee = self._extract_fill(
            data, client, order_id, preview, params
        )
        if size <= 0:
            self._record_order(
                order_id,
                market,
                token_id,
                side,
                preview,
                status="rejected",
                reason=f"outcome={side}; {reason}; zero fill",
            )
            log.warning("live.zero_fill", asset=market.asset, side=side)
            return None

        now = time.time()
        self._record_order(
            order_id,
            market,
            token_id,
            side,
            preview,
            status="filled",
            reason=f"outcome={side}; {reason}; live",
            price=avg_price,
            size=size,
            filled_notional=spent,
        )
        self._db_insert_fill(
            order_id=order_id,
            ts=now,
            market=market,
            token_id=token_id,
            price=avg_price,
            size=size,
            fee=fee,
        )
        pos = PaperPosition(
            id=0,
            asset=market.asset,
            start_ts=market.start_ts,
            end_ts=market.end_ts,
            token_id=token_id,
            side=side.upper(),
            size=size,
            avg_price=avg_price,
            cost=spent + fee,
            fees=fee,
            entry_ts=now,
            price_to_beat=float(price_to_beat),
            mode="live",
        )
        if self._db is not None:
            pos.id = self._db.insert_position(pos)
        log.info(
            "live.filled",
            asset=market.asset,
            window=market.start_ts,
            side=side,
            order_id=order_id,
            shares=round(size, 4),
            avg_price=round(avg_price, 4),
            notional=round(spent, 4),
            fee=round(fee, 5),
            cost=round(pos.cost, 4),
        )
        return pos

    def _extract_fill(
        self,
        data: dict[str, Any],
        client: Any,
        order_id: str,
        preview: Any,
        params: StrategyParams,
    ) -> tuple[float, float, float, float]:
        """Return (shares, notional, avg_price, fee) for a BUY fill.

        The CLOB response reports ``takingAmount`` / ``makingAmount`` from the
        taker's perspective: for a BUY, ``makingAmount`` is USDC spent and
        ``takingAmount`` is shares received. A few client versions have
        returned them the other way around, so the ratio is sanity-checked.
        """
        status_text = str(data.get("status") or "").lower()
        taking = _num(data.get("takingAmount"))
        making = _num(data.get("makingAmount"))
        size = 0.0
        spent = 0.0
        if taking > 0 and making > 0:
            if 0.0 < making / taking < 1.0:
                size, spent = taking, making
            elif 0.0 < taking / making < 1.0:
                size, spent = making, taking
            else:
                size, spent = taking, making
        elif taking > 0:
            size = taking
        elif making > 0:
            spent = making

        if size <= 0 or spent <= 0:
            # Some response shapes expose the matched size directly.
            direct = _num(
                data.get("size_matched")
                or data.get("sizeMatched")
                or data.get("matchedAmount")
                or data.get("matched_amount")
            )
            if direct > 0:
                size = direct
                price = _num(data.get("price"))
                spent = size * price if price > 0 else size * preview.avg_price

        if size <= 0 or spent <= 0:
            detail: dict[str, Any] = {}
            detail_ok = False
            if order_id and hasattr(client, "get_order"):
                try:
                    raw_detail = client.get_order(order_id) or {}
                    if isinstance(raw_detail, dict):
                        detail = raw_detail
                        detail_ok = bool(raw_detail)
                except Exception:  # noqa: BLE001
                    detail = {}
            if detail_ok:
                size = _num(
                    detail.get("size_matched")
                    or detail.get("sizeMatched")
                    or detail.get("matched_amount")
                )
                if size <= 0:
                    # The exchange knows this order and reports no fill: do not
                    # invent a position from the local book preview.
                    return 0.0, 0.0, 0.0, 0.0
                price = _num(detail.get("price"))
                spent = size * price if price > 0 else size * preview.avg_price
            elif status_text in {"matched", "filled"}:
                # The response says matched but omits amounts and order detail
                # is unavailable. Use the local preview as a best effort, with
                # a loud warning so the operator can reconcile.
                size, spent = preview.shares, preview.notional
                log.warning(
                    "live.fill_estimated",
                    order_id=order_id,
                    shares=round(size, 4),
                    notional=round(spent, 4),
                    note="exchange response had no fill amount; using book preview",
                )
            else:
                return 0.0, 0.0, 0.0, 0.0

        if size <= 0 or spent <= 0:
            return 0.0, 0.0, 0.0, 0.0
        avg_price = spent / size
        fee = _num(data.get("fee"), -1.0)
        if fee < 0:
            fee = per_share_fee(params.taker_fee_rate, avg_price) * size
        return size, spent, avg_price, fee

    def _record_order(
        self,
        order_id: str,
        market: WindowMarket,
        token_id: str,
        side: str,
        preview: Any,
        *,
        status: str,
        reason: str,
        price: float | None = None,
        size: float | None = None,
        filled_notional: float | None = None,
    ) -> None:
        if self._db is None:
            return
        self._db.insert_order(
            order_id=order_id,
            ts=time.time(),
            asset=market.asset,
            start_ts=market.start_ts,
            token_id=token_id,
            side="BUY",
            price=preview.avg_price if price is None else price,
            size=preview.shares if size is None else size,
            notional=preview.notional if filled_notional is None else filled_notional,
            status=status,
            mode="live",
            reason=reason,
        )

    def _db_insert_fill(
        self,
        *,
        order_id: str,
        ts: float,
        market: WindowMarket,
        token_id: str,
        price: float,
        size: float,
        fee: float,
    ) -> None:
        if self._db is None:
            return
        self._db.insert_fill(
            fill_id=uuid.uuid4().hex[:16],
            order_id=order_id,
            ts=ts,
            asset=market.asset,
            start_ts=market.start_ts,
            token_id=token_id,
            side="BUY",
            price=price,
            size=size,
            fee=fee,
            role="TAKER",
            mode="live",
        )
