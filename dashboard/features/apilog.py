"""API calls panel ("Show me the API call").

Records every TopstepX REST request/response the dashboard makes (through the kit's
``ApiHttp.add_observer`` hook) and summarises the SignalR hub traffic (``BaseHub.add_raw_observer``),
so each click on the page can be traced to the exact API call behind it. The page can copy any
REST call as a curl command or as Python that uses this kit.

What is stored:

- REST: the newest 500 calls (path, status, duration, the JSON body sent and the JSON received).
  A body over about 20 KB is shortened before it is stored (long lists keep their first items),
  so a 1500-bar response does not fill memory.
- Hub: market data arrives many times a second, so per (hub, direction, event) only a count, a
  rate over the last 10 seconds, the last message and when it arrived are kept. User-hub events
  and every subscribe/unsubscribe call are rare, so the newest 200 of them are kept in full.

What is never stored: secrets. Any field named apiKey, token, password or authorization (any
case, nested anywhere, plus close variants such as newToken or access_token) is replaced with
"<redacted>" before anything is kept, and JWT-looking strings are masked too. Headers are never
recorded (the kit hooks never see them).

This module only observes. It never calls the API itself and never sends, changes or cancels an
order. It exists to teach the API mechanics; nothing it shows is trading advice.
"""

from __future__ import annotations

import json
import re
import threading
import time
import weakref
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

NAME = "apilog"
TITLE = "API calls"
DESCRIPTION = ("The exact TopstepX REST requests and responses, and a summary of the SignalR hub "
               "traffic, behind what the dashboard does. Copy any call as curl or Python.")
ORDER = 10
JS = "apilog.js"
CSS = "apilog.css"

API_BASE = "https://api.topstepx.com/api"
REST_CAPACITY = 500          # REST calls kept (newest)
HUB_RING_CAPACITY = 200      # user-hub events and subscribe/unsubscribe calls kept (newest)
BODY_LIMIT = 20_000          # bytes of JSON kept per request or response body
HUB_PAYLOAD_LIMIT = 4_000    # bytes of JSON shown per hub message
RATE_WINDOW = 10             # seconds behind the hub rate figures
REDACTED = "<redacted>"

# Field names whose values are always masked. Names are compared lower-case with "_" and "-"
# removed, so apiKey, api_key, API-Key and APIKEY all match.
SENSITIVE_FIELDS = frozenset({
    "apikey", "token", "password", "authorization",
    "newtoken", "accesstoken", "refreshtoken", "sessiontoken", "secret", "clientsecret",
})
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}")
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_SECRET_PARAM = re.compile(r"(?i)((?:access_token|token|apikey|api_key|password)=)[^&\s\"']+")

# Order-sending endpoints: their snippets carry a warning line.
ORDER_PATHS = frozenset({"/Order/place", "/Order/cancel", "/Order/modify",
                         "/Position/closeContract", "/Position/partialCloseContract"})

# -- redaction ------------------------------------------------------------------------------------


def is_sensitive(key: object) -> bool:
    return isinstance(key, str) and key.replace("_", "").replace("-", "").lower() in SENSITIVE_FIELDS


def scrub_text(text: str) -> str:
    """Mask JWTs, "Bearer <token>" and token=/apiKey= query parameters inside a string."""
    if len(text) < 16:
        return text
    if "eyJ" in text:
        text = _JWT.sub(REDACTED, text)
    if "earer" in text or "EARER" in text:
        text = _BEARER.sub(lambda m: m.group(1) + REDACTED, text)
    if "=" in text:
        text = _SECRET_PARAM.sub(lambda m: m.group(1) + REDACTED, text)
    return text


