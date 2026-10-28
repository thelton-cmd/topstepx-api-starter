"""Offline tests for the feature framework: discovery, route dispatch, state merge, order-route
guard, the indicator plugin contract, and the kit's REST/hub observer hooks."""

from __future__ import annotations

import json
import types

import httpx
import pytest

from dashboard import features as feature_registry
from dashboard import indicator_plugins
from tests.test_dashboard_server import ES, as_json, bar, dash, static_dir, utc  # noqa: F401 - fixtures
from topstepx.config import Settings
from topstepx.http import ApiHttp
from topstepx.realtime.base import BaseHub


# -- a tiny feature module built in memory ------------------------------------------------


def make_feature(name="demo", **attrs):
    module = types.ModuleType(f"dashboard.features.{name}")
    module.NAME = name
    module.TITLE = attrs.pop("TITLE", "Demo feature")
    module.DESCRIPTION = attrs.pop("DESCRIPTION", "Used by tests")
    module.JS = attrs.pop("JS", "demo.js")
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def install(session, *modules):
    session.features = list(modules)
    session.feature_routes = feature_registry.collect_routes(modules, session)


def test_feature_registry_describe_and_selection():
    module = make_feature(ORDER=5)
    assert feature_registry.name_of(module) == "demo"
    assert feature_registry.describe(module) == {
        "name": "demo", "title": "Demo feature", "description": "Used by tests", "js": "demo.js", "css": None, "order": 5,
    }
    assert feature_registry.discover("none") == []
    for module in feature_registry.discover():   # whatever is installed must at least be well formed
        feature_registry.describe(module)
        assert feature_registry.name_of(module)


def test_feature_routes_are_validated():
    good = make_feature(routes=lambda session: {"GET": {"/api/demo/ping": lambda q: (200, {"pong": True})}})
    table = feature_registry.collect_routes([good], None)
    assert set(table["GET"]) == {"/api/demo/ping"} and table["POST"] == {}
    for bad_path in ("/api/state", "/ping", "/api/orders"):
        bad = make_feature("bad", routes=lambda session, p=bad_path: {"GET": {p: lambda q: (200, {})}})
        with pytest.raises(ValueError):
            feature_registry.collect_routes([bad], None)
    dup = make_feature("dup", routes=lambda session: {"GET": {"/api/demo/ping": lambda q: (200, {})}})
    with pytest.raises(ValueError):
        feature_registry.collect_routes([good, dup], None)
    broken = make_feature("broken", routes=lambda session: (_ for _ in ()).throw(RuntimeError("boom")))
    assert feature_registry.collect_routes([broken], None) == {"GET": {}, "POST": {}}   # skipped, not fatal
    assert feature_registry.order_routes([make_feature(ORDER_ROUTES={"/api/demo/send"}), good]) == {"/api/demo/send"}


def test_feature_routes_dispatch_and_listing(dash):
    seen = {}

    def ping(query):
        seen["query"] = dict(query)
        return 200, {"pong": True, "q": query.get("x")}

    def echo(body):
        seen["body"] = body
        return 201, {"echo": body}

    def bad(body):
        raise ValueError("no good")

    module = make_feature(routes=lambda session: {"GET": {"/api/demo/ping": ping},
                                                  "POST": {"/api/demo/echo": echo, "/api/demo/bad": bad}},
                          snapshot=lambda session: {"count": 3})
    install(dash.session, module)

    status, body, _ = dash.get("/api/features")
    assert status == 200 and as_json(body) == {"features": [feature_registry.describe(module)]}

    status, body, _ = dash.get("/api/demo/ping?x=7")
    assert status == 200 and as_json(body) == {"pong": True, "q": "7"} and seen["query"] == {"x": "7"}
    assert dash.get("/api/demo/missing")[0] == 404

    status, body, _ = dash.post("/api/demo/echo", {"a": 1})
    assert status == 201 and as_json(body) == {"echo": {"a": 1}} and seen["body"] == {"a": 1}
    assert dash.post("/api/demo/echo", {"a": 1}, token=False)[0] == 403   # same token rule as every POST
    assert dash.post("/api/demo/bad", {})[0] == 400                        # ValueError -> 400

    snap = as_json(dash.get("/api/state")[1])
    assert snap["features"] == {"demo": {"count": 3}}

    # A feature whose snapshot() raises reports the error instead of breaking the page.
    install(dash.session, make_feature("boom", snapshot=lambda session: 1 / 0))
    assert as_json(dash.get("/api/state")[1])["features"]["boom"]["error"].startswith("ZeroDivisionError")


