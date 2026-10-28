"""Offline tests for dashboard/orders.py: ticket parsing, preflight rules, the practice-only
guard and the send helpers (through a TopstepXClient whose transport is a mock).

No test here, or anywhere in this kit, touches the real API.
"""

import json
from dataclasses import replace

import httpx
import pytest

from dashboard import orders
from dashboard.orders import OrderTicket, cancel_order, close_position, parse_ticket, preflight, send_order
from topstepx.client import TopstepXClient

from .conftest import make_jwt

PRACTICE = {"id": 7, "name": "PRAC-1", "balance": 50_000.0, "simulated": True, "can_trade": True}
FUNDED = {"id": 8, "name": "FUNDED-1", "balance": 100_000.0, "simulated": False, "can_trade": True}
HALTED = {"id": 9, "name": "PRAC-2", "balance": 49_000.0, "simulated": True, "can_trade": False}
ES = {"id": "CON.F.US.EP.Z26", "name": "ESZ26", "description": "E-mini S&P 500", "tick_size": 0.25, "tick_value": 12.5}

BODY = {"account_id": 7, "contract_id": ES["id"], "side": "buy", "order_type": "limit", "size": 2, "limit_price": 5000.25}


def ticket(**overrides) -> OrderTicket:
    base = OrderTicket(account_id=7, contract_id=ES["id"], side="buy", order_type="limit", size=2, limit_price=5000.25)
    return replace(base, **overrides)


# -- parse_ticket ------------------------------------------------------------------------


def test_parse_ticket_accepts_a_full_body_and_normalizes_text():
    parsed = parse_ticket({**BODY, "side": " BUY ", "order_type": "Limit", "stop_price": ""})
    assert parsed == OrderTicket(account_id=7, contract_id=ES["id"], side="buy", order_type="limit", size=2, limit_price=5000.25, stop_price=None)


def test_parse_ticket_accepts_numeric_strings_from_form_fields():
    parsed = parse_ticket({**BODY, "account_id": "7", "size": "3", "limit_price": "5000.25", "contract_id": f" {ES['id']} "})
    assert (parsed.account_id, parsed.size, parsed.limit_price, parsed.contract_id) == (7, 3, 5000.25, ES["id"])


def test_parse_ticket_market_has_no_prices():
    parsed = parse_ticket({"account_id": 7, "contract_id": ES["id"], "side": "sell", "order_type": "market", "size": 1})
    assert parsed.limit_price is None and parsed.stop_price is None and parsed.side == "sell"


@pytest.mark.parametrize(
    "body, fragment",
    [
        ("not a dict", "JSON object"),
        ({**BODY, "contract_id": ""}, "contract"),
        ({k: v for k, v in BODY.items() if k != "contract_id"}, "contract"),
        ({**BODY, "account_id": "seven"}, "Account id"),
        ({**BODY, "account_id": 7.5}, "Account id"),
        ({**BODY, "account_id": True}, "Account id"),
        ({**BODY, "side": "long"}, "Side"),
        ({**BODY, "side": None}, "Side"),
        ({**BODY, "order_type": "trailing_stop"}, "Order type"),
        ({**BODY, "size": "two"}, "Size"),
        ({**BODY, "size": 1.5}, "Size"),
        ({**BODY, "limit_price": "abc"}, "Limit price"),
        ({**BODY, "limit_price": -1}, "Limit price"),
        ({**BODY, "limit_price": 0}, "Limit price"),
        ({**BODY, "limit_price": float("inf")}, "Limit price"),
        ({**BODY, "limit_price": True}, "Limit price"),
        ({**BODY, "stop_price": "nan"}, "Stop price"),
        ({**BODY, "stop_price": [5000]}, "Stop price"),
    ],
)
def test_parse_ticket_rejects_bad_fields_with_plain_messages(body, fragment):
    with pytest.raises(ValueError) as err:
        parse_ticket(body)
    assert fragment in str(err.value)


# -- preflight ---------------------------------------------------------------------------


