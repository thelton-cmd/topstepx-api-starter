"""Record the live stream to a file, then replay it later at any speed.

Three parts, all offline-testable:

- ``Recorder`` (``--record PATH``): every hub message and the REST answers the page needs to start
  offline are appended to newline-delimited JSON (``.gz`` suffix = gzip) by one background thread.
- ``Recording``: the loaded file (hub events sorted by recorded time, REST answers by path).
- ``ReplayDriver`` + ``ReplayClient`` (``--replay PATH``): the dashboard is built with the *real*
  ``MarketHub`` / ``UserHub`` objects, so parsing and callbacks are identical to live; the driver
  only replaces ``start()``/``stop()``/subscribing with no-ops and feeds the recorded "in"
  messages to the hubs' event handlers on the recorded timeline. ``ReplayClient`` answers the
  REST calls from the recording. Nothing here can send an order: ``ReplayClient`` has no
  order methods at all, and the replay feature blocks every order route.

Line formats (one JSON object per line):

    {"kind": "meta", "version": 1, "started": "<iso utc>"}
    {"t": 1.234, "wall": "<iso utc>", "hub": "market"|"user", "dir": "in"|"out", "name": "GatewayQuote", "args": [...]}
    {"t": 1.5, "wall": "<iso utc>", "kind": "rest", "path": "/Account/search", "request": {...}, "response": {...}}

Tokens, API keys and passwords are redacted from REST records. A recording still contains your
account data (balances, positions, fills); keep it private and out of git.
"""

from __future__ import annotations

import asyncio
import bisect
import contextlib
import gzip
import json
import math
import os
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from topstepx import BarUnit, TopstepXError
from topstepx.models import Account, Bar, Contract, Order, Position, Trade