def redact(value: Any) -> Any:
    """A copy of ``value`` with every sensitive field masked, at any depth.

    A sensitive field that is null or empty stays as it is (there is nothing to hide, and showing
    "<redacted>" there would suggest a value came back).
    """
    if isinstance(value, dict):
        return {k: (REDACTED if is_sensitive(k) and v not in (None, "") else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value


# -- size limits ----------------------------------------------------------------------------------


def _encode(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False, separators=(",", ":"))


def _cut_lists(value: Any, keep: int) -> Any:
    if isinstance(value, dict):
        return {k: _cut_lists(v, keep) for k, v in value.items()}
    if isinstance(value, list):
        head = [_cut_lists(v, keep) for v in value[:keep]]
        if len(value) > keep:
            head.append(f"... {len(value) - keep:,} more items not stored")
        return head
    if isinstance(value, str) and len(value) > 2000:
        return value[:2000] + f"... ({len(value) - 2000:,} more characters not stored)"
    return value


def shrink(value: Any, limit: int = BODY_LIMIT) -> tuple[str, int, str | None]:
    """Return ``(json_text, original_bytes, note)``: the JSON to keep, at most about ``limit`` bytes.

    Small values are kept whole (note is None). Larger ones keep the first items of every long list
    (still valid JSON, so the page can pretty-print it); if that is not enough, the start of the
    JSON text is kept as a string.
    """
    text = _encode(value)
    size = len(text.encode("utf-8"))
    if size <= limit:
        return text, size, None
    for keep in (200, 100, 50, 20, 10, 5, 2, 1):
        cut = _encode(_cut_lists(value, keep))
        if len(cut.encode("utf-8")) <= limit:
            note = (f"Shortened before storing: the original was {size:,} bytes; "
                    f"long lists keep their first {keep} items.")
            return cut, size, note
    note = f"Shortened before storing: the original was {size:,} bytes; only the start of the JSON text is kept."
    return _encode(text[:limit] + " ..."), size, note


# -- one-line summaries ---------------------------------------------------------------------------


def _short_value(value: Any, width: int = 40) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, dict):
        return "{...}"
    if isinstance(value, list):
        return f"[{len(value)}]"
    text = str(value)
    return text if len(text) <= width else text[: width - 3] + "..."


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 3] + "..."


def summarize_request(body: Any) -> str:
    """``searchText=ES live=false``: the top-level fields that were sent (nulls left out)."""
    if isinstance(body, dict):
        return _clip(" ".join(f"{k}={_short_value(v)}" for k, v in body.items() if v is not None), 160)
    if body in (None, ""):
        return ""
    return _clip(_short_value(body, 160), 160)


def summarize_response(response: Any, error: str | None) -> str:
    """``20 contracts``, ``orderId=123``, ``errorCode 2: ...``: what came back, in a few words."""
    if isinstance(response, dict):
        if response.get("success") is False:
            code, message = response.get("errorCode"), response.get("errorMessage")
            return _clip(f"errorCode {code}" + (f": {message}" if message else ""), 160)
        for key, value in response.items():
            if isinstance(value, list):
                return f"{len(value):,} {key}"
        extras = [f"{k}={_short_value(v)}" for k, v in response.items()
                  if k not in ("success", "errorCode", "errorMessage") and v is not None][:3]
        return _clip(("success " + " ".join(extras)).strip(), 160)
    if error:
        return _clip(scrub_text(str(error)), 160)
    if isinstance(response, str):
        return _clip(response, 80)
    return ""


def summarize_args(args: Any) -> str:
    """A hub message's arguments in one line: ``CON.F.US.EP.Z26`` or ``action=0 id=12 status=1``."""
    parts = []
    for arg in args if isinstance(args, list) else [args]:
        if isinstance(arg, dict):
            data = arg.get("data") if isinstance(arg.get("data"), dict) else None
            fields = ([f"action={_short_value(arg.get('action'))}"] if data is not None and "action" in arg else [])
            source = data if data is not None else arg
            fields += [f"{k}={_short_value(v, 24)}" for k, v in source.items()
                       if not isinstance(v, (dict, list)) and v is not None][:5]
            parts.append(" ".join(fields) or "{...}")
        elif isinstance(arg, list):
            parts.append(f"[{len(arg)} items]")
        else:
            parts.append(_short_value(arg))
    return _clip(", ".join(parts), 160)


