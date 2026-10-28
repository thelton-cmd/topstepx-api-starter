"""Enums matching the ProjectX Gateway API integer codes.

Values are taken from the gateway's Swagger spec
(https://api.topstepx.com/swagger/index.html) — the source of truth if
these ever drift.
"""

from enum import IntEnum


class OrderType(IntEnum):
    UNKNOWN = 0  # returned by the API; not valid when placing an order
    LIMIT = 1
    MARKET = 2
    STOP_LIMIT = 3
    STOP = 4
    TRAILING_STOP = 5
    JOIN_BID = 6
    JOIN_ASK = 7


class OrderSide(IntEnum):
    """0 = Bid (buy), 1 = Ask (sell)."""

    BUY = 0
    SELL = 1


class OrderStatus(IntEnum):
    """Interprets ``Order.status`` (the model keeps the raw int)."""

    NONE = 0
    OPEN = 1
    FILLED = 2
    CANCELLED = 3
    EXPIRED = 4
    REJECTED = 5
    PENDING = 6
    PENDING_CANCELLATION = 7
    SUSPENDED = 8


class PositionType(IntEnum):
    """Interprets ``Position.type`` (the model keeps the raw int)."""

    UNDEFINED = 0
    LONG = 1
    SHORT = 2


class BarUnit(IntEnum):
    UNSPECIFIED = 0
    SECOND = 1
    MINUTE = 2
    HOUR = 3
    DAY = 4
    WEEK = 5
    MONTH = 6
    TICK = 7  # AggregateBarUnit.Tick in the Swagger spec — unit_number = ticks per bar
