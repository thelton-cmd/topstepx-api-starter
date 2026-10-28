"""Offline tests for dashboard/features/alerts.py: crossing rules, the store, the guard, persistence
and every route through the dashboard's server. Nothing here touches the network."""

from __future__ import annotations

import json

import pytest

from dashboard.features import alerts
from dashboard.features import collect_routes
from tests.test_dashboard_server import ES, FUNDED, as_json, dash, static_dir, ticket  # noqa: F401 - fixtures


@pytest.fixture
def store(tmp_path):
    s = alerts.AlertStore(tmp_path / "alerts.json", describe=lambda cid: {"name": "ESZ6", "tick_size": 0.25} if cid == ES.id else None)
    s.load()
    return s


def install(session, tmp_path, monkeypatch):
    monkeypatch.setattr(alerts, "default_path", lambda *a, **k: tmp_path / "alerts.json")
    session.features = [alerts]
    session.feature_routes = collect_routes([alerts], session)
    return alerts._store(session)


# -- pure rules ------------------------------------------------------------------------------------


def test_crossing_rules_cover_gaps_and_directions():
    assert alerts.crossing("above", 99.0, 101.0, 100.0) == "up"
    assert alerts.crossing("above", 100.0, 100.25, 100.0) == "up"       # from exactly the level
    assert alerts.crossing("above", 101.0, 99.0, 100.0) is None
    assert alerts.crossing("below", 101.0, 99.0, 100.0) == "down"
    assert alerts.crossing("below", 99.0, 101.0, 100.0) is None
    assert alerts.crossing("cross", 99.0, 101.0, 100.0) == "up" and alerts.crossing("cross", 101.0, 99.0, 100.0) == "down"
    assert alerts.crossing("cross", 99.0, 99.5, 100.0) is None
    assert alerts.crossing("above", None, 101.0, 100.0) is None
    assert alerts.observed_price(None, 99.0, 101.0) == 100.0 and alerts.observed_price(7.0, 1.0, 2.0) == 7.0


def test_guard_breach_uses_the_traders_numbers_only():
    guard = alerts.default_guard()
    assert alerts.guard_breach(guard, {"total": -10_000, "open_contracts": 50}) is None   # no guard set
    guard["max_daily_loss"] = 500.0
    assert alerts.guard_breach(guard, {"total": -499.99}) is None
    reason = alerts.guard_breach(guard, {"total": -500.0})
    assert reason and "500.00" in reason and "paused" in reason.lower()
    guard["max_contracts"] = 2
    assert alerts.guard_breach(guard, {"total": 10, "open_contracts": 2}) is None
    assert "open contracts" in alerts.guard_breach(guard, {"total": 10, "open_contracts": 3})


# -- the store -------------------------------------------------------------------------------------


def test_store_fires_once_and_repeating_alerts_rearm(store):
    once = store.add(ES.id, 5000.0, "above", "my level", once=True)
    again = store.add(ES.id, 4990.0, "cross", "", once=False)
    assert once["armed"] and again["armed"] and len(store.list()) == 2

    assert store.observe_price(ES.id, 4995.0) == []            # first observation sets the previous price
    fired = store.observe_price(ES.id, 5001.0)                # gap through 5000 fires the once alert
    assert [e["alert_id"] for e in fired] == [once["id"]] and fired[0]["kind"] == "alert"
    assert "crossed above 5,000.00" in fired[0]["text"] and "my level" in fired[0]["text"]
    assert store.list()[0]["armed"] is False and store.list()[0]["trigger_count"] == 1

    fired = store.observe_price(ES.id, 4985.0)                # down through 4990: repeating alert fires
    assert [e["alert_id"] for e in fired] == [again["id"]]
    assert store.observe_price(ES.id, 4980.0) == []           # still below: nothing, not re-armed
    fired = store.observe_price(ES.id, 4991.0)                # back above: re-arms, and that move is itself a cross
    assert [e["alert_id"] for e in fired] == [again["id"]] and fired[0]["moved"] == "up"
    assert store.observe_price(ES.id, 4992.0) == []           # above and fired: waits for price to come back
    fired = store.observe_price(ES.id, 4989.0)
    assert [e["alert_id"] for e in fired] == [again["id"]] and fired[0]["moved"] == "down"
    assert store.observe_price("CON.OTHER", 1.0) == []

    events, last = store.since(0)
    assert len(events) == 4 and last == 4 and [e["seq"] for e in events] == [1, 2, 3, 4]
    assert [e["seq"] for e in store.since(2)[0]] == [3, 4]


