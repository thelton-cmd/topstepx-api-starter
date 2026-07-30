"""Sample API payloads used by the tests.

Shapes mirror the ProjectX Gateway API envelopes. If you capture real
payloads while running the examples, update these to match reality.
"""

ACCOUNT = {
    "id": 123,
    "name": "PRACTICE-001",
    "balance": 50000.0,
    "canTrade": True,
    "isVisible": True,
    "simulated": True,
}

CONTRACT = {
    "id": "CON.F.US.EP.U26",
    "name": "ESU26",
    "description": "E-mini S&P 500: September 2026",
    "tickSize": 0.25,
    "tickValue": 12.5,
    "activeContract": True,
    "symbolId": "F.US.EP",
}

BAR = {
    "t": "2026-06-12T13:30:00+00:00",
    "o": 5301.25,
    "h": 5305.50,
    "l": 5299.00,
    "c": 5304.75,
    "v": 12345,
}

ORDER = {
    "id": 9001,
    "accountId": 123,
    "contractId": "CON.F.US.EP.U26",
    "creationTimestamp": "2026-06-12T14:00:00+00:00",
    "updateTimestamp": "2026-06-12T14:00:05+00:00",
    "status": 1,
    "type": 1,
    "side": 0,
    "size": 1,
    "limitPrice": 5000.0,
    "stopPrice": None,
}

POSITION = {
    "id": 5001,
    "accountId": 123,
    "contractId": "CON.F.US.EP.U26",
    "creationTimestamp": "2026-06-12T14:05:00+00:00",
    "type": 1,
    "size": 2,
    "averagePrice": 5302.0,
}

TRADE = {
    "id": 7001,
    "accountId": 123,
    "contractId": "CON.F.US.EP.U26",
    "creationTimestamp": "2026-06-12T14:05:00+00:00",
    "price": 5302.0,
    "profitAndLoss": 62.5,
    "fees": 2.8,
    "side": 0,
    "size": 1,
    "voided": False,
    "orderId": 9001,
}

# A half-turn (position-opening) trade: the gateway reports P&L as null
# until the closing trade completes the round turn.
TRADE_HALF_TURN = {**TRADE, "id": 7000, "profitAndLoss": None}

QUOTE = {
    "symbol": "F.US.EP",
    "bestBid": 5301.0,
    "bestAsk": 5301.25,
    "lastPrice": 5301.25,
    "change": 12.5,
    "changePercent": 0.24,
    "open": 5290.0,
    "high": 5310.0,
    "low": 5288.5,
    "volume": 250000,
    "lastUpdated": "2026-06-12T14:30:00+00:00",
    "timestamp": "2026-06-12T14:30:00+00:00",
}

MARKET_TRADE = {
    "symbolId": "F.US.EP",
    "price": 5301.25,
    "timestamp": "2026-06-12T14:30:00+00:00",
    "type": 0,
    "volume": 3,
}

DEPTH_ENTRY = {
    "timestamp": "2026-06-12T14:30:00+00:00",
    "type": 4,
    "price": 5301.0,
    "volume": 42,
    "currentVolume": 17,
}
