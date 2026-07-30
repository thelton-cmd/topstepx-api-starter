"""The REST facade — one class covering the full TopstepX REST API.

Every method is a thin wrapper: build the request body, POST it through
``ApiHttp`` (which handles auth, retries, and errors), and parse the
response into dataclasses. Adding a new endpoint is ~5 lines.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from .auth import TokenManager
from .config import Settings, load_settings
from .enums import BarUnit, OrderSide, OrderType
from .http import ApiHttp
from .models import Account, Bar, Contract, Order, Position, Trade


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
        body = self._http.post("/Contract/searchById", {"contractId": contract_id})
        return Contract.from_api(body.get("contract") or {})

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
        request.
        """
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
        body = self._http.post(
            "/Order/place",
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
        return body.get("orderId", 0)

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
        self._http.close()

    def __enter__(self) -> "TopstepXClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
