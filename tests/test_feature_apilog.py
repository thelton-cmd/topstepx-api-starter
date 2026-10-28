"""Offline tests for the "API calls" feature (dashboard/features/apilog.py): the REST ring buffer and
its sequence numbers, redaction of secrets, body size limits, hub statistics, the snippets, and every
route through the running test server. Nothing here touches the network or sends an order."""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dashboard import features as feature_registry
from dashboard.features import apilog
from tests.test_dashboard_server import as_json, dash, static_dir  # noqa: F401 - fixtures
from topstepx import BarUnit, TopstepXClient

ROOT = Path(__file__).resolve().parents[1]
SECRET_KEY = "sk-live-key-0123456789abcdef"
SECRET_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0cmFkZXIifQ.c2lnbmF0dXJlLXNpZ25hdHVyZQ"


class Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t


def rest(path="/Contract/search", request=None, response=..., status=200, error=None, ms=12.5):
    return {
        "time": "2026-10-28T13:00:00.123456+00:00", "path": path,
        "request": {"searchText": "ES", "live": False} if request is None else request,
        "status": status,
        "response": {"success": True, "errorCode": 0, "errorMessage": None,
                     "contracts": [{"id": "CON.F.US.EP.Z26", "name": "ESZ26"}]} if response is ... else response,
        "error": error, "duration_ms": ms,
    }


# -- ring buffer --------------------------------------------------------------------------------


def test_rest_ring_keeps_the_newest_calls_with_rising_seq():
    rec = apilog.Recorder()
    for i in range(apilog.REST_CAPACITY + 10):
        rec.observe_rest(rest(request={"searchText": f"S{i}", "live": False}))
    page = rec.calls(after=0, limit=apilog.REST_CAPACITY)
    seqs = [c["seq"] for c in page["calls"]]
    assert page["last_seq"] == apilog.REST_CAPACITY + 10
    assert seqs == list(range(11, apilog.REST_CAPACITY + 11))       # oldest first, the first ten dropped
    assert [c["seq"] for c in rec.calls(after=505, limit=100)["calls"]] == [506, 507, 508, 509, 510]
    assert [c["seq"] for c in rec.calls(after=0, limit=2)["calls"]] == [509, 510]   # newest two, oldest first
    assert rec.calls(after=510)["calls"] == []

    light = rec.calls(after=508, bodies=False)["calls"][0]
    assert "request" not in light and "response" not in light and light["summary"] == "searchText=S508 live=false"
    full = rec.calls(after=508, bodies=True)["calls"][0]
    assert full["request"] == {"searchText": "S508", "live": False} and full["response"]["contracts"][0]["id"] == "CON.F.US.EP.Z26"
    assert full["result"] == "1 contracts" and full["ok"] is True and full["path"] == "/Contract/search"
    assert rec.call(510)["request"]["searchText"] == "S509" and rec.call(1) is None

    cleared = rec.clear()
    assert cleared["last_seq"] == 510 and rec.calls()["calls"] == []
    rec.observe_rest(rest())
    assert rec.calls()["calls"][0]["seq"] == 511            # seq keeps rising after a clear


def test_failed_calls_are_marked_and_summarised():
    rec = apilog.Recorder()
    rec.observe_rest(rest(path="/Order/searchOpen", request={"accountId": 7}, status=200,
                          response={"success": False, "errorCode": 3, "errorMessage": "Account not found"},
                          error="/Order/searchOpen failed (errorCode=3): Account not found"))
    rec.observe_rest(rest(path="/Contract/available", request={"live": False}, status=500,
                          response="server down", error="HTTP 500 from /Contract/available: server down"))
    rec.observe_rest(rest(path="/Account/search", request={"onlyActiveAccounts": True}, status=None,
                          response=None, error="Could not reach the TopstepX API"))
    first, second, third = rec.calls()["calls"]
    assert first["ok"] is False and first["result"] == "errorCode 3: Account not found"
    assert second["ok"] is False and second["status"] == 500 and second["response"] == "server down"
    assert third["ok"] is False and third["status"] is None and third["result"] == "Could not reach the TopstepX API"


# -- redaction ----------------------------------------------------------------------------------


