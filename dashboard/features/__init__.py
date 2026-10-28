"""Feature modules: drop a ``.py`` file in this folder and the dashboard picks it up at startup.

A feature is a plain module. Everything is optional:

    NAME = "apilog"                     # id (default: the module name); used in URLs and /api/features
    TITLE = "API calls"                 # shown in the page
    DESCRIPTION = "..."                 # one line
    ORDER = 100                         # load/mount order (lower first)
    JS = "apilog.js"                    # page module in dashboard/static/features/, must export mount(api)
    CSS = "apilog.css"                  # optional stylesheet in the same folder
    ORDER_ROUTES = {"/api/orders/flatten"}   # POST routes that send orders: the server applies the
                                        # Practice-only + "confirm": "yes" guard to them (see server.py)
    BODY_LIMITS = {"/api/pine/convert": 64 * 1024 * 1024}  # POST routes that may take bodies larger than the
                                        # default 64 KB cap (bytes; the server never allows more than MAX_FEATURE_BODY)

    def routes(session) -> dict:        # {"GET": {"/api/apilog/calls": fn(query) -> (status, payload)},
        ...                             #  "POST": {"/api/apilog/clear": fn(body) -> (status, payload)}}
                                        # Handlers run on server threads: read session.state (it locks),
                                        # call REST on session.client, reach asyncio via session.call_async().
                                        # Raise ValueError -> 400, PermissionError -> 403, RouteError -> its status.

    async def start(session): ...       # after hubs are connected and the first subscriptions are up
    async def stop(session): ...        # on shutdown (best effort)
    def snapshot(session) -> dict: ...  # merged into GET /api/state under snapshot["features"][NAME]
    def order_entry_blocked(session) -> str | None   # a reason string blocks every order route (403) and
                                        # makes /api/state report order_entry_allowed false with that reason

Rules every feature follows: never place, modify or cancel orders on its own initiative (only in
response to an explicit user action, through the same guards as the ticket); never log request
bodies or tokens; keep output ASCII on the terminal; keep every number it shows an approximation
built from API data, never advice. See dashboard/ARCHITECTURE.md, "Features (extension points)".
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from types import ModuleType
from typing import Iterable

# Paths the core server owns; a feature route may not shadow them.
BUILTIN_PATHS = {
    "/", "/favicon.ico", "/api/state", "/api/bars", "/api/depth", "/api/tape", "/api/features",
    "/api/account", "/api/focus", "/api/watch", "/api/timeframe", "/api/history",
    "/api/orders", "/api/orders/preflight", "/api/orders/cancel", "/api/positions/close",
}


def name_of(module: ModuleType) -> str:
    return str(getattr(module, "NAME", None) or module.__name__.rsplit(".", 1)[-1])


def _selection(selection: str | Iterable[str] | None) -> set[str] | None:
    """None/"all" -> every module; "none" -> nothing; "a,b" or ["a", "b"] -> those names."""
    if selection is None:
        return None
    if isinstance(selection, str):
        text = selection.strip().lower()
        if text in ("", "all"):
            return None
        if text == "none":
            return set()
        return {part.strip() for part in text.split(",") if part.strip()}
    return {str(part).strip() for part in selection}


def discover(selection: str | Iterable[str] | None = None) -> list[ModuleType]:
    """Import every feature module in this package (or the selected ones), sorted by ORDER, then name.

    A module that fails to import is skipped with a warning on stderr; one broken feature must
    never stop the dashboard.
    """
    wanted = _selection(selection)
    if wanted is not None and not wanted:
        return []
    modules: list[ModuleType] = []
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{__name__}.{info.name}")
        except Exception as exc:  # noqa: BLE001 - report and keep going
            print(f"[dashboard] feature {info.name} failed to import: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        if wanted is not None and name_of(module) not in wanted and info.name not in wanted:
            continue
        modules.append(module)
    modules.sort(key=lambda m: (int(getattr(m, "ORDER", 100)), name_of(m)))
    return modules


def describe(module: ModuleType) -> dict:
    """What GET /api/features tells the page about one feature."""
    return {
        "name": name_of(module),
        "title": str(getattr(module, "TITLE", None) or name_of(module)),
        "description": str(getattr(module, "DESCRIPTION", "") or ""),
        "js": getattr(module, "JS", None),
        "css": getattr(module, "CSS", None),
        "order": int(getattr(module, "ORDER", 100)),
    }


def collect_routes(modules: Iterable[ModuleType], session) -> dict[str, dict]:
    """Gather every feature's routes into {"GET": {path: handler}, "POST": {path: handler}}.

    Paths must start with "/api/" and may not shadow a built-in route or another feature's.
    A feature whose ``routes()`` raises is skipped with a warning.
    """
    table: dict[str, dict] = {"GET": {}, "POST": {}}
    for module in modules:
        fn = getattr(module, "routes", None)
        if fn is None:
            continue
        try:
            declared = fn(session) or {}
        except Exception as exc:  # noqa: BLE001
            print(f"[dashboard] feature {name_of(module)} routes() failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        for method in ("GET", "POST"):
            for path, handler in (declared.get(method) or {}).items():
                if not isinstance(path, str) or not path.startswith("/api/") or path in BUILTIN_PATHS:
                    raise ValueError(f"feature {name_of(module)}: route {method} {path!r} is not allowed")
                if path in table[method]:
                    raise ValueError(f"feature {name_of(module)}: route {method} {path!r} is already taken")
                if not callable(handler):
                    raise ValueError(f"feature {name_of(module)}: handler for {method} {path!r} is not callable")
                table[method][path] = handler
    return table


MAX_FEATURE_BODY = 64 * 1024 * 1024   # hard ceiling for any BODY_LIMITS entry


def body_limit(modules: Iterable[ModuleType], path: str) -> int | None:
    """The largest request body a feature allows on ``path`` (capped), or None for the server default."""
    best = None
    for module in modules:
        limits = getattr(module, "BODY_LIMITS", None) or {}
        value = limits.get(path) if isinstance(limits, dict) else None
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            best = max(best or 0, min(value, MAX_FEATURE_BODY))
    return best


def block_reason(modules: Iterable[ModuleType], session) -> str | None:
    """The first reason any feature gives for blocking order entry (e.g. a user-set daily loss guard
    that was hit, or replay mode), or None. A failing hook is ignored so it can never unblock by error
    the other way round: it is treated as not blocking, and the error is printed once per call."""
    for module in modules:
        fn = getattr(module, "order_entry_blocked", None)
        if fn is None:
            continue
        try:
            reason = fn(session)
        except Exception as exc:  # noqa: BLE001
            print(f"[dashboard] feature {name_of(module)} order_entry_blocked() failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        if reason:
            return str(reason)
    return None


def order_routes(modules: Iterable[ModuleType]) -> set[str]:
    """POST paths features mark as order-sending (they get the Practice-only + confirm guard)."""
    out: set[str] = set()
    for module in modules:
        out.update(str(p) for p in getattr(module, "ORDER_ROUTES", ()) or ())
    return out