def test_preflight_summary_for_a_limit_order():
    summary = preflight(ticket(), PRACTICE, ES)
    assert summary["custom_tag"].startswith("dash-") and len(summary["custom_tag"]) == 5 + 32
    assert summary == {
        **summary,
        "account": "PRAC-1", "account_id": 7, "simulated": True, "can_trade": True,
        "contract": "ESZ26", "contract_id": ES["id"], "side": "BUY", "type": "LIMIT", "size": 2,
        "limit_price": 5000.25, "stop_price": None, "tick_size": 0.25,
        "warning": "Topstep states that API orders are final and cannot be reviewed or reversed.",
    }
    assert preflight(ticket(), PRACTICE, ES)["custom_tag"] != summary["custom_tag"]


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"order_type": "market", "limit_price": None}, ("MARKET", None, None)),
        ({"order_type": "stop", "limit_price": None, "stop_price": 5010.0, "side": "sell"}, ("STOP", None, 5010.0)),
        ({"order_type": "stop_limit", "limit_price": 5010.25, "stop_price": 5010.0}, ("STOP_LIMIT", 5010.25, 5010.0)),
    ],
)
def test_preflight_maps_every_order_type(kwargs, expected):
    summary = preflight(ticket(**kwargs), PRACTICE, ES)
    assert (summary["type"], summary["limit_price"], summary["stop_price"]) == expected
    assert summary["side"] == ("SELL" if kwargs.get("side") == "sell" else "BUY")


@pytest.mark.parametrize("account", [None, {}, {"name": "no id"}, FUNDED, HALTED, {**PRACTICE, "simulated": None}, {**PRACTICE, "can_trade": None}, {**PRACTICE, "simulated": 1}])
def test_preflight_rejects_accounts_that_are_not_practice(account):
    with pytest.raises(ValueError) as err:
        preflight(ticket(), account, ES)
    message = str(err.value)
    assert "account" in message.lower()
    if account and account.get("id") is not None:
        assert message == "Order entry is only enabled for Practice Accounts (the API marks them simulated)."


def test_preflight_rejects_an_account_mismatch():
    with pytest.raises(ValueError, match="does not match the selected account"):
        preflight(ticket(account_id=8), PRACTICE, ES)


@pytest.mark.parametrize("contract", [None, {}, {**ES, "id": "CON.F.US.ENQ.Z26"}, {**ES, "active_contract": False}])
def test_preflight_rejects_missing_or_inactive_contracts(contract):
    with pytest.raises(ValueError) as err:
        preflight(ticket(), PRACTICE, contract)
    assert "ontract" in str(err.value)


def test_preflight_accepts_a_contract_that_is_explicitly_active():
    assert preflight(ticket(), PRACTICE, {**ES, "active_contract": True})["contract"] == "ESZ26"


@pytest.mark.parametrize("size, fragment", [(0, "at least 1"), (-1, "at least 1"), (51, "50 or fewer"), (1000, "50 or fewer")])
def test_preflight_rejects_sizes_outside_1_to_50(size, fragment):
    with pytest.raises(ValueError, match=fragment):
        preflight(ticket(size=size), PRACTICE, ES)


def test_preflight_accepts_size_bounds():
    assert preflight(ticket(size=1), PRACTICE, ES)["size"] == 1
    assert preflight(ticket(size=50), PRACTICE, ES)["size"] == 50


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        ({"order_type": "market"}, "MARKET orders do not take a limit price"),
        ({"order_type": "market", "limit_price": None, "stop_price": 5000.0}, "MARKET orders do not take a stop price"),
        ({"order_type": "limit", "limit_price": None}, "LIMIT orders need a limit price"),
        ({"order_type": "limit", "stop_price": 5000.0}, "LIMIT orders do not take a stop price"),
        ({"order_type": "stop", "limit_price": None}, "STOP orders need a stop price"),
        ({"order_type": "stop", "stop_price": 5000.0}, "STOP orders do not take a limit price"),
        ({"order_type": "stop_limit"}, "STOP LIMIT orders need a stop price"),
        ({"order_type": "stop_limit", "limit_price": None, "stop_price": 5000.0}, "STOP LIMIT orders need a limit price"),
    ],
)
def test_preflight_enforces_price_rules_per_type(kwargs, fragment):
    with pytest.raises(ValueError, match=fragment):
        preflight(ticket(**kwargs), PRACTICE, ES)


@pytest.mark.parametrize("kwargs", [{"side": "long"}, {"order_type": "trailing_stop"}, {"limit_price": -5.0}])
def test_preflight_rejects_tickets_built_outside_parse_ticket(kwargs):
    with pytest.raises(ValueError):
        preflight(ticket(**kwargs), PRACTICE, ES)


@pytest.mark.parametrize(
    "kwargs, label",
    [
        ({"limit_price": 5000.1}, "Limit price"),
        ({"limit_price": 5000.3}, "Limit price"),
        ({"order_type": "stop", "limit_price": None, "stop_price": 4999.99}, "Stop price"),
        ({"order_type": "stop_limit", "limit_price": 5000.25, "stop_price": 5000.2}, "Stop price"),
    ],
)
def test_preflight_rejects_prices_off_the_tick_grid(kwargs, label):
    with pytest.raises(ValueError, match=f"{label} .* is not aligned to this contract's 0.25 tick size"):
        preflight(ticket(**kwargs), PRACTICE, ES)


