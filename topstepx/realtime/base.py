"""Shared SignalR hub plumbing.

``BaseHub`` wraps :class:`pysignalr.client.SignalRClient` and adds the
two behaviors every TopstepX hub needs:

1. **Auth that survives long streams** — every connection attempt asks
   ``TokenManager`` for a current JWT and passes it in the hub URL
   (``?access_token=...``, the way the ProjectX docs connect), so a
   refreshed token is picked up transparently on reconnect.
2. **Auto-reconnect + auto-resubscribe** — if the connection fails or
   drops, ``BaseHub`` reconnects with backoff, and every subscription is
   recorded in a registry and replayed whenever the connection
   (re)opens. Drop your Wi-Fi mid-stream and the data resumes by itself.

If you ever need to swap SignalR libraries, this is the only file
that touches pysignalr.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from typing import Any, Callable
from urllib.parse import urlencode

from pysignalr.client import SignalRClient

from ..auth import TokenManager
from ..exceptions import TopstepXError

logger = logging.getLogger("topstepx.realtime")

CONNECT_TIMEOUT = 30.0  # seconds to wait for the first successful connect
RECONNECT_MIN_DELAY = 1.0  # backoff between attempts: 1s, 2s, 4s ... capped below
RECONNECT_MAX_DELAY = 30.0

_TOKEN_IN_URL = re.compile(r"(access_token=)[^&\s`'\"]+")


def _redact(text: str) -> str:
    return _TOKEN_IN_URL.sub(r"\1<redacted>", text)


class _RedactTokenFilter(logging.Filter):
    """pysignalr logs connection URLs; keep the JWT in their query string out of logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "access_token=" in message:
            record.msg, record.args = _redact(message), None
        return True


logging.getLogger("pysignalr.transport").addFilter(_RedactTokenFilter())