def test_redaction_masks_secrets_at_any_depth_and_any_case():
    value = {
        "userName": "trader", "apiKey": SECRET_KEY, "TOKEN": SECRET_JWT,
        "nested": [{"Password": "hunter2-hunter2"}, {"deeper": {"authorization": "Bearer " + SECRET_JWT}}],
        "api_key": SECRET_KEY, "newToken": SECRET_JWT, "note": f"url?access_token={SECRET_JWT}&x=1",
        "empty": {"token": None}, "price": 6789.25,
    }
    out = apilog.redact(value)
    text = json.dumps(out)
    assert SECRET_KEY not in text and SECRET_JWT not in text and "hunter2" not in text
    assert out["apiKey"] == out["TOKEN"] == out["api_key"] == out["newToken"] == apilog.REDACTED
    assert out["nested"][0]["Password"] == apilog.REDACTED
    assert out["nested"][1]["deeper"]["authorization"] == apilog.REDACTED
    assert out["note"] == "url?access_token=<redacted>&x=1"
    assert out["empty"] == {"token": None}            # nothing to hide, so it is left alone
    assert out["userName"] == "trader" and out["price"] == 6789.25
    assert value["apiKey"] == SECRET_KEY               # the original is not modified
    assert apilog.scrub_text(f"Authorization: Bearer {SECRET_KEY}") == "Authorization: Bearer <redacted>"


def test_the_login_call_is_stored_without_its_key_or_token():
    rec = apilog.Recorder()
    rec.observe_rest(rest(path="/Auth/loginKey", request={"userName": "trader", "apiKey": SECRET_KEY},
                          response={"success": True, "errorCode": 0, "errorMessage": None, "token": SECRET_JWT},
                          error=f"echo {SECRET_JWT}"))
    seq = rec.calls()["last_seq"]
    stored = rec.call(seq)
    everything = json.dumps([rec.calls(), stored, rec.calls(bodies=False), apilog.snippets_for(stored), rec.snapshot()])
    assert SECRET_KEY not in everything and SECRET_JWT not in everything
    assert stored["request"] == {"userName": "trader", "apiKey": "<redacted>"}
    assert stored["response"]["token"] == "<redacted>" and stored["error"] == "echo <redacted>"
    assert "<redacted>" in apilog.snippets_for(stored)["curl"] and "never paste secrets" in apilog.snippets_for(stored)["curl"]


def test_hub_payloads_are_redacted_too():
    rec = apilog.Recorder()
    rec.observe_hub("user", "in", "GatewayUserAccount", [{"action": 1, "data": {"id": 7, "token": SECRET_JWT}}])
    rec.observe_hub("market", "in", "GatewayQuote", ["CON.X", {"bid": 1.0, "apiKey": SECRET_KEY}])
    assert SECRET_JWT not in json.dumps(rec.hub()) and SECRET_KEY not in json.dumps(rec.hub())


# -- size limits --------------------------------------------------------------------------------


def test_large_bodies_are_shortened_before_storing():
    bars = [{"t": f"2026-10-{20 + i // 1440}T{i // 60 % 24:02d}:{i % 60:02d}:00+00:00", "o": 6789.25, "h": 6790.0,
             "l": 6788.0, "c": 6789.5, "v": 1234} for i in range(1500)]
    rec = apilog.Recorder()
    rec.observe_rest(rest(path="/History/retrieveBars", request={"contractId": "CON.F.US.EP.Z26"},
                          response={"success": True, "errorCode": 0, "errorMessage": None, "bars": bars}))
    stored = rec.call(1)
    kept = stored["response"]["bars"]
    assert stored["truncated"] is True and stored["response_bytes"] > 100_000
    assert len(json.dumps(stored["response"])) <= apilog.BODY_LIMIT + 2_000
    assert kept[0] == bars[0] and isinstance(kept[-1], str) and "more items not stored" in kept[-1]
    assert stored["result"] == "1,500 bars"                       # counted before shortening
    assert "Response: Shortened before storing" in stored["note"] and f"{stored['response_bytes']:,}" in stored["note"]
    assert stored["request"] == {"contractId": "CON.F.US.EP.Z26"} and stored["request_bytes"] < 100

    text, size, note = apilog.shrink("x" * 50_000, limit=1_000)
    assert size == 50_002 and len(text) <= 1_100 and "only the start" in note
    assert apilog.shrink({"a": 1}) == ('{"a":1}', 7, None)