def test_preflight_tick_alignment_uses_decimal_not_float_remainder():
    assert preflight(ticket(limit_price=5000.75), PRACTICE, ES)["limit_price"] == 5000.75
    nq = {**ES, "id": "CON.F.US.ENQ.Z26", "name": "NQZ26", "tick_size": 0.25}
    assert preflight(ticket(contract_id=nq["id"], limit_price=20_001.5), PRACTICE, nq)["tick_size"] == 0.25
    tenth = {**ES, "tick_size": 0.1}
    assert preflight(ticket(limit_price=5000.3), PRACTICE, tenth)["limit_price"] == 5000.3  # 0.3 / 0.1 is exact in Decimal


@pytest.mark.parametrize("tick_size", [None, 0, -0.25])
def test_preflight_needs_a_usable_tick_size_when_a_price_is_given(tick_size):
    with pytest.raises(ValueError, match="tick size"):
        preflight(ticket(), PRACTICE, {**ES, "tick_size": tick_size})
    assert preflight(ticket(order_type="market", limit_price=None), PRACTICE, {**ES, "tick_size": tick_size})["type"] == "MARKET"


# -- send helpers through a mock transport ------------------------------------------------


class Recorder:
    """Handler that captures every request's path + JSON body and returns a canned envelope."""

    def __init__(self, envelope: dict | None = None):
        self.envelope = {"success": True, "errorCode": 0, "errorMessage": None, **(envelope or {})}
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=self.envelope)


@pytest.fixture
def make_client(settings, mock_client_factory):
    """A TopstepXClient whose REST and login traffic both hit mocks (see tests/test_client.py)."""

    def login_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/Auth/loginKey"
        return httpx.Response(200, json={"success": True, "token": make_jwt()})

    def factory(handler) -> TopstepXClient:
        client = TopstepXClient(settings, transport=httpx.MockTransport(handler))
        client.tokens._http = mock_client_factory(login_handler)
        return client

    return factory


def test_send_order_posts_the_exact_body_and_returns_the_order_id(make_client):
    recorder = Recorder({"orderId": 123})
    client = make_client(recorder)
    summary = preflight(ticket(), PRACTICE, ES)

    assert send_order(client, ticket(), summary) == 123

    assert [path for path, _ in recorder.requests] == ["/Order/place"]
    assert recorder.requests[0][1] == {
        "accountId": 7,
        "contractId": ES["id"],
        "type": 1,
        "side": 0,
        "size": 2,
        "limitPrice": 5000.25,
        "stopPrice": None,
        "trailPrice": None,
        "customTag": summary["custom_tag"],
        "stopLossBracket": None,
        "takeProfitBracket": None,
    }


@pytest.mark.parametrize(
    "kwargs, type_code, side_code, limit, stop",
    [
        ({"order_type": "market", "limit_price": None, "side": "sell"}, 2, 1, None, None),
        ({"order_type": "stop", "limit_price": None, "stop_price": 5010.0}, 4, 0, None, 5010.0),
        ({"order_type": "stop_limit", "limit_price": 5010.25, "stop_price": 5010.0, "side": "sell"}, 3, 1, 5010.25, 5010.0),
    ],
)
def test_send_order_maps_types_and_sides_to_gateway_codes(make_client, kwargs, type_code, side_code, limit, stop):
    recorder = Recorder({"orderId": 55})
    client = make_client(recorder)
    t = ticket(**kwargs)
    send_order(client, t, preflight(t, PRACTICE, ES))
    body = recorder.requests[0][1]
    assert (body["type"], body["side"], body["limitPrice"], body["stopPrice"]) == (type_code, side_code, limit, stop)


def test_send_order_refuses_a_summary_that_is_not_practice_and_sends_nothing(make_client):
    recorder = Recorder({"orderId": 123})
    client = make_client(recorder)
    summary = preflight(ticket(), PRACTICE, ES)

    for tampered in ({**summary, "simulated": False}, {**summary, "can_trade": False}, {**summary, "simulated": None}, {**summary, "account_id": 8}, None):
        with pytest.raises(PermissionError):
            send_order(client, ticket(), tampered)
    assert recorder.requests == []


def test_send_order_never_reaches_the_transport_for_a_funded_account(make_client):
    recorder = Recorder({"orderId": 123})
    client = make_client(recorder)
    with pytest.raises(ValueError, match="Practice Accounts"):
        preflight(ticket(account_id=8), FUNDED, ES)
    assert recorder.requests == []