# -- hub helpers ----------------------------------------------------------------------------------

_HUB_EVENTS = {
    ("market", "GatewayQuote"): "market.on_quote(callback)",
    ("market", "GatewayTrade"): "market.on_trade(callback)",
    ("market", "GatewayDepth"): "market.on_depth(callback)",
    ("user", "GatewayUserAccount"): "user.on_account(callback)",
    ("user", "GatewayUserOrder"): "user.on_order(callback)",
    ("user", "GatewayUserPosition"): "user.on_position(callback)",
    ("user", "GatewayUserTrade"): "user.on_user_trade(callback)",
}
_HUB_METHODS = {
    ("market", "SubscribeContractQuotes"): "subscribe_quotes",
    ("market", "UnsubscribeContractQuotes"): "unsubscribe_quotes",
    ("market", "SubscribeContractTrades"): "subscribe_trades",
    ("market", "UnsubscribeContractTrades"): "unsubscribe_trades",
    ("market", "SubscribeContractMarketDepth"): "subscribe_depth",
    ("market", "UnsubscribeContractMarketDepth"): "unsubscribe_depth",
    ("user", "SubscribeAccounts"): "subscribe_accounts",
    ("user", "UnsubscribeAccounts"): "unsubscribe_accounts",
    ("user", "SubscribeOrders"): "subscribe_orders",
    ("user", "UnsubscribeOrders"): "unsubscribe_orders",
    ("user", "SubscribePositions"): "subscribe_positions",
    ("user", "UnsubscribePositions"): "unsubscribe_positions",
    ("user", "SubscribeTrades"): "subscribe_trades",
    ("user", "UnsubscribeTrades"): "unsubscribe_trades",
}


def kit_hint(hub: str, direction: str, name: str, args: Any = None) -> str | None:
    """The kit call behind a hub message (``market`` = MarketHub(client.tokens), ``user`` = UserHub)."""
    if direction == "in":
        return _HUB_EVENTS.get((hub, name))
    method = _HUB_METHODS.get((hub, name))
    if method is None:
        return None
    shown = ", ".join(_py(a) for a in args) if isinstance(args, list) else "..."
    return f"await {hub}.{method}({shown})"


class _HubStat:
    """Running figures for one (hub, direction, event name)."""

    __slots__ = ("hub", "direction", "name", "count", "first", "last", "payload", "buckets")

    def __init__(self, hub: str, direction: str, name: str, now: float):
        self.hub, self.direction, self.name = hub, direction, name
        self.count = 0
        self.first = now
        self.last = now
        self.payload: Any = None
        self.buckets = [[-1, 0] for _ in range(RATE_WINDOW)]   # [whole second, events in it]

    def bump(self, now: float, payload: Any) -> None:
        second = int(now)
        bucket = self.buckets[second % RATE_WINDOW]
        if bucket[0] != second:
            bucket[0], bucket[1] = second, 0
        bucket[1] += 1
        self.count += 1
        self.last = now
        self.payload = payload   # a reference; redacted and shortened only when served

    def rate(self, now: float) -> float:
        """Messages per second over the last RATE_WINDOW seconds (shorter right after the first one)."""
        second = int(now)
        total = sum(n for s, n in self.buckets if second - RATE_WINDOW < s <= second)
        span = min(RATE_WINDOW - 1 + (now - second), now - self.first)
        return total / max(span, 1.0)


# -- the recorder ---------------------------------------------------------------------------------


