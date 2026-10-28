"""Offline tests for dashboard/server.py (routes, token/Host/Origin rules, order-route guards)
and the pure helpers in dashboard/app.py and dashboard/cli.py.

The server is started on port 0 with a fake session: a real ``DashboardState`` when
``dashboard.state`` imports, otherwise a minimal fake with the methods the routes use.
``dashboard.orders`` / ``dashboard.indicators`` are stubbed in ``sys.modules`` only when
they are not available yet. Nothing here touches the network; the fake client records
``place_order`` calls instead of sending anything.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
from argparse import Namespace
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

from topstepx import Account, Bar, Contract, DepthEntry, MarketTrade, OrderSide, OrderType, Quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ES = Contract(id="CON.F.US.EP.Z26", name="ESZ26", description="E-mini S&P 500",
              tick_size=0.25, tick_value=12.5, active_contract=True)
PRACTICE = Account(id=7, name="PRAC-1", balance=50_000.0, simulated=True, can_trade=True)
FUNDED = Account(id=8, name="FUNDED-1", balance=150_000.0, simulated=False, can_trade=True)
SINCE = datetime(2026, 10, 26, tzinfo=timezone.utc)


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


# -- fallbacks for modules other agents may still be writing -----------------------------


def _module_available(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except ImportError:
        return False


@dataclass
class _StubTicket:
    account_id: int
    contract_id: str
    side: str
    order_type: str
    size: int
    limit_price: float | None = None
    stop_price: float | None = None


def _stub_orders_module() -> types.ModuleType:
    mod = types.ModuleType("dashboard.orders")

    def parse_ticket(body: dict) -> _StubTicket:
        try:
            ticket = _StubTicket(
                account_id=int(body["account_id"]), contract_id=str(body["contract_id"]),
                side=str(body["side"]).lower(), order_type=str(body.get("order_type", "market")).lower(),
                size=int(body["size"]),
                limit_price=body.get("limit_price"), stop_price=body.get("stop_price"),
            )
        except (KeyError, TypeError, ValueError):
            raise ValueError("ticket fields are missing or malformed") from None
        if ticket.side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        return ticket

    def _check_account(account: dict | None) -> None:
        if not account or account.get("simulated") is not True or account.get("can_trade") is not True:
            raise PermissionError("order entry needs a Practice Account that can trade")

    def preflight(ticket, account, contract) -> dict:
        if not account or account.get("simulated") is not True or account.get("can_trade") is not True:
            raise ValueError("order entry needs a Practice Account that can trade")
        if contract is None:
            raise ValueError("unknown contract")
        if not 0 < ticket.size <= 50:
            raise ValueError("size must be between 1 and 50")
        return {"account": account["name"], "simulated": True, "contract": contract["name"],
                "side": ticket.side.upper(), "type": ticket.order_type.upper(), "size": ticket.size,
                "limit_price": ticket.limit_price, "stop_price": ticket.stop_price,
                "tick_size": contract["tick_size"], "custom_tag": "dash-stub",
                "warning": "Topstep states that API orders are final and cannot be reviewed or reversed."}

    def send_order(client, ticket, summary) -> int:
        return client.place_order(
            account_id=ticket.account_id, contract_id=ticket.contract_id,
            order_type=OrderType.MARKET if ticket.order_type == "market" else OrderType.LIMIT,
            side=OrderSide.BUY if ticket.side == "buy" else OrderSide.SELL, size=ticket.size,
            limit_price=ticket.limit_price, stop_price=ticket.stop_price, custom_tag=summary["custom_tag"],
        )

    def cancel_order(client, account, order_id) -> None:
        _check_account(account)
        client.cancel_order(account["id"], order_id)

    def close_position(client, account, contract_id) -> None:
        _check_account(account)
        client.close_position(account["id"], contract_id)

    mod.OrderTicket = _StubTicket
    mod.parse_ticket, mod.preflight, mod.send_order = parse_ticket, preflight, send_order
    mod.cancel_order, mod.close_position = cancel_order, close_position
    return mod


def _stub_indicators_module() -> types.ModuleType:
    mod = types.ModuleType("dashboard.indicators")

    def indicator_series(candles, *, ema_periods, vwap_on, supertrend, session_start_utc=(22, 0)) -> dict:
        times = [c["time"] for c in candles]
        return {
            "ema": {str(p): [{"time": t, "value": c["close"]} for t, c in zip(times, candles)] for p in ema_periods},
            "vwap": [{"time": t, "value": c["close"]} for t, c in zip(times, candles)] if vwap_on else [],
            "supertrend": [{"time": t, "value": c["close"], "direction": 1} for t, c in zip(times, candles)]
            if supertrend else [],
        }

    mod.indicator_series = indicator_series
    return mod


HAS_REAL_ORDERS = _module_available("dashboard.orders")
HAS_REAL_INDICATORS = _module_available("dashboard.indicators")
HAS_REAL_STATE = _module_available("dashboard.state")
if not HAS_REAL_ORDERS:
    sys.modules["dashboard.orders"] = _stub_orders_module()
if not HAS_REAL_INDICATORS:
    sys.modules["dashboard.indicators"] = _stub_indicators_module()

import dashboard.server as server_mod  # noqa: E402

if HAS_REAL_STATE:
    from dashboard.state import DashboardState
else:
    DashboardState = None


class FakeState:
    """Only what the routes call, in the shapes ARCHITECTURE.md promises."""

    def __init__(self, since: datetime):
        self.since = since
        self.accounts: dict[int, dict] = {}
        self.account_id: int | None = None
        self.contracts: dict[str, dict] = {}
        self.bars: dict[str, dict] = {}
        self.depths: dict[str, dict] = {}
        self.tapes: dict[str, list] = {}
        self.focus_id: str | None = None

    def set_accounts(self, accounts):
        self.accounts = {a.id: {"id": a.id, "name": a.name, "balance": a.balance,
                                "simulated": a.simulated, "can_trade": a.can_trade} for a in accounts}

    def select_account(self, account_id):
        if account_id not in self.accounts:
            raise ValueError("unknown account")
        self.account_id = account_id

    def selected_account(self):
        return dict(self.accounts[self.account_id]) if self.account_id in self.accounts else None

    def order_entry_allowed(self):
        a = self.selected_account()
        return bool(a) and a["simulated"] is True and a["can_trade"] is True

    def add_contract(self, contract):
        self.contracts.setdefault(contract.id, {"id": contract.id, "name": contract.name,
                                                "description": contract.description,
                                                "tick_size": contract.tick_size, "tick_value": contract.tick_value})

    def has_contract(self, contract_id):
        return contract_id in self.contracts

    def contract(self, contract_id):
        return dict(self.contracts[contract_id]) if contract_id in self.contracts else None

    def contract_ids(self):
        return list(self.contracts)

    def set_bars(self, contract_id, bars):
        candles = [{"time": int(b.time.timestamp()), "open": b.open, "high": b.high, "low": b.low,
                    "close": b.close, "volume": b.volume} for b in sorted(bars, key=lambda b: b.time)]
        self.bars[contract_id] = {"version": self.bars.get(contract_id, {}).get("version", 0) + 1, "candles": candles}

    def candles(self, contract_id):
        stored = self.bars.get(contract_id)
        return [dict(c) for c in stored["candles"]] if stored else None

    def bars_version(self, contract_id):
        return self.bars.get(contract_id, {}).get("version", 0)

    def apply_depth(self, contract_id, entries):
        book = self.depths.setdefault(contract_id, {"bids": [], "asks": [], "best_bid": None, "best_ask": None,
                                                    "last_price": None, "last_size": None, "low": None,
                                                    "high": None, "version": 0})
        for e in entries:
            side = "asks" if e.type in {1, 3, 10} else "bids"
            book[side].append({"price": e.price, "size": e.volume})
        book["best_bid"] = max((b["price"] for b in book["bids"]), default=None)
        book["best_ask"] = min((a["price"] for a in book["asks"]), default=None)
        book["version"] += 1

    def depth(self, contract_id, levels=20):
        book = self.depths.get(contract_id)
        return None if book is None else {**book, "bids": book["bids"][:levels], "asks": book["asks"][:levels]}

    def apply_trades(self, contract_id, trades):
        tape = self.tapes.setdefault(contract_id, [])
        for t in trades:
            tape.append({"seq": len(tape) + 1, "time": t.timestamp.isoformat() if t.timestamp else None,
                         "kind": "trade", "price": t.price, "size": t.volume,
                         "side": "SELL" if t.type == 1 else "BUY", "bid": None, "ask": None})

    def tape_since(self, contract_id, seq):
        tape = self.tapes.get(contract_id, [])
        events = [e for e in tape if e["seq"] > seq]
        return events, (tape[-1]["seq"] if tape else 0)

    def set_focus(self, contract_id):
        self.focus_id = contract_id

    def focus(self):
        return self.focus_id

    def snapshot(self):
        return {"generated_at": datetime.now(timezone.utc).isoformat(), "since": self.since.isoformat(),
                "last_error": None, "connection": {"market": False, "user": False},
                "accounts": list(self.accounts.values()), "account": self.selected_account(),
                "order_entry_allowed": self.order_entry_allowed(), "focus": self.focus_id,
                "contracts": list(self.contracts.values()), "quotes": {}, "positions": [], "orders": [],
                "trades": [], "totals": {"realized": 0.0, "costs": 0.0, "unrealized": 0.0, "total": 0.0,
                                         "open_contracts": 0, "fills": 0, "pending_prices": []},
                "bars_versions": {cid: b["version"] for cid, b in self.bars.items()}}


class FakeClient:
    """Records order calls; never talks to anything."""

    def __init__(self):
        self.place_calls: list[dict] = []
        self.cancel_calls: list[tuple] = []
        self.close_calls: list[tuple] = []

    def place_order(self, *args, **kwargs) -> int:
        self.place_calls.append({"args": args, **kwargs})
        return 4242

    def cancel_order(self, account_id, order_id) -> None:
        self.cancel_calls.append((account_id, order_id))

    def close_position(self, account_id, contract_id) -> None:
        self.close_calls.append((account_id, contract_id))


class FakeSession:
    def __init__(self, state):
        self.state = state
        self.client = FakeClient()
        self.args = Namespace(unit="minute", unit_number=5, bars=300, refresh=60.0, refresh_focus=5.0)
        self.timeframe = {"unit": "minute", "unit_number": 5, "bars": 300, "bar_seconds": 300}
        self.loop = None
        self.selected: list[int] = []
        self.focused: list[str] = []
        self.watched: list[str] = []
        self.timeframes: list[dict] = []
        self.histories: list[tuple[str, int]] = []

    def call_async(self, coro, timeout=15.0):
        return asyncio.run(coro)  # handler threads have no running loop, so this is safe here

    async def select_account(self, account_id):
        self.state.select_account(account_id)
        self.selected.append(account_id)

    async def set_focus(self, contract_id):
        self.state.set_focus(contract_id)
        self.focused.append(contract_id)

    async def add_watch(self, text):
        self.watched.append(text)
        self.state.add_contract(ES)
        return ES.id

    async def load_history(self, contract_id, before):
        if not self.state.has_contract(contract_id) or before <= 0:
            raise ValueError("bad history request")
        self.histories.append((contract_id, before))
        self.state.add_history(contract_id, [bar(utc(2026, 10, 26, 13, 55)), bar(utc(2026, 10, 26, 14, 0))])
        return {"added": 2, "oldest": self.state.oldest_time(contract_id), "count": self.state.candle_count(contract_id), "complete": False}

    async def set_timeframe(self, unit, unit_number, bars):
        seconds = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
        if unit not in seconds or not 1 <= int(unit_number) <= 1000 or not 1 <= int(bars) <= 5000:
            raise ValueError("bad timeframe")
        self.timeframe = {"unit": unit, "unit_number": int(unit_number), "bars": int(bars),
                          "bar_seconds": seconds[unit] * int(unit_number)}
        self.timeframes.append(dict(self.timeframe))
        return dict(self.timeframe)


def make_state(selected: Account = PRACTICE):
    state = DashboardState(SINCE) if DashboardState is not None else FakeState(SINCE)
    state.set_accounts([PRACTICE, FUNDED])
    state.select_account(selected.id)
    state.add_contract(ES)
    return state


@pytest.fixture
def static_dir(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text(
        '<!doctype html><meta name="dashboard-token" content="__TOKEN__"><script src="/static/app.js"></script>',
        encoding="utf-8",
    )
    (tmp_path / "style.css").write_text("body { color: red; }", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("not served", encoding="utf-8")
    monkeypatch.setattr(server_mod, "STATIC_DIR", tmp_path)
    return tmp_path


class Dash:
    """A running server plus urllib helpers that return (status, body) instead of raising."""

    def __init__(self, session):
        self.session = session
        self.server = server_mod.make_server(session, port=0)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def get(self, path, headers=None):
        return self._call(Request(self.base + path, headers=headers or {}, method="GET"))

    def post(self, path, body=None, *, token=True, headers=None, raw: bytes | None = None):
        data = raw if raw is not None else json.dumps(body if body is not None else {}).encode("utf-8")
        h = {"Content-Type": "application/json"}
        if token:
            h["X-Dashboard-Token"] = server_mod.TOKEN
        h.update(headers or {})
        return self._call(Request(self.base + path, data=data, headers=h, method="POST"))

    @staticmethod
    def _call(request):
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, response.read(), response.headers
        except HTTPError as exc:
            return exc.code, exc.read(), exc.headers


def as_json(payload: bytes) -> dict:
    return json.loads(payload.decode("utf-8"))


@pytest.fixture
def dash(static_dir):
    d = Dash(FakeSession(make_state()))
    yield d
    d.close()


@pytest.fixture
def funded_dash(static_dir):
    d = Dash(FakeSession(make_state(selected=FUNDED)))
    yield d
    d.close()


def bar(t: datetime, o=1.0, c=1.5) -> Bar:
    return Bar(time=t, open=o, high=max(o, c) + 0.5, low=min(o, c) - 0.5, close=c, volume=10)


# -- page and static files ----------------------------------------------------------------------


def test_server_binds_loopback_only(dash):
    assert dash.server.server_address[0] == "127.0.0.1"
    with pytest.raises(ValueError):
        server_mod.make_server(dash.session, host="0.0.0.0", port=0)


def test_index_injects_the_token_and_never_serves_the_placeholder(dash):
    status, body, headers = dash.get("/")
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    page = body.decode("utf-8")
    assert "__TOKEN__" not in page and server_mod.TOKEN in page
    assert headers["Cache-Control"] == "no-store"


def test_static_files_have_content_types_and_no_traversal(dash, static_dir):
    status, body, headers = dash.get("/static/style.css")
    assert status == 200 and headers["Content-Type"].startswith("text/css") and body == b"body { color: red; }"
    assert headers["Cache-Control"] == "no-store"
    assert dash.get("/static/notes.txt")[0] == 404  # only html/css/js/svg/png/ico are served
    assert dash.get("/static/missing.js")[0] == 404
    for path in ("/static/../server.py", "/static/..%2Fserver.py", "/static/%2e%2e/server.py", "/static/"):
        assert dash.get(path)[0] in {403, 404}, path
    assert (static_dir.parent / "server.py").exists() is False  # nothing above the static dir is reachable anyway


# -- GET routes -----------------------------------------------------------------------------------


def test_state_route_returns_the_snapshot(dash):
    status, body, headers = dash.get("/api/state")
    assert status == 200 and headers["Content-Type"] == "application/json"
    snap = as_json(body)
    assert snap["account"]["name"] == "PRAC-1" and snap["order_entry_allowed"] is True
    assert [c["id"] for c in snap["contracts"]] == [ES.id]


def test_favicon_is_served_inline(dash):
    status, body, headers = dash.get("/favicon.ico")
    assert status == 200 and body.startswith(b"<svg") and headers.get("Content-Type", "").startswith("image/svg+xml")


def test_bars_404_without_bars_then_200_with_indicators(dash):
    status, body, _ = dash.get(f"/api/bars?contract={ES.id}")
    assert status == 404 and "error" in as_json(body)
    assert dash.get("/api/bars")[0] == 400

    dash.session.state.set_bars(ES.id, [bar(utc(2026, 10, 26, 14, 5)), bar(utc(2026, 10, 26, 14, 10), o=1.5, c=1.0),
                                        bar(utc(2026, 10, 26, 14, 15))])
    status, body, _ = dash.get(f"/api/bars?contract={ES.id}&ema=9,21&vwap=1&st=10,3")
    assert status == 200
    data = as_json(body)
    assert data["contract"] == ES.id and data["version"] == 1 and data["bar_seconds"] == 300
    assert data["timeframe"] == {"unit": "minute", "unit_number": 5, "bars": 300}
    assert data["count"] == 3 and data["oldest"] == int(utc(2026, 10, 26, 14, 5).timestamp()) and data["after"] is None
    assert [c["open"] for c in data["candles"]] == [1.0, 1.5, 1.0]
    assert [v["color"] for v in data["volume"]] == ["#26a69a66", "#ef535066", "#26a69a66"]
    assert set(data["indicators"]) == {"ema", "vwap", "supertrend"}
    assert set(data["indicators"]["ema"]) == {"9", "21"}
    assert isinstance(data["indicators"]["vwap"], list) and isinstance(data["indicators"]["supertrend"], list)

    status, body, _ = dash.get(f"/api/bars?contract={ES.id}&ema=&vwap=0&st=0")
    assert status == 200 and as_json(body)["indicators"] == {"ema": {}, "vwap": [], "supertrend": []}
    assert dash.get(f"/api/bars?contract={ES.id}&ema=abc")[0] == 400
    assert dash.get(f"/api/bars?contract={ES.id}&st=10")[0] == 400


def test_depth_and_tape_shapes(dash):
    assert dash.get(f"/api/depth?contract={ES.id}")[0] == 404
    dash.session.state.apply_depth(ES.id, [DepthEntry(price=5000.25, volume=12, type=1),
                                           DepthEntry(price=5000.0, volume=9, type=2)])
    status, body, _ = dash.get(f"/api/depth?contract={ES.id}&levels=5")
    assert status == 200
    book = as_json(body)
    assert {"bids", "asks", "best_bid", "best_ask", "last_price", "last_size", "low", "high", "version"} <= set(book)
    assert book["best_bid"] == 5000.0 and book["best_ask"] == 5000.25
    assert book["bids"][0] == {"price": 5000.0, "size": 9} and book["asks"][0] == {"price": 5000.25, "size": 12}
    assert dash.get(f"/api/depth?contract={ES.id}&levels=0")[0] == 400

    status, body, _ = dash.get(f"/api/tape?contract={ES.id}&after=0")
    assert status == 200 and as_json(body) == {"events": [], "last_seq": 0}
    dash.session.state.apply_trades(ES.id, [MarketTrade(price=5000.25, volume=2, type=0, timestamp=utc(2026, 10, 26, 14)),
                                            MarketTrade(price=5000.0, volume=1, type=1, timestamp=utc(2026, 10, 26, 14, 0, 1))])
    status, body, _ = dash.get(f"/api/tape?contract={ES.id}&after=0")
    tape = as_json(body)
    assert status == 200 and tape["last_seq"] == 2 and len(tape["events"]) == 2
    event = tape["events"][0]
    assert {"seq", "time", "kind", "price", "size", "side", "bid", "ask"} <= set(event)
    assert event["kind"] == "trade" and event["price"] == 5000.25 and event["side"] == "BUY"
    status, body, _ = dash.get(f"/api/tape?contract={ES.id}&after=1")
    assert [e["seq"] for e in as_json(body)["events"]] == [2]


def test_unknown_routes(dash):
    assert dash.get("/api/nope")[0] == 404
    assert dash.get("/nope")[0] == 404
    assert dash.post("/api/nope", {})[0] == 404


# -- POST security ----------------------------------------------------------------------------------


def test_post_without_token_is_rejected(dash):
    status, body, _ = dash.post("/api/focus", {"contract_id": ES.id}, token=False)
    assert status == 403 and dash.session.focused == []
    status, _, _ = dash.post("/api/focus", {"contract_id": ES.id}, token=False, headers={"X-Dashboard-Token": "nope"})
    assert status == 403 and dash.session.focused == []


def test_post_with_bad_host_is_rejected(dash):
    status, _, _ = dash.post("/api/focus", {"contract_id": ES.id}, headers={"Host": "evil.example.com"})
    assert status == 403
    status, _, _ = dash.post("/api/focus", {"contract_id": ES.id}, headers={"Host": f"127.0.0.1:{dash.port + 1}"})
    assert status == 403
    assert dash.session.focused == []
    status, _, _ = dash.post("/api/focus", {"contract_id": ES.id}, headers={"Host": f"localhost:{dash.port}"})
    assert status == 200


def test_post_with_bad_origin_is_rejected(dash):
    for origin in ("http://evil.example.com", "null", f"https://127.0.0.1:{dash.port}", f"http://127.0.0.1:{dash.port + 1}"):
        status, _, _ = dash.post("/api/focus", {"contract_id": ES.id}, headers={"Origin": origin})
        assert status == 403, origin
    assert dash.session.focused == []
    status, _, _ = dash.post("/api/focus", {"contract_id": ES.id}, headers={"Origin": f"http://localhost:{dash.port}"})
    assert status == 200
    status, _, _ = dash.post("/api/focus", {"contract_id": ES.id}, headers={"Origin": f"http://127.0.0.1:{dash.port}"})
    assert status == 200


def test_oversized_body_is_rejected(dash):
    raw = json.dumps({"contract_id": ES.id, "pad": "x" * (70 * 1024)}).encode("utf-8")
    try:
        status, _, _ = dash.post("/api/focus", raw=raw)
    except (URLError, ConnectionError, OSError):
        status = 413  # the server may close the socket before the client finishes sending
    assert status in {400, 413}
    assert dash.session.focused == []


def test_bad_json_bodies(dash):
    assert dash.post("/api/focus", raw=b"not json")[0] == 400
    assert dash.post("/api/focus", raw=b"[1, 2]")[0] == 400
    assert dash.post("/api/focus", {"contract_id": 5})[0] == 400
    assert dash.post("/api/account", {"account_id": "7"})[0] == 400


# -- POST routes ---------------------------------------------------------------------------------------


def test_focus_account_and_watch_routes(dash):
    status, body, _ = dash.post("/api/focus", {"contract_id": ES.id})
    assert status == 200 and as_json(body) == {"focus": ES.id} and dash.session.focused == [ES.id]

    status, body, _ = dash.post("/api/account", {"account_id": FUNDED.id})
    assert status == 200 and as_json(body)["account"]["id"] == FUNDED.id and dash.session.selected == [FUNDED.id]
    assert as_json(body)["order_entry_allowed"] is False
    assert dash.post("/api/account", {"account_id": 999})[0] == 400

    status, body, _ = dash.post("/api/watch", {"symbol": "ES"})
    assert status == 200 and as_json(body) == {"contract_id": ES.id} and dash.session.watched == ["ES"]
    assert dash.post("/api/watch", {"symbol": "  "})[0] == 400


def test_timeframe_route_changes_the_bars_the_page_gets(dash):
    status, body, _ = dash.post("/api/timeframe", {"unit": "minute", "unit_number": 15, "bars": 1000})
    assert status == 200
    assert as_json(body) == {"timeframe": {"unit": "minute", "unit_number": 15, "bars": 1000}, "bar_seconds": 900}
    assert dash.session.timeframes == [{"unit": "minute", "unit_number": 15, "bars": 1000, "bar_seconds": 900}]

    dash.session.state.set_bars(ES.id, [bar(utc(2026, 10, 26, 14, 0)), bar(utc(2026, 10, 26, 14, 15))])
    data = as_json(dash.get(f"/api/bars?contract={ES.id}")[1])
    assert data["bar_seconds"] == 900 and data["timeframe"] == {"unit": "minute", "unit_number": 15, "bars": 1000}

    # Validation: bad unit, zero/negative numbers, missing fields, non-numbers -> 400, timeframe unchanged.
    for bad in ({"unit": "week", "unit_number": 1, "bars": 100}, {"unit": "minute", "unit_number": 0, "bars": 100},
                {"unit": "minute", "unit_number": 5, "bars": 50_000}, {"unit": "minute", "unit_number": 5},
                {"unit": "minute", "unit_number": "five", "bars": 100}, {"unit": "", "unit_number": 5, "bars": 100}):
        assert dash.post("/api/timeframe", bad)[0] == 400, bad
    assert dash.session.timeframe["unit_number"] == 15
    # Like every POST, it needs the page token.
    assert dash.post("/api/timeframe", {"unit": "hour", "unit_number": 1, "bars": 500}, token=False,
                     headers={"X-Dashboard-Token": "nope"})[0] == 403
    assert dash.session.timeframe["unit"] == "minute"


def test_bars_after_returns_only_the_tail_with_matching_indicator_points(dash):
    times = [utc(2026, 10, 26, 14, minute) for minute in (0, 5, 10, 15, 20)]
    dash.session.state.set_bars(ES.id, [bar(t) for t in times])
    after = int(times[3].timestamp())
    status, body, _ = dash.get(f"/api/bars?contract={ES.id}&after={after}&ema=2&vwap=1&st=2,1")
    assert status == 200
    data = as_json(body)
    assert [c["time"] for c in data["candles"]] == [int(times[3].timestamp()), int(times[4].timestamp())]
    assert [v["time"] for v in data["volume"]] == [int(times[3].timestamp()), int(times[4].timestamp())]
    assert data["count"] == 5 and data["oldest"] == int(times[0].timestamp()) and data["after"] == after
    for points in [data["indicators"]["ema"]["2"], data["indicators"]["vwap"], data["indicators"]["supertrend"]]:
        assert points and all(p["time"] >= after for p in points)
    # The EMA tail equals the tail of the full series: indicators are computed over everything stored.
    full = as_json(dash.get(f"/api/bars?contract={ES.id}&ema=2&vwap=1&st=2,1")[1])
    assert data["indicators"]["ema"]["2"] == [p for p in full["indicators"]["ema"]["2"] if p["time"] >= after]
    assert dash.get(f"/api/bars?contract={ES.id}&after=abc")[0] == 400
    assert as_json(dash.get(f"/api/bars?contract={ES.id}&after={after + 10_000}")[1])["candles"] == []


def test_history_route_merges_older_bars(dash):
    dash.session.state.set_bars(ES.id, [bar(utc(2026, 10, 26, 14, 5)), bar(utc(2026, 10, 26, 14, 10))])
    before = int(utc(2026, 10, 26, 14, 5).timestamp())
    status, body, _ = dash.post("/api/history", {"contract_id": ES.id, "before": before})
    assert status == 200
    assert as_json(body) == {"added": 2, "oldest": int(utc(2026, 10, 26, 13, 55).timestamp()), "count": 4, "complete": False}
    assert dash.session.histories == [(ES.id, before)]
    assert dash.post("/api/history", {"contract_id": "CON.X", "before": before})[0] == 400
    assert dash.post("/api/history", {"contract_id": ES.id, "before": "soon"})[0] == 400
    assert dash.post("/api/history", {"contract_id": ES.id})[0] == 400
    assert dash.post("/api/history", {"contract_id": ES.id, "before": before}, token=False)[0] == 403


def test_session_load_history_asks_for_the_page_before_and_merges_it():
    from dashboard import app

    calls = []

    class BarsClient:
        def get_bars(self, contract_id, start, end, unit, unit_number, limit, include_partial_bar):
            calls.append({"contract_id": contract_id, "start": start, "end": end, "unit_number": unit_number,
                          "limit": limit, "include_partial_bar": include_partial_bar})
            # The gateway may include the bar at ``end``; only strictly older bars are history.
            return [bar(end), bar(end - app.timedelta(minutes=5)), bar(end - app.timedelta(minutes=10))]

    args = Namespace(account_id=None, since=None, unit="minute", unit_number=5, bars=300, watch=["ES"])
    state = DashboardState(SINCE)
    state.add_contract(ES)
    session = app.Session(args, client=BarsClient(), state=state, market=object(), user=object())
    before_dt = utc(2026, 10, 26, 14, 0)
    state.set_bars(ES.id, [bar(before_dt), bar(utc(2026, 10, 26, 14, 5))])

    result = asyncio.run(session.load_history(ES.id, int(before_dt.timestamp())))
    assert result == {"added": 2, "oldest": int(utc(2026, 10, 26, 13, 50).timestamp()), "count": 4, "complete": False}
    assert calls[0]["end"] == before_dt and calls[0]["limit"] == 300 and calls[0]["unit_number"] == 5
    assert calls[0]["include_partial_bar"] is False and calls[0]["start"] < before_dt
    # The next page overlaps what is stored: only genuinely new times count.
    result = asyncio.run(session.load_history(ES.id, int(utc(2026, 10, 26, 13, 50).timestamp())))
    assert result["added"] == 2 and result["count"] == 6  # 13:45 and 13:40 are new
    assert asyncio.run(session.load_history(ES.id, int(utc(2026, 10, 26, 13, 45).timestamp())))["added"] == 1  # 13:35
    with pytest.raises(ValueError):
        asyncio.run(session.load_history("CON.X", int(before_dt.timestamp())))
    with pytest.raises(ValueError):
        asyncio.run(session.load_history(ES.id, 0))
    with pytest.raises(ValueError):
        asyncio.run(session.load_history(ES.id, True))

    # A timeframe change drops the 5-minute candles (they are the wrong size now).
    asyncio.run(session.set_timeframe("minute", 15, 300))
    assert session.timeframe["unit_number"] == 15
    assert int(utc(2026, 10, 26, 13, 35).timestamp()) not in [c["time"] for c in state.candles(ES.id)]


def test_parse_timeframe_validates_like_the_cli():
    from dashboard import app

    assert app.parse_timeframe("minute", 5, 300) == {"unit": "minute", "unit_number": 5, "bars": 300, "bar_seconds": 300}
    assert app.parse_timeframe("hour", "4", "1000")["bar_seconds"] == 14_400
    assert app.parse_timeframe("day", 1, 5000)["bars"] == 5000
    for unit, number, bars in (("week", 1, 10), (None, 1, 10), ("minute", 0, 10), ("minute", 1001, 10),
                               ("minute", 5, 0), ("minute", 5, 5001), ("minute", "x", 10), ("minute", 5, None),
                               ("minute", True, 10)):
        with pytest.raises(ValueError):
            app.parse_timeframe(unit, number, bars)


def ticket(**overrides) -> dict:
    base = {"account_id": PRACTICE.id, "contract_id": ES.id, "side": "buy", "order_type": "limit",
            "size": 1, "limit_price": 5000.0}
    base.update(overrides)
    return base


def test_order_without_confirm_is_refused_and_nothing_is_sent(dash):
    for body in (ticket(), ticket(confirm="YES"), ticket(confirm="y"), ticket(confirm=True)):
        status, payload, _ = dash.post("/api/orders", body)
        assert status == 403 and as_json(payload)["ok"] is False, body
    assert dash.session.client.place_calls == []
    assert dash.post("/api/orders/cancel", {"order_id": 1})[0] == 403
    assert dash.post("/api/positions/close", {"contract_id": ES.id})[0] == 403
    assert dash.session.client.cancel_calls == [] and dash.session.client.close_calls == []


def test_order_without_token_is_refused_even_with_confirm(dash):
    status, _, _ = dash.post("/api/orders", ticket(confirm="yes"), token=False)
    assert status == 403 and dash.session.client.place_calls == []


def test_preflight_returns_a_summary_without_sending(dash):
    status, body, _ = dash.post("/api/orders/preflight", ticket())
    assert status == 200, body
    data = as_json(body)
    assert data["ok"] is True and data["summary"]["size"] == 1 and data["summary"]["simulated"] is True
    assert "warning" in data["summary"] and data["summary"]["custom_tag"].startswith("dash-")
    assert dash.session.client.place_calls == []

    status, body, _ = dash.post("/api/orders/preflight", ticket(size=0))
    assert status == 400 and as_json(body)["ok"] is False and as_json(body)["error"]
    status, body, _ = dash.post("/api/orders/preflight", ticket(contract_id="CON.F.US.NQ.Z26"))
    assert status == 400 and as_json(body)["ok"] is False
    status, body, _ = dash.post("/api/orders/preflight", ticket(account_id=FUNDED.id))
    assert status == 400 and as_json(body)["ok"] is False


def test_valid_order_on_a_practice_account_calls_place_order_once(dash):
    status, body, _ = dash.post("/api/orders", ticket(confirm="yes"))
    assert status == 200, body
    assert as_json(body)["order_id"] == 4242
    assert len(dash.session.client.place_calls) == 1
    call = dash.session.client.place_calls[0]
    assert call.get("account_id", call["args"][0] if call["args"] else None) == PRACTICE.id


def test_cancel_and_close_on_a_practice_account(dash):
    status, body, _ = dash.post("/api/orders/cancel", {"order_id": 31, "confirm": "yes"})
    assert status == 200 and as_json(body) == {"ok": True}
    assert dash.session.client.cancel_calls == [(PRACTICE.id, 31)]
    status, body, _ = dash.post("/api/positions/close", {"contract_id": ES.id, "confirm": "yes"})
    assert status == 200 and as_json(body) == {"ok": True}
    assert dash.session.client.close_calls == [(PRACTICE.id, ES.id)]
    assert dash.post("/api/orders/cancel", {"order_id": "31", "confirm": "yes"})[0] == 400


def test_funded_account_is_read_only_no_matter_what_the_browser_sends(funded_dash):
    client = funded_dash.session.client
    status, body, _ = funded_dash.post("/api/orders", ticket(account_id=FUNDED.id, confirm="yes"))
    assert status == 403 and as_json(body)["ok"] is False
    assert funded_dash.post("/api/orders/preflight", ticket(account_id=FUNDED.id))[0] == 403
    assert funded_dash.post("/api/orders/cancel", {"order_id": 1, "confirm": "yes"})[0] == 403
    assert funded_dash.post("/api/positions/close", {"contract_id": ES.id, "confirm": "yes"})[0] == 403
    assert client.place_calls == [] and client.cancel_calls == [] and client.close_calls == []
    # Reads still work on a funded account.
    assert funded_dash.get("/api/state")[0] == 200


def test_api_errors_map_to_502_and_unexpected_errors_to_500_without_details(dash, capsys):
    from topstepx import ApiError

    def failing_place(*args, **kwargs):
        raise ApiError("/Order/place", "rejected by the gateway")

    dash.session.client.place_order = failing_place
    status, body, _ = dash.post("/api/orders", ticket(confirm="yes"))
    assert status == 502 and "rejected by the gateway" in as_json(body)["error"]

    def exploding_place(*args, **kwargs):
        raise RuntimeError("secret internal detail")

    dash.session.client.place_order = exploding_place
    status, body, _ = dash.post("/api/orders", ticket(confirm="yes"))
    assert status == 500 and "secret internal detail" not in as_json(body)["error"]
    assert "secret internal detail" in capsys.readouterr().err  # traceback goes to the terminal, not the browser


@pytest.mark.skipif(not HAS_REAL_ORDERS, reason="brackets live in the real dashboard.orders")
def test_preflight_with_brackets_shows_the_legs_and_sends_nothing(dash):
    status, body, _ = dash.post("/api/orders/preflight", ticket(stop_ticks=8, target_ticks="16"))
    assert status == 200, body
    summary = as_json(body)["summary"]
    assert summary["stop_ticks"] == 8 and summary["target_ticks"] == 16 and "Auto OCO" in summary["bracket_note"]
    legs = summary["brackets"]
    assert legs["basis"] == "limit price" and legs["approx"] is False
    assert (legs["stop_loss"]["price"], legs["stop_loss"]["gateway_ticks"]) == (4998.0, -8)
    assert (legs["take_profit"]["price"], legs["take_profit"]["gateway_ticks"]) == (5004.0, 16)
    assert dash.session.client.place_calls == []


@pytest.mark.skipif(not HAS_REAL_ORDERS, reason="brackets live in the real dashboard.orders")
def test_order_with_brackets_hands_the_gateway_objects_to_place_order(dash):
    status, body, _ = dash.post("/api/orders", ticket(side="sell", stop_ticks=10, target_ticks=20, confirm="yes"))
    assert status == 200, body
    (call,) = dash.session.client.place_calls
    assert call["stop_loss_bracket"] == {"ticks": 10, "type": 4}
    assert call["take_profit_bracket"] == {"ticks": -20, "type": 1}
    assert call["side"] == OrderSide.SELL and call["limit_price"] == 5000.0

    status, body, _ = dash.post("/api/orders", ticket(confirm="yes"))   # no brackets: none are sent
    assert status == 200
    assert dash.session.client.place_calls[1]["stop_loss_bracket"] is None
    assert dash.session.client.place_calls[1]["take_profit_bracket"] is None


@pytest.mark.skipif(not (HAS_REAL_ORDERS and HAS_REAL_STATE), reason="needs the real orders + state modules")
def test_market_bracket_preview_is_measured_from_the_last_quote(dash):
    market = ticket(order_type="market", limit_price=None, stop_ticks=4, target_ticks=4)
    legs = as_json(dash.post("/api/orders/preflight", market)[1])["summary"]["brackets"]
    assert legs["approx"] is True and legs["reference_price"] is None and legs["stop_loss"]["price"] is None

    dash.session.state.apply_quote(ES.id, Quote(last_price=5000.5))
    legs = as_json(dash.post("/api/orders/preflight", market)[1])["summary"]["brackets"]
    assert legs["approx"] is True and legs["reference_price"] == 5000.5
    assert (legs["stop_loss"]["price"], legs["take_profit"]["price"]) == (4999.5, 5001.5)
    assert dash.session.client.place_calls == []


@pytest.mark.skipif(not HAS_REAL_ORDERS, reason="brackets live in the real dashboard.orders")
def test_bad_bracket_values_are_refused_before_anything_is_sent(dash):
    for bad in ({"stop_ticks": 0}, {"target_ticks": 1001}, {"stop_ticks": "abc"}, {"target_ticks": -3}, {"stop_ticks": 2.5}):
        status, body, _ = dash.post("/api/orders/preflight", ticket(**bad))
        assert status == 400 and as_json(body)["ok"] is False and "ticks" in as_json(body)["error"], bad
        status, body, _ = dash.post("/api/orders", ticket(**bad, confirm="yes"))
        assert status == 400 and as_json(body)["ok"] is False, bad
    assert dash.session.client.place_calls == []


# -- pure helpers in app.py / cli.py -----------------------------------------------------------------


@pytest.mark.skipif(not HAS_REAL_STATE, reason="dashboard.app imports dashboard.state")
def test_app_resolves_contracts_without_prompting_and_picks_the_initial_account():
    from dashboard import app

    NQ = Contract(id="CON.F.US.ENQ.Z26", name="NQZ26", description="E-mini Nasdaq", active_contract=True)
    ES_NEXT = Contract(id="CON.F.US.EP.H27", name="ESH27", description="E-mini S&P 500", active_contract=True)
    MES = Contract(id="CON.F.US.MES.Z26", name="MESZ26", description="Micro E-mini S&P", active_contract=True)

    class Client:
        def __init__(self, contracts):
            self.contracts = contracts

        def search_contracts(self, text, live=False):
            return self.contracts

    assert app.resolve_contract(Client([ES, MES]), "ES").id == ES.id
    assert app.resolve_contract(Client([ES, NQ]), "esz26").id == ES.id
    assert app.resolve_contract(Client([NQ]), "NQ").id == NQ.id
    with pytest.raises(ValueError, match="several"):
        app.resolve_contract(Client([ES, ES_NEXT]), "ES")
    with pytest.raises(ValueError):
        app.resolve_contract(Client([]), "ZZ")
    with pytest.raises(ValueError):
        app.resolve_contract(Client([ES]), "   ")

    args = Namespace(account_id=None, since=None, unit="minute", unit_number=5, bars=300, watch=["ES"])
    session = app.Session(args, client=object(), state=object(), market=object(), user=object())
    assert session._initial_account([FUNDED, PRACTICE]).id == PRACTICE.id
    assert session._initial_account([FUNDED]).id == FUNDED.id
    session.args.account_id = FUNDED.id
    assert session._initial_account([PRACTICE, FUNDED]).id == FUNDED.id
    session.args.account_id = 999
    with pytest.raises(ValueError):
        session._initial_account([PRACTICE])

    assert app.since_from_args(Namespace(since="2026-10-26")) == SINCE
    assert app.lookback_start(utc(2026, 10, 26), 60, 5, 300) == utc(2026, 10, 26) - app.timedelta(days=4)


def test_cli_arguments_validate_like_the_v1_tool():
    from dashboard import cli

    args = cli.parse_args(["--watch", "ES", "NQ", "--port", "0", "--no-open", "--seconds", "5"])
    assert args.watch == ["ES", "NQ"] and args.refresh == 60.0 and args.refresh_focus == 5.0 and args.unit == "minute"
    defaults = cli.parse_args(["--port", "0", "--no-open"])
    assert defaults.watch == ["ES", "NQ", "GC", "CL", "MES", "MNQ", "MGC", "MCL"] == list(cli.DEFAULT_WATCH)
    assert defaults.bars == 1500 and defaults.unit_number == 5
    defaults.watch.append("RTY")  # the default list is a fresh copy each parse
    assert cli.parse_args(["--port", "0"]).watch == list(cli.DEFAULT_WATCH)
    for bad in (["--refresh", "5"], ["--refresh-focus", "1"], ["--bars", "0"], ["--unit-number", "0"],
                ["--port", "70000"], ["--since", "yesterday"], ["--seconds", "0"]):
        with pytest.raises(SystemExit):
            cli.parse_args(bad)


def test_cli_startup_lines_are_ascii(static_dir):
    import ast

    from dashboard import cli

    source = Path(cli.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "print":
            for const in ast.walk(node):
                if isinstance(const, ast.Constant) and isinstance(const.value, str):
                    const.value.encode("ascii")

    session = FakeSession(make_state())
    session.watch_ids = [ES.id]
    import io
    from contextlib import redirect_stdout

    out = io.StringIO()
    with redirect_stdout(out):
        cli.print_startup(session, "http://127.0.0.1:8765/")
    text = out.getvalue()
    text.encode("ascii")
    assert "http://127.0.0.1:8765/" in text and "ESZ26" in text
    assert "Order entry: enabled for Practice Accounts only" in text

    session = FakeSession(make_state(selected=FUNDED))
    session.watch_ids = [ES.id]
    session.state.set_accounts([FUNDED])
    session.state.select_account(FUNDED.id)
    out = io.StringIO()
    with redirect_stdout(out):
        cli.print_startup(session, "http://127.0.0.1:8765/")
    assert "Order entry: disabled (no Practice Account)" in out.getvalue()


def test_launcher_and_package_never_hardcode_an_order():
    launcher = (ROOT / "tools" / "dashboard.py").read_text(encoding="utf-8")
    assert "from dashboard.cli import main" in launcher
    for call in ("place_order", "cancel_order", "close_position"):
        assert call not in launcher
    for name in ("app.py", "cli.py", "__main__.py", "__init__.py"):
        source = (ROOT / "dashboard" / name).read_text(encoding="utf-8")
        for call in ("place_order", "cancel_order", "modify_order", "close_position", "partial_close_position"):
            assert call not in source, f"dashboard/{name} must not send orders itself ({call})"