# -- hub statistics -----------------------------------------------------------------------------


def test_hub_stats_aggregate_market_events_and_list_the_rare_ones():
    clock = Clock(1000.0)
    rec = apilog.Recorder(clock=clock)
    for i in range(50):                                  # 10 quotes a second for 5 seconds
        clock.t = 1000.0 + i * 0.1
        rec.observe_hub("market", "in", "GatewayQuote", ["CON.F.US.EP.Z26", {"lastPrice": 6000 + i}])
    rec.observe_hub("market", "in", "GatewayDepth", ["CON.F.US.EP.Z26", [{"price": 1.0, "volume": 2, "type": 1}] * 500])
    rec.observe_hub("market", "out", "SubscribeContractQuotes", ["CON.F.US.EP.Z26"])
    rec.observe_hub("user", "out", "SubscribeAccounts", [])
    rec.observe_hub("user", "in", "GatewayUserOrder", [{"action": 0, "data": {"id": 99, "accountId": 7, "status": 1}}])

    hub = rec.hub()
    events = {(e["hub"], e["direction"], e["name"]): e for e in hub["events"]}
    quote = events[("market", "in", "GatewayQuote")]
    assert quote["count"] == 50 and quote["kind"] == "summarised"
    assert 9.0 <= quote["rate"] <= 11.0
    assert quote["last_payload"] == ["CON.F.US.EP.Z26", {"lastPrice": 6049}]
    assert quote["kit"] == "market.on_quote(callback)"
    depth = events[("market", "in", "GatewayDepth")]
    assert len(json.dumps(depth["last_payload"])) <= apilog.HUB_PAYLOAD_LIMIT + 500 and depth["note"]
    assert events[("user", "in", "GatewayUserOrder")]["kind"] == "listed"
    assert events[("market", "out", "SubscribeContractQuotes")]["kit"] == 'await market.subscribe_quotes("CON.F.US.EP.Z26")'

    # Only the rare messages are kept one by one: no market "in" events in the ring.
    ring = hub["recent"]
    assert [(m["hub"], m["direction"], m["name"]) for m in ring] == [
        ("market", "out", "SubscribeContractQuotes"), ("user", "out", "SubscribeAccounts"), ("user", "in", "GatewayUserOrder")]
    assert [m["seq"] for m in ring] == [1, 2, 3] and hub["last_seq"] == 3
    assert ring[1]["kit"] == "await user.subscribe_accounts()"
    assert ring[2]["summary"] == "action=0 id=99 accountId=7 status=1" and ring[2]["args"][0]["data"]["id"] == 99
    assert [m["seq"] for m in rec.hub(after=2)["recent"]] == [3]

    snap = rec.snapshot()
    assert snap["hub_total"] == 54 and snap["hub_events_per_second"] >= 9.0

    clock.t += 30                                         # quiet for 30 s: the rate falls to zero
    assert {e["name"]: e["rate"] for e in rec.hub()["events"]}["GatewayQuote"] == 0.0
    assert rec.hub()["events"][0]["age_seconds"] >= 30

    rec.set_capture_hub(False)
    rec.observe_hub("market", "in", "GatewayQuote", ["CON.X", {}])
    rec.observe_hub("user", "out", "SubscribeOrders", [7])
    assert rec.hub()["capture_hub"] is False and rec.snapshot()["hub_total"] == 54 and len(rec.hub()["recent"]) == 3

    for i in range(apilog.HUB_RING_CAPACITY + 5):
        rec.set_capture_hub(True)
        rec.observe_hub("user", "out", "SubscribeOrders", [i])
    assert len(rec.hub()["recent"]) == apilog.HUB_RING_CAPACITY
    rec.clear()
    assert rec.hub()["events"] == [] and rec.hub()["recent"] == []


def test_snapshot_counts_calls_in_the_last_minute():
    clock = Clock(5000.0)
    rec = apilog.Recorder(clock=clock)
    for _ in range(3):
        rec.observe_rest(rest(path="/History/retrieveBars", request={"contractId": "X"}))
    clock.t += 40
    rec.observe_rest(rest())
    snap = rec.snapshot()
    assert snap["calls_total"] == 4 and snap["calls_last_minute"] == 4 and snap["bars_calls_last_30s"] == 0
    clock.t += 25                                         # the first three are now 65 s old
    snap = rec.snapshot()
    assert snap["calls_last_minute"] == 1 and snap["calls_total"] == 4 and snap["last_seq"] == 4
    rec.clear()
    assert rec.snapshot()["calls_last_minute"] == 1       # the API still counts cleared calls