class Recorder:
    """Thread-safe store behind the panel. REST observers run on worker threads, hub observers on the
    event loop, and the routes on server threads; one lock covers all of it."""

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self._clock = clock
        self._now = now
        self._lock = threading.Lock()
        self._calls: deque[dict] = deque(maxlen=REST_CAPACITY)
        self._seq = 0
        self._total = 0
        self._recent: deque[tuple[float, str]] = deque()   # (monotonic, path) of the last 60 s
        self._stats: dict[tuple[str, str, str], _HubStat] = {}
        self._ring: deque[dict] = deque(maxlen=HUB_RING_CAPACITY)
        self._hub_seq = 0
        self._hub_total = 0
        self.capture_hub = True
        self.hooked: set[int] = set()   # id() of the objects whose observer hooks are installed

    # -- observers -------------------------------------------------------------------------------

    def observe_rest(self, record: dict) -> None:
        """``ApiHttp`` observer: redact, shorten and keep one REST call."""
        path = str(record.get("path") or "")
        request = redact(record.get("request"))
        response = redact(record.get("response"))
        error = record.get("error")
        error = scrub_text(str(error)) if error else None
        status = record.get("status")
        request_json, request_bytes, request_note = shrink(request)
        response_json, response_bytes, response_note = shrink(response)
        notes = [n for n in (request_note and "Request: " + request_note,
                             response_note and "Response: " + response_note) if n]
        entry = {
            "time": str(record.get("time") or self._now().isoformat()),
            "path": path,
            "status": status,
            "ok": error is None and status == 200,
            "error": error,
            "duration_ms": record.get("duration_ms"),
            "summary": summarize_request(request),
            "result": summarize_response(response, error),
            "request_bytes": request_bytes,
            "response_bytes": response_bytes,
            "truncated": bool(notes),
            "note": " ".join(notes) or None,
            "_request": request_json,
            "_response": response_json,
        }
        mono = self._clock()
        with self._lock:
            self._seq += 1
            self._total += 1
            entry["seq"] = self._seq
            self._calls.append(entry)
            self._recent.append((mono, path))
            self._prune(mono)

    def observe_hub(self, hub: str, direction: str, name: str, args: Any) -> None:
        """``BaseHub`` raw observer: count every message; keep the rare ones in full."""
        if not self.capture_hub:
            return
        now = self._clock()
        key = (hub, direction, str(name))
        with self._lock:
            stat = self._stats.get(key)
            if stat is None:
                stat = self._stats[key] = _HubStat(hub, direction, str(name), now)
            stat.bump(now, args)
            self._hub_total += 1
        if hub == "market" and direction == "in":
            return   # high-frequency market data: summarised only
        text, _, note = shrink(redact(args), HUB_PAYLOAD_LIMIT)
        entry = {
            "time": self._now().isoformat(),
            "hub": hub,
            "direction": direction,
            "name": str(name),
            "summary": summarize_args(args if isinstance(args, list) else [args]),
            "kit": kit_hint(hub, direction, str(name), redact(args) if isinstance(args, list) else None),
            "note": note,
            "_args": text,
        }
        with self._lock:
            self._hub_seq += 1
            entry["seq"] = self._hub_seq
            self._ring.append(entry)

    def _prune(self, mono: float) -> None:   # caller holds the lock
        while self._recent and mono - self._recent[0][0] > 60.0:
            self._recent.popleft()

    # -- reads -------------------------------------------------------------------------------------

    @staticmethod
    def _public(entry: dict, bodies: bool) -> dict:
        out = {k: v for k, v in entry.items() if not k.startswith("_")}
        if bodies:
            out["request"] = json.loads(entry["_request"])
            out["response"] = json.loads(entry["_response"])
        else:
            out.pop("note", None)
        return out

    def calls(self, after: int = 0, limit: int = 100, bodies: bool = True) -> dict:
        """Calls with seq > after: the newest ``limit`` of them, oldest first."""
        with self._lock:
            newer = [e for e in self._calls if e["seq"] > after]
            last_seq = self._seq
        newer = newer[-limit:] if limit > 0 else []
        return {"calls": [self._public(e, bodies) for e in newer], "last_seq": last_seq,
                "capacity": REST_CAPACITY}

    def call(self, seq: int) -> dict | None:
        with self._lock:
            entry = next((e for e in self._calls if e["seq"] == seq), None)
        return None if entry is None else self._public(entry, True)

    def hub(self, after: int = 0) -> dict:
        """Per-event figures plus the user-hub/subscription messages with seq > after (oldest first)."""
        mono, wall = self._clock(), self._now()
        with self._lock:
            stats = [(s.hub, s.direction, s.name, s.count, s.rate(mono), mono - s.last, s.payload)
                     for s in self._stats.values()]
            recent = [e for e in self._ring if e["seq"] > after]
            last_seq, total = self._hub_seq, self._hub_total
        events = []
        for hub, direction, name, count, rate, age, payload in sorted(stats, key=lambda s: (s[0], s[1], s[2])):
            text, _, note = shrink(redact(payload), HUB_PAYLOAD_LIMIT)
            events.append({
                "hub": hub, "direction": direction, "name": name,
                "kind": "summarised" if (hub == "market" and direction == "in") else "listed",
                "count": count, "rate": round(rate, 2), "age_seconds": round(age, 1),
                "last_time": (wall - timedelta(seconds=age)).isoformat(),
                "last_payload": json.loads(text), "note": note,
                "kit": kit_hint(hub, direction, name, redact(payload) if isinstance(payload, list) else None),
            })
        ring = [{**{k: v for k, v in e.items() if not k.startswith("_")}, "args": json.loads(e["_args"])}
                for e in recent]
        return {"capture_hub": self.capture_hub, "window_seconds": RATE_WINDOW, "total": total,
                "events": events, "recent": ring, "last_seq": last_seq, "capacity": HUB_RING_CAPACITY}

    def snapshot(self) -> dict:
        """Small counters for the header badge (merged into /api/state)."""
        mono = self._clock()
        with self._lock:
            self._prune(mono)
            last_minute = len(self._recent)
            bars_30s = sum(1 for t, p in self._recent if p == "/History/retrieveBars" and mono - t <= 30.0)
            rate = sum(s.rate(mono) for s in self._stats.values() if s.direction == "in")
            return {"calls_total": self._total, "calls_last_minute": last_minute,
                    "bars_calls_last_30s": bars_30s, "hub_events_per_second": round(rate, 1),
                    "hub_total": self._hub_total, "capture_hub": self.capture_hub, "last_seq": self._seq}

    # -- changes -------------------------------------------------------------------------------------

    def clear(self) -> dict:
        """Empty the lists. Sequence numbers keep rising, and the per-minute counter keeps counting
        (the API's rate limit counts those calls whatever the panel shows)."""
        with self._lock:
            self._calls.clear()
            self._ring.clear()
            self._stats.clear()
            return {"ok": True, "last_seq": self._seq, "hub_last_seq": self._hub_seq}

    def set_capture_hub(self, on: bool) -> dict:
        self.capture_hub = bool(on)
        return {"ok": True, "capture_hub": self.capture_hub}


