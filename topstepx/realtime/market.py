"""Market hub — live quotes, trades, and market depth.

Usage:
    hub = MarketHub(client.tokens)
    hub.on_quote(lambda contract_id, quote: print(quote.best_bid, quote.best_ask))
    await hub.start()
    await hub.subscribe_quotes("CON.F.US.EP.U26")
"""

from __future__ import annotations

from typing import Any, Callable

from ..auth import TokenManager
from ..config import RTC_BASE, Settings
from ..models import DepthEntry, MarketTrade, Quote
from .base import BaseHub


class MarketHub(BaseHub):
    def __init__(self, tokens: TokenManager, settings: Settings | None = None):
        rtc_base = settings.rtc_base if settings else RTC_BASE
        super().__init__(f"{rtc_base}/market", tokens)

        self._quote_callbacks: list[Callable[[str, Quote], Any]] = []
        self._trade_callbacks: list[Callable[[str, list[MarketTrade]], Any]] = []
        self._depth_callbacks: list[Callable[[str, list[DepthEntry]], Any]] = []

        self._register_event("GatewayQuote", _parse_quote, self._quote_callbacks)
        self._register_event("GatewayTrade", _parse_trades, self._trade_callbacks)
        self._register_event("GatewayDepth", _parse_depth, self._depth_callbacks)

    # -- callback registration (sync or async callables both work) ------------

    def on_quote(self, callback: Callable[[str, Quote], Any]) -> None:
        self._quote_callbacks.append(callback)

    def on_trade(self, callback: Callable[[str, list[MarketTrade]], Any]) -> None:
        self._trade_callbacks.append(callback)

    def on_depth(self, callback: Callable[[str, list[DepthEntry]], Any]) -> None:
        self._depth_callbacks.append(callback)

    # -- subscriptions ---------------------------------------------------------

    async def subscribe_quotes(self, contract_id: str) -> None:
        await self._subscribe("SubscribeContractQuotes", contract_id)

    async def unsubscribe_quotes(self, contract_id: str) -> None:
        await self._unsubscribe("UnsubscribeContractQuotes", contract_id)

    async def subscribe_trades(self, contract_id: str) -> None:
        await self._subscribe("SubscribeContractTrades", contract_id)

    async def unsubscribe_trades(self, contract_id: str) -> None:
        await self._unsubscribe("UnsubscribeContractTrades", contract_id)

    async def subscribe_depth(self, contract_id: str) -> None:
        await self._subscribe("SubscribeContractMarketDepth", contract_id)

    async def unsubscribe_depth(self, contract_id: str) -> None:
        await self._unsubscribe("UnsubscribeContractMarketDepth", contract_id)


# -- payload parsers: raw SignalR args -> (contract_id, model(s)) -------------


def _parse_quote(args: list) -> tuple[str, Quote]:
    contract_id, data = args[0], args[1]
    return contract_id, Quote.from_api(data or {})


def _parse_trades(args: list) -> tuple[str, list[MarketTrade]]:
    contract_id, data = args[0], args[1]
    items = data if isinstance(data, list) else [data]
    return contract_id, [MarketTrade.from_api(t or {}) for t in items]


def _parse_depth(args: list) -> tuple[str, list[DepthEntry]]:
    contract_id, data = args[0], args[1]
    items = data if isinstance(data, list) else [data]
    return contract_id, [DepthEntry.from_api(e or {}) for e in items]