def test_send_order_rejects_a_ticket_that_skipped_preflight(make_client):
    recorder = Recorder({"orderId": 123})
    client = make_client(recorder)
    summary = preflight(ticket(), PRACTICE, ES)
    with pytest.raises(ValueError):
        send_order(client, ticket(side="long"), summary)
    assert recorder.requests == []


def test_cancel_order_posts_account_and_order_id(make_client):
    recorder = Recorder()
    client = make_client(recorder)
    cancel_order(client, PRACTICE, 9001)
    assert recorder.requests == [("/Order/cancel", {"accountId": 7, "orderId": 9001})]


def test_close_position_posts_account_and_contract(make_client):
    recorder = Recorder()
    client = make_client(recorder)
    close_position(client, PRACTICE, f" {ES['id']} ")
    assert recorder.requests == [("/Position/closeContract", {"accountId": 7, "contractId": ES["id"]})]


@pytest.mark.parametrize("account", [FUNDED, HALTED, {**PRACTICE, "simulated": None}, {**PRACTICE, "can_trade": "yes"}, {}, None])
def test_cancel_and_close_refuse_non_practice_accounts_and_send_nothing(make_client, account):
    recorder = Recorder()
    client = make_client(recorder)
    with pytest.raises(PermissionError):
        cancel_order(client, account, 9001)
    with pytest.raises(PermissionError):
        close_position(client, account, ES["id"])
    assert recorder.requests == []


@pytest.mark.parametrize("order_id", [0, -1, "9001", True, None])
def test_cancel_order_validates_the_order_id(make_client, order_id):
    recorder = Recorder()
    client = make_client(recorder)
    with pytest.raises(ValueError):
        cancel_order(client, PRACTICE, order_id)
    assert recorder.requests == []


@pytest.mark.parametrize("contract_id", ["", "   ", None, 5])
def test_close_position_validates_the_contract_id(make_client, contract_id):
    recorder = Recorder()
    client = make_client(recorder)
    with pytest.raises(ValueError):
        close_position(client, PRACTICE, contract_id)
    assert recorder.requests == []


# -- the module itself ----------------------------------------------------------------------


def test_warning_and_practice_message_are_the_contract_strings():
    assert orders.WARNING == "Topstep states that API orders are final and cannot be reviewed or reversed."
    assert orders.PRACTICE_ONLY.startswith("Order entry is only enabled for Practice Accounts (the API marks them simulated)")
    assert orders.MAX_SIZE == 50


def test_orders_module_never_calls_modify_or_partial_close():
    from pathlib import Path

    source = (Path(orders.__file__)).read_text(encoding="utf-8")
    assert "modify_order" not in source and "partial_close_position" not in source
    source.encode("ascii")  # comments and messages stay ASCII for Windows consoles
    (Path(orders.__file__).with_name("state.py")).read_text(encoding="utf-8").encode("ascii")


# -- brackets: parsing ---------------------------------------------------------------------


def test_parse_ticket_bracket_fields_default_to_none_and_blank_means_none():
    assert parse_ticket(BODY).stop_ticks is None and parse_ticket(BODY).target_ticks is None
    parsed = parse_ticket({**BODY, "stop_ticks": "", "target_ticks": None})
    assert (parsed.stop_ticks, parsed.target_ticks) == (None, None)
    parsed = parse_ticket({**BODY, "stop_ticks": "  ", "target_ticks": "   "})
    assert (parsed.stop_ticks, parsed.target_ticks) == (None, None)


def test_parse_ticket_accepts_bracket_ticks_from_1_to_1000_as_ints_or_digit_strings():
    parsed = parse_ticket({**BODY, "stop_ticks": 1, "target_ticks": 1000})
    assert (parsed.stop_ticks, parsed.target_ticks) == (1, 1000)
    parsed = parse_ticket({**BODY, "stop_ticks": " 12 ", "target_ticks": "40"})
    assert (parsed.stop_ticks, parsed.target_ticks) == (12, 40)


@pytest.mark.parametrize("name, label", [("stop_ticks", "Stop (ticks)"), ("target_ticks", "Target (ticks)")])
@pytest.mark.parametrize("value", [0, -1, 1001, 10_000, "0", "-5", "1001", "1.5", "ten", 2.5, 10.0, True, False, [10], {"t": 1}])
def test_parse_ticket_rejects_bracket_ticks_outside_the_bounds(name, label, value):
    with pytest.raises(ValueError) as err:
        parse_ticket({**BODY, name: value})
    assert str(err.value) == f"{label} must be a whole number from 1 to 1000, or blank for none."


