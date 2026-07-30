"""TopstepX API starter kit.

Quick tour:
    from topstepx import TopstepXClient, MarketHub

    client = TopstepXClient()                       # reads .env
    contracts = client.search_contracts("ES")       # REST
    hub = MarketHub(client.tokens)                  # real-time (asyncio)

Each module is independent — swap or extend any piece. See examples/.
"""

from .auth import TokenManager
from .client import TopstepXClient
from .config import Settings, load_settings
from .enums import BarUnit, OrderSide, OrderStatus, OrderType, PositionType
from .exceptions import ApiError, AuthenticationError, RateLimitError, TopstepXError
from .models import (
    Account,
    Bar,
    Contract,
    DepthEntry,
    MarketTrade,
    Order,
    Position,
    Quote,
    Trade,
)
from .realtime import MarketHub, UserHub

__all__ = [
    "TopstepXClient",
    "TokenManager",
    "Settings",
    "load_settings",
    "MarketHub",
    "UserHub",
    "OrderType",
    "OrderSide",
    "OrderStatus",
    "PositionType",
    "BarUnit",
    "TopstepXError",
    "AuthenticationError",
    "RateLimitError",
    "ApiError",
    "Account",
    "Contract",
    "Bar",
    "Order",
    "Position",
    "Trade",
    "Quote",
    "MarketTrade",
    "DepthEntry",
]