# -- snippets -----------------------------------------------------------------------------------


def test_snippets_for_a_known_path_use_the_kit_method():
    entry = {"seq": 3, "path": "/Contract/search", "request": {"searchText": "ES", "live": False}}
    out = apilog.snippets_for(entry)
    assert out["method"] == "search_contracts" and out["url"] == "https://api.topstepx.com/api/Contract/search"
    python = out["python"]
    assert python.startswith("from topstepx import TopstepXClient\n")
    assert "client = TopstepXClient()" in python
    assert 'contracts = client.search_contracts(search_text="ES", live=False)' in python
    assert "for contract in contracts:" in python
    compile(python, "<snippet>", "exec")
    curl = out["curl"]
    assert 'curl -X POST "https://api.topstepx.com/api/Contract/search"' in curl
    assert '-H "Authorization: Bearer $TOPSTEPX_TOKEN"' in curl
    assert """-d '{"searchText": "ES", "live": false}'""" in curl
    assert "Order" not in curl


def test_snippets_for_an_unknown_path_fall_back_to_the_raw_post():
    out = apilog.snippets_for({"seq": 1, "path": "/Some/newEndpoint", "request": {"x": 1, "it's": None}})
    assert out["method"] is None
    assert out["python"].startswith("from topstepx import TopstepXClient\n") and "client = TopstepXClient()" in out["python"]
    assert 'client._http.post("/Some/newEndpoint", {' in out["python"]
    compile(out["python"], "<snippet>", "exec")
    assert """-d '{"x": 1, "it'\\''s": null}'""" in out["curl"]   # shell-safe single quotes
    # A known path with a body the kit method does not cover exactly also uses the raw post.
    odd = apilog.snippets_for({"seq": 2, "path": "/Contract/search", "request": {"searchText": "ES", "extra": 1}})
    assert odd["method"] is None and 'client._http.post("/Contract/search"' in odd["python"]


def test_order_snippets_carry_a_warning():
    body = {"accountId": 7, "contractId": "CON.F.US.EP.Z26", "type": 1, "side": 0, "size": 1, "limitPrice": 6000.25,
            "stopPrice": None, "trailPrice": None, "customTag": "dash-ab12", "stopLossBracket": None, "takeProfitBracket": None}
    out = apilog.snippets_for({"seq": 9, "path": "/Order/place", "request": body})
    assert out["method"] == "place_order"
    assert apilog.ORDER_WARNING in out["python"] and apilog.ORDER_WARNING in out["curl"]
    assert "order_type=OrderType.LIMIT" in out["python"] and "side=OrderSide.BUY" in out["python"]
    assert "stop_price" not in out["python"]             # nulls that are optional are left out
    compile(out["python"], "<snippet>", "exec")


def test_python_snippet_reconstructs_the_exact_kit_call(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self):
            pass

        def __getattr__(self, name):
            return lambda *args, **kwargs: calls.append((name, args, kwargs)) or []

        def close(self):
            calls.append(("close", (), {}))

    import topstepx
    monkeypatch.setattr(topstepx, "TopstepXClient", FakeClient)
    body = {"contractId": "CON.F.US.EP.Z26", "live": False, "startTime": "2026-10-27T13:00:00+00:00",
            "endTime": "2026-10-28T13:00:00Z", "unit": 2, "unitNumber": 5, "limit": 1500, "includePartialBar": True}
    code, method = apilog.python_snippet("/History/retrieveBars", body)
    assert method == "get_bars"
    exec(compile(code, "<snippet>", "exec"), {"print": lambda *a: None})   # noqa: S102 - our own generated code
    name, args, kwargs = calls[0]
    assert name == "get_bars" and args == () and calls[-1][0] == "close"
    assert kwargs == {
        "contract_id": "CON.F.US.EP.Z26",
        "start": datetime(2026, 10, 27, 13, tzinfo=timezone.utc), "end": datetime(2026, 10, 28, 13, tzinfo=timezone.utc),
        "unit": BarUnit.MINUTE, "unit_number": 5, "limit": 1500, "live": False, "include_partial_bar": True,
    }