# -- brackets: the gateway mapping -----------------------------------------------------------


@pytest.mark.parametrize(
    "side, stop, target, expected",
    [
        ("buy", 10, 20, ({"ticks": -10, "type": 4}, {"ticks": 20, "type": 1})),
        ("sell", 10, 20, ({"ticks": 10, "type": 4}, {"ticks": -20, "type": 1})),
        ("buy", 8, None, ({"ticks": -8, "type": 4}, None)),
        ("sell", None, 15, (None, {"ticks": -15, "type": 1})),
        ("buy", None, None, (None, None)),
    ],
)
def test_bracket_requests_sign_ticks_from_the_entry_and_use_stop_and_limit_legs(side, stop, target, expected):
    assert orders.bracket_requests(ticket(side=side, stop_ticks=stop, target_ticks=target)) == expected
    for leg in expected:
        if leg is not None:
            assert type(leg["ticks"]) is int and type(leg["type"]) is int  # plain JSON numbers


# -- brackets: preflight legs and summary ----------------------------------------------------


def test_preflight_shows_bracket_legs_measured_from_a_limit_entry():
    summary = preflight(ticket(stop_ticks=10, target_ticks=20), PRACTICE, ES)
    assert summary["stop_ticks"] == 10 and summary["target_ticks"] == 20
    assert summary["bracket_note"] == orders.BRACKET_NOTE and "Auto OCO Brackets" in orders.BRACKET_NOTE
    assert summary["warning"] == orders.WARNING
    assert summary["brackets"] == {
        "reference_price": 5000.25, "basis": "limit price", "approx": False,
        "stop_loss": {"ticks": 10, "gateway_ticks": -10, "type": "STOP", "type_code": 4, "direction": "below", "price": 4997.75},
        "take_profit": {"ticks": 20, "gateway_ticks": 20, "type": "LIMIT", "type_code": 1, "direction": "above", "price": 5005.25},
    }


def test_preflight_bracket_legs_for_sell_stop_and_stop_limit_entries():
    sell_stop = preflight(ticket(side="sell", order_type="stop", limit_price=None, stop_price=4990.0,
                                 stop_ticks=8, target_ticks=12), PRACTICE, ES)["brackets"]
    assert sell_stop["basis"] == "stop price" and sell_stop["reference_price"] == 4990.0
    assert sell_stop["stop_loss"]["price"] == 4992.0 and sell_stop["stop_loss"]["direction"] == "above"
    assert sell_stop["stop_loss"]["gateway_ticks"] == 8
    assert sell_stop["take_profit"]["price"] == 4987.0 and sell_stop["take_profit"]["gateway_ticks"] == -12

    stop_limit = preflight(ticket(order_type="stop_limit", limit_price=5010.25, stop_price=5010.0, target_ticks=4),
                           PRACTICE, ES)["brackets"]
    assert stop_limit["basis"] == "limit price" and stop_limit["stop_loss"] is None
    assert stop_limit["take_profit"]["price"] == 5011.25


def test_preflight_market_bracket_legs_are_approx_from_the_last_price_snapped_to_the_grid():
    market = ticket(order_type="market", limit_price=None, stop_ticks=4, target_ticks=6)
    legs = preflight(market, PRACTICE, ES, last_price=5000.3)["brackets"]  # 5000.3 is off the 0.25 grid
    assert legs["approx"] is True and legs["basis"] == "last price" and legs["reference_price"] == 5000.25
    assert legs["stop_loss"]["price"] == 4999.25 and legs["take_profit"]["price"] == 5001.75

    unknown = preflight(market, PRACTICE, ES)["brackets"]   # no last price yet
    assert unknown["approx"] is True and unknown["reference_price"] is None
    assert unknown["stop_loss"]["price"] is None and unknown["stop_loss"]["gateway_ticks"] == -4
    for bad_last in (0, -1.0, float("nan"), float("inf"), "5000", True):
        assert preflight(market, PRACTICE, ES, last_price=bad_last)["brackets"]["reference_price"] is None