# -- snippets -------------------------------------------------------------------------------------

# path -> (client method, name for the result or None, iterate over it?, [(body key, parameter, kind, required)])
_V, _DT, _UNIT, _TYPE, _SIDE = "value", "datetime", "bar_unit", "order_type", "order_side"
CLIENT_METHODS: dict[str, tuple[str, str | None, bool, list[tuple[str, str, str, bool]]]] = {
    "/Contract/search": ("search_contracts", "contracts", True,
                         [("searchText", "search_text", _V, True), ("live", "live", _V, False)]),
    "/Contract/available": ("list_available_contracts", "contracts", True, [("live", "live", _V, False)]),
    "/Contract/searchById": ("get_contract", "contract", False, [("contractId", "contract_id", _V, True)]),
    "/History/retrieveBars": ("get_bars", "bars", True, [
        ("contractId", "contract_id", _V, True), ("startTime", "start", _DT, True),
        ("endTime", "end", _DT, True), ("unit", "unit", _UNIT, False), ("unitNumber", "unit_number", _V, False),
        ("limit", "limit", _V, False), ("live", "live", _V, False),
        ("includePartialBar", "include_partial_bar", _V, False)]),
    "/Account/search": ("search_accounts", "accounts", True, [("onlyActiveAccounts", "only_active", _V, False)]),
    "/Order/place": ("place_order", "order_id", False, [
        ("accountId", "account_id", _V, True), ("contractId", "contract_id", _V, True),
        ("type", "order_type", _TYPE, True), ("side", "side", _SIDE, True), ("size", "size", _V, True),
        ("limitPrice", "limit_price", _V, False), ("stopPrice", "stop_price", _V, False),
        ("trailPrice", "trail_price", _V, False), ("customTag", "custom_tag", _V, False),
        ("stopLossBracket", "stop_loss_bracket", _V, False),
        ("takeProfitBracket", "take_profit_bracket", _V, False)]),
    "/Order/cancel": ("cancel_order", None, False,
                      [("accountId", "account_id", _V, True), ("orderId", "order_id", _V, True)]),
    "/Order/modify": ("modify_order", None, False, [
        ("accountId", "account_id", _V, True), ("orderId", "order_id", _V, True), ("size", "size", _V, False),
        ("limitPrice", "limit_price", _V, False), ("stopPrice", "stop_price", _V, False),
        ("trailPrice", "trail_price", _V, False)]),
    "/Order/search": ("search_orders", "orders", True, [
        ("accountId", "account_id", _V, True), ("startTimestamp", "start", _DT, True),
        ("endTimestamp", "end", _DT, False)]),
    "/Order/searchOpen": ("search_open_orders", "orders", True, [("accountId", "account_id", _V, True)]),
    "/Position/searchOpen": ("search_open_positions", "positions", True, [("accountId", "account_id", _V, True)]),
    "/Position/closeContract": ("close_position", None, False,
                                [("accountId", "account_id", _V, True), ("contractId", "contract_id", _V, True)]),
    "/Position/partialCloseContract": ("partial_close_position", None, False, [
        ("accountId", "account_id", _V, True), ("contractId", "contract_id", _V, True), ("size", "size", _V, True)]),
    "/Trade/search": ("search_trades", "trades", True, [
        ("accountId", "account_id", _V, True), ("startTimestamp", "start", _DT, True),
        ("endTimestamp", "end", _DT, False)]),
}
_SINGULAR = {"contracts": "contract", "bars": "bar", "accounts": "account", "orders": "order",
             "positions": "position", "trades": "trade"}
