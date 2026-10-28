"""Offline regression tests for the dashboard hardening pass (security review follow-ups)."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from topstepx import Account, Contract

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))

from dashboard import orders, server  # noqa: E402
from dashboard.state import DashboardState  # noqa: E402

ES = Contract(id="CON.F.US.EP.Z26", name="ESZ26", tick_size=0.25, tick_value=12.5, active_contract=True)
SINCE = datetime(2026, 10, 26, tzinfo=timezone.utc)


def make_state() -> DashboardState:
    state = DashboardState(SINCE)
    state.set_accounts([
        Account(id=1, name="PRAC-A", balance=1.0, simulated=True, can_trade=True),
        Account(id=2, name="PRAC-B", balance=2.0, simulated=True, can_trade=True),
    ])
    state.select_account(1)
    state.add_contract(ES)
    return state


# -- state: events for another account are ignored ------------------------------------


def test_position_order_and_fill_events_for_other_accounts_are_ignored():
    state = make_state()
    state.apply_position({"accountId": 2, "contractId": ES.id, "type": 1, "size": 3, "averagePrice": 5000.0})
    state.apply_order({"accountId": 2, "id": 10, "contractId": ES.id, "status": 1, "type": 1, "side": 0, "size": 1})
    state.apply_trade({"accountId": 2, "id": 20, "contractId": ES.id, "profitAndLoss": 50.0,
                       "creationTimestamp": "2026-10-26T14:00:00Z"})
    snap = state.snapshot()
    assert snap["positions"] == [] and snap["orders"] == [] and snap["trades"] == []

    state.apply_position({"accountId": 1, "contractId": ES.id, "type": 1, "size": 3, "averagePrice": 5000.0})
    state.apply_trade({"id": 21, "contractId": ES.id, "profitAndLoss": 5.0, "creationTimestamp": "2026-10-26T14:00:00Z"})
    snap = state.snapshot()
    assert len(snap["positions"]) == 1 and len(snap["trades"]) == 1  # own account, and payloads without accountId


def test_naive_fill_timestamp_is_treated_as_utc():
    state = make_state()
    state.apply_trade({"id": 1, "contractId": ES.id, "profitAndLoss": 1.0, "creationTimestamp": "2026-10-26T14:00:00"})
    state.apply_trade({"id": 2, "contractId": ES.id, "profitAndLoss": 1.0, "creationTimestamp": "2026-10-25T14:00:00"})
    assert [t["id"] for t in state.snapshot()["trades"]] == [1]


def test_contract_keeps_its_active_flag_for_preflight():
    state = make_state()
    assert state.contract(ES.id)["active_contract"] is True
    expired = Contract(id="CON.F.US.EP.U26", name="ESU26", tick_size=0.25, tick_value=12.5, active_contract=False)
    state.add_contract(expired)
    ticket = orders.parse_ticket({"account_id": 1, "contract_id": expired.id, "side": "buy",
                                  "order_type": "market", "size": 1})
    with pytest.raises(ValueError, match="(?i)not active|expired|inactive"):
        orders.preflight(ticket, state.selected_account(), state.contract(expired.id))


# -- orders: the reviewed tag is the tag that is sent ------------------------------------


def test_preflight_accepts_a_well_formed_tag_back_and_rejects_others():
    state = make_state()
    ticket = orders.parse_ticket({"account_id": 1, "contract_id": ES.id, "side": "buy", "order_type": "market", "size": 1})
    first = orders.preflight(ticket, state.selected_account(), state.contract(ES.id))
    again = orders.preflight(ticket, state.selected_account(), state.contract(ES.id), custom_tag=first["custom_tag"])
    assert again["custom_tag"] == first["custom_tag"]
    forged = orders.preflight(ticket, state.selected_account(), state.contract(ES.id), custom_tag="my-own-tag")
    assert forged["custom_tag"] != "my-own-tag" and orders.TAG_PATTERN.match(forged["custom_tag"])


# -- server: GET routes require a loopback Host, the page refuses framing ------------------


class FakeClient:
    def __init__(self):
        self.calls = []

    def place_order(self, **kwargs):
        self.calls.append(kwargs)
        return 4242


class FakeSession:
    def __init__(self, state):
        self.state = state
        self.client = FakeClient()
        self.args = type("Args", (), {"unit": "minute", "unit_number": 5})()

    def call_async(self, coro, timeout=15.0):
        coro.close()
        return None


@pytest.fixture
def running(monkeypatch, tmp_path):
    (tmp_path / "index.html").write_text('<meta name="dashboard-token" content="__TOKEN__">', encoding="utf-8")
    monkeypatch.setattr(server, "STATIC_DIR", tmp_path)
    state = make_state()
    session = FakeSession(state)
    srv = server.make_server(session, port=0)
    port = srv.server_address[1]
    try:
        yield session, port
    finally:
        srv.shutdown()
        srv.server_close()


def request(port, path, *, method="GET", body=None, headers=None):
    req = Request(f"http://127.0.0.1:{port}{path}", method=method,
                  data=json.dumps(body).encode() if body is not None else None,
                  headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_get_with_foreign_host_is_refused(running):
    _session, port = running
    status, _headers, body = request(port, "/", headers={"Host": f"attacker.example:{port}"})
    assert status == 403 and b"__TOKEN__" not in body and server.TOKEN.encode() not in body
    status, _headers, _body = request(port, "/api/state", headers={"Host": "evil.test"})
    assert status == 403
    status, _headers, _body = request(port, "/api/state")
    assert status == 200


def test_page_refuses_to_be_framed(running):
    _session, port = running
    status, headers, _body = request(port, "/")
    assert status == 200
    assert headers.get("X-Frame-Options") == "DENY"
    assert "frame-ancestors 'none'" in headers.get("Content-Security-Policy", "")


def test_order_send_uses_the_reviewed_tag(running):
    session, port = running
    ticket = {"account_id": 1, "contract_id": ES.id, "side": "buy", "order_type": "limit", "size": 1, "limit_price": 5000.0}
    headers = {"X-Dashboard-Token": server.TOKEN}
    status, _h, body = request(port, "/api/orders/preflight", method="POST", body=ticket, headers=headers)
    assert status == 200
    tag = json.loads(body)["summary"]["custom_tag"]
    status, _h, body = request(port, "/api/orders", method="POST",
                               body={**ticket, "confirm": "yes", "custom_tag": tag}, headers=headers)
    assert status == 200 and json.loads(body)["order_id"] == 4242
    assert len(session.client.calls) == 1 and session.client.calls[0]["custom_tag"] == tag