def test_the_method_table_matches_the_kit_client():
    for path, (method, _result, _iterate, params) in apilog.CLIENT_METHODS.items():
        fn = getattr(TopstepXClient, method, None)
        assert fn is not None, f"{path}: TopstepXClient.{method} does not exist"
        names = set(inspect.signature(fn).parameters)
        for _key, param, _kind, _required in params:
            assert param in names, f"{path}: {method}() has no parameter {param}"
        # and the kit really calls that path
        assert f'"{path}"' in inspect.getsource(fn), f"TopstepXClient.{method} does not post to {path}"


# -- wiring and routes --------------------------------------------------------------------------


class FakeHttp:
    def __init__(self):
        self.observers = []

    def add_observer(self, fn):
        self.observers.append(fn)

    def emit(self, record):
        for fn in self.observers:
            fn(record)


class FakeHub:
    def __init__(self):
        self.observers = []

    def add_raw_observer(self, fn):
        self.observers.append(fn)

    def emit(self, direction, name, args):
        for fn in self.observers:
            fn(direction, name, args)


def wire(session):
    session.client._http = FakeHttp()
    session.market, session.user = FakeHub(), FakeHub()
    session.features = [apilog]
    session.feature_routes = feature_registry.collect_routes([apilog], session)
    return session.client._http, session.market, session.user


def test_feature_module_is_discovered_and_its_files_exist():
    assert apilog in feature_registry.discover("apilog")
    info = feature_registry.describe(apilog)
    assert info == {"name": "apilog", "title": "API calls", "description": apilog.DESCRIPTION,
                    "js": "apilog.js", "css": "apilog.css", "order": 10}
    static = ROOT / "dashboard" / "static" / "features"
    assert (static / "apilog.js").is_file() and (static / "apilog.css").is_file()
    assert (ROOT / "docs" / "features" / "apilog.md").is_file()
    assert getattr(apilog, "ORDER_ROUTES", None) is None    # it sends no orders, so it declares no order routes


def test_page_module_never_touches_order_routes():
    source = (ROOT / "dashboard" / "static" / "features" / "apilog.js").read_text(encoding="utf-8")
    assert "export async function mount(api, info)" in source
    for route in ("/api/orders", "/api/positions", "api.orders"):
        assert route not in source
    posts = [line for line in source.splitlines() if "api.post(" in line]
    assert posts and all("/api/apilog/" in line for line in posts)


def test_install_hooks_once_and_tolerates_missing_hooks():
    class Bare:
        pass

    bare = Bare()
    assert isinstance(apilog.install(bare), apilog.Recorder)   # no client, no hubs: nothing to hook, no error
    session = Bare()
    session.client, session.market, session.user = Bare(), FakeHub(), FakeHub()
    session.client._http = FakeHttp()
    rec = apilog.install(session)
    assert apilog.install(session) is rec and apilog.recorder_for(session) is rec
    import asyncio
    asyncio.run(apilog.start(session))
    assert len(session.client._http.observers) == 1 and len(session.market.observers) == 1 and len(session.user.observers) == 1
    session.client._http.emit(rest())
    session.market.emit("in", "GatewayQuote", ["CON.X", {"bid": 1}])
    session.user.emit("out", "SubscribeOrders", [7])
    assert rec.snapshot()["calls_total"] == 1 and rec.snapshot()["hub_total"] == 2
    assert {(e["hub"], e["name"]) for e in rec.hub()["events"]} == {("market", "GatewayQuote"), ("user", "SubscribeOrders")}


