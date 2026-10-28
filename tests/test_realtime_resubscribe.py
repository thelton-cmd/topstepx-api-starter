"""Hub logic tests using a fake SignalR connection — no network needed."""

import asyncio
import logging

import pytest

import topstepx.realtime.base as base_module
from topstepx.exceptions import TopstepXError
from topstepx.realtime.market import MarketHub

from .fixtures import payloads


class FakeSignalRClient:
    """Stands in for pysignalr.client.SignalRClient."""

    def __init__(self, url, **kwargs):
        self.url = url
        self.handlers: dict[str, object] = {}
        self.sent: list[tuple[str, list]] = []

    def on(self, event, handler):
        self.handlers[event] = handler

    def on_open(self, cb):
        self._open_cb = cb

    def on_close(self, cb):
        self._close_cb = cb

    def on_error(self, cb):
        self._error_cb = cb

    async def send(self, method, args):
        self.sent.append((method, args))

    async def run(self):
        await asyncio.Event().wait()


class FakeTokens:
    def get_token(self):
        return "fake-token"


class RotatingTokens:
    """Hands out a new token on every call, like TokenManager after a refresh."""

    def __init__(self):
        self.calls = 0

    def get_token(self):
        self.calls += 1
        return f"token-{self.calls}"


@pytest.fixture
def hub(monkeypatch) -> MarketHub:
    monkeypatch.setattr(base_module, "SignalRClient", FakeSignalRClient)
    hub = MarketHub(FakeTokens())
    hub._client = hub._new_client()  # what each connection attempt does
    return hub


def fake_client(hub) -> FakeSignalRClient:
    return hub._client


async def test_subscribe_while_disconnected_only_records(hub):
    await hub.subscribe_quotes("CON.A")
    assert fake_client(hub).sent == []  # nothing sent yet
    assert ("SubscribeContractQuotes", ("CON.A",)) in hub._subscriptions


async def test_on_open_replays_all_subscriptions(hub):
    await hub.subscribe_quotes("CON.A")
    await hub.subscribe_depth("CON.B")

    await hub._on_open()  # simulates (re)connect

    assert hub.connected
    assert sorted(fake_client(hub).sent) == [
        ("SubscribeContractMarketDepth", ["CON.B"]),
        ("SubscribeContractQuotes", ["CON.A"]),
    ]


async def test_reconnect_resubscribes_again(hub):
    await hub.subscribe_quotes("CON.A")
    await hub._on_open()
    await hub._on_close()
    assert not hub.connected
    await hub._on_open()  # reconnect

    sends = [s for s in fake_client(hub).sent if s == ("SubscribeContractQuotes", ["CON.A"])]
    assert len(sends) == 2


async def test_unsubscribe_removes_from_registry(hub):
    await hub._on_open()
    await hub.subscribe_quotes("CON.A")
    await hub.unsubscribe_quotes("CON.A")

    assert hub._subscriptions == set()
    assert ("UnsubscribeContractQuotes", ["CON.A"]) in fake_client(hub).sent


async def test_quote_event_dispatches_sync_and_async_callbacks(hub):
    received = []

    def sync_cb(contract_id, quote):
        received.append(("sync", contract_id, quote.best_bid))

    async def async_cb(contract_id, quote):
        received.append(("async", contract_id, quote.best_ask))

    hub.on_quote(sync_cb)
    hub.on_quote(async_cb)

    handler = fake_client(hub).handlers["GatewayQuote"]
    await handler(["CON.A", payloads.QUOTE])

    assert ("sync", "CON.A", 5301.0) in received
    assert ("async", "CON.A", 5301.25) in received


async def test_callback_exception_does_not_kill_dispatch(hub):
    received = []

    def bad_cb(contract_id, quote):
        raise RuntimeError("user bug")

    hub.on_quote(bad_cb)
    hub.on_quote(lambda cid, q: received.append(cid))

    handler = fake_client(hub).handlers["GatewayQuote"]
    await handler(["CON.A", payloads.QUOTE])  # must not raise

    assert received == ["CON.A"]


async def test_malformed_payload_does_not_raise(hub):
    handler = fake_client(hub).handlers["GatewayQuote"]
    await handler([])  # missing args entirely — logged, not raised


async def test_client_url_carries_current_token(hub):
    assert fake_client(hub).url == "wss://rtc.topstepx.com/hubs/market?access_token=fake-token"
    assert "GatewayQuote" in fake_client(hub).handlers


async def test_failed_connection_retries_with_fresh_client_and_token(monkeypatch):
    attempts = []

    class FlakyClient(FakeSignalRClient):
        async def run(self):
            attempts.append(self.url)
            if len(attempts) == 1:
                raise RuntimeError("negotiation rejected: HTTP 401")
            await self._open_cb()
            await asyncio.Event().wait()

    monkeypatch.setattr(base_module, "SignalRClient", FlakyClient)
    monkeypatch.setattr(base_module, "RECONNECT_MIN_DELAY", 0)
    hub = MarketHub(RotatingTokens())
    await hub.subscribe_quotes("CON.A")

    await hub.start()
    try:
        assert hub.connected
        assert [url.rsplit("=", 1)[1] for url in attempts] == ["token-1", "token-2"]
        assert ("SubscribeContractQuotes", ["CON.A"]) in hub._client.sent
    finally:
        await hub.stop()


async def test_start_timeout_reports_last_error(monkeypatch):
    class RejectedClient(FakeSignalRClient):
        async def run(self):
            raise RuntimeError("negotiation rejected: HTTP 401")

    monkeypatch.setattr(base_module, "SignalRClient", RejectedClient)
    monkeypatch.setattr(base_module, "CONNECT_TIMEOUT", 0.2)
    monkeypatch.setattr(base_module, "RECONNECT_MIN_DELAY", 0.01)
    hub = MarketHub(FakeTokens())

    with pytest.raises(TopstepXError, match="last error: negotiation rejected: HTTP 401"):
        await hub.start()
    assert hub._run_task is None


async def test_duplicate_close_notifies_once(hub):
    changes = []
    hub.on_connection_change(changes.append)

    await hub._on_open()
    await hub._on_close()  # pysignalr: connected -> reconnecting
    await hub._on_close()  # then reconnecting -> disconnected

    assert changes == [True, False]


def test_pysignalr_logs_redact_token(caplog):
    caplog.set_level(logging.INFO, logger="pysignalr.transport")
    logging.getLogger("pysignalr.transport").info(
        "Performing negotiation, URL: `%s`", "https://rtc.example/hubs/market/negotiate?access_token=abc.def.ghi"
    )
    assert "abc.def.ghi" not in caplog.text
    assert "access_token=<redacted>" in caplog.text
