"""Regression guard: enum integer codes must match the official ProjectX
Gateway Swagger spec (https://api.topstepx.com/swagger/index.html).

Every member of every enum is pinned to its exact wire value — if any of
these ever drift, orders would be placed with the wrong type/side.
"""

from topstepx.enums import BarUnit, OrderSide, OrderStatus, OrderType, PositionType


def test_order_type_values():
    assert OrderType.UNKNOWN == 0
    assert OrderType.LIMIT == 1
    assert OrderType.MARKET == 2
    assert OrderType.STOP_LIMIT == 3
    assert OrderType.STOP == 4
    assert OrderType.TRAILING_STOP == 5
    assert OrderType.JOIN_BID == 6
    assert OrderType.JOIN_ASK == 7
    assert len(OrderType) == 8


def test_order_side_values():
    assert OrderSide.BUY == 0
    assert OrderSide.SELL == 1
    assert len(OrderSide) == 2


def test_order_status_values():
    assert OrderStatus.NONE == 0
    assert OrderStatus.OPEN == 1
    assert OrderStatus.FILLED == 2
    assert OrderStatus.CANCELLED == 3
    assert OrderStatus.EXPIRED == 4
    assert OrderStatus.REJECTED == 5
    assert OrderStatus.PENDING == 6
    assert OrderStatus.PENDING_CANCELLATION == 7
    assert OrderStatus.SUSPENDED == 8
    assert len(OrderStatus) == 9


def test_position_type_values():
    assert PositionType.UNDEFINED == 0
    assert PositionType.LONG == 1
    assert PositionType.SHORT == 2
    assert len(PositionType) == 3


def test_bar_unit_values():
    assert BarUnit.UNSPECIFIED == 0
    assert BarUnit.SECOND == 1
    assert BarUnit.MINUTE == 2
    assert BarUnit.HOUR == 3
    assert BarUnit.DAY == 4
    assert BarUnit.WEEK == 5
    assert BarUnit.MONTH == 6
    assert BarUnit.TICK == 7
    assert len(BarUnit) == 8


def test_int_comparison_and_conversion():
    # Raw ints from API payloads must compare equal to enum members ...
    assert OrderStatus.OPEN == 1
    assert OrderStatus(2) is OrderStatus.FILLED
    assert PositionType(1) is PositionType.LONG
    # ... and members must serialize as plain ints for request bodies.
    assert int(OrderType.STOP_LIMIT) == 3
    assert int(OrderSide.SELL) == 1
    assert isinstance(OrderType.MARKET, int)