def test_routes_through_the_server(dash):
    http, market, user = wire(dash.session)
    http.emit(rest())
    http.emit(rest(path="/Auth/loginKey", request={"userName": "u", "apiKey": SECRET_KEY},
                   response={"success": True, "token": SECRET_JWT}))
    http.emit(rest(path="/Some/unknown", request={"a": 1}, status=500, response="nope", error="HTTP 500 from /Some/unknown: nope"))
    market.emit("in", "GatewayQuote", ["CON.F.US.EP.Z26", {"bid": 6000.25}])
    market.emit("out", "SubscribeContractQuotes", ["CON.F.US.EP.Z26"])
    user.emit("in", "GatewayUserAccount", [{"action": 1, "data": {"id": 7, "balance": 50000.0}}])

    status, body, _ = dash.get("/api/apilog/calls?after=0&limit=50")
    page = as_json(body)
    assert status == 200 and page["last_seq"] == 3 and [c["seq"] for c in page["calls"]] == [1, 2, 3]
    assert page["calls"][0]["request"] == {"searchText": "ES", "live": False}
    assert page["calls"][2]["ok"] is False and page["calls"][2]["status"] == 500
    assert SECRET_KEY not in body.decode() and SECRET_JWT not in body.decode()

    light = as_json(dash.get("/api/apilog/calls?after=1&bodies=0")[1])
    assert [c["seq"] for c in light["calls"]] == [2, 3] and "response" not in light["calls"][0]
    assert as_json(dash.get("/api/apilog/calls?after=0&limit=1")[1])["calls"][0]["seq"] == 3
    assert dash.get("/api/apilog/calls?after=abc")[0] == 400
    assert dash.get("/api/apilog/calls?after=-1")[0] == 400

    status, body, _ = dash.get("/api/apilog/call?seq=2")
    assert status == 200 and as_json(body)["request"]["apiKey"] == "<redacted>" and SECRET_KEY not in body.decode()
    assert dash.get("/api/apilog/call?seq=99")[0] == 404
    assert dash.get("/api/apilog/call")[0] == 400

    status, body, _ = dash.get("/api/apilog/snippets?seq=1")
    snip = as_json(body)
    assert status == 200 and snip["method"] == "search_contracts" and "client.search_contracts(" in snip["python"]
    assert snip["curl"].startswith('curl -X POST "https://api.topstepx.com/api/Contract/search"')
    unknown = as_json(dash.get("/api/apilog/snippets?seq=3")[1])
    assert unknown["method"] is None and 'client._http.post("/Some/unknown", {"a": 1})' in unknown["python"]
    assert dash.get("/api/apilog/snippets?seq=42")[0] == 404

    status, body, _ = dash.get("/api/apilog/hub")
    hub = as_json(body)
    assert status == 200 and hub["capture_hub"] is True
    assert {(e["hub"], e["direction"], e["name"]) for e in hub["events"]} == {
        ("market", "in", "GatewayQuote"), ("market", "out", "SubscribeContractQuotes"), ("user", "in", "GatewayUserAccount")}
    assert [m["name"] for m in hub["recent"]] == ["SubscribeContractQuotes", "GatewayUserAccount"]
    assert [m["name"] for m in as_json(dash.get("/api/apilog/hub?after=1")[1])["recent"]] == ["GatewayUserAccount"]

    snap = as_json(dash.get("/api/state")[1])["features"]["apilog"]
    assert snap["calls_total"] == 3 and snap["calls_last_minute"] == 3 and snap["capture_hub"] is True and snap["hub_total"] == 3

    # POSTs carry the dashboard token like every other POST.
    assert dash.post("/api/apilog/clear", {}, token=False)[0] == 403
    assert dash.post("/api/apilog/capture", {"hub": False}, token=False)[0] == 403
    status, body, _ = dash.post("/api/apilog/capture", {"hub": False})
    assert status == 200 and as_json(body) == {"ok": True, "capture_hub": False}
    assert dash.post("/api/apilog/capture", {"hub": "no"})[0] == 400
    market.emit("in", "GatewayQuote", ["CON.F.US.EP.Z26", {"bid": 6000.5}])
    assert as_json(dash.get("/api/apilog/hub")[1])["total"] == 3          # not recorded while off
    assert dash.post("/api/apilog/capture", {"hub": True})[0] == 200

    status, body, _ = dash.post("/api/apilog/clear", {})
    assert status == 200 and as_json(body) == {"ok": True, "last_seq": 3, "hub_last_seq": 2}
    assert as_json(dash.get("/api/apilog/calls")[1])["calls"] == []
    assert as_json(dash.get("/api/apilog/hub")[1])["events"] == []
    http.emit(rest())
    assert [c["seq"] for c in as_json(dash.get("/api/apilog/calls?after=3")[1])["calls"]] == [4]

    # Nothing in this feature reached the order path of the fake client.
    assert dash.session.client.place_calls == [] and dash.session.client.cancel_calls == [] and dash.session.client.close_calls == []
