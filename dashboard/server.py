"""The local HTTP server: the page, the JSON routes, the token check and static files.

Everything here runs on ``ThreadingHTTPServer`` worker threads. Handlers read
``session.state`` (which locks internally), call REST on ``session.client`` directly
(httpx and TokenManager are thread-safe) and reach the asyncio side only through
``session.call_async(coro)``.

Security model (see ARCHITECTURE.md): the server binds 127.0.0.1 only; every POST
must carry the per-process token in ``X-Dashboard-Token``, a loopback ``Host`` and,
when present, a loopback ``Origin``; bodies are capped at 64 KB; order routes also
need ``state.order_entry_allowed()`` and ``"confirm": "yes"``. Request bodies are
never logged.
"""

from __future__ import annotations

import concurrent.futures
import hmac
import json
import secrets
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse, urlsplit

from topstepx import TopstepXError

from . import features as feature_registry
from . import indicators, orders

TOKEN = secrets.token_urlsafe(32)  # one per process; the page reads it from a meta tag

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY_BYTES = 64 * 1024
DRAIN_LIMIT_BYTES = 64 * 1024 * 1024  # read-and-discard oversized bodies up to this, so the client gets a clean 413
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86_400}
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}
ORDER_ROUTES = {"/api/orders", "/api/orders/preflight", "/api/orders/cancel", "/api/positions/close"}
VOLUME_UP, VOLUME_DOWN = "#26a69a66", "#ef535066"


class RouteError(Exception):
    """A response the route wants to send instead of a result."""

    def __init__(self, status: int, message: str, **extra: object):
        super().__init__(message)
        self.status = status
        self.payload = {**extra, "error": message}


# Browser tab icon, served inline so the console never logs a 404 for /favicon.ico.
FAVICON_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    b'<rect width="32" height="32" rx="6" fill="#151c18"/>'
    b'<path d="M7 20l5-6 5 4 8-10" fill="none" stroke="#26a69a" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>'
    b'</svg>'
)


def bar_seconds(args) -> int:
    """Seconds per chart bar, from the ``--unit`` / ``--unit-number`` arguments."""
    return UNIT_SECONDS[args.unit] * args.unit_number


def state_snapshot(session) -> dict:
    """``state.snapshot()`` plus every feature's ``snapshot(session)`` under ``"features"``."""
    snap = session.state.snapshot()
    extras = {}
    for module in getattr(session, "features", None) or []:
        fn = getattr(module, "snapshot", None)
        if fn is None:
            continue
        try:
            extras[feature_registry.name_of(module)] = fn(session)
        except Exception as exc:  # noqa: BLE001 - one feature never breaks the page
            extras[feature_registry.name_of(module)] = {"error": f"{type(exc).__name__}: {exc}"}
    if extras:
        snap["features"] = extras
    reason = feature_registry.block_reason(getattr(session, "features", None) or [], session)
    if reason:
        snap["order_entry_allowed"] = False
        snap["order_entry_block"] = reason
    return snap


def feature_block_reason(session) -> str | None:
    return feature_registry.block_reason(getattr(session, "features", None) or [], session)


def feature_routes(session, method: str) -> dict:
    table = getattr(session, "feature_routes", None) or {}
    return table.get(method, {}) if isinstance(table, dict) else {}


def feature_body_limit(session, path: str) -> int:
    """The body cap for a POST: a feature's BODY_LIMITS for that path, else MAX_BODY_BYTES."""
    return feature_registry.body_limit(getattr(session, "features", None) or [], path) or MAX_BODY_BYTES


def feature_order_routes(session) -> set[str]:
    return feature_registry.order_routes(getattr(session, "features", None) or [])


def features_listing(session) -> list[dict]:
    return [feature_registry.describe(m) for m in getattr(session, "features", None) or []]


def session_timeframe(session) -> dict:
    """The session's current chart timeframe (falls back to its CLI arguments)."""
    timeframe = getattr(session, "timeframe", None)
    if isinstance(timeframe, dict) and {"unit", "unit_number", "bars"} <= set(timeframe):
        return {k: timeframe[k] for k in ("unit", "unit_number", "bars")}
    args = session.args
    return {"unit": args.unit, "unit_number": args.unit_number, "bars": args.bars}


# -- query / body parsing helpers -----------------------------------------------------


