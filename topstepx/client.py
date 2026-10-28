"""The REST facade — one class covering the full TopstepX REST API.

Every method is a thin wrapper: build the request body, POST it through
``ApiHttp`` (which handles auth, retries, and errors), and parse the
response into dataclasses. Adding a new endpoint is ~5 lines.
"""

from __future__ import annotations

from datetime import datetime
import math

import httpx

from .auth import TokenManager
from .config import Settings, load_settings
from .enums import BarUnit, OrderSide, OrderType
from .exceptions import ApiError
from .http import ApiHttp
from .models import Account, Bar, Contract, Order, Position, Trade


def _require_aware_datetime(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def _require_positive_size(size: int) -> None:
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ValueError("size must be a positive integer")


def _coerce_order_type(value: OrderType) -> OrderType:
    try:
        order_type = OrderType(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("order_type must be a documented OrderType") from exc
    if order_type is OrderType.UNKNOWN:
        raise ValueError("OrderType.UNKNOWN cannot be used to place an order")
    return order_type


def _validate_order_prices(
    order_type: OrderType,
    *,
    limit_price: float | None,
    stop_price: float | None,
    trail_price: float | None,
) -> None:
    prices = {
        "limit_price": limit_price,
        "stop_price": stop_price,
        "trail_price": trail_price,
    }
    requirements = {
        OrderType.LIMIT: ({"limit_price"}, {"stop_price", "trail_price"}),
        OrderType.MARKET: (set(), set(prices)),
        OrderType.STOP_LIMIT: ({"limit_price", "stop_price"}, {"trail_price"}),
        OrderType.STOP: ({"stop_price"}, {"limit_price", "trail_price"}),
        OrderType.TRAILING_STOP: ({"trail_price"}, {"limit_price", "stop_price"}),
        OrderType.JOIN_BID: (set(), set(prices)),
        OrderType.JOIN_ASK: (set(), set(prices)),
    }
    required, forbidden = requirements[order_type]
    for name in required:
        if prices[name] is None:
            raise ValueError(f"{order_type.name} orders require {name}")
    for name in forbidden:
        if prices[name] is not None:
            raise ValueError(f"{order_type.name} orders do not accept {name}")
    for name, value in prices.items():
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{name} must be a positive finite number")

class TopstepXClient:
    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.BaseTransport | None = None,  # injection point for tests
    ):
        self.settings = settings or load_settings()
        self.tokens = TokenManager(self.settings)
        self._http = ApiHttp(self.settings, self.tokens, transport=transport)

    # -- contracts -----------------------------------------------------------

    def search_contracts(self, search_text: str, live: bool = False) -> list[Contract]:
        """Search contracts by text. Returns up to 20 matches."""
        body = self._http.post("/Contract/search", {"searchText": search_text, "live": live})
        return [Contract.from_api(c) for c in body.get("contracts", [])]

    def list_available_contracts(self, live: bool = False) -> list[Contract]:
        """List every contract available to trade.

        ``live=False`` selects the sim data subscription — the universe your
        practice account trades in.
        """
        body = self._http.post("/Contract/available", {"live": live})
        return [Contract.from_api(c) for c in body.get("contracts", [])]

    def get_contract(self, contract_id: str) -> Contract:
        path = "/Contract/searchById"
        body = self._http.post(path, {"contractId": contract_id})
        contract = body.get("contract")
        if (
            not isinstance(contract, dict)
            or not isinstance(contract.get("id"), str)
            or not contract["id"]
        ):
            raise ApiError(path, "response is missing the required contract.id", body=body)
        return Contract.from_api(contract)

    # -- history -------------------------------------------------------------

    def get_bars(
        self,
        contract_id: str,
        start: datetime,
        end: datetime,
        unit: BarUnit = BarUnit.MINUTE,
        unit_number: int = 5,
        limit: int = 1000,
        live: bool = False,
        include_partial_bar: bool = False,
    ) -> list[Bar]:
        """Fetch historical bars.

        Rate limit: /History/retrieveBars allows 50 requests per 30 seconds
        (all other endpoints: 200 per 60s); ApiHttp automatically backs off
        and retries on HTTP 429. The API returns at most 20,000 bars per
        request. Timestamps must include a UTC offset. The gateway returns bars
        newest-first.
        """
        _require_aware_datetime("start", start)
        _require_aware_datetime("end", end)
        if start >= end:
            raise ValueError("start must be earlier than end")
        try:
            unit = BarUnit(unit)
        except (TypeError, ValueError) as exc:
            raise ValueError("unit must be a documented BarUnit value (SECOND through TICK)") from exc
        if unit is BarUnit.UNSPECIFIED:
            raise ValueError("unit must be SECOND, MINUTE, HOUR, DAY, WEEK, MONTH, or TICK")
        if isinstance(unit_number, bool) or not isinstance(unit_number, int) or unit_number <= 0:
            raise ValueError("unit_number must be a positive integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20_000:
            raise ValueError("limit must be an integer from 1 through 20,000")

        body = self._http.post(
            "/History/retrieveBars",
            {
                "contractId": contract_id,
                "live": live,
                "startTime": start.isoformat(),
                "endTime": end.isoformat(),
                "unit": int(unit),
                "unitNumber": unit_number,
                "limit": limit,
                "includePartialBar": include_partial_bar,
            },
        )
        return [Bar.from_api(b) for b in body.get("bars", [])]

    # -- accounts ------------------------------------------------------------

    def search_accounts(self, only_active: bool = True) -> list[Account]:
        body = self._http.post("/Account/search", {"onlyActiveAccounts": only_active})
        return [Account.from_api(a) for a in body.get("accounts", [])]

    # -- orders ----------------------------------------------------------------

    def place_order(
        self,
        account_id: int,
        contract_id: str,
        order_type: OrderType,
        side: OrderSide,
        size: int,
        limit_price: float | None = None,
        stop_price: float | None = None,
        trail_price: float | None = None,
        custom_tag: str | None = None,
        stop_loss_bracket: dict | None = None,
        take_profit_bracket: dict | None = None,
    ) -> int:
        """Place an order with explicit, caller-supplied parameters. Returns the order id.

        Brackets are passed through as-is; the gateway expects
        ``{"ticks": int, "type": OrderType}`` (see the Swagger spec).
        """
        order_type = _coerce_order_type(order_type)
        try:
            side = OrderSide(side)
        except (TypeError, ValueError) as exc:
            raise ValueError("side must be OrderSide.BUY or OrderSide.SELL") from exc
        _require_positive_size(size)
        _validate_order_prices(
            order_type,
            limit_price=limit_price,
            stop_price=stop_price,
            trail_price=trail_price,
        )

        path = "/Order/place"
        body = self._http.post(
            path,
            {
                "accountId": account_id,
                "contractId": contract_id,
                "type": int(order_type),
                "side": int(side),
                "size": size,
                "limitPrice": limit_price,
                "stopPrice": stop_price,
                "trailPrice": trail_price,
                "customTag": custom_tag,
                "stopLossBracket": stop_loss_bracket,
                "takeProfitBracket": take_profit_bracket,
            },
        )
        order_id = body.get("orderId")
        if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id <= 0:
            raise ApiError(path, "response is missing a valid positive orderId", body=body)
        return order_id

    def cancel_order(self, account_id: int, order_id: int) -> None:
        self._http.post("/Order/cancel", {"accountId": account_id, "orderId": order_id})

    def modify_order(
        self,
        account_id: int,
        order_id: int,
        *,
        size: int | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
        trail_price: float | None = None,
    ) -> None:
        self._http.post(
            "/Order/modify",
            {
                "accountId": account_id,
                "orderId": order_id,
                "size": size,
                "limitPrice": limit_price,
                "stopPrice": stop_price,
                "trailPrice": trail_price,
            },
        )

    def search_orders(self, account_id: int, start: datetime, end: datetime | None = None) -> list[Order]:
        body = self._http.post(
            "/Order/search",
            {
                "accountId": account_id,
                "startTimestamp": start.isoformat(),
                "endTimestamp": end.isoformat() if end else None,
            },
        )
        return [Order.from_api(o) for o in body.get("orders", [])]

    def search_open_orders(self, account_id: int) -> list[Order]:
        body = self._http.post("/Order/searchOpen", {"accountId": account_id})
        return [Order.from_api(o) for o in body.get("orders", [])]

    # -- positions -------------------------------------------------------------

    def search_open_positions(self, account_id: int) -> list[Position]:
        body = self._http.post("/Position/searchOpen", {"accountId": account_id})
        return [Position.from_api(p) for p in body.get("positions", [])]

    def close_position(self, account_id: int, contract_id: str) -> None:
        self._http.post(
            "/Position/closeContract", {"accountId": account_id, "contractId": contract_id}
        )

    def partial_close_position(self, account_id: int, contract_id: str, size: int) -> None:
        self._http.post(
            "/Position/partialCloseContract",
            {"accountId": account_id, "contractId": contract_id, "size": size},
        )

    # -- trades ----------------------------------------------------------------

    def search_trades(self, account_id: int, start: datetime, end: datetime | None = None) -> list[Trade]:
        """Search executed trades. A trade with ``profit_and_loss is None``
        is a half-turn (position-opening) trade."""
        body = self._http.post(
            "/Trade/search",
            {
                "accountId": account_id,
                "startTimestamp": start.isoformat(),
                "endTimestamp": end.isoformat() if end else None,
            },
        )
        return [Trade.from_api(t) for t in body.get("trades", [])]

    def close(self) -> None:
        try:
            self._http.close()
        finally:
            self.tokens.close()

    def __enter__(self) -> "TopstepXClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
