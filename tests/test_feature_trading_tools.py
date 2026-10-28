"""Offline tests for dashboard/features/trading_tools.py: the Flatten all / Cancel all routes,
their Practice-only + confirm guard, per-item reporting, and the hotkeys page module's files.

Everything runs against the fake session from tests/test_dashboard_server.py; the fake client
records calls and never talks to anything.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dashboard import features as feature_registry
from dashboard.features import trading_tools
from tests.test_dashboard_features import install
from tests.test_dashboard_server import ES, FUNDED, PRACTICE, as_json, dash, static_dir  # noqa: F401 - fixtures
from topstepx import ApiError, Contract

ROOT = Path(__file__).resolve().parents[1]
NQ = Contract(id="CON.F.US.ENQ.Z26", name="NQZ26", description="E-mini Nasdaq", tick_size=0.25, tick_value=5.0,
              active_contract=True)
FLATTEN, CANCEL_ALL = "/api/positions/flatten", "/api/orders/cancel_all"


@pytest.fixture
def tools(dash):
    """The dashboard with the trading_tools feature installed and an account holding 2 positions + 3 orders."""
    install(dash.session, trading_tools)
    state = dash.session.state
    state.add_contract(NQ)
    state.apply_position({"accountId": PRACTICE.id, "contractId": ES.id, "type": 1, "size": 2, "averagePrice": 5000.0})
    state.apply_position({"accountId": PRACTICE.id, "contractId": NQ.id, "type": 2, "size": 1, "averagePrice": 21000.0})
    for order_id, contract_id, side, price in ((11, ES.id, 1, 5010.0), (12, NQ.id, 0, 21010.0), (13, ES.id, 0, 4990.0)):
        state.apply_order({"id": order_id, "accountId": PRACTICE.id, "contractId": contract_id, "status": 1,
                           "type": 1, "side": side, "size": 1, "limitPrice": price})
    return dash


def flatten_body(**overrides) -> dict:
    body = {"account_id": PRACTICE.id, "contract_ids": [ES.id, NQ.id], "order_ids": [11, 12, 13], "confirm": "yes"}
    body.update(overrides)
    return body


def cancel_body(**overrides) -> dict:
    body = {"account_id": PRACTICE.id, "order_ids": [11, 12, 13], "confirm": "yes"}
    body.update(overrides)
    return body


def sent_nothing(dash) -> bool:
    client = dash.session.client
    return client.place_calls == [] and client.cancel_calls == [] and client.close_calls == []


# -- the module contract -------------------------------------------------------------------


def test_module_declares_its_routes_as_order_routes_and_ships_its_page_files():
    assert trading_tools.ORDER_ROUTES == {FLATTEN, CANCEL_ALL}
    assert feature_registry.order_routes([trading_tools]) == {FLATTEN, CANCEL_ALL}
    table = feature_registry.collect_routes([trading_tools], object())
    assert set(table["POST"]) == {FLATTEN, CANCEL_ALL} and table["GET"] == {}
    info = feature_registry.describe(trading_tools)
    assert info["name"] == "trading_tools" and info["js"] == "hotkeys.js" and info["css"] == "hotkeys.css"
    static = ROOT / "dashboard" / "static" / "features"
    for name in (info["js"], info["css"]):
        (static / name).read_text(encoding="utf-8").encode("ascii")
    assert "trading_tools" in [feature_registry.name_of(m) for m in feature_registry.discover()]


def test_features_listing_names_trading_tools(tools):
    status, body, _ = tools.get("/api/features")
    assert status == 200 and as_json(body)["features"] == [feature_registry.describe(trading_tools)]


def test_hotkeys_page_module_is_off_by_default_and_only_trades_through_api_orders():
    source = (ROOT / "dashboard" / "static" / "features" / "hotkeys.js").read_text(encoding="utf-8")
    assert "export function mount(api" in source
    assert "settings.hotkeys.enabled === true" in source            # on only when explicitly enabled
    assert "api.post" not in source and "fetch(" not in source      # never sends anything itself
    for call in ("o.flatten()", "o.cancelAll()", 'side: "buy", order_type: "market"', 'side: "sell", order_type: "market"'):
        assert call in source
    assert "ev.repeat" in source and ".modal-backdrop" in source   # no auto-repeat, not while the modal is open
    assert "Escape" in source and 'key === "escape"' not in source.lower()   # Escape stays ticket.js's job


def test_feature_source_never_sends_on_its_own_and_stays_ascii():
    source = Path(trading_tools.__file__).read_text(encoding="utf-8")
    source.encode("ascii")
    for word in ("place_order", "modify_order", "partial_close", "def start", "call_later", "Timer("):
        assert word not in source, word


# -- the guard: Practice-only, confirm, token ------------------------------------------------


@pytest.mark.parametrize("path, body", [(FLATTEN, flatten_body), (CANCEL_ALL, cancel_body)])
def test_routes_refuse_without_confirm_yes(tools, path, body):
    for confirm in (None, "YES", "y", True, "yes "):
        payload = body()
        if confirm is None:
            payload.pop("confirm")
        else:
            payload["confirm"] = confirm
        status, reply, _ = tools.post(path, payload)
        assert status == 403 and as_json(reply)["ok"] is False and "confirm" in as_json(reply)["error"], confirm
    assert sent_nothing(tools)


@pytest.mark.parametrize("path, body", [(FLATTEN, flatten_body), (CANCEL_ALL, cancel_body)])
def test_routes_refuse_without_the_page_token(tools, path, body):
    assert tools.post(path, body(), token=False)[0] == 403
    assert tools.post(path, body(), token=False, headers={"X-Dashboard-Token": "nope"})[0] == 403
    assert tools.post(path, body(), headers={"Origin": "http://evil.example.com"})[0] == 403
    assert sent_nothing(tools)


@pytest.mark.parametrize("path, body", [(FLATTEN, flatten_body), (CANCEL_ALL, cancel_body)])
def test_routes_refuse_a_funded_account_whatever_the_body_says(tools, path, body):
    tools.session.state.select_account(FUNDED.id)
    for account_id in (FUNDED.id, PRACTICE.id):
        status, reply, _ = tools.post(path, body(account_id=account_id))
        assert status == 403 and as_json(reply)["ok"] is False and "Practice" in as_json(reply)["error"]
    assert sent_nothing(tools)


@pytest.mark.parametrize("path, body", [(FLATTEN, flatten_body), (CANCEL_ALL, cancel_body)])
def test_routes_refuse_a_mismatched_account_or_bad_lists(tools, path, body):
    for bad in (body(account_id=FUNDED.id), body(account_id="7"), {k: v for k, v in body().items() if k != "account_id"},
                body(order_ids="11,12"), body(order_ids=[11, "12"]), body(order_ids=[0])):
        status, reply, _ = tools.post(path, bad)
        assert status == 400 and as_json(reply)["ok"] is False, bad
    status, reply, _ = tools.post(FLATTEN, flatten_body(contract_ids=[], order_ids=[]))
    assert status == 400 and "Nothing to flatten" in as_json(reply)["error"]
    status, reply, _ = tools.post(CANCEL_ALL, cancel_body(order_ids=[]))
    assert status == 400 and "Nothing to cancel" in as_json(reply)["error"]
    assert sent_nothing(tools)


# -- what gets sent -------------------------------------------------------------------------------


def test_flatten_closes_each_reviewed_position_then_cancels_each_reviewed_order(tools):
    status, body, _ = tools.post(FLATTEN, flatten_body())
    assert status == 200, body
    client = tools.session.client
    assert client.close_calls == [(PRACTICE.id, ES.id), (PRACTICE.id, NQ.id)]
    assert client.cancel_calls == [(PRACTICE.id, 11), (PRACTICE.id, 12), (PRACTICE.id, 13)]
    assert client.place_calls == []
    report = as_json(body)
    assert report["ok"] is True and report["action"] == "flatten" and report["account_id"] == PRACTICE.id
    assert [p["status"] for p in report["positions"]] == ["sent", "sent"]
    assert [(o["order_id"], o["contract_id"], o["status"]) for o in report["orders"]] == [
        (11, ES.id, "sent"), (12, NQ.id, "sent"), (13, ES.id, "sent")]
    assert (report["sent"], report["failed"], report["not_found"], report["kept"]) == (5, 0, 0, 0)
    assert report["not_included"] == {"positions": [], "orders": []}


def test_flatten_continues_past_a_failing_close_and_keeps_that_contracts_orders(tools):
    client = tools.session.client
    closes, cancels = [], []

    def close_position(account_id, contract_id):
        closes.append(contract_id)
        if contract_id == ES.id:
            raise ApiError("/Position/closeContract", "Order rejected", error_code=5)

    def cancel_order(account_id, order_id):
        cancels.append(order_id)
        if order_id == 12:
            raise ApiError("/Order/cancel", "Order not found", error_code=2)

    client.close_position, client.cancel_order = close_position, cancel_order
    status, body, _ = tools.post(FLATTEN, flatten_body())
    assert status == 200, body
    assert closes == [ES.id, NQ.id] and cancels == [12]   # 11 and 13 rest on ES, whose close failed
    report = as_json(body)
    assert report["ok"] is False
    assert [(p["contract_id"], p["status"]) for p in report["positions"]] == [(ES.id, "failed"), (NQ.id, "sent")]
    assert "Order rejected" in report["positions"][0]["error"]
    assert [(o["order_id"], o["status"]) for o in report["orders"]] == [(11, "kept"), (12, "not_found"), (13, "kept")]
    assert (report["sent"], report["failed"], report["not_found"], report["kept"]) == (1, 1, 1, 2)


def test_flatten_acts_only_on_what_was_reviewed_and_reports_the_rest(tools):
    status, body, _ = tools.post(FLATTEN, flatten_body(contract_ids=[NQ.id], order_ids=[12, 99]))
    assert status == 200, body
    client = tools.session.client
    assert client.close_calls == [(PRACTICE.id, NQ.id)]
    assert client.cancel_calls == [(PRACTICE.id, 12), (PRACTICE.id, 99)]   # 99 is attempted; the gateway decides
    report = as_json(body)
    assert report["not_included"] == {"positions": [ES.id], "orders": [11, 13]}
    assert report["orders"][1] == {"order_id": 99, "contract_id": None, "status": "sent"}


def test_cancel_all_cancels_each_reviewed_order_and_continues_past_failures(tools):
    client = tools.session.client
    seen = []

    def cancel_order(account_id, order_id):
        seen.append((account_id, order_id))
        if order_id == 11:
            raise ApiError("/Order/cancel", "Rejected", error_code=3)

    client.cancel_order = cancel_order
    status, body, _ = tools.post(CANCEL_ALL, cancel_body(order_ids=[11, 12]))
    assert status == 200, body
    assert seen == [(PRACTICE.id, 11), (PRACTICE.id, 12)] and client.close_calls == [] and client.place_calls == []
    report = as_json(body)
    assert report["action"] == "cancel_all" and report["ok"] is False and report["positions"] == []
    assert [(o["order_id"], o["status"]) for o in report["orders"]] == [(11, "failed"), (12, "sent")]
    assert "Rejected" in report["orders"][0]["error"]
    assert report["not_included"] == {"positions": [], "orders": [13]}


def test_routes_are_absent_without_the_feature(dash):
    assert dash.post(FLATTEN, flatten_body())[0] in {403, 404}
    assert dash.post(CANCEL_ALL, cancel_body())[0] in {403, 404}
    assert sent_nothing(dash)