@pytest.mark.parametrize("tick_size, entry", [(0.25, 5000.25), (0.01, 70.13), (0.1, 5000.3), (0.005, 1.105), (1, 4321), (0.03125, 110.15625)])
def test_computed_bracket_legs_are_always_tick_aligned(tick_size, entry):
    from decimal import Decimal

    contract = {**ES, "tick_size": tick_size}
    for side in ("buy", "sell"):
        for ticks in (1, 3, 7, 10, 33, 100):
            legs = preflight(ticket(side=side, limit_price=entry, stop_ticks=ticks, target_ticks=ticks), PRACTICE, contract)["brackets"]
            for key in ("stop_loss", "take_profit"):
                price = legs[key]["price"]
                steps = Decimal(str(price)) / Decimal(str(tick_size))
                assert steps == steps.to_integral_value(), (side, ticks, key, price)
                expected = Decimal(str(entry)) + (1 if legs[key]["direction"] == "above" else -1) * ticks * Decimal(str(tick_size))
                assert Decimal(str(price)) == expected
    # Plain float arithmetic drifts here (70.13 - 1 * 0.01 == 70.11999999999999); the legs do not.
    cl = {**ES, "tick_size": 0.01}
    assert 70.13 - 1 * 0.01 != 70.12
    assert preflight(ticket(limit_price=70.13, stop_ticks=1), PRACTICE, cl)["brackets"]["stop_loss"]["price"] == 70.12
    assert preflight(ticket(limit_price=5000.3, target_ticks=1), PRACTICE, {**ES, "tick_size": 0.1})["brackets"]["take_profit"]["price"] == 5000.4


def test_preflight_refuses_a_leg_at_or_below_zero():
    cheap = {**ES, "tick_size": 0.25}
    with pytest.raises(ValueError, match="Stop of 4 ticks would put that leg at or below zero"):
        preflight(ticket(limit_price=1.0, stop_ticks=4), PRACTICE, cheap)
    assert preflight(ticket(limit_price=1.0, stop_ticks=3), PRACTICE, cheap)["brackets"]["stop_loss"]["price"] == 0.25
    with pytest.raises(ValueError, match="Target of 8 ticks"):
        preflight(ticket(side="sell", limit_price=2.0, target_ticks=8), PRACTICE, cheap)


@pytest.mark.parametrize("tick_size", [None, 0, -0.25, "0.25"])
def test_preflight_brackets_need_a_usable_tick_size_even_for_market_orders(tick_size):
    market = ticket(order_type="market", limit_price=None, stop_ticks=5)
    with pytest.raises(ValueError, match="tick size"):
        preflight(market, PRACTICE, {**ES, "tick_size": tick_size}, last_price=5000.0)


@pytest.mark.parametrize("kwargs", [{"stop_ticks": 0}, {"target_ticks": 1001}, {"stop_ticks": True}, {"target_ticks": 2.0}])
def test_preflight_rechecks_bracket_bounds_for_tickets_built_outside_parse_ticket(kwargs):
    with pytest.raises(ValueError, match="whole number from 1 to 1000"):
        preflight(ticket(**kwargs), PRACTICE, ES)


def test_preflight_without_brackets_has_no_legs_or_note():
    summary = preflight(ticket(), PRACTICE, ES)
    assert summary["brackets"] is None and summary["stop_ticks"] is None and summary["target_ticks"] is None
    assert "bracket_note" not in summary


def test_last_price_from_reads_the_snapshot_quote():
    snap = {"quotes": {ES["id"]: {"bid": 5000.0, "last": 5000.25}, "CON.X": {"last": None}}}
    assert orders.last_price_from(snap, ES["id"]) == 5000.25
    assert orders.last_price_from(snap, "CON.X") is None
    assert orders.last_price_from(snap, "CON.Y") is None
    assert orders.last_price_from({}, ES["id"]) is None and orders.last_price_from(None, ES["id"]) is None


# -- brackets: what goes to /Order/place -------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, stop_loss, take_profit",
    [
        ({"stop_ticks": 10, "target_ticks": 20}, {"ticks": -10, "type": 4}, {"ticks": 20, "type": 1}),
        ({"side": "sell", "stop_ticks": 10, "target_ticks": 20}, {"ticks": 10, "type": 4}, {"ticks": -20, "type": 1}),
        ({"order_type": "market", "limit_price": None, "stop_ticks": 6}, {"ticks": -6, "type": 4}, None),
        ({"side": "sell", "order_type": "stop", "limit_price": None, "stop_price": 4990.0, "target_ticks": 9}, None, {"ticks": -9, "type": 1}),
    ],
)
def test_send_order_posts_the_gateway_bracket_objects_exactly(make_client, kwargs, stop_loss, take_profit):
    recorder = Recorder({"orderId": 77})
    client = make_client(recorder)
    t = ticket(**kwargs)
    summary = preflight(t, PRACTICE, ES, last_price=5000.0)
    assert send_order(client, t, summary) == 77
    assert [path for path, _ in recorder.requests] == ["/Order/place"]
    body = recorder.requests[0][1]
    assert body["stopLossBracket"] == stop_loss and body["takeProfitBracket"] == take_profit
    # Everything else is the plain order; the brackets never change the entry itself.
    assert body["size"] == t.size and body["limitPrice"] == t.limit_price and body["stopPrice"] == t.stop_price
    assert body["customTag"] == summary["custom_tag"]