FORMAT_VERSION = 1
# REST answers the page needs to start offline (plus the by-id lookup and the account seed calls).
RECORDED_PATHS = frozenset({
    "/Account/search", "/Contract/search", "/Contract/available", "/Contract/searchById",
    "/History/retrieveBars", "/Position/searchOpen", "/Order/searchOpen", "/Trade/search",
})
SECRET_KEYS = frozenset({"token", "apikey", "api_key", "password", "accesstoken", "access_token"})
MAX_QUEUE = 500_000          # recorder backlog before lines are dropped (and counted)
MAX_SPEED = 1000.0
TICK_SECONDS = 0.02          # replay clock resolution
YIELD_EVERY = 500            # events dispatched between loop yields during a fast seek


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def redact(value: Any) -> Any:
    """A deep copy of ``value`` with token / apiKey / password style fields replaced."""
    if isinstance(value, dict):
        return {k: ("<redacted>" if isinstance(k, str) and k.lower() in SECRET_KEYS else redact(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def _open_text(path: Path, mode: str):
    if path.suffix.lower() == ".gz":
        return gzip.open(path, mode + "t", encoding="utf-8")
    return open(path, mode, encoding="utf-8", newline="\n")


def _human_size(count: int) -> str:
    return f"{count} bytes" if count < 1024 else f"{count / 1024:.1f} KB" if count < 1024 * 1024 else f"{count / 1024 / 1024:.1f} MB"


# -- recording ---------------------------------------------------------------------------------


class Recorder:
    """Appends hub messages and REST answers to a file from one background thread.

    ``record_*`` are safe to call from the asyncio thread or any other: they serialise the line
    and put it on a queue, never touching the disk. The writer flushes about once a second and
    on ``stop()``.
    """

    def __init__(self, path: str | os.PathLike, *, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], datetime] = _now, flush_interval: float = 1.0,
                 announce: Callable[[str], None] | None = print, max_queue: int = MAX_QUEUE):
        self.path = Path(path)
        self._clock = clock
        self._wall = wall
        self._flush_interval = flush_interval
        self._announce = announce
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self._thread: threading.Thread | None = None
        self._handle = None
        self._t0 = 0.0
        self._lock = threading.Lock()
        self._lines = 0
        self._hub_lines = 0
        self._rest_lines = 0
        self._dropped = 0
        self._stopped = False
        self._summary: dict | None = None

    # -- lifecycle -------------------------------------------------------------------------------

    def start(self) -> "Recorder":
        if self._thread is not None:
            return self
        if self.path.exists():
            raise ValueError(f"{self.path} already exists; choose a new file name for --record.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = _open_text(self.path, "w")
        self._t0 = self._clock()
        started = self._wall()
        self._handle.write(json.dumps({"kind": "meta", "version": FORMAT_VERSION, "started": started.isoformat()}) + "\n")
        self._thread = threading.Thread(target=self._writer, name="dashboard-recorder", daemon=True)
        self._thread.start()
        if self._announce:
            self._announce(f"Recording to {self.path}")
        return self

    def stop(self) -> dict:
        """Flush, close and return ``{path, lines, hub, rest, dropped, bytes}``. Safe to call twice."""
        if self._summary is not None:
            return self._summary
        self._stopped = True
        if self._thread is not None:
            self._queue.put(None)      # sentinel: after every queued line
            self._thread.join(timeout=30)
        if self._handle is not None:
            with contextlib.suppress(Exception):
                self._handle.close()
        size = self.path.stat().st_size if self.path.exists() else 0
        with self._lock:
            self._summary = {"path": str(self.path), "lines": self._lines, "hub": self._hub_lines,
                             "rest": self._rest_lines, "dropped": self._dropped, "bytes": size}
        if self._announce and self._thread is not None:
            extra = f", {self._dropped} dropped" if self._dropped else ""
            self._announce(f"Recording saved: {self.path} ({self._hub_lines} hub messages, "
                           f"{self._rest_lines} REST answers{extra}, {_human_size(size)})")
        return self._summary

    @property
    def active(self) -> bool:
        return self._thread is not None and not self._stopped

    @property
    def lines(self) -> int:
        with self._lock:
            return self._lines

    # -- producers -------------------------------------------------------------------------------

    def _put(self, record: dict, kind: str) -> None:
        if self._stopped or self._thread is None:
            return
        try:
            line = json.dumps(record, default=str, separators=(",", ":"))
            self._queue.put_nowait((kind, line))
        except queue.Full:
            with self._lock:
                self._dropped += 1
        except (TypeError, ValueError):
            with self._lock:
                self._dropped += 1

    def _stamp(self) -> dict:
        return {"t": round(self._clock() - self._t0, 6), "wall": self._wall().isoformat()}

    def record_hub(self, hub: str, direction: str, name: str, args: list) -> None:
        self._put({**self._stamp(), "hub": hub, "dir": direction, "name": name,
                   "args": redact(list(args) if isinstance(args, (list, tuple)) else [args])}, "hub")

    def hub_observer(self, hub: str) -> Callable[[str, str, list], None]:
        return lambda direction, name, args: self.record_hub(hub, direction, name, args)

    def record_rest(self, record: dict) -> None:
        """An ``ApiHttp`` observer record -> a ``kind: rest`` line (only the paths the page needs, only successes)."""
        path = record.get("path")
        response = record.get("response")
        if path not in RECORDED_PATHS or record.get("error") or not isinstance(response, dict) \
                or response.get("success") is not True:
            return
        self._put({**self._stamp(), "kind": "rest", "path": path,
                   "request": redact(record.get("request") or {}), "response": redact(response)}, "rest")

    # -- the writer thread -----------------------------------------------------------------------

    def _writer(self) -> None:
        last_flush = time.monotonic()
        handle = self._handle
        while True:
            try:
                item = self._queue.get(timeout=0.25)
            except queue.Empty:
                item = False
            if item is None:
                break
            if item:
                kind, line = item
                try:
                    handle.write(line + "\n")
                except Exception:  # noqa: BLE001 - a full disk must not kill the dashboard
                    with self._lock:
                        self._dropped += 1
                else:
                    with self._lock:
                        self._lines += 1
                        if kind == "hub":
                            self._hub_lines += 1
                        else:
                            self._rest_lines += 1
            if time.monotonic() - last_flush >= self._flush_interval:
                with contextlib.suppress(Exception):
                    handle.flush()
                last_flush = time.monotonic()
        with contextlib.suppress(Exception):
            handle.flush()


# -- a loaded recording -----------------------------------------------------------------------


@dataclass
class Event:
    t: float
    wall: str | None
    hub: str
    name: str
    args: list


class Recording:
    """Everything in a recording file, indexed for replay."""

    def __init__(self, path: str | os.PathLike = "<memory>"):
        self.path = Path(path)
        self.started: datetime | None = None
        self.events: list[Event] = []       # "in" messages, sorted by t
        self.outs: list[Event] = []         # "out" messages (the subscriptions made while recording)
        self.rest: list[dict] = []          # {"t", "path", "request", "response"} in file order
        self.skipped = 0                    # unreadable lines
        self.truncated = False              # the file ended mid-way (a recording that was not closed)
        self.duration = 0.0
        self._first_wall: tuple[float, datetime] | None = None

    # -- loading ---------------------------------------------------------------------------------

    @classmethod
    def load(cls, path: str | os.PathLike) -> "Recording":
        rec = cls(path)
        if not rec.path.is_file():
            raise ValueError(f"Replay file not found: {rec.path}")
        try:
            with _open_text(rec.path, "r") as handle:
                for raw in handle:
                    rec._add_line(raw)
        except (EOFError, OSError, UnicodeDecodeError) as exc:   # gzip cut short, etc.: keep what was read
            rec.truncated = True
            if not (rec.events or rec.outs or rec.rest) and not isinstance(exc, EOFError):
                raise ValueError(f"Could not read the replay file {rec.path}: {type(exc).__name__}") from exc
        rec._finish()
        return rec

    @classmethod
    def from_lines(cls, lines: list[str] | list[dict]) -> "Recording":
        rec = cls()
        for item in lines:
            rec._add_line(item if isinstance(item, str) else json.dumps(item))
        rec._finish()
        return rec

    def _add_line(self, raw: str) -> None:
        raw = raw.strip()
        if not raw:
            return
        try:
            row = json.loads(raw)
        except ValueError:
            self.skipped += 1
            return
        if not isinstance(row, dict):
            self.skipped += 1
            return
        if row.get("kind") == "meta":
            self.started = _parse_iso(row.get("started"))
            return
        t = row.get("t")
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) or t < 0:
            self.skipped += 1
            return
        if self._first_wall is None:
            wall = _parse_iso(row.get("wall"))
            if wall is not None:
                self._first_wall = (float(t), wall)
        if row.get("kind") == "rest":
            if isinstance(row.get("path"), str) and isinstance(row.get("response"), dict):
                self.rest.append({"t": float(t), "path": row["path"], "request": row.get("request") or {},
                                  "response": row["response"]})
            else:
                self.skipped += 1
            return
        hub, name, args = row.get("hub"), row.get("name"), row.get("args")
        if hub not in ("market", "user") or not isinstance(name, str) or not isinstance(args, list) \
                or row.get("dir") not in ("in", "out"):
            self.skipped += 1
            return
        event = Event(float(t), row.get("wall"), hub, name, args)
        (self.events if row["dir"] == "in" else self.outs).append(event)

    def _finish(self) -> None:
        self.events.sort(key=lambda e: e.t)   # stable: equal times keep file order
        self.outs.sort(key=lambda e: e.t)
        last = [self.events[-1].t] if self.events else []
        last += [self.outs[-1].t] if self.outs else []
        last += [max(r["t"] for r in self.rest)] if self.rest else []
        self.duration = max(last) if last else 0.0
        if self.started is None and self._first_wall is not None:
            t, wall = self._first_wall
            self.started = wall - timedelta(seconds=t)

    # -- queries -----------------------------------------------------------------------------------

    def clock_at(self, position: float) -> datetime | None:
        """The recorded wall-clock time ``position`` seconds into the recording."""
        return None if self.started is None else self.started + timedelta(seconds=max(0.0, position))

    def responses(self, path: str) -> list[dict]:
        return [r for r in self.rest if r["path"] == path]

    def watch_ids(self) -> list[str]:
        """Contracts that were quoted while recording, the depth-focus contract first."""
        quotes: list[str] = []
        focus: list[str] = []
        for event in self.outs:
            if event.hub != "market" or not event.args or not isinstance(event.args[0], str):
                continue
            if event.name == "SubscribeContractQuotes" and event.args[0] not in quotes:
                quotes.append(event.args[0])
            elif event.name == "SubscribeContractMarketDepth" and event.args[0] not in focus:
                focus.append(event.args[0])
        ordered = [c for c in focus[:1]] + [c for c in quotes if c not in focus[:1]]
        return ordered


# -- the replay client ----------------------------------------------------------------------------


class _NullHttp:
    """Stands in for ``client._http`` so features that observe REST calls find something to attach to."""

    def __init__(self) -> None:
        self.observers: list[Callable[[dict], Any]] = []

    def add_observer(self, callback: Callable[[dict], Any]) -> None:
        self.observers.append(callback)


class _NoTokens:
    def get_token(self) -> str:
        raise TopstepXError("Replay mode has no login.")

    def invalidate(self) -> None:
        pass

    def close(self) -> None:
        pass


def _unit_key(unit: object, number: object) -> tuple[int, int]:
    return int(unit), int(number)


class ReplayClient:
    """Answers the dashboard's REST reads from a ``Recording``. There are no order methods.

    Accounts, contracts and bars come from the recorded answers; a request with no recorded
    answer goes to ``fallback`` (a live ``TopstepXClient``, only passed when credentials exist) for
    contract and account lookups, otherwise it returns nothing and ``on_note`` is told why (the
    dashboard shows that as its last error). Open positions, open orders and fills are replayed
    from what was recorded and are never taken from the live account.
    """

    def __init__(self, recording: Recording, fallback: Any = None, on_note: Callable[[str], None] | None = None):
        self.recording = recording
        self.fallback = fallback
        self.on_note = on_note
        self.tokens = _NoTokens()
        self._http = _NullHttp()
        self._position: Callable[[], float] = lambda: 0.0
        self._noted: set[str] = set()
        self._contracts: dict[str, Contract] = {}
        for path in ("/Contract/search", "/Contract/available", "/Contract/searchById"):
            for rec in recording.responses(path):
                body = rec["response"]
                items = list(body.get("contracts") or [])
                if isinstance(body.get("contract"), dict):
                    items.append(body["contract"])
                for item in items:
                    if isinstance(item, dict) and isinstance(item.get("id"), str):
                        self._contracts.setdefault(item["id"], Contract.from_api(item))
        self._bars: dict[tuple[str, int, int], list[tuple[float, list[dict]]]] = {}
        for rec in recording.responses("/History/retrieveBars"):
            req = rec["request"]
            try:
                key = (str(req["contractId"]),) + _unit_key(req["unit"], req["unitNumber"])
            except (KeyError, TypeError, ValueError):
                continue
            bars = [b for b in rec["response"].get("bars") or [] if isinstance(b, dict)]
            self._bars.setdefault(key, []).append((rec["t"], bars))
        for series in self._bars.values():
            series.sort(key=lambda item: item[0])

    def bind_clock(self, position: Callable[[], float]) -> None:
        self._position = position

    def clock(self) -> datetime | None:
        return self.recording.clock_at(self._position())

    def _note(self, message: str) -> None:
        if message in self._noted:
            return
        self._noted.add(message)
        if self.on_note:
            self.on_note(message)

    # -- accounts and contracts -----------------------------------------------------------------

    def search_accounts(self, only_active: bool = True) -> list[Account]:
        answers = self.recording.responses("/Account/search")
        if answers:
            return [Account.from_api(a) for a in answers[0]["response"].get("accounts") or [] if isinstance(a, dict)]
        return self._fallback_list("search_accounts", "accounts", only_active)

    def search_contracts(self, search_text: str, live: bool = False) -> list[Contract]:
        text = (search_text or "").strip().casefold()
        for rec in self.recording.responses("/Contract/search"):
            if str(rec["request"].get("searchText", "")).strip().casefold() == text:
                return [Contract.from_api(c) for c in rec["response"].get("contracts") or [] if isinstance(c, dict)]
        known = [c for c in self._contracts.values()
                 if text and (text in c.name.casefold() or text in c.description.casefold() or text in c.id.casefold())]
        if known:
            return known
        return self._fallback_list("search_contracts", "contracts", search_text, live)

    def list_available_contracts(self, live: bool = False) -> list[Contract]:
        answers = self.recording.responses("/Contract/available")
        if answers:
            return [Contract.from_api(c) for c in answers[0]["response"].get("contracts") or [] if isinstance(c, dict)]
        if self._contracts:
            return list(self._contracts.values())
        return self._fallback_list("list_available_contracts", "contracts", live)

    def get_contract(self, contract_id: str) -> Contract:
        found = self._contracts.get(contract_id)
        if found is not None:
            return found
        if self.fallback is not None:
            try:
                return self.fallback.get_contract(contract_id)
            except Exception:  # noqa: BLE001 - fall through to the placeholder
                pass
        self._note(f"Replay: contract {contract_id} is not in the recording; tick size and value are unknown.")
        return Contract(id=contract_id, name=contract_id, active_contract=None)

    def _fallback_list(self, method: str, what: str, *args) -> list:
        if self.fallback is not None:
            try:
                return list(getattr(self.fallback, method)(*args))
            except Exception as exc:  # noqa: BLE001
                self._note(f"Replay: no recorded {what}, and the live lookup failed ({type(exc).__name__}).")
                return []
        self._note(f"Replay: no recorded {what} and no credentials for a live lookup; showing none.")
        return []

    # -- bars ------------------------------------------------------------------------------------

    def get_bars(self, contract_id: str, start: datetime, end: datetime, unit: BarUnit = BarUnit.MINUTE,
                 unit_number: int = 5, limit: int = 1000, live: bool = False,
                 include_partial_bar: bool = False) -> list[Bar]:
        """Recorded bars no later than the replay clock (and ``end``), newest first, at most ``limit``."""
        key = (contract_id,) + _unit_key(unit, unit_number)
        clock = self.clock()
        horizon = end if clock is None else min(end, clock)
        series = self._bars.get(key)
        if series:
            position = self._position()
            times = [t for t, _ in series]
            latest = max(bisect.bisect_right(times, position) - 1, 0)
            merged: dict[str, dict] = {}
            for index in {0, latest}:
                for raw in series[index][1]:
                    if isinstance(raw.get("t"), str):
                        merged[raw["t"]] = raw
            bars = [Bar.from_api(raw) for raw in merged.values()]
        elif self.fallback is not None:
            try:
                bars = list(self.fallback.get_bars(contract_id, start, end, unit, unit_number, limit, live,
                                                   include_partial_bar))
            except Exception as exc:  # noqa: BLE001
                raise TopstepXError(f"Replay: no recorded bars for {contract_id} and the live lookup failed "
                                    f"({type(exc).__name__}).") from exc
        else:
            have = sorted({f"{n} x unit {u}" for (c, u, n) in self._bars if c == contract_id})
            raise TopstepXError(
                f"Replay: no recorded bars for {contract_id} at this bar size"
                + (f" (recorded: {', '.join(have)})" if have else "") + ". Use the bar size the recording was made with.")
        kept = [b for b in bars if b.time is not None and (horizon is None or b.time <= horizon)]
        kept.sort(key=lambda b: b.time, reverse=True)
        return kept[: max(int(limit), 0)]

    # -- account state: replayed, never live --------------------------------------------------------

    def _seed(self, path: str, account_id: int, field: str) -> list[dict]:
        for rec in self.recording.responses(path):
            if rec["request"].get("accountId") == account_id:
                return [x for x in rec["response"].get(field) or [] if isinstance(x, dict)]
        return []

    def search_open_positions(self, account_id: int) -> list[Position]:
        return [Position.from_api(p) for p in self._seed("/Position/searchOpen", account_id, "positions")]

    def search_open_orders(self, account_id: int) -> list[Order]:
        return [Order.from_api(o) for o in self._seed("/Order/searchOpen", account_id, "orders")]

    def search_trades(self, account_id: int, start: datetime, end: datetime | None = None) -> list[Trade]:
        return [Trade.from_api(t) for t in self._seed("/Trade/search", account_id, "trades")]

    def search_orders(self, account_id: int, start: datetime, end: datetime | None = None) -> list[Order]:
        return []

    def close(self) -> None:
        if self.fallback is not None:
            with contextlib.suppress(Exception):
                self.fallback.close()


def live_fallback() -> Any:
    """A live ``TopstepXClient`` for lookups the recording lacks, or None when there are no credentials."""
    try:
        from topstepx import TopstepXClient
        return TopstepXClient()
    except Exception:  # noqa: BLE001 - no .env / no credentials: replay works offline
        return None


# -- the driver --------------------------------------------------------------------------------------


class ReplayDriver:
    """Feeds recorded "in" messages to the real hubs on the recorded timeline.

    ``advance(dt)`` is the whole engine (the background task just calls it every tick), so tests can
    drive it deterministically. Position is seconds into the recording; ``speed`` multiplies real time.
    """

    def __init__(self, recording: Recording, client: ReplayClient, *, path: str | None = None,
                 speed: float = 1.0, loop: bool = False, playing: bool = True):
        self.recording = recording
        self.client = client
        self.file = str(path or recording.path)
        self.speed = float(speed)
        self.loop = bool(loop)
        self.playing = bool(playing)
        self.position = 0.0
        self.index = 0
        self.loops = 0
        self.ended = False
        self.hubs: dict[str, Any] = {}
        self.subscriptions: dict[tuple[str, str, tuple], None] = {}
        self.state = None
        self.dispatch_errors = 0
        self._lock: asyncio.Lock | None = None
        self._task: asyncio.Task | None = None
        client.bind_clock(lambda: self.position)

    # -- wiring ------------------------------------------------------------------------------------

    @classmethod
    def from_args(cls, args, fallback: Any = "auto") -> "ReplayDriver":
        recording = Recording.load(args.replay)
        client = ReplayClient(recording, live_fallback() if fallback == "auto" else fallback)
        return cls(recording, client, path=str(args.replay), speed=getattr(args, "speed", None) or 1.0,
                   loop=bool(getattr(args, "replay_loop", False)))

    def attach(self, market, user, state=None) -> None:
        """Turn the two real hubs into replay hubs: connected, no network, subscriptions swallowed."""
        self.state = state
        if state is not None:
            self.client.on_note = self.client.on_note or state.set_error
        for name, hub in (("market", market), ("user", user)):
            self.hubs[name] = hub
            self._patch(name, hub)

    def _patch(self, name: str, hub) -> None:
        async def start() -> None:
            hub._connected.set()
            hub._notify_connection(True)

        async def stop() -> None:
            hub._connected.clear()

        async def subscribe(method: str, *args) -> None:
            hub._subscriptions.add((method, args))
            self.subscriptions[(name, method, tuple(args))] = None

        async def unsubscribe(method: str, *args) -> None:
            hub._subscriptions.discard((method.replace("Unsubscribe", "Subscribe", 1), args))
            self.subscriptions.pop((name, method.replace("Unsubscribe", "Subscribe", 1), tuple(args)), None)

        hub.start, hub.stop, hub._subscribe, hub._unsubscribe = start, stop, subscribe, unsubscribe

    def _guard(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # -- status ----------------------------------------------------------------------------------------

    @property
    def duration(self) -> float:
        return self.recording.duration

    def status(self) -> dict:
        clock = self.recording.clock_at(self.position)
        return {
            "active": True,
            "file": Path(self.file).name,
            "speed": self.speed,
            "playing": self.playing,
            "position_s": round(self.position, 3),
            "duration_s": round(self.duration, 3),
            "clock": None if clock is None else clock.isoformat(timespec="seconds"),
            "loop": self.loop,
            "ended": self.ended,
            "loops": self.loops,
            "events": len(self.recording.events),
            "replayed": self.index,
            "subscriptions": [f"{hub} {method} {', '.join(str(a) for a in args)}".strip()
                              for hub, method, args in self.subscriptions],
        }

    # -- controls ----------------------------------------------------------------------------------------

    def play(self) -> None:
        if self.ended and not self.loop:
            return
        self.playing = True

    def pause(self) -> None:
        self.playing = False

    def set_speed(self, speed: float) -> None:
        if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not math.isfinite(speed) \
                or not 0 < speed <= MAX_SPEED:
            raise ValueError(f"speed must be a number greater than 0 and at most {MAX_SPEED:g}")
        self.speed = float(speed)

    async def advance(self, dt: float) -> None:
        """Move the clock forward ``dt`` real seconds (times the speed) and dispatch what became due."""
        if not self.playing or dt <= 0:
            return
        async with self._guard():
            self.ended = False
            self.position = min(self.position + dt * self.speed, self.duration)
            await self._dispatch_until(self.position)
            if self.index >= len(self.recording.events) and self.position >= self.duration:
                if self.loop:
                    await self._restart()
                else:
                    self.playing = False
                    self.ended = True

    async def step(self) -> bool:
        """Dispatch the next recorded event now (used while paused). False at the end."""
        async with self._guard():
            if self.index >= len(self.recording.events):
                return False
            event = self.recording.events[self.index]
            self.position = max(self.position, event.t)
            await self._dispatch(event)
            self.index += 1
            return True

    async def seek(self, target: float) -> None:
        """Jump to ``target`` seconds. Backwards restarts from the beginning (state is cleared) and
        fast-forwards without pacing, so the ladder, tape and positions match that moment."""
        if isinstance(target, bool) or not isinstance(target, (int, float)) or not math.isfinite(target):
            raise ValueError("seek needs a number of seconds")
        target = min(max(float(target), 0.0), self.duration)
        async with self._guard():
            if target < self.position or (self.index and self.recording.events[self.index - 1].t > target):
                await self._rewind()
            self.position = target
            await self._dispatch_until(target)
            self.ended = False

    async def _rewind(self) -> None:
        self.index = 0
        self.position = 0.0
        self._reset_state()

    async def _restart(self) -> None:
        await self._rewind()
        self.loops += 1
        self.ended = False

    async def _dispatch_until(self, position: float) -> None:
        events = self.recording.events
        count = 0
        while self.index < len(events) and events[self.index].t <= position:
            await self._dispatch(events[self.index])
            self.index += 1
            count += 1
            if count % YIELD_EVERY == 0:
                await asyncio.sleep(0)

    async def _dispatch(self, event: Event) -> None:
        hub = self.hubs.get(event.hub)
        handler = None if hub is None else hub._event_handlers.get(event.name)
        if handler is None:
            return
        try:
            result = handler(list(event.args))
            if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                await result
        except Exception:  # noqa: BLE001 - one bad recorded message never stops the replay
            self.dispatch_errors += 1

    def _reset_state(self) -> None:
        """Clear what events build up (quotes, depth, tape, positions, orders, fills), then seed it
        again from the recording, the way the dashboard seeded it at the start."""
        state = self.state
        if state is None:
            return
        from .state import DepthBook, Tape

        with state._lock:  # noqa: SLF001 - state.py has no reset; this keeps its invariants
            state.quotes.clear()
            for contract_id in list(state.depths):
                state.depths[contract_id] = DepthBook()   # empty, not missing: /api/depth keeps answering 200
            state.positions.clear()
            state.orders.clear()
            state.trades.clear()
            for contract_id, old in list(state.tapes.items()):
                fresh = Tape()
                fresh._seq = old._seq   # noqa: SLF001 - keep seq rising so the page keeps polling
                state.tapes[contract_id] = fresh
            account_id = state.account_id
        if account_id is None:
            return
        since = getattr(state, "since", None) or datetime.now(timezone.utc)
        for position in self.client.search_open_positions(account_id):
            state.apply_position(position.raw)
        for order in self.client.search_open_orders(account_id):
            state.apply_order(order.raw)
        for trade in self.client.search_trades(account_id, since):
            state.apply_trade(trade.raw)

    # -- the background task ---------------------------------------------------------------------------

    def begin(self) -> None:
        """Start the clock task on the running loop."""
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="dashboard-replay")

    async def _run(self) -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(TICK_SECONDS)
            now = time.monotonic()
            dt, last = now - last, now
            try:
                await self.advance(dt)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                self.dispatch_errors += 1

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


# -- Session helpers --------------------------------------------------------------------------------


def attach_recorder(args, client, market, user) -> Recorder:
    """Start ``--record PATH`` and hook the hubs' raw observers and the client's REST observer."""
    recorder = Recorder(args.record).start()
    market.add_raw_observer(recorder.hub_observer("market"))
    user.add_raw_observer(recorder.hub_observer("user"))
    client._http.add_observer(recorder.record_rest)   # noqa: SLF001 - the kit's documented observer hook
    return recorder
