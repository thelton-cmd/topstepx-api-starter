"""Tests for the TopstepXClient REST facade.

REST traffic goes through the ``transport`` injection point on
``TopstepXClient``. The internal TokenManager owns a *separate*
httpx.Client (it never sees the injected transport), so the ``make_client``
fixture rewires ``client.tokens._http`` to a mock that serves
/Auth/loginKey — no test touches the network.
"""

import json
from datetime import datetime, timezone

import httpx
import pytest

from topstepx.client import TopstepXClient
from topstepx.enums import OrderSide, OrderType

from .conftest import make_jwt
from .fixtures import payloads

START = datetime(2026, 6, 12, 13, 0, tzinfo=timezone.utc)


class Recorder:
    """Handler that captures the request path + JSON body and returns a canned envelope."""

    def __init__(self, envelope: dict | None = None):
        self.envelope = {"success": True, "errorCode": 0, "errorMessage": None, **(envelope or {})}
        self.path: str | None = None
        self.body: dict | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.path = request.url.path
        self.body = json.loads(request.content)
        return httpx.Response(200, json=self.envelope)


@pytest.fixture
def make_client(settings, mock_client_factory):
    """Build a TopstepXClient whose REST and auth traffic both hit mocks."""

    def login_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/Auth/loginKey"
        return httpx.Response(200, json={"success": True, "token": make_jwt()})

    def factory(handler) -> TopstepXClient:
        client = TopstepXClient(settings, transport=httpx.MockTransport(handler))
        # TokenManager builds its own httpx.Client; swap it for a mock. This is
        # the same object ApiHttp holds, so one mutation covers both paths.
        client.tokens._http = mock_client_factory(login_handler)
        return client

    return factory


# -- orders --------------------------------------------------------------


def test_place_order_sends_plain_ints_and_returns_order_id(make_client):
    recorder = Recorder({"orderId": 9001})
    client = make_client(recorder)

    order_id = client.place_order(
        account_id=123,
        contract_id="CON.F.US.EP.U26",
        order_type=OrderType.MARKET,
        side=OrderSide.BUY,
        size=1,
    )

    assert recorder.path == "/Order/place"
    assert recorder.body["accountId"] == 123
    assert recorder.body["contractId"] == "CON.F.US.EP.U26"
    assert recorder.body["type"] == 2  # MARKET, serialized as a plain int
    assert recorder.body["side"] == 0  # BUY
    assert recorder.body["size"] == 1
    assert order_id == 9001


def test_place_order_stop_limit_serializes_type_3(make_client):
    recorder = Recorder({"orderId": 9002})
    client = make_client(recorder)

    order_id = client.place_order(
        account_id=123,
        contract_id="CON.F.US.EP.U26",
        order_type=OrderType.STOP_LIMIT,
        side=OrderSide.SELL,
        size=2,
        limit_price=5300.0,
        stop_price=5301.0,
    )

    assert recorder.path == "/Order/place"
    assert recorder.body["type"] == 3  # STOP_LIMIT per the Swagger spec
    assert recorder.body["side"] == 1  # SELL
    assert recorder.body["limitPrice"] == 5300.0
    assert recorder.body["stopPrice"] == 5301.0
    assert order_id == 9002


def test_place_order_forwards_brackets_verbatim(make_client):
    recorder = Recorder({"orderId": 9003})
    client = make_client(recorder)

    order_id = client.place_order(
        account_id=123,
        contract_id="CON.F.US.EP.U26",
        order_type=OrderType.MARKET,
        side=OrderSide.BUY,
        size=1,
        stop_loss_bracket={"ticks": 10, "type": 4},
        take_profit_bracket={"ticks": 20, "type": 1},
    )

    assert recorder.path == "/Order/place"
    assert recorder.body["stopLossBracket"] == {"ticks": 10, "type": 4}
    assert recorder.body["takeProfitBracket"] == {"ticks": 20, "type": 1}
    assert order_id == 9003


def test_place_order_omits_brackets_by_default(make_client):
    recorder = Recorder({"orderId": 9004})
    client = make_client(recorder)

    client.place_order(123, "CON.F.US.EP.U26", OrderType.MARKET, OrderSide.BUY, 1)

    assert recorder.body["stopLossBracket"] is None
    assert recorder.body["takeProfitBracket"] is None


def test_modify_order_posts_supplied_fields(make_client):
    recorder = Recorder()
    client = make_client(recorder)

    client.modify_order(123, 9001, size=3, limit_price=5299.5)

    assert recorder.path == "/Order/modify"
    assert recorder.body["accountId"] == 123
    assert recorder.body["orderId"] == 9001
    assert recorder.body["size"] == 3
    assert recorder.body["limitPrice"] == 5299.5
    assert recorder.body["stopPrice"] is None
    assert recorder.body["trailPrice"] is None


def test_cancel_order_posts_account_and_order_id(make_client):
    recorder = Recorder()
    client = make_client(recorder)

    client.cancel_order(123, 9001)

    assert recorder.path == "/Order/cancel"
    assert recorder.body == {"accountId": 123, "orderId": 9001}


# -- contracts -----------------------------------------------------------


def test_get_contract_posts_id_and_parses_contract_key(make_client):
    recorder = Recorder({"contract": payloads.CONTRACT})
    client = make_client(recorder)

    contract = client.get_contract("CON.F.US.EP.U26")

    assert recorder.path == "/Contract/searchById"
    assert recorder.body == {"contractId": "CON.F.US.EP.U26"}
    assert contract.id == "CON.F.US.EP.U26"
    assert contract.tick_size == 0.25


def test_list_available_contracts_posts_live_flag(make_client):
    recorder = Recorder({"contracts": [payloads.CONTRACT]})
    client = make_client(recorder)

    contracts = client.list_available_contracts(live=False)

    assert recorder.path == "/Contract/available"
    assert recorder.body == {"live": False}
    assert len(contracts) == 1
    assert contracts[0].id == "CON.F.US.EP.U26"


# -- positions -----------------------------------------------------------


def test_close_position_posts_contract(make_client):
    recorder = Recorder()
    client = make_client(recorder)

    client.close_position(123, "CON.F.US.EP.U26")

    assert recorder.path == "/Position/closeContract"
    assert recorder.body == {"accountId": 123, "contractId": "CON.F.US.EP.U26"}


def test_partial_close_position_posts_size(make_client):
    recorder = Recorder()
    client = make_client(recorder)

    client.partial_close_position(123, "CON.F.US.EP.U26", size=1)

    assert recorder.path == "/Position/partialCloseContract"
    assert recorder.body == {"accountId": 123, "contractId": "CON.F.US.EP.U26", "size": 1}


# -- trades --------------------------------------------------------------


def test_search_trades_parses_half_turn_pnl_as_none(make_client):
    recorder = Recorder({"trades": [payloads.TRADE_HALF_TURN, payloads.TRADE]})
    client = make_client(recorder)

    trades = client.search_trades(123, start=START)

    assert recorder.path == "/Trade/search"
    assert recorder.body["accountId"] == 123
    assert recorder.body["startTimestamp"] == START.isoformat()
    assert recorder.body["endTimestamp"] is None
    half_turn, round_turn = trades
    assert half_turn.profit_and_loss is None  # opening trade
    assert round_turn.profit_and_loss == 62.5