def test_send_order_refuses_brackets_the_summary_did_not_show_and_sends_nothing(make_client):
    recorder = Recorder({"orderId": 77})
    client = make_client(recorder)
    plain = preflight(ticket(), PRACTICE, ES)
    with pytest.raises(PermissionError, match="brackets"):
        send_order(client, ticket(stop_ticks=10), plain)       # brackets added after the review
    with_legs = preflight(ticket(stop_ticks=10, target_ticks=20), PRACTICE, ES)
    with pytest.raises(PermissionError):
        send_order(client, ticket(), with_legs)                 # brackets removed after the review
    with pytest.raises(PermissionError):
        send_order(client, ticket(stop_ticks=11, target_ticks=20), with_legs)   # distance changed
    with pytest.raises(PermissionError):
        send_order(client, ticket(stop_ticks=10), with_legs)    # a leg dropped
    tampered = {**with_legs, "brackets": {**with_legs["brackets"], "stop_loss": {**with_legs["brackets"]["stop_loss"], "gateway_ticks": 10}}}
    with pytest.raises(PermissionError):
        send_order(client, ticket(stop_ticks=10, target_ticks=20), tampered)
    assert recorder.requests == []


def test_a_gateway_bracket_rejection_surfaces_as_an_api_error(make_client):
    from topstepx import ApiError

    message = "Brackets cannot be used with Position Brackets. You must enable Auto OCO Brackets."
    recorder = Recorder({"success": False, "errorCode": 2, "errorMessage": message, "orderId": 9057})
    client = make_client(recorder)
    t = ticket(stop_ticks=10)
    with pytest.raises(ApiError) as err:
        send_order(client, t, preflight(t, PRACTICE, ES))
    assert message in str(err.value) and err.value.error_code == 2


# -- flatten all / cancel all ------------------------------------------------------------------


NQ_ID = "CON.F.US.ENQ.Z26"
CL_ID = "CON.F.US.CLE.Z26"


class Gateway:
    """Mock transport for batches: records every request; chosen items fail with a gateway envelope."""

    def __init__(self, close_errors: dict | None = None, cancel_errors: dict | None = None):
        self.close_errors = close_errors or {}      # contract id -> (errorCode, message)
        self.cancel_errors = cancel_errors or {}    # order id -> (errorCode, message)
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append((request.url.path, body))
        failure = None
        if request.url.path == "/Position/closeContract":
            failure = self.close_errors.get(body["contractId"])
        elif request.url.path == "/Order/cancel":
            failure = self.cancel_errors.get(body["orderId"])
        if failure:
            code, message = failure
            return httpx.Response(200, json={"success": False, "errorCode": code, "errorMessage": message})
        return httpx.Response(200, json={"success": True, "errorCode": 0, "errorMessage": None})


def test_flatten_closes_every_position_then_cancels_every_order_and_continues_past_failures(make_client):
    gateway = Gateway(close_errors={NQ_ID: (5, "Order rejected")}, cancel_errors={303: (2, "Order not found")})
    client = make_client(gateway)
    reviewed_orders = [{"id": 301, "contract_id": ES["id"]}, {"id": 302, "contract_id": NQ_ID},
                       {"id": 303, "contract_id": CL_ID}, {"id": 304, "contract_id": None}]

    report = orders.flatten_account(client, PRACTICE, [ES["id"], NQ_ID, CL_ID], reviewed_orders)

    assert gateway.requests == [
        ("/Position/closeContract", {"accountId": 7, "contractId": ES["id"]}),
        ("/Position/closeContract", {"accountId": 7, "contractId": NQ_ID}),
        ("/Position/closeContract", {"accountId": 7, "contractId": CL_ID}),
        ("/Order/cancel", {"accountId": 7, "orderId": 301}),
        ("/Order/cancel", {"accountId": 7, "orderId": 303}),
        ("/Order/cancel", {"accountId": 7, "orderId": 304}),
    ]   # 302 rests on NQ, whose close failed: it stays working
    assert [(p["contract_id"], p["status"]) for p in report["positions"]] == [(ES["id"], "sent"), (NQ_ID, "failed"), (CL_ID, "sent")]
    assert "Order rejected" in report["positions"][1]["error"]
    assert [(o["order_id"], o["status"]) for o in report["orders"]] == [(301, "sent"), (302, "kept"), (303, "not_found"), (304, "sent")]
    assert report["orders"][1]["error"].startswith("Left working")
    assert (report["sent"], report["failed"], report["not_found"], report["kept"]) == (4, 1, 1, 1)
    assert report["ok"] is False and report["action"] == "flatten" and report["account"] == "PRAC-1" and report["account_id"] == 7
    assert json.dumps(report)   # JSON-ready for the route


