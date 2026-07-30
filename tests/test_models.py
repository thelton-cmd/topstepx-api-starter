from datetime import datetime, timezone

from topstepx.models import (
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

from .fixtures import payloads


def test_account_parses():
    account = Account.from_api(payloads.ACCOUNT)
    assert account.id == 123
    assert account.balance == 50000.0
    assert account.can_trade is True


def test_contract_parses():
    contract = Contract.from_api(payloads.CONTRACT)
    assert contract.id == "CON.F.US.EP.U26"
    assert contract.tick_size == 0.25


def test_bar_parses_tz_aware_time():
    bar = Bar.from_api(payloads.BAR)
    assert bar.time == datetime(2026, 6, 12, 13, 30, tzinfo=timezone.utc)
    assert bar.high >= bar.low


def test_order_parses():
    order = Order.from_api(payloads.ORDER)
    assert order.id == 9001
    assert order.limit_price == 5000.0
    assert order.creation_timestamp is not None


def test_position_parses():
    position = Position.from_api(payloads.POSITION)
    assert position.size == 2
    assert position.average_price == 5302.0


def test_trade_parses():
    trade = Trade.from_api(payloads.TRADE)
    assert trade.profit_and_loss == 62.5
    assert trade.fees == 2.8


def test_half_turn_trade_has_no_pnl():
    trade = Trade.from_api(payloads.TRADE_HALF_TURN)
    assert trade.profit_and_loss is None  # opening trade: P&L not yet realized
    assert trade.fees == 2.8


def test_quote_parses():
    quote = Quote.from_api(payloads.QUOTE)
    assert quote.best_bid == 5301.0
    assert quote.best_ask == 5301.25
    assert quote.last_updated is not None


def test_market_trade_and_depth_parse():
    trade = MarketTrade.from_api(payloads.MARKET_TRADE)
    assert trade.price == 5301.25
    entry = DepthEntry.from_api(payloads.DEPTH_ENTRY)
    assert entry.volume == 42


def test_unknown_fields_ignored():
    quote = Quote.from_api({**payloads.QUOTE, "someNewField": 1, "another": {"x": 2}})
    assert quote.best_bid == 5301.0
    assert quote.raw["someNewField"] == 1  # raw payload always preserved


def test_empty_payload_does_not_crash():
    for model in (Account, Contract, Bar, Order, Position, Trade, Quote, MarketTrade, DepthEntry):
        model.from_api({})