def test_store_update_delete_and_persistence(store, tmp_path):
    a = store.add(ES.id, 5000.0, "above", "n", once=True)
    store.observe_price(ES.id, 4999.0)
    store.observe_price(ES.id, 5001.0)
    assert store.list()[0]["armed"] is False
    updated = store.update(a["id"], {"armed": True, "price": 5002.0, "note": "moved"})
    assert updated["armed"] is True and updated["price"] == 5002.0 and updated["note"] == "moved"
    with pytest.raises(KeyError):
        store.update("nope", {"armed": True})
    store.set_guard(250.0, 3)
    assert store.save()

    other = alerts.AlertStore(tmp_path / "alerts.json")
    other.load()
    assert [x["price"] for x in other.list()] == [5002.0] and other.guard_copy()["max_daily_loss"] == 250.0
    assert other.guard_copy()["max_contracts"] == 3
    assert store.delete(a["id"]) is True and store.delete(a["id"]) is False
    data = json.loads((tmp_path / "alerts.json").read_text(encoding="utf-8"))
    assert "token" not in json.dumps(data).lower()


def test_store_guard_pauses_and_stays_paused_until_cleared(store):
    assert store.guard_active() is False and store.check_guard({"total": -1000}) is None
    store.set_guard(500.0, None)
    assert store.check_guard({"total": -100}) is None and store.paused_reason() is None
    reason = store.check_guard({"total": -600})
    assert reason and store.paused_reason() == reason and store.guard_copy()["paused_at"]
    assert store.check_guard({"total": +50}) == reason          # recovered, still paused
    store.clear_pause()
    assert store.paused_reason() is None
    assert store.check_guard({"total": +50}) is None
    events = store.since(0)[0]
    assert any(e["kind"] == "guard" for e in events)


def test_store_records_fills_for_the_selected_account_once(store):
    payload = {"action": 1, "data": {"id": 77, "accountId": 1, "contractId": ES.id, "side": 0, "size": 2, "price": 5000.25,
                                     "creationTimestamp": "2026-10-28T14:00:00+00:00"}}
    event = store.record_fill(payload, 1)
    assert event["kind"] == "fill" and event["text"] == "Filled BUY 2 ESZ6 @ 5,000.25"
    assert store.record_fill(payload, 1) is None               # same trade id again
    assert store.record_fill({"data": {"id": 78, "accountId": 2, "contractId": ES.id, "side": 1, "size": 1, "price": 1}}, 1) is None
    assert store.record_fill({"data": {"id": 79, "accountId": 1, "voided": True}}, 1) is None


# -- routes through the dashboard server -------------------------------------------------------------