class BaseHub:
    def __init__(self, hub_url: str, tokens: TokenManager):
        self._url = hub_url
        self._tokens = tokens
        self._client: SignalRClient | None = None  # rebuilt on every connection attempt
        self._event_handlers: dict[str, Callable[[list], Any]] = {}

        self._subscriptions: set[tuple[str, tuple]] = set()
        self._connected = asyncio.Event()
        self._run_task: asyncio.Task | None = None
        self._reconnect_delay = RECONNECT_MIN_DELAY
        self._last_error: str | None = None
        self._connection_callbacks: list[Callable[[bool], Any]] = []
        self._raw_observers: list[Callable[[str, str, list], Any]] = []

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Connect and wait until the hub is ready (or raise with a clear message)."""
        if self._run_task is not None:
            return
        self._run_task = asyncio.create_task(self._run_forever(), name=f"signalr:{self._url}")
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            await self.stop()
            detail = f" (last error: {self._last_error})" if self._last_error else ""
            raise TopstepXError(
                f"Could not connect to {self._url} within {CONNECT_TIMEOUT:.0f}s{detail}. "
                "Check your network/firewall and that your credentials are valid "
                "(run examples/00_check_setup.py)."
            ) from None

    async def stop(self) -> None:
        """Disconnect cleanly."""
        if self._run_task is None:
            return
        self._run_task.cancel()
        try:
            await self._run_task
        except (asyncio.CancelledError, Exception):
            pass
        self._run_task = None
        self._client = None
        self._connected.clear()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def on_connection_change(self, callback: Callable[[bool], Any]) -> None:
        """Register a callback invoked with True on (re)connect, False on disconnect."""
        self._connection_callbacks.append(callback)

    def add_raw_observer(self, callback: Callable[[str, str, list], Any]) -> None:
        """Register ``callback(direction, name, args)`` for every hub message.

        ``direction`` is "in" for events the hub pushes (``name`` = event such as
        "GatewayQuote", ``args`` = the raw payload list) and "out" for methods this side
        invokes (``name`` = "SubscribeContractQuotes" etc., ``args`` = its arguments). Sync
        callbacks only; exceptions are logged and never stop the stream.
        """
        self._raw_observers.append(callback)

    def _notify_raw(self, direction: str, name: str, args: list) -> None:
        for callback in self._raw_observers:
            try:
                callback(direction, name, args)
            except Exception:
                logger.exception("Error in raw observer for %s %s", direction, name)

    # -- connection loop ---------------------------------------------------------

    async def _run_forever(self) -> None:
        """Connect, then reconnect with backoff whenever the connection fails or drops.

        Each attempt builds a fresh client with a current token. pysignalr negotiates
        before it calls ``access_token_factory`` (so the gateway answers 401), and its
        own run loop only retries network failures during negotiation — any other
        connection error, like an expired token's 401 on reconnect, ends it for good.
        """
        while True:
            try:
                self._client = self._new_client()
                await self._client.run()
                self._last_error = "connection closed"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = _redact(str(exc)) or type(exc).__name__
            await self._on_close()  # no-op unless the drop went unreported

            delay = self._reconnect_delay
            logger.warning(
                "Connection to %s failed (%s) — retrying in %.0fs", self._url, self._last_error, delay
            )
            await asyncio.sleep(delay)
            self._reconnect_delay = min(delay * 2, RECONNECT_MAX_DELAY)

    def _new_client(self) -> SignalRClient:
        token = self._tokens.get_token()
        client = SignalRClient(url=f"{self._url}?{urlencode({'access_token': token})}")
        client.on_open(self._on_open)
        client.on_close(self._on_close)
        client.on_error(self._on_error)
        for event, handler in self._event_handlers.items():
            client.on(event, handler)
        return client

    # -- subscriptions ---------------------------------------------------------

    async def _subscribe(self, method: str, *args: Any) -> None:
        """Invoke a Subscribe* hub method and remember it for auto-resubscribe."""
        self._subscriptions.add((method, args))
        if self.connected:
            self._notify_raw("out", method, list(args))
            await self._client.send(method, list(args))

    async def _unsubscribe(self, method: str, *args: Any) -> None:
        """Invoke an Unsubscribe* hub method and forget the matching subscription."""
        subscribe_method = method.replace("Unsubscribe", "Subscribe", 1)
        self._subscriptions.discard((subscribe_method, args))
        if self.connected:
            self._notify_raw("out", method, list(args))
            await self._client.send(method, list(args))

    # -- event dispatch ----------------------------------------------------------

    def _register_event(self, event: str, parser: Callable[[list], Any], callbacks: list) -> None:
        """Route a hub event through a payload parser to user callbacks.

        Callback errors are logged but never kill the stream.
        """

        async def handler(args: list) -> None:
            if self._raw_observers:
                self._notify_raw("in", event, args if isinstance(args, list) else [args])
            try:
                parsed = parser(args)
            except Exception:
                logger.exception("Failed to parse %s payload: %r", event, args)
                return
            for callback in callbacks:
                try:
                    result = callback(*parsed)
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    logger.exception("Error in %s callback %r", event, callback)

        self._event_handlers[event] = handler
        if self._client is not None:
            self._client.on(event, handler)

    # -- internal pysignalr hooks ------------------------------------------------

    async def _on_open(self) -> None:
        logger.info("Connected to %s", self._url)
        self._connected.set()
        self._reconnect_delay = RECONNECT_MIN_DELAY  # healthy again — reset backoff
        self._last_error = None
        # Replay every remembered subscription — this is what makes
        # reconnects seamless.
        for method, args in self._subscriptions:
            try:
                self._notify_raw("out", method, list(args))
                await self._client.send(method, list(args))
            except Exception:
                logger.exception("Failed to resubscribe %s%r", method, args)
        self._notify_connection(True)

    async def _on_close(self) -> None:
        if not self.connected:
            return  # pysignalr can report one drop twice (reconnecting, then disconnected)
        logger.warning("Disconnected from %s — reconnecting automatically", self._url)
        self._connected.clear()
        self._notify_connection(False)

    async def _on_error(self, message: Any) -> None:
        logger.error("Hub error from %s: %s", self._url, getattr(message, "error", message))

    def _notify_connection(self, is_connected: bool) -> None:
        for callback in self._connection_callbacks:
            try:
                callback(is_connected)
            except Exception:
                logger.exception("Error in connection-change callback")
