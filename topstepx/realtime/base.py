"""Shared SignalR hub plumbing.

``BaseHub`` wraps :class:`pysignalr.client.SignalRClient` and adds the
two behaviors every TopstepX hub needs:

1. **Auth that survives long streams** — the JWT is supplied via
   ``access_token_factory``, which pysignalr calls on every (re)connect,
   so ``TokenManager`` can hand out a refreshed token transparently.
2. **Auto-resubscribe** — every subscription is recorded in a registry
   and replayed whenever the connection (re)opens. Drop your Wi-Fi
   mid-stream and the data resumes by itself.

If you ever need to swap SignalR libraries, this is the only file
that touches pysignalr.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Callable

from pysignalr.client import SignalRClient

from ..auth import TokenManager
from ..exceptions import TopstepXError

logger = logging.getLogger("topstepx.realtime")

CONNECT_TIMEOUT = 30.0  # seconds to wait for the first successful connect


class BaseHub:
    def __init__(self, hub_url: str, tokens: TokenManager):
        self._url = hub_url
        self._tokens = tokens
        self._client = SignalRClient(
            url=hub_url,
            access_token_factory=tokens.get_token,
            retry_count=9999,  # keep trying — traders leave streams running all session
        )
        self._client.on_open(self._on_open)
        self._client.on_close(self._on_close)
        self._client.on_error(self._on_error)

        self._subscriptions: set[tuple[str, tuple]] = set()
        self._connected = asyncio.Event()
        self._run_task: asyncio.Task | None = None
        self._connection_callbacks: list[Callable[[bool], Any]] = []

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Connect and wait until the hub is ready (or raise with a clear message)."""
        if self._run_task is not None:
            return
        self._run_task = asyncio.create_task(self._client.run(), name=f"signalr:{self._url}")
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            await self.stop()
            raise TopstepXError(
                f"Could not connect to {self._url} within {CONNECT_TIMEOUT:.0f}s. "
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
        self._connected.clear()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def on_connection_change(self, callback: Callable[[bool], Any]) -> None:
        """Register a callback invoked with True on (re)connect, False on disconnect."""
        self._connection_callbacks.append(callback)

    # -- subscriptions ---------------------------------------------------------

    async def _subscribe(self, method: str, *args: Any) -> None:
        """Invoke a Subscribe* hub method and remember it for auto-resubscribe."""
        self._subscriptions.add((method, args))
        if self.connected:
            await self._client.send(method, list(args))

    async def _unsubscribe(self, method: str, *args: Any) -> None:
        """Invoke an Unsubscribe* hub method and forget the matching subscription."""
        subscribe_method = method.replace("Unsubscribe", "Subscribe", 1)
        self._subscriptions.discard((subscribe_method, args))
        if self.connected:
            await self._client.send(method, list(args))

    # -- event dispatch ----------------------------------------------------------

    def _register_event(self, event: str, parser: Callable[[list], Any], callbacks: list) -> None:
        """Route a hub event through a payload parser to user callbacks.

        Callback errors are logged but never kill the stream.
        """

        async def handler(args: list) -> None:
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

        self._client.on(event, handler)

    # -- internal pysignalr hooks ------------------------------------------------

    async def _on_open(self) -> None:
        logger.info("Connected to %s", self._url)
        self._connected.set()
        # Replay every remembered subscription — this is what makes
        # reconnects seamless.
        for method, args in self._subscriptions:
            try:
                await self._client.send(method, list(args))
            except Exception:
                logger.exception("Failed to resubscribe %s%r", method, args)
        self._notify_connection(True)

    async def _on_close(self) -> None:
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