def parse_ema(text: str | None) -> list[int]:
    """``"9,21"`` -> ``[9, 21]``; blank entries are ignored; an empty string means none."""
    if text is None:
        text = "9,21"
    periods: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit() or int(part) <= 0:
            raise RouteError(400, f"EMA periods must be whole numbers greater than 0 (got {part!r})")
        periods.append(int(part))
    return periods


def parse_supertrend(text: str | None) -> tuple[int, float] | None:
    """``"10,3"`` -> ``(10, 3.0)``; ``"0"`` or ``""`` disables the overlay."""
    if text is None:
        text = "10,3"
    text = text.strip()
    if text in {"", "0", "off", "false"}:
        return None
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 2 or not parts[0].isdigit() or int(parts[0]) <= 0:
        raise RouteError(400, "Supertrend settings must be period,multiplier, for example 10,3")
    try:
        multiplier = float(parts[1])
    except ValueError:
        raise RouteError(400, "Supertrend multiplier must be a number, for example 3") from None
    if not multiplier > 0:
        raise RouteError(400, "Supertrend multiplier must be greater than 0")
    return int(parts[0]), multiplier


def parse_flag(text: str | None, default: bool = True) -> bool:
    if text is None or text.strip() == "":
        return default
    return text.strip().lower() in {"1", "true", "yes", "on"}


def parse_after(text: str | None, name: str = "after") -> int | None:
    """An optional UTC unix time in whole seconds from the query (``/api/bars``, ``/api/indicators/*``).

    Blank or missing -> None; anything that is not digits -> 400.
    """
    text = (text or "").strip()
    if not text:
        return None
    if not text.isdigit():
        raise RouteError(400, f"{name} must be a UTC unix time in seconds")
    return int(text)


def _int_field(body: dict, key: str) -> int:
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RouteError(400, f"{key} must be a whole number")
    return value