def test_feature_order_routes_get_the_practice_and_confirm_guard(dash):
    calls = []
    module = make_feature(ORDER_ROUTES={"/api/demo/send"},
                          routes=lambda session: {"POST": {"/api/demo/send": lambda body: (calls.append(body) or (200, {"ok": True}))}})
    install(dash.session, module)
    status, body, _ = dash.post("/api/demo/send", {"size": 1})
    assert status == 403 and as_json(body)["ok"] is False and "confirm" in as_json(body)["error"]
    status, body, _ = dash.post("/api/demo/send", {"size": 1, "confirm": "yes"})
    assert status == 200 and calls == [{"size": 1, "confirm": "yes"}]
    # Read-only account: refused before the handler runs.
    from tests.test_dashboard_server import FUNDED
    dash.session.state.select_account(FUNDED.id)
    status, body, _ = dash.post("/api/demo/send", {"size": 1, "confirm": "yes"})
    assert status == 403 and "Practice" in as_json(body)["error"] and len(calls) == 1


def test_feature_can_raise_the_post_body_cap_for_its_own_routes(dash):
    big = make_feature("big", BODY_LIMITS={"/api/big/in": 1024 * 1024},
                       routes=lambda session: {"POST": {"/api/big/in": lambda body: (200, {"chars": len(body.get("text", ""))})}})
    install(dash.session, big)
    text = "x" * (200 * 1024)
    status, body, _ = dash.post("/api/big/in", {"text": text})
    assert status == 200 and as_json(body) == {"chars": 200 * 1024}
    # The default cap still protects every other route.
    status, body, _ = dash.post("/api/focus", {"contract_id": ES.id, "pad": text})
    assert status == 413 and "64 KB" in as_json(body)["error"]
    assert feature_registry.body_limit([big], "/api/big/in") == 1024 * 1024
    assert feature_registry.body_limit([big], "/api/other") is None
    huge = make_feature("huge", BODY_LIMITS={"/api/huge/in": 10 ** 12})
    assert feature_registry.body_limit([huge], "/api/huge/in") == feature_registry.MAX_FEATURE_BODY


def test_feature_can_block_order_entry_with_a_reason(dash):
    from tests.test_dashboard_server import ticket

    blocker = {"reason": None}
    module = make_feature("guard", order_entry_blocked=lambda session: blocker["reason"])
    install(dash.session, module)
    assert as_json(dash.get("/api/state")[1])["order_entry_allowed"] is True
    blocker["reason"] = "Your daily loss limit of 500.00 was reached; order entry is paused until you change it."
    snap = as_json(dash.get("/api/state")[1])
    assert snap["order_entry_allowed"] is False and snap["order_entry_block"] == blocker["reason"]
    status, body, _ = dash.post("/api/orders/preflight", ticket())
    assert status == 403 and as_json(body) == {"ok": False, "error": blocker["reason"]}
    blocker["reason"] = None
    assert dash.post("/api/orders/preflight", ticket())[0] == 200
    broken = make_feature("broken", order_entry_blocked=lambda session: 1 / 0)
    install(dash.session, broken)
    assert as_json(dash.get("/api/state")[1])["order_entry_allowed"] is True   # a failing hook never blocks


# -- indicator plugins --------------------------------------------------------------------


def test_indicator_plugin_contract_with_the_sma_example():
    plugins = indicator_plugins.discover()
    assert "sma" in plugins
    sma = plugins["sma"]
    info = indicator_plugins.describe(sma)
    assert info["name"] == "sma" and info["overlay"] is True
    assert [p["name"] for p in info["params"]] == ["length", "source", "color"]

    params = indicator_plugins.parse_params(sma, {"length": "3", "source": "close", "junk": 1})
    assert params == {"length": 3, "source": "close", "color": "#f7dc6f"}
    assert indicator_plugins.parse_params(sma, {"length": 9999})["length"] == 500   # clamped
    with pytest.raises(ValueError):
        indicator_plugins.parse_params(sma, {"length": "abc"})
    with pytest.raises(ValueError):
        indicator_plugins.parse_params(sma, {"source": "typical"})

    candles = [{"time": 60 * i, "open": 1.0, "high": 2.0, "low": 0.5, "close": float(i), "volume": 1} for i in range(1, 6)]
    out = indicator_plugins.run(sma, candles, {"length": 3})
    assert out["name"] == "sma" and out["params"]["length"] == 3 and out["overlay"] is True
    series = out["series"][0]
    assert series["type"] == "line" and series["label"] == "SMA 3" and series["line_style"] == "solid"
    assert [p["value"] for p in series["points"]] == [2.0, 3.0, 4.0] and [p["time"] for p in series["points"]] == [180, 240, 300]
    assert json.dumps(out)   # JSON-ready

    assert indicator_plugins.source_values(candles[:1], "hlc3") == [(2.0 + 0.5 + 1.0) / 3]
    assert indicator_plugins.source_values([{"time": 0}], "close") == [None]


