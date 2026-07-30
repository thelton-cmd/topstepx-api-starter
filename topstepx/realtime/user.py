"""User hub — live account, order, position, and trade events.

Usage:
    hub = UserHub(client.tokens)
    hub.on_order(lambda data: print("order update:", data))
    await hub.start()
    await hub.subscribe_orders(account_id)
"""

from __future__ import annotations

from typing import Any, Callable

from ..auth import TokenManager
from ..config import RTC_BASE, Settings
from .base import BaseHub


def _parse_passthrough(args: list) -> tuple:
    """User-hub events arrive as a single data payload; pass it through raw.

    Payload shapes vary slightly between event types, so callbacks
    receive the raw dict (with an ``action`` code and a ``data`` body
    where applicable). Parse what you need — or add a dataclass in
    models.py and a parser here, mirroring market.py.
    """
    return (args[0] if len(args) == 1 else tuple(args),)


class UserHub(BaseHub):
    def __init__(self, tokens: TokenManager, settings: Settings | None = None):
        rtc_base = settings.rtc_base if settings else RTC_BASE
        super().__init__(f"{rtc_base}/user", tokens)

        self._account_callbacks: list[Callable[[Any], Any]] = []
        self._order_callbacks: list[Callable[[Any], Any]] = []
        self._position_callbacks: list[Callable[[Any], Any]] = []
        self._trade_callbacks: list[Callable[[Any], Any]] = []

        self._register_event("GatewayUserAccount", _parse_passthrough, self._account_callbacks)
        self._register_event("GatewayUserOrder", _parse_passthrough, self._order_callbacks)
        self._register_event("GatewayUserPosition", _parse_passthrough, self._position_callbacks)
        self._register_event("GatewayUserTrade", _parse_passthrough, self._trade_callbacks)

    # -- callback registration (sync or async callables both work) ------------

    def on_account(self, callback: Callable[[Any], Any]) -> None:
        self._account_callbacks.append(callback)

    def on_order(self, callback: Callable[[Any], Any]) -> None:
        self._order_callbacks.append(callback)

    def on_position(self, callback: Callable[[Any], Any]) -> None:
        self._position_callbacks.append(callback)

    def on_user_trade(self, callback: Callable[[Any], Any]) -> None:
        self._trade_callbacks.append(callback)

    # -- subscriptions ---------------------------------------------------------

    async def subscribe_accounts(self) -> None:
        await self._subscribe("SubscribeAccounts")

    async def unsubscribe_accounts(self) -> None:
        await self._unsubscribe("UnsubscribeAccounts")

    async def subscribe_orders(self, account_id: int) -> None:
        await self._subscribe("SubscribeOrders", account_id)

    async def unsubscribe_orders(self, account_id: int) -> None:
        await self._unsubscribe("UnsubscribeOrders", account_id)

    async def subscribe_positions(self, account_id: int) -> None:
        await self._subscribe("SubscribePositions", account_id)

    async def unsubscribe_positions(self, account_id: int) -> None:
        await self._unsubscribe("UnsubscribePositions", account_id)

    async def subscribe_trades(self, account_id: int) -> None:
        await self._subscribe("SubscribeTrades", account_id)

    async def unsubscribe_trades(self, account_id: int) -> None:
        await self._unsubscribe("UnsubscribeTrades", account_id)
