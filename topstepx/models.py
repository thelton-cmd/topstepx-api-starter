"""Plain-dataclass models for API payloads.

Each model has a ``from_api()`` classmethod that maps the gateway's
camelCase keys and silently ignores unknown fields, so new fields added
by the API never break this kit. Swap these for Pydantic models if you
want validation — nothing else in the package would need to change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def _parse_time(value: Any) -> datetime | None:
    """Parse the gateway's ISO-8601 timestamps (e.g. '2026-06-12T13:30:00+00:00')."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class Account:
    id: int
    name: str = ""
    balance: float | None = None
    can_trade: bool | None = None
    is_visible: bool | None = None
    simulated: bool | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: dict) -> "Account":
        return cls(
            id=d.get("id", 0),
            name=d.get("name", ""),
            balance=d.get("balance"),
            can_trade=d.get("canTrade"),
            is_visible=d.get("isVisible"),
            simulated=d.get("simulated"),
            raw=d,
        )


@dataclass
class Contract:
    id: str
    name: str = ""
    description: str = ""
    tick_size: float | None = None
    tick_value: float | None = None
    active_contract: bool | None = None
    symbol_id: str | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: dict) -> "Contract":
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            description=d.get("description", ""),
            tick_size=d.get("tickSize"),
            tick_value=d.get("tickValue"),
            active_contract=d.get("activeContract"),
            symbol_id=d.get("symbolId"),
            raw=d,
        )


@dataclass
class Bar:
    time: datetime | None
    open: float
    high: float
    low: float
    close: float
    volume: float
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: dict) -> "Bar":
        return cls(
            time=_parse_time(d.get("t")),
            open=d.get("o", 0.0),
            high=d.get("h", 0.0),
            low=d.get("l", 0.0),
            close=d.get("c", 0.0),
            volume=d.get("v", 0.0),
            raw=d,
        )


@dataclass
class Order:
    id: int
    account_id: int | None = None
    contract_id: str | None = None
    status: int | None = None  # interpret with enums.OrderStatus
    type: int | None = None  # interpret with enums.OrderType
    side: int | None = None  # interpret with enums.OrderSide
    size: int | None = None
    limit_price: float | None = None
    stop_price: float | None = None
    creation_timestamp: datetime | None = None
    update_timestamp: datetime | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: dict) -> "Order":
        return cls(
            id=d.get("id", 0),
            account_id=d.get("accountId"),
            contract_id=d.get("contractId"),
            status=d.get("status"),
            type=d.get("type"),
            side=d.get("side"),
            size=d.get("size"),
            limit_price=d.get("limitPrice"),
            stop_price=d.get("stopPrice"),
            creation_timestamp=_parse_time(d.get("creationTimestamp")),
            update_timestamp=_parse_time(d.get("updateTimestamp")),
            raw=d,
        )


@dataclass
class Position:
    id: int
    account_id: int | None = None
    contract_id: str | None = None
    type: int | None = None  # interpret with enums.PositionType (1 = long, 2 = short)
    size: int | None = None
    average_price: float | None = None
    creation_timestamp: datetime | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: dict) -> "Position":
        return cls(
            id=d.get("id", 0),
            account_id=d.get("accountId"),
            contract_id=d.get("contractId"),
            type=d.get("type"),
            size=d.get("size"),
            average_price=d.get("averagePrice"),
            creation_timestamp=_parse_time(d.get("creationTimestamp")),
            raw=d,
        )


@dataclass
class Trade:
    id: int
    account_id: int | None = None
    contract_id: str | None = None
    price: float | None = None
    size: int | None = None
    side: int | None = None
    profit_and_loss: float | None = None  # None for a half-turn (opening) trade
    fees: float | None = None
    voided: bool | None = None
    order_id: int | None = None
    creation_timestamp: datetime | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: dict) -> "Trade":
        return cls(
            id=d.get("id", 0),
            account_id=d.get("accountId"),
            contract_id=d.get("contractId"),
            price=d.get("price"),
            size=d.get("size"),
            side=d.get("side"),
            profit_and_loss=d.get("profitAndLoss"),
            fees=d.get("fees"),
            voided=d.get("voided"),
            order_id=d.get("orderId"),
            creation_timestamp=_parse_time(d.get("creationTimestamp")),
            raw=d,
        )


# -- real-time payloads ------------------------------------------------------


@dataclass
class Quote:
    symbol: str | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    last_price: float | None = None
    change: float | None = None
    change_percent: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    last_updated: datetime | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: dict) -> "Quote":
        return cls(
            symbol=d.get("symbol"),
            best_bid=d.get("bestBid"),
            best_ask=d.get("bestAsk"),
            last_price=d.get("lastPrice"),
            change=d.get("change"),
            change_percent=d.get("changePercent"),
            open=d.get("open"),
            high=d.get("high"),
            low=d.get("low"),
            volume=d.get("volume"),
            last_updated=_parse_time(d.get("lastUpdated")),
            raw=d,
        )


@dataclass
class MarketTrade:
    symbol_id: str | None = None
    price: float | None = None
    volume: float | None = None
    type: int | None = None  # 0 = buy, 1 = sell
    timestamp: datetime | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: dict) -> "MarketTrade":
        return cls(
            symbol_id=d.get("symbolId"),
            price=d.get("price"),
            volume=d.get("volume"),
            type=d.get("type"),
            timestamp=_parse_time(d.get("timestamp")),
            raw=d,
        )


@dataclass
class DepthEntry:
    price: float | None = None
    volume: float | None = None
    type: int | None = None  # DOM type code (bid/ask/etc.)
    current_volume: float | None = None
    timestamp: datetime | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: dict) -> "DepthEntry":
        return cls(
            price=d.get("price"),
            volume=d.get("volume"),
            type=d.get("type"),
            current_volume=d.get("currentVolume"),
            timestamp=_parse_time(d.get("timestamp")),
            raw=d,
        )