def test_indicator_plugin_output_is_normalised():
    module = types.ModuleType("dashboard.indicators_ext.fake")
    module.NAME = "fake"
    module.OVERLAY = False
    module.PARAMS = [{"name": "on", "type": "bool", "default": False}]
    module.compute = lambda candles, params: {"series": [
        {"id": "h", "type": "histogram", "points": [{"time": 1, "value": None}, {"time": 2, "value": 3}]},
        {"id": "m", "type": "markers", "points": [{"time": 2, "position": "aboveBar", "shape": "circle", "color": "#fff", "text": "x"}]},
    ]}
    out = indicator_plugins.run(module, [], {"on": "yes"})
    assert out["params"] == {"on": True} and out["overlay"] is False
    assert out["series"][0]["points"] == [{"time": 2, "value": 3.0}]
    assert out["series"][1]["points"] == [{"time": 2, "position": "aboveBar", "shape": "circle", "color": "#fff", "text": "x"}]
    module.compute = lambda candles, params: {"series": [{"type": "spline", "points": []}]}
    with pytest.raises(ValueError):
        indicator_plugins.run(module, [], {})
    module.compute = lambda candles, params: None
    with pytest.raises(ValueError):
        indicator_plugins.run(module, [], {})


# -- kit hooks: REST observer and hub raw observer -------------------------------------------


def test_api_http_observer_sees_every_call_without_the_token():
    records = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        if request.url.path.endswith("/boom"):
            return httpx.Response(500, text="server down")
        return httpx.Response(200, json={"success": True, "echo": payload})

    settings = Settings(username="u", api_key="k", api_base="https://api.example", rtc_base="wss://rtc.example")

    class Tokens:   # what ApiHttp needs from TokenManager, without a login round trip
        def get_token(self):
            return "secret-token"

        def invalidate(self):
            pass

    http = ApiHttp(settings, Tokens(), transport=httpx.MockTransport(handler))
    http.add_observer(records.append)
    http.add_observer(lambda r: 1 / 0)   # a broken observer never breaks the call

    assert http.post("/Contract/search", {"searchText": "ES"})["echo"] == {"searchText": "ES"}
    with pytest.raises(Exception):
        http.post("/boom", {})
    assert [r["path"] for r in records] == ["/Contract/search", "/boom"]
    ok, bad = records
    assert ok["status"] == 200 and ok["request"] == {"searchText": "ES"} and ok["response"]["success"] is True and ok["error"] is None
    assert bad["status"] == 500 and bad["response"] == "server down" and "HTTP 500" in bad["error"]
    assert "secret-token" not in json.dumps(records)
    assert all(isinstance(r["duration_ms"], float) and r["time"].endswith("+00:00") for r in records)


def test_hub_raw_observer_sees_incoming_events_and_outgoing_methods():
    import asyncio

    class Tokens:
        def get_token(self):
            return "t"

    hub = BaseHub("wss://rtc.example/hubs/market", Tokens())
    seen = []
    hub.add_raw_observer(lambda d, n, a: seen.append((d, n, a)))
    hub.add_raw_observer(lambda d, n, a: 1 / 0)   # logged, never fatal
    parsed = []
    hub._register_event("GatewayQuote", lambda args: (args[0], args[1]), [lambda cid, q: parsed.append((cid, q))])  # noqa: SLF001
    asyncio.run(hub._event_handlers["GatewayQuote"](["CON.X", {"bid": 1}]))  # noqa: SLF001
    assert parsed == [("CON.X", {"bid": 1})]
    assert seen == [("in", "GatewayQuote", ["CON.X", {"bid": 1}])]
    # Not connected: the subscription is remembered, nothing is sent and nothing is observed.
    asyncio.run(hub._subscribe("SubscribeContractQuotes", "CON.X"))  # noqa: SLF001
    assert ("SubscribeContractQuotes", ("CON.X",)) in hub._subscriptions and len(seen) == 1  # noqa: SLF001