ORDER_WARNING = "Running this sends a real order request to the API. Use it only on a Practice Account."


def _py(value: Any, indent: int = 0) -> str:
    """A Python literal for a JSON value: strings in double quotes, true -> True, null -> None.

    Short dicts and lists stay on one line; longer ones get one item per line, indented.
    """
    if isinstance(value, str):
        return json.dumps(value)
    if value is None or isinstance(value, (bool, int, float)):
        return repr(value)
    if isinstance(value, dict):
        items, opening, closing = [f"{_py(str(k))}: {_py(v, indent + 4)}" for k, v in value.items()], "{", "}"
    elif isinstance(value, (list, tuple)):
        items, opening, closing = [_py(v, indent + 4) for v in value], "[", "]"
    else:
        return json.dumps(str(value))
    flat = opening + ", ".join(items) + closing
    if indent + len(flat) <= 72 and "\n" not in flat:
        return flat
    pad = " " * (indent + 4)
    return opening + "\n" + "".join(f"{pad}{item},\n" for item in items) + " " * indent + closing


def _enum_literal(kind: str, value: Any) -> tuple[str, str | None]:
    from topstepx.enums import BarUnit, OrderSide, OrderType   # local: keeps module import light
    enum = {_UNIT: BarUnit, _TYPE: OrderType, _SIDE: OrderSide}[kind]
    try:
        member = enum(value)
    except (TypeError, ValueError):
        return _py(value), None
    return f"{enum.__name__}.{member.name}", enum.__name__


