"""Session: the client, both hubs, the state, account/focus switching and the bar refresh loop.

Threading model (see ARCHITECTURE.md): the asyncio event loop on the main thread owns the
hubs and the refresh loop and is the only thing that calls ``await hub.*``. HTTP handler
threads reach it through ``Session.call_async``. Blocking REST calls made from the loop go
through ``asyncio.to_thread`` so the hubs keep streaming while they run.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import sys
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "examples") not in sys.path:
    sys.path.insert(0, str(ROOT / "examples"))  # the shared helpers live next to the examples

from topstepx import OrderStatus, BarUnit, Contract, MarketHub, TopstepXClient, TopstepXError, UserHub  # noqa: E402
from _safety import choose_active_contract, is_root_symbol_match  # noqa: E402

from . import features as feature_registry  # noqa: E402
from . import replay as replay_engine  # noqa: E402
from .features import replay as replay_feature  # noqa: E402
from .server import UNIT_SECONDS, make_server  # noqa: E402
from .state import DashboardState  # noqa: E402

UNITS = {name: (BarUnit[name.upper()], seconds) for name, seconds in UNIT_SECONDS.items()}
MAX_BARS = 5000          # per contract; the gateway allows up to 20,000 per request
MAX_UNIT_NUMBER = 1000


def parse_timeframe(unit: object, unit_number: object, bars: object) -> dict:
    """Validate a chart timeframe from the CLI or the page.

    Returns ``{"unit", "unit_number", "bars", "bar_seconds"}`` or raises ``ValueError`` with a
    message the page can show as-is.
    """
    if not isinstance(unit, str) or unit not in UNIT_SECONDS:
        raise ValueError("unit must be one of: " + ", ".join(UNIT_SECONDS))
    if isinstance(unit_number, bool) or isinstance(bars, bool):
        raise ValueError("unit_number and bars must be whole numbers")
    try:
        number = int(unit_number)
        count = int(bars)
    except (TypeError, ValueError):
        raise ValueError("unit_number and bars must be whole numbers") from None
    if not 1 <= number <= MAX_UNIT_NUMBER:
        raise ValueError(f"unit_number must be between 1 and {MAX_UNIT_NUMBER}")
    if not 1 <= count <= MAX_BARS:
        raise ValueError(f"bars must be between 1 and {MAX_BARS}")
    return {"unit": unit, "unit_number": number, "bars": count, "bar_seconds": UNIT_SECONDS[unit] * number}


def since_from_args(args) -> datetime:
    """``--since YYYY-MM-DD`` (UTC midnight) or the start of today in UTC."""
    if getattr(args, "since", None):
        return datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def lookback_start(end: datetime, unit_seconds: int, unit_number: int, bars: int) -> datetime:
    """Ask for 3x the bars' span (markets close nights and weekends), at least 4 days, at most 10 years."""
    span = timedelta(seconds=unit_seconds * unit_number * bars * 3)
    return end - min(max(span, timedelta(days=4)), timedelta(days=3650))


def resolve_contract(client: TopstepXClient, text: str) -> Contract:
    """Resolve an exact ID or a root symbol without ever waiting for terminal input.

    Browser requests (``/api/watch``) run on a server thread, so the interactive prompt in
    ``choose_active_contract`` would block with nobody to answer. Exact IDs go through
    ``choose_active_contract`` (no prompt on that path); symbols use the same matching rules
    and raise ValueError listing the candidates when several contracts match.
    """
    text = text.strip()
    if not text:
        raise ValueError("Enter a symbol (ES, NQ, CL) or an exact contract ID.")
    if text.upper().startswith("CON."):
        return choose_active_contract(client, text)
    matches = [c for c in client.search_contracts(text) if c.active_contract is True]
    if not matches:
        raise ValueError(f"No active contracts found for '{text}'. Try a root symbol like ES or an exact contract ID.")
    exact = [c for c in matches if c.name.casefold() == text.casefold()]
    if len(exact) == 1:
        return exact[0]
    roots = [c for c in matches if is_root_symbol_match(c.name, text)]
    if len(roots) == 1:
        return roots[0]
    if len(matches) == 1:
        return matches[0]
    options = ", ".join(f"{c.name} ({c.id})" for c in (roots or matches)[:8])
    raise ValueError(f"'{text}' matched several active contracts: {options}. Enter one exact contract ID.")


