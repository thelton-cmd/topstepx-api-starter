"""Hub logic tests using a fake SignalR connection — no network needed."""

import asyncio

import pytest

import topstepx.realtime.base as base_module
from topstepx.realtime.market import MarketHub

from .fixtures import payloads


class FakeSignalRClient:
    """Stands in for pysignalr.client.SignalRClient."""

    def __init__(self, url, access_token_factory=None, **kwargs):
        self.url = url
        self.access_token_factory = access_token_factory
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


@pytest.fixture
def hub(monkeypatch) -> MarketHub:
    monkeypatch.setattr(base_module, "SignalRClient", FakeSignalRClient)
    return MarketHub(FakeTokens())


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