def _python_argument(kind: str, value: Any) -> tuple[str, set[str]]:
    """(source text, imports it needs) for one argument."""
    if kind == _DT and isinstance(value, str):
        text = value[:-1] + "+00:00" if value.endswith("Z") else value   # Python 3.10 rejects "Z"
        return f"datetime.fromisoformat({json.dumps(text)})", {"from datetime import datetime"}
    if kind in (_UNIT, _TYPE, _SIDE) and value is not None:
        literal, enum_name = _enum_literal(kind, value)
        return literal, ({f"from topstepx import {enum_name}"} if enum_name else set())
    return _py(value), set()


def python_snippet(path: str, body: Any) -> tuple[str, str | None]:
    """Python that repeats this call with the kit. Returns (code, client method name or None)."""
    body = body if isinstance(body, dict) else {}
    spec = CLIENT_METHODS.get(path)
    lines_head = ["from topstepx import TopstepXClient"]
    if spec is not None:
        method, result, iterate, params = spec
        known = {key for key, *_ in params}
        missing = [key for key, _, _, required in params if required and key not in body]
        if set(body) - known or missing:
            spec = None   # the body does not match the kit method exactly; show the raw call instead
    warning = [f"# {ORDER_WARNING}"] if path in ORDER_PATHS else []
    if spec is None:
        code = [*lines_head, "", "client = TopstepXClient()  # reads TOPSTEPX_USERNAME and TOPSTEPX_API_KEY from .env",
                *warning,
                f"body = client._http.post({json.dumps(path)}, {_py(body)})",
                "print(body)", "client.close()"]
        return "\n".join(code) + "\n", None
    imports: set[str] = set()
    args = []
    for key, param, kind, required in params:
        if key not in body or (body[key] is None and not required):
            continue
        text, needs = _python_argument(kind, body[key])
        imports |= needs
        args.append(f"{param}={text}")
    one_line = f"client.{method}({', '.join(args)})"
    target = f"{result} = " if result else ""
    if len(target) + len(one_line) <= 88 and "\n" not in one_line:
        call = [target + one_line]
    else:
        call = [f"{target}client.{method}("] + [f"    {a}," for a in args] + [")"]
    if result and iterate:
        tail = [f"for {_SINGULAR.get(result, 'item')} in {result}:", f"    print({_SINGULAR.get(result, 'item')})"]
    elif result:
        tail = [f"print({result})"]
    else:
        tail = ['print("done")']
    code = [*lines_head, *sorted(imports, key=lambda s: (not s.startswith("from topstepx"), s)), "",
            "client = TopstepXClient()  # reads TOPSTEPX_USERNAME and TOPSTEPX_API_KEY from .env",
            *warning, *call, *tail, "client.close()"]
    return "\n".join(code) + "\n", method


def curl_snippet(path: str, body: Any, api_base: str = API_BASE) -> str:
    """The same call as a curl command (bash/zsh quoting). The token comes from $TOPSTEPX_TOKEN."""
    url = api_base.rstrip("/") + path
    url = re.sub(r'([\\"$`])', r"\\\1", url)
    data = json.dumps(body if body is not None else {}, ensure_ascii=True).replace("'", "'\\''")
    lines = []
    if path in ORDER_PATHS:
        lines.append(f"# {ORDER_WARNING}")
    if REDACTED in data:
        lines.append("# Replace <redacted> with your own value in your terminal; never paste secrets into a chat.")
    lines += [f'curl -X POST "{url}" \\',
              '  -H "Authorization: Bearer $TOPSTEPX_TOKEN" \\',
              '  -H "Content-Type: application/json" \\',
              '  -H "Accept: application/json" \\',
              f"  -d '{data}'"]
    return "\n".join(lines) + "\n"


def snippets_for(entry: dict, api_base: str = API_BASE) -> dict:
    path, body = entry["path"], entry.get("request")
    python, method = python_snippet(path, body)
    out = {"seq": entry["seq"], "path": path, "url": api_base.rstrip("/") + path, "method": method,
           "curl": curl_snippet(path, body, api_base), "python": python}
    if entry.get("truncated") and (entry.get("note") or "").startswith("Request"):
        out["note"] = "The stored request was shortened, so this body is incomplete."
    return out