class Session:
    """Everything that lives for the duration of one dashboard run."""

    def __init__(self, args, *, client: TopstepXClient | None = None, state: DashboardState | None = None,
                 market: MarketHub | None = None, user: UserHub | None = None):
        self.args = args
        self.loop: asyncio.AbstractEventLoop | None = None
        self.since = since_from_args(args)
        # --replay PATH: a ReplayClient answers REST from the recording and a ReplayDriver turns the
        # (real) hubs into replay hubs. --record PATH: the hubs and REST client are observed.
        self.replay = None
        self.recorder = None
        if getattr(args, "replay", None) and client is None:
            self.replay = replay_engine.ReplayDriver.from_args(args)
            client = self.replay.client
            args.watch = self.replay.recording.watch_ids() or args.watch   # what was recorded, focus first
        self.client = client if client is not None else TopstepXClient()
        self.state = state if state is not None else DashboardState(self.since)
        self.market = market if market is not None else MarketHub(self.client.tokens)
        self.user = user if user is not None else UserHub(self.client.tokens)
        if self.replay is not None:
            self.replay.attach(self.market, self.user, self.state)
        elif getattr(args, "record", None):
            self.recorder = replay_engine.attach_recorder(args, self.client, self.market, self.user)
        self.account_id: int | None = None
        self.watch_ids: list[str] = []
        self.timeframe = parse_timeframe(args.unit, args.unit_number, args.bars)
        self._last_fetch: dict[str, float] = {}   # contract id -> monotonic time of its last bar fetch
        self._quotes_subscribed: set[str] = set()
        self._started = False
        # Feature modules (dashboard/features/*.py): routes are collected now, start() runs after the hubs.
        self.features = feature_registry.discover(getattr(args, "features", None))
        if self.replay is not None and not any(feature_registry.name_of(m) == "replay" for m in self.features):
            self.features.append(replay_feature)   # replay mode always blocks order entry, whatever --features says
        self.feature_routes = feature_registry.collect_routes(self.features, self)

    # -- lifecycle ------------------------------------------------------------------------

    async def start(self) -> None:
        """Login, accounts, initial account + watch list, REST seed, hubs, subscriptions."""
        self.loop = asyncio.get_running_loop()
        accounts = await asyncio.to_thread(self.client.search_accounts, True)
        if not accounts:
            raise ValueError("No active accounts were found for this login.")
        self.state.set_accounts(accounts)
        account = self._initial_account(accounts)
        self.state.select_account(account.id)
        self.account_id = account.id

        for text in self.args.watch:
            contract = await asyncio.to_thread(choose_active_contract, self.client, text)
            self.state.add_contract(contract)
            if contract.id not in self.watch_ids:
                self.watch_ids.append(contract.id)

        await self._seed_account(account.id)

        self.market.on_quote(self.state.apply_quote)
        self.market.on_trade(self.state.apply_trades)
        self.market.on_depth(self.state.apply_depth)
        self.market.on_connection_change(lambda ok: self.state.set_connection("market", ok))
        self.user.on_connection_change(lambda ok: self.state.set_connection("user", ok))
        self.user.on_account(self.state.apply_account_event)
        self.user.on_position(self._on_position)
        self.user.on_order(self.state.apply_order)
        self.user.on_user_trade(self.state.apply_trade)

        await self.market.start()
        await self.user.start()
        self._started = True
        for contract_id in self.state.contract_ids():
            await self._watch_quotes(contract_id)
        await self.user.subscribe_accounts()
        await self._subscribe_account(account.id)
        if self.watch_ids:
            await self.set_focus(self.watch_ids[0])
        await self._run_feature_hook("start")
        if self.replay is not None:
            self.replay.begin()

    async def stop(self) -> None:
        await self._run_feature_hook("stop")
        if self.replay is not None:
            await self.replay.close()
        if self.recorder is not None:
            await asyncio.to_thread(self.recorder.stop)
        with contextlib.suppress(Exception):
            await self.user.stop()
        with contextlib.suppress(Exception):
            await self.market.stop()
        with contextlib.suppress(Exception):
            self.client.close()

    async def _run_feature_hook(self, hook: str) -> None:
        """Call every feature's start()/stop(); a failing feature is reported, never fatal."""
        for module in self.features:
            fn = getattr(module, hook, None)
            if fn is None:
                continue
            try:
                result = fn(self)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # noqa: BLE001
                name = feature_registry.name_of(module)
                message = f"Feature {name} failed to {hook}: {type(exc).__name__}: {exc}"
                print(f"[dashboard] {message}", file=sys.stderr)
                if hook == "start":
                    self.state.set_error(message)

    def _initial_account(self, accounts):
        """``--account-id``, else the first simulated + can_trade account, else the first active one."""
        requested = getattr(self.args, "account_id", None)
        if requested is not None:
            account = next((a for a in accounts if a.id == requested), None)
            if account is None:
                raise ValueError(f"Active account {requested} was not found.")
            return account
        practice = next((a for a in accounts if a.simulated is True and a.can_trade is True), None)
        return practice or accounts[0]

    # -- account switching -----------------------------------------------------------------

    async def _seed_account(self, account_id: int) -> None:
        """Seed positions, open orders and fills from REST so the page is filled before events arrive."""
        since = getattr(self.state, "since", None) or self.since
        positions = await asyncio.to_thread(self.client.search_open_positions, account_id)
        for position in positions:
            self.state.apply_position(position.raw)
        open_orders = await asyncio.to_thread(self.client.search_open_orders, account_id)
        for order in open_orders:
            self.state.apply_order(order.raw)
        # Order/searchOpen leaves out Suspended bracket legs (exits waiting for their entry to fill);
        # today's Order/search includes them, so Flatten all / Cancel all see them from the start.
        search_orders = getattr(self.client, "search_orders", None)
        if callable(search_orders):
            try:
                for order in await asyncio.to_thread(search_orders, account_id, since):
                    if order.status == OrderStatus.SUSPENDED:
                        self.state.apply_order(order.raw)
            except Exception as exc:  # noqa: BLE001 - seeding extras must never stop startup
                print(f"[dashboard] could not load suspended orders: {type(exc).__name__}: {exc}", file=sys.stderr)
        trades = await asyncio.to_thread(self.client.search_trades, account_id, since)
        for trade in trades:
            self.state.apply_trade(trade.raw)
        for contract_id in self.state.position_contract_ids():
            await self._know_contract(contract_id)
            if self._started:
                await self._watch_quotes(contract_id)

    async def _subscribe_account(self, account_id: int) -> None:
        await self.user.subscribe_orders(account_id)
        await self.user.subscribe_positions(account_id)
        await self.user.subscribe_trades(account_id)

    async def _unsubscribe_account(self, account_id: int) -> None:
        await self.user.unsubscribe_orders(account_id)
        await self.user.unsubscribe_positions(account_id)
        await self.user.unsubscribe_trades(account_id)

    async def select_account(self, account_id: int) -> None:
        """Switch the page to another active account: drop the old user-hub subscriptions, reseed, subscribe."""
        previous = self.account_id
        if account_id not in {a["id"] for a in self.state.snapshot()["accounts"]}:
            raise ValueError(f"Account {account_id} is not one of the active accounts on this login.")
        if previous is not None and previous != account_id:
            await self._unsubscribe_account(previous)  # stop old-account events before the switch
        self.state.select_account(account_id)
        self.account_id = account_id
        await self._seed_account(account_id)
        if previous != account_id:
            await self._subscribe_account(account_id)

    # -- contracts and focus ------------------------------------------------------------------

    async def _know_contract(self, contract_id: str) -> None:
        if contract_id and not self.state.has_contract(contract_id):
            self.state.add_contract(await asyncio.to_thread(self.client.get_contract, contract_id))

    async def _watch_quotes(self, contract_id: str | None) -> None:
        """Know the contract (tick size/value) and stream its quotes, once."""
        if not contract_id or contract_id in self._quotes_subscribed:
            return
        await self._know_contract(contract_id)
        self._quotes_subscribed.add(contract_id)
        await self.market.subscribe_quotes(contract_id)

    async def _on_position(self, payload: object) -> None:
        await self._watch_quotes(self.state.apply_position(payload))

    async def set_focus(self, contract_id: str) -> None:
        """Stream depth + trades for the focused contract only."""
        if not self.state.has_contract(contract_id):
            raise ValueError(f"Contract {contract_id} is not on the watch list.")
        previous = self.state.focus()
        if previous == contract_id:
            return
        if previous:
            await self.market.unsubscribe_depth(previous)
            await self.market.unsubscribe_trades(previous)
        await self.market.subscribe_depth(contract_id)
        await self.market.subscribe_trades(contract_id)
        self.state.set_focus(contract_id)

    async def add_watch(self, text: str) -> str:
        """Resolve a symbol or ID, add it to the watch list and stream its quotes. Returns the ID."""
        contract = await asyncio.to_thread(resolve_contract, self.client, text)
        self.state.add_contract(contract)
        if contract.id not in self.watch_ids:
            self.watch_ids.append(contract.id)
        await self._watch_quotes(contract.id)
        return contract.id

    # -- bridge for server threads ------------------------------------------------------------

    def call_async(self, coro, timeout: float = 15.0):
        """Run a coroutine on the event loop from another thread and wait for its result."""
        if self.loop is None:
            coro.close()
            raise RuntimeError("the dashboard session has not started")
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    # -- chart bars -----------------------------------------------------------------------------

    async def refresh_bars(self, contract_id: str) -> None:
        timeframe = self.timeframe
        unit, unit_seconds = UNITS[timeframe["unit"]]
        number, count = timeframe["unit_number"], timeframe["bars"]
        end = datetime.now(timezone.utc)
        try:
            bars = await asyncio.to_thread(
                self.client.get_bars, contract_id,
                start=lookback_start(end, unit_seconds, number, count), end=end,
                unit=unit, unit_number=number, limit=count, include_partial_bar=True,
            )
            if self.timeframe is not timeframe:
                return  # the timeframe changed while this request was in flight; drop the stale bars
            self.state.set_bars(contract_id, bars)
            self.state.set_error(None)
        except TopstepXError as exc:
            self.state.set_error(f"Chart refresh failed for {contract_id}: {exc}")
        except Exception as exc:  # never let one bad refresh stop the loop
            self.state.set_error(f"Chart refresh failed for {contract_id}: {type(exc).__name__}: {exc}")

    async def refresh_bars_forever(self) -> None:
        """Focus contract every ``--refresh-focus`` seconds, every other contract every ``--refresh`` seconds.

        History/retrieveBars allows 50 requests per 30 s; with the defaults (5 s focus, 60 s
        others) a handful of watched contracts stays far below that.
        """
        last_fetch = self._last_fetch
        while True:
            focus = self.state.focus()
            now = time.monotonic()
            for contract_id in self.state.contract_ids():
                interval = self.args.refresh_focus if contract_id == focus else self.args.refresh
                if now - last_fetch.get(contract_id, float("-inf")) < interval:
                    continue
                last_fetch[contract_id] = now
                await self.refresh_bars(contract_id)
            await asyncio.sleep(1.0)

    async def set_timeframe(self, unit: object, unit_number: object, bars: object) -> dict:
        """Change the chart bars for every watched contract. The focus contract reloads right away;
        the others reload on the next pass of ``refresh_bars_forever``."""
        timeframe = parse_timeframe(unit, unit_number, bars)
        if timeframe != self.timeframe:
            self.timeframe = timeframe
            self.state.clear_bars()   # stored candles are the old bar size
            self._last_fetch.clear()
            focus = self.state.focus()
            if focus:
                self._last_fetch[focus] = time.monotonic()
                await self.refresh_bars(focus)
        return dict(self.timeframe)

    async def load_history(self, contract_id: str, before: object) -> dict:
        """Fetch the page of bars that ends at ``before`` (UTC seconds) and merge it in.

        The page is one ``timeframe["bars"]`` worth of bars in the current bar size. Returns
        ``{"added", "oldest", "count", "complete"}``; ``complete`` means the API returned nothing
        older, so the page can stop asking.
        """
        if not self.state.has_contract(contract_id):
            raise ValueError(f"Contract {contract_id} is not on the watch list.")
        if isinstance(before, bool) or not isinstance(before, int) or before <= 0:
            raise ValueError("before must be a UTC unix time in seconds")
        timeframe = self.timeframe
        unit, unit_seconds = UNITS[timeframe["unit"]]
        number, count = timeframe["unit_number"], timeframe["bars"]
        end = datetime.fromtimestamp(before, tz=timezone.utc)
        bars = await asyncio.to_thread(
            self.client.get_bars, contract_id,
            start=lookback_start(end, unit_seconds, number, count), end=end,
            unit=unit, unit_number=number, limit=count, include_partial_bar=False,
        )
        added = 0
        if self.timeframe is timeframe:   # otherwise the bars are the wrong size now; drop them
            older = [b for b in bars if b.time is not None and int(b.time.timestamp()) < before]
            added = self.state.add_history(contract_id, older)
        return {
            "added": added,
            "oldest": self.state.oldest_time(contract_id),
            "count": self.state.candle_count(contract_id),
            "complete": added == 0,
        }


def _default_announce(session: Session, url: str) -> None:
    print(f"Dashboard -> {url}")


async def run(args, announce: Callable[[Session, str], None] | None = None) -> None:
    """Build the session, start it, serve the page, wait, then stop everything."""
    session = Session(args)
    server = None
    try:
        await session.start()
        server = make_server(session, port=args.port)
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        (announce or _default_announce)(session, url)
        if not args.no_open:
            webbrowser.open(url)

        refresh = asyncio.create_task(session.refresh_bars_forever(), name="dashboard-bars")
        try:
            if args.seconds:
                await asyncio.sleep(args.seconds)
            else:
                await asyncio.Event().wait()
        finally:
            refresh.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresh
    finally:
        if server is not None:
            await asyncio.to_thread(server.shutdown)  # keep the loop free for handlers still in call_async
            server.server_close()
        await session.stop()