def test_flatten_with_nothing_failing_is_ok():
    class Client:
        def __init__(self):
            self.calls = []

        def close_position(self, account_id, contract_id):
            self.calls.append(("close", account_id, contract_id))

        def cancel_order(self, account_id, order_id):
            self.calls.append(("cancel", account_id, order_id))

    client = Client()
    report = orders.flatten_account(client, PRACTICE, [ES["id"]], [{"id": 5, "contract_id": ES["id"]}])
    assert client.calls == [("close", 7, ES["id"]), ("cancel", 7, 5)]
    assert report["ok"] is True and (report["sent"], report["failed"]) == (2, 0)


def test_cancel_orders_continues_past_failures_and_bad_ids(make_client):
    gateway = Gateway(cancel_errors={401: (3, "Rejected")})
    client = make_client(gateway)
    report = orders.cancel_orders(client, PRACTICE, [{"id": 401, "contract_id": ES["id"]}, {"id": 0, "contract_id": None},
                                                     {"id": 402, "contract_id": NQ_ID}])
    assert gateway.requests == [("/Order/cancel", {"accountId": 7, "orderId": 401}), ("/Order/cancel", {"accountId": 7, "orderId": 402})]
    assert [(o["order_id"], o["status"]) for o in report["orders"]] == [(401, "failed"), (0, "failed"), (402, "sent")]
    assert report["positions"] == [] and report["action"] == "cancel_all" and report["ok"] is False


def test_batch_items_survive_network_and_unexpected_errors(capsys):
    from topstepx import TopstepXError

    class Client:
        def __init__(self):
            self.calls = []

        def close_position(self, account_id, contract_id):
            self.calls.append(contract_id)
            if contract_id == ES["id"]:
                raise TopstepXError("Could not reach the TopstepX API")
            raise RuntimeError("boom")

        def cancel_order(self, account_id, order_id):
            self.calls.append(order_id)

    client = Client()
    report = orders.flatten_account(client, PRACTICE, [ES["id"], NQ_ID], [{"id": 9, "contract_id": CL_ID}])
    assert client.calls == [ES["id"], NQ_ID, 9]
    assert [p["status"] for p in report["positions"]] == ["failed", "failed"]
    assert "Could not reach" in report["positions"][0]["error"] and "RuntimeError" in report["positions"][1]["error"]
    assert report["orders"][0]["status"] == "sent"
    assert "boom" in capsys.readouterr().err   # the traceback goes to the terminal only


@pytest.mark.parametrize("account", [FUNDED, HALTED, {**PRACTICE, "simulated": None}, {}, None])
def test_flatten_and_cancel_all_refuse_non_practice_accounts_and_send_nothing(make_client, account):
    gateway = Gateway()
    client = make_client(gateway)
    with pytest.raises(PermissionError):
        orders.flatten_account(client, account, [ES["id"]], [{"id": 1, "contract_id": ES["id"]}])
    with pytest.raises(PermissionError):
        orders.cancel_orders(client, account, [{"id": 1, "contract_id": ES["id"]}])
    assert gateway.requests == []


def test_parse_id_list_validates_and_dedupes():
    assert orders.parse_id_list({}, "order_ids", int) == []
    assert orders.parse_id_list({"order_ids": None}, "order_ids", int) == []
    assert orders.parse_id_list({"order_ids": [3, 1, 3, 2]}, "order_ids", int) == [3, 1, 2]
    assert orders.parse_id_list({"contract_ids": [" CON.A ", "CON.A", "CON.B"]}, "contract_ids", str) == ["CON.A", "CON.B"]
    for bad in ("1,2", 5, {"a": 1}, [0], [-1], ["7"], [True], [1.5], list(range(1, 300))):
        with pytest.raises(ValueError):
            orders.parse_id_list({"order_ids": bad}, "order_ids", int)
    for bad in ([""], ["  "], [5], [None]):
        with pytest.raises(ValueError):
            orders.parse_id_list({"contract_ids": bad}, "contract_ids", str)