# -- wiring ---------------------------------------------------------------------------------------

_RECORDERS: "weakref.WeakKeyDictionary[object, Recorder]" = weakref.WeakKeyDictionary()
_FALLBACK: dict[int, Recorder] = {}   # for session objects that cannot be weakly referenced
_REGISTRY_LOCK = threading.Lock()


def recorder_for(session) -> Recorder:
    with _REGISTRY_LOCK:
        try:
            rec = _RECORDERS.get(session)
            if rec is None:
                rec = _RECORDERS[session] = Recorder()
        except TypeError:
            rec = _FALLBACK.setdefault(id(session), Recorder())
        return rec


def install(session) -> Recorder:
    """Hook the REST observer and both hub observers once per session (missing pieces are skipped)."""
    rec = recorder_for(session)
    http = getattr(getattr(session, "client", None), "_http", None)
    hooks = [(http, "add_observer", rec.observe_rest)]
    for hub_name in ("market", "user"):
        hub = getattr(session, hub_name, None)
        hooks.append((hub, "add_raw_observer",
                      lambda d, n, a, _h=hub_name: rec.observe_hub(_h, d, n, a)))
    for target, method, callback in hooks:
        add = getattr(target, method, None) if target is not None else None
        if add is None or id(target) in rec.hooked:
            continue
        try:
            add(callback)
            rec.hooked.add(id(target))
        except Exception:  # noqa: BLE001 - one missing hook never stops the dashboard
            pass
    return rec


def _api_base(session) -> str:
    base = getattr(getattr(getattr(session, "client", None), "settings", None), "api_base", None)
    return base if isinstance(base, str) and base.startswith("http") else API_BASE


def _int_param(query: dict, key: str, default: int | None, *, minimum: int = 0, maximum: int | None = None) -> int:
    text = str(query.get(key) or "").strip()
    if not text:
        if default is None:
            raise ValueError(f"{key} is required")
        return default
    try:
        value = int(text)
    except ValueError:
        raise ValueError(f"{key} must be a whole number") from None
    if value < minimum:
        raise ValueError(f"{key} must be {minimum} or more")
    return min(value, maximum) if maximum is not None else value


def _flag(query: dict, key: str, default: bool) -> bool:
    text = str(query.get(key) or "").strip().lower()
    if not text:
        return default
    return text not in ("0", "false", "no", "off")


def routes(session) -> dict:
    # Hooks go in now (Session.__init__ collects routes), so the startup calls are recorded too.
    rec = install(session)
    base = _api_base(session)

    def calls(query):
        return 200, rec.calls(_int_param(query, "after", 0),
                              _int_param(query, "limit", 100, minimum=1, maximum=REST_CAPACITY),
                              _flag(query, "bodies", True))

    def one_call(query):
        entry = rec.call(_int_param(query, "seq", None, minimum=1))
        return (200, entry) if entry else (404, {"error": "that call is no longer stored"})

    def hub(query):
        return 200, rec.hub(_int_param(query, "after", 0))

    def snippets(query):
        entry = rec.call(_int_param(query, "seq", None, minimum=1))
        return (200, snippets_for(entry, base)) if entry else (404, {"error": "that call is no longer stored"})

    def clear(body):
        return 200, rec.clear()

    def capture(body):
        value = body.get("hub") if isinstance(body, dict) else None
        if not isinstance(value, bool):
            raise ValueError('send {"hub": true} or {"hub": false}')
        return 200, rec.set_capture_hub(value)

    return {
        "GET": {"/api/apilog/calls": calls, "/api/apilog/call": one_call,
                "/api/apilog/hub": hub, "/api/apilog/snippets": snippets},
        "POST": {"/api/apilog/clear": clear, "/api/apilog/capture": capture},
    }


async def start(session) -> None:
    install(session)   # no-op when routes() already hooked everything


def snapshot(session) -> dict:
    return recorder_for(session).snapshot()