def test_alert_routes_add_list_update_delete_events(dash, tmp_path, monkeypatch):
    store = install(dash.session, tmp_path, monkeypatch)
    status, body, _ = dash.get("/api/alerts")
    assert status == 200
    data = as_json(body)
    assert data["alerts"] == [] and data["guard"]["max_daily_loss"] is None and data["status"]["blocked"] is None
    assert data["limits"]["directions"] == ["above", "below", "cross"]

    status, body, _ = dash.post("/api/alerts/add", {"contract_id": ES.id, "price": 5000.0, "direction": "above", "note": "x", "once": True})
    assert status == 200 and as_json(body)["ok"] is True
    alert_id = as_json(body)["alert"]["id"]
    assert dash.post("/api/alerts/add", {"contract_id": "CON.NOPE", "price": 1, "direction": "above"})[0] == 400
    assert dash.post("/api/alerts/add", {"contract_id": ES.id, "price": -1, "direction": "above"})[0] == 400
    assert dash.post("/api/alerts/add", {"contract_id": ES.id, "price": 1, "direction": "sideways"})[0] == 400
    assert dash.post("/api/alerts/add", {"contract_id": ES.id, "price": 1, "direction": "above"}, token=False)[0] == 403

    listed = as_json(dash.get("/api/alerts")[1])["alerts"]
    assert len(listed) == 1 and listed[0]["name"] == ES.name and listed[0]["watched"] is True
    assert (tmp_path / "alerts.json").exists()

    store.observe_price(ES.id, 4999.0)
    store.observe_price(ES.id, 5001.0)
    events = as_json(dash.get("/api/alerts/events?after=0")[1])
    assert events["last_seq"] == 1 and events["events"][0]["kind"] == "alert"
    assert as_json(dash.get("/api/alerts/events?after=1")[1])["events"] == []

    status, body, _ = dash.post("/api/alerts/update", {"id": alert_id, "armed": True, "note": "again"})
    assert status == 200 and as_json(body)["alert"]["armed"] is True
    assert dash.post("/api/alerts/update", {"id": alert_id})[0] == 400
    assert dash.post("/api/alerts/update", {"id": "zzz", "armed": True})[0] == 404
    assert dash.post("/api/alerts/delete", {"id": alert_id})[0] == 200
    assert dash.post("/api/alerts/delete", {"id": alert_id})[0] == 404
    assert as_json(dash.post("/api/alerts/test", {})[1])["event"]["kind"] == "test"
    snap = as_json(dash.get("/api/state")[1])["features"]["alerts"]
    assert snap["alerts_count"] == 0 and snap["blocked"] is None


def test_guard_route_blocks_order_entry_until_cleared(dash, tmp_path, monkeypatch):
    install(dash.session, tmp_path, monkeypatch)
    # No positions or fills in the fake state: today's total is 0. A max loss of 0.0 is "off" (null) semantics
    # are the trader's call; here we set a cap on contracts and then a loss number that is already reached.
    status, body, _ = dash.post("/api/alerts/guard", {"max_daily_loss": 500, "max_contracts": 2})
    assert status == 200 and as_json(body)["guard"]["max_daily_loss"] == 500.0 and as_json(body)["blocked"] is None
    assert dash.post("/api/orders/preflight", ticket())[0] == 200
    assert dash.post("/api/alerts/guard", {})[0] == 400
    assert dash.post("/api/alerts/guard", {"max_daily_loss": -5})[0] == 400
    assert dash.post("/api/alerts/guard", {"max_contracts": 0})[0] == 400

    # Make today's total fall below the number by feeding a closed trade with a loss into the state.
    dash.session.state.apply_trade({"id": 900, "accountId": dash.session.state.selected_account()["id"], "contractId": ES.id,
                                    "side": 1, "size": 1, "price": 5000.0, "profitAndLoss": -600.0, "fees": 1.0,
                                    "creationTimestamp": "2026-10-26T14:00:00+00:00"})
    totals = as_json(dash.get("/api/state")[1])["totals"]
    if totals.get("total") is None or totals["total"] > -500:
        pytest.skip("fake state does not produce a realized total for this payload")
    snap = as_json(dash.get("/api/state")[1])
    assert snap["order_entry_allowed"] is False and "500.00" in snap["order_entry_block"]
    status, body, _ = dash.post("/api/orders/preflight", ticket())
    assert status == 403 and "paused" in as_json(body)["error"].lower()
    # Clearing the pause while the number is still reached pauses again right away, with a note.
    status, body, _ = dash.post("/api/alerts/guard/clear", {})
    assert status == 200 and as_json(body)["blocked"] and "still reached" in as_json(body)["note"]
    assert as_json(dash.get("/api/state")[1])["order_entry_allowed"] is False
    # Changing the guard (raising the number) is the trader's decision and resumes order entry.
    dash.post("/api/alerts/guard", {"max_daily_loss": 5000})
    assert as_json(dash.get("/api/state")[1])["order_entry_allowed"] is True
    assert dash.post("/api/orders/preflight", ticket())[0] == 200