def _str_field(body: dict, key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RouteError(400, f"{key} must be a non-empty string")
    return value.strip()


def _loopback_host(value: str | None, server_port: int) -> bool:
    """True for ``127.0.0.1[:port]`` / ``localhost[:port]`` where the port, if given, is ours."""
    if not value:
        return False
    try:
        parts = urlsplit(f"//{value.strip()}")
        hostname, port = parts.hostname, parts.port
    except ValueError:
        return False
    if hostname not in ALLOWED_HOSTS:
        return False
    return port is None or port == server_port


def _loopback_origin(value: str, server_port: int) -> bool:
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return False
    if parts.scheme != "http" or parts.path not in {"", "/"} or parts.query or parts.fragment:
        return False
    return _loopback_host(parts.netloc, server_port)


# -- the handler -------------------------------------------------------------------------


def make_handler(session) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "TopstepXDashboard"
        sys_version = ""
        timeout = 30  # seconds of socket inactivity before a worker gives up on a client

        # -- plumbing -------------------------------------------------------------------

        def _send(self, status: int, content_type: str, body: bytes, extra_headers: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if status >= 400:
                self.send_header("Connection", "close")
                self.close_connection = True
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_json(self, status: int, payload: object) -> None:
            self._send(status, "application/json", json.dumps(payload, default=str).encode("utf-8"))

        def _run(self, route) -> None:
            """Call a route and translate every failure into a JSON error response."""
            path = urlparse(self.path).path
            order_route = self._is_order_route(path)
            try:
                status, payload = route()
            except RouteError as exc:
                payload = {"ok": False, **exc.payload} if order_route and exc.status < 500 else exc.payload
                self._send_json(exc.status, payload)
                return
            except ValueError as exc:
                payload = {"error": str(exc)}
                if order_route:
                    payload = {"ok": False, **payload}
                self._send_json(400, payload)
                return
            except PermissionError as exc:
                payload = {"error": str(exc) or "not allowed"}
                if order_route:
                    payload = {"ok": False, **payload}
                self._send_json(403, payload)
                return
            except TopstepXError as exc:
                self._send_json(502, {"error": str(exc)})
                return
            except (TimeoutError, concurrent.futures.TimeoutError):
                self._send_json(504, {"error": "The dashboard did not answer in time. Try again."})
                return
            except Exception:
                traceback.print_exc(file=sys.stderr)
                self._send_json(500, {"error": "Internal error in the dashboard. See the terminal for details."})
                return
            self._send_json(status, payload)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass  # one line per poll would drown the terminal

        def log_request(self, code="-", size="-") -> None:
            try:
                status = int(code)
            except (TypeError, ValueError):
                return
            if status >= 500:
                sys.stderr.write(f"dashboard: {self.command} {urlparse(self.path).path} -> {status}\n")

        # -- GET ---------------------------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            try:
                self._do_get()
            except (BrokenPipeError, ConnectionError):
                pass

        do_HEAD = do_GET  # noqa: N815

        def _do_get(self) -> None:
            if not _loopback_host(self.headers.get("Host"), self.server.server_address[1]):
                # A page on another hostname rebound to 127.0.0.1 must not read the token or account data.
                self._send_json(403, {"error": "requests are only accepted from this computer"})
                return
            url = urlparse(self.path)
            path = unquote(url.path)
            query = {k: v[-1] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
            if path == "/":
                self._serve_index()
            elif path == "/favicon.ico":
                self._send(200, "image/svg+xml; charset=utf-8", FAVICON_SVG)
            elif path.startswith("/static/"):
                self._serve_static(path[len("/static/"):])
            elif path == "/api/state":
                self._run(lambda: (200, state_snapshot(session)))
            elif path == "/api/features":
                self._run(lambda: (200, {"features": features_listing(session)}))
            elif path == "/api/bars":
                self._run(lambda: self._bars(query))
            elif path == "/api/depth":
                self._run(lambda: self._depth(query))
            elif path == "/api/tape":
                self._run(lambda: self._tape(query))
            elif path in feature_routes(session, "GET"):
                handler = feature_routes(session, "GET")[path]
                self._run(lambda: handler(query))
            elif path.startswith("/api/"):
                self._send_json(404, {"error": "no such route"})
            else:
                self._send(404, "text/plain; charset=utf-8", b"not found")

        def _serve_index(self) -> None:
            index = STATIC_DIR / "index.html"
            try:
                page = index.read_text(encoding="utf-8")
            except OSError:
                self._send(500, "text/plain; charset=utf-8", b"dashboard/static/index.html is missing")
                return
            self._send(200, CONTENT_TYPES[".html"], page.replace("__TOKEN__", TOKEN).encode("utf-8"),
                       extra_headers={"X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'"})

        def _serve_static(self, name: str) -> None:
            root = STATIC_DIR.resolve()
            if not name or ".." in name or name.startswith(("/", "\\")) or "\\" in name or "\0" in name:
                self._send(404, "text/plain; charset=utf-8", b"not found")
                return
            target = (root / name).resolve()
            content_type = CONTENT_TYPES.get(target.suffix.lower())
            if content_type is None or root not in target.parents or not target.is_file():
                self._send(404, "text/plain; charset=utf-8", b"not found")
                return
            try:
                body = target.read_bytes()
            except OSError:
                self._send(404, "text/plain; charset=utf-8", b"not found")
                return
            self._send(200, content_type, body)

        def _contract_param(self, query: dict) -> str:
            contract_id = (query.get("contract") or "").strip()
            if not contract_id:
                raise RouteError(400, "contract is required")
            return contract_id

        def _bars(self, query: dict) -> tuple[int, dict]:
            contract_id = self._contract_param(query)
            after = parse_after(query.get("after"))
            candles = session.state.candles(contract_id)
            if candles is None:
                raise RouteError(404, "no bars for that contract yet")
            ema_periods = parse_ema(query.get("ema"))
            vwap_on = parse_flag(query.get("vwap"), default=True)
            supertrend = parse_supertrend(query.get("st"))
            series = indicators.indicator_series(
                candles, ema_periods=ema_periods, vwap_on=vwap_on, supertrend=supertrend
            )
            volume = [
                {
                    "time": c["time"],
                    "value": c.get("volume"),
                    "color": VOLUME_UP if c["close"] >= c["open"] else VOLUME_DOWN,
                }
                for c in candles
            ]
            total = len(candles)
            oldest = candles[0]["time"] if candles else None
            if after is not None:
                # Indicators were computed over everything stored; only the tail goes out.
                def tail(rows: list[dict]) -> list[dict]:
                    return [r for r in rows if r["time"] >= after]
                candles = tail(candles)
                volume = tail(volume)
                series = {
                    "ema": {period: tail(points) for period, points in series.get("ema", {}).items()},
                    "vwap": tail(series.get("vwap", [])),
                    "supertrend": tail(series.get("supertrend", [])),
                }
            timeframe = session_timeframe(session)
            return 200, {
                "contract": contract_id,
                "version": session.state.bars_version(contract_id),
                "bar_seconds": UNIT_SECONDS[timeframe["unit"]] * timeframe["unit_number"],
                "timeframe": timeframe,
                "count": total,
                "oldest": oldest,
                "after": after,
                "candles": candles,
                "volume": volume,
                "indicators": series,
            }

        def _depth(self, query: dict) -> tuple[int, dict]:
            contract_id = self._contract_param(query)
            levels_text = (query.get("levels") or "20").strip()
            if not levels_text.isdigit() or not 1 <= int(levels_text) <= 200:
                raise RouteError(400, "levels must be a whole number from 1 through 200")
            book = session.state.depth(contract_id, int(levels_text))
            if book is None:
                raise RouteError(404, "no depth for that contract yet")
            return 200, book

        def _tape(self, query: dict) -> tuple[int, dict]:
            contract_id = self._contract_param(query)
            after_text = (query.get("after") or "0").strip()
            if not after_text.lstrip("-").isdigit():
                raise RouteError(400, "after must be a whole number")
            events, last_seq = session.state.tape_since(contract_id, int(after_text))
            return 200, {"events": events, "last_seq": last_seq}

        # -- POST ----------------------------------------------------------------------------

        def do_POST(self) -> None:  # noqa: N802
            try:
                self._do_post()
            except (BrokenPipeError, ConnectionError):
                pass

        def _do_post(self) -> None:
            path = urlparse(self.path).path
            if not self._authorized():
                return
            body = self._read_body()
            if body is None:
                return
            routes = {
                "/api/account": lambda: self._select_account(body),
                "/api/focus": lambda: self._set_focus(body),
                "/api/watch": lambda: self._add_watch(body),
                "/api/timeframe": lambda: self._set_timeframe(body),
                "/api/history": lambda: self._load_history(body),
                "/api/orders/preflight": lambda: self._order_preflight(body),
                "/api/orders": lambda: self._order_send(body),
                "/api/orders/cancel": lambda: self._order_cancel(body),
                "/api/positions/close": lambda: self._position_close(body),
            }
            route = routes.get(path)
            if route is None:
                extra = feature_routes(session, "POST").get(path)
                if extra is not None:
                    route = lambda: extra(body)  # noqa: E731
            if route is None:
                self._send_json(404, {"error": "no such route"})
                return
            if self._is_order_route(path) and not session.state.order_entry_allowed():
                self._send_json(403, {
                    "ok": False,
                    "error": "Order entry is only available on a Practice Account the API marks "
                             "simulated and able to trade. The selected account is read-only.",
                })
                return
            if self._is_order_route(path):
                reason = feature_block_reason(session)
                if reason:
                    self._send_json(403, {"ok": False, "error": reason})
                    return
            if self._is_order_route(path) and path != "/api/orders/preflight" and body.get("confirm") != "yes":
                self._send_json(403, {"ok": False, "error": 'This action needs "confirm": "yes".'})
                return
            self._run(route)

        def _is_order_route(self, path: str) -> bool:
            """Built-in order routes plus any a feature declares in ORDER_ROUTES."""
            return path in ORDER_ROUTES or path in feature_order_routes(session)

        def _authorized(self) -> bool:
            """Token, Host and Origin checks for state-changing requests."""
            server_port = self.server.server_address[1]
            token = self.headers.get("X-Dashboard-Token") or ""
            if not hmac.compare_digest(token.encode("utf-8"), TOKEN.encode("utf-8")):
                self._reject(403, {"error": "missing or wrong dashboard token; reload the page"})
                return False
            if not _loopback_host(self.headers.get("Host"), server_port):
                self._reject(403, {"error": "requests are only accepted from this computer"})
                return False
            origin = self.headers.get("Origin")
            if origin is not None and not _loopback_origin(origin, server_port):
                self._reject(403, {"error": "requests are only accepted from the dashboard page"})
                return False
            return True

        def _reject(self, status: int, payload: dict) -> None:
            """Refuse a request without reading its body for real.

            The unread body is drained (up to a small limit) before the response goes
            out; otherwise Windows resets the socket and the client sees a connection
            error instead of the status code.
            """
            self._drain_body()
            self._send_json(status, payload)

        def _drain_body(self) -> None:
            try:
                remaining = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                return
            if remaining <= 0 or remaining > DRAIN_LIMIT_BYTES:
                return
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)

        def _read_body(self) -> dict | None:
            """Return the parsed JSON object, or None after sending the error response."""
            if self.headers.get("Transfer-Encoding"):
                self._send_json(400, {"error": "chunked request bodies are not supported"})
                return None
            length_text = self.headers.get("Content-Length") or "0"
            try:
                length = int(length_text)
            except ValueError:
                self._send_json(400, {"error": "bad Content-Length"})
                return None
            if length < 0:
                self._send_json(400, {"error": "bad Content-Length"})
                return None
            limit = feature_body_limit(session, urlparse(self.path).path)
            if length > limit:
                self._reject(413, {"error": f"request body is larger than {limit // 1024} KB"})
                return None
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else None
            except (UnicodeDecodeError, ValueError):
                self._send_json(400, {"error": "the request body is not valid JSON"})
                return None
            if not isinstance(body, dict):
                self._send_json(400, {"error": "the request body must be a JSON object"})
                return None
            return body

        # -- POST routes -----------------------------------------------------------------------

        def _select_account(self, body: dict) -> tuple[int, dict]:
            account_id = _int_field(body, "account_id")
            session.call_async(session.select_account(account_id))
            return 200, state_snapshot(session)

        def _set_focus(self, body: dict) -> tuple[int, dict]:
            contract_id = _str_field(body, "contract_id")
            session.call_async(session.set_focus(contract_id))
            return 200, {"focus": contract_id}

        def _add_watch(self, body: dict) -> tuple[int, dict]:
            symbol = _str_field(body, "symbol")
            contract_id = session.call_async(session.add_watch(symbol), timeout=30.0)
            return 200, {"contract_id": contract_id}

        def _load_history(self, body: dict) -> tuple[int, dict]:
            contract_id = _str_field(body, "contract_id")
            before = _int_field(body, "before")
            result = session.call_async(session.load_history(contract_id, before), timeout=45.0)
            return 200, result

        def _set_timeframe(self, body: dict) -> tuple[int, dict]:
            unit = _str_field(body, "unit")
            unit_number = _int_field(body, "unit_number")
            bars = _int_field(body, "bars")
            session.call_async(session.set_timeframe(unit, unit_number, bars), timeout=30.0)
            timeframe = session_timeframe(session)
            return 200, {"timeframe": timeframe, "bar_seconds": UNIT_SECONDS[timeframe["unit"]] * timeframe["unit_number"]}

        def _ticket_and_summary(self, body: dict) -> tuple[object, dict, dict]:
            ticket = orders.parse_ticket(body)
            account = session.state.selected_account()
            if account is None:
                raise ValueError("No account is selected.")
            if ticket.account_id != account["id"]:
                raise ValueError("The ticket names a different account than the one selected on the page.")
            tag = body.get("custom_tag")
            last_price = None
            if ticket.order_type == "market" and orders.has_brackets(ticket):
                # Only to show approximate leg prices for a market entry; never sent to the gateway.
                last_price = orders.last_price_from(session.state.snapshot(), ticket.contract_id)
            summary = orders.preflight(ticket, account, session.state.contract(ticket.contract_id),
                                       custom_tag=tag if isinstance(tag, str) else None, last_price=last_price)
            return ticket, account, summary

        def _order_preflight(self, body: dict) -> tuple[int, dict]:
            _ticket, _account, summary = self._ticket_and_summary(body)
            return 200, {"ok": True, "summary": summary}

        def _order_send(self, body: dict) -> tuple[int, dict]:
            ticket, _account, summary = self._ticket_and_summary(body)
            order_id = orders.send_order(session.client, ticket, summary)
            return 200, {"ok": True, "order_id": order_id}

        def _order_cancel(self, body: dict) -> tuple[int, dict]:
            order_id = _int_field(body, "order_id")
            account = session.state.selected_account()
            if account is None:
                raise ValueError("No account is selected.")
            orders.cancel_order(session.client, account, order_id)
            return 200, {"ok": True}

        def _position_close(self, body: dict) -> tuple[int, dict]:
            contract_id = _str_field(body, "contract_id")
            account = session.state.selected_account()
            if account is None:
                raise ValueError("No account is selected.")
            orders.close_position(session.client, account, contract_id)
            return 200, {"ok": True}

    return DashboardHandler


def make_server(session, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """Start the local server on a background thread. Loopback only: never reachable from outside."""
    if host not in ALLOWED_HOSTS:
        raise ValueError("The dashboard only serves on 127.0.0.1 / localhost.")
    server = ThreadingHTTPServer((host, port), make_handler(session))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True, name="dashboard-http").start()
    return server
