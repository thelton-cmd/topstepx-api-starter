"""Feature: alerts, notifications and the trader's own guardrails.

Three things, all driven by numbers the trader types (nothing here has a suggested value):

- **Price alerts.** ``{id, contract_id, price, direction, note, once, created, armed, last_triggered}``
  with ``direction`` "above" | "below" | "cross". Every quote is checked (the ``last`` price, or the
  bid/ask midpoint while no last price is known) against the previous observed price, so a gap
  straight through the level still counts: "above" fires when price moves from at-or-below the
  level to above it, "below" the opposite, "cross" either way. A ``once`` alert disarms after it
  fires; any other alert waits until price is back on the other side of its level and re-arms.
- **Fill notifications.** Each fill on the selected account (user hub ``GatewayUserTrade``) becomes an
  event, so the page can say "Filled BUY 1 ESZ26 @ 7,765.25".
- **Guardrails.** ``max_daily_loss`` is compared with ``totals["total"]`` from ``state.snapshot()``
  (today's realized + unrealized minus fees, an approximation from API data) and ``max_contracts``
  with ``totals["open_contracts"]``. When one is reached, ``order_entry_blocked()`` returns a reason
  naming the trader's own number, and the dashboard's order routes answer 403 with it. The pause
  stays until the trader changes or clears the guard, even if the figure recovers.

Events (alert triggers, fills, guard pauses, test notifications) go to one ring of 200 with a
monotonic ``seq`` that the page polls with ``GET /api/alerts/events?after=seq``.

Alerts and the guard are saved to ``%LOCALAPPDATA%\\topstepx\\alerts.json`` on Windows and
``~/.topstepx/alerts.json`` elsewhere, or to the file named by the ``TOPSTEPX_ALERTS_FILE``
environment variable. Nothing else is ever written there: no account data, no token.

This module never places, modifies, cancels or closes anything. It only watches the data the
dashboard already receives and, when a limit the trader set is reached, pauses order entry.
Nothing it shows is trading advice.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import secrets
import sys
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from topstepx import OrderSide

from ..state import enum_name, unwrap

NAME = "alerts"
TITLE = "Alerts and guardrails"
DESCRIPTION = ("Price alerts you set, notifications for your fills, and your own daily loss and "
               "contract limits that pause order entry on this dashboard.")
ORDER = 50
JS = "alerts.js"
CSS = "alerts.css"

DIRECTIONS = ("above", "below", "cross")
MAX_ALERTS = 200
MAX_EVENTS = 200
MAX_NOTE = 120
MAX_PRICE = 1e9
MAX_LOSS_LIMIT = 1e9
MAX_CONTRACT_LIMIT = 10_000
SEEN_FILLS = 2000            # fill ids remembered so a repeated stream message notifies once
LOOP_SECONDS = 1.0           # guard check + pending save, while the dashboard runs
FILE_ENV = "TOPSTEPX_ALERTS_FILE"
FILE_NAME = "alerts.json"
FILE_VERSION = 1

ALERT_FIELDS = ("id", "contract_id", "price", "direction", "note", "once", "created", "armed",
                "last_triggered", "trigger_count", "rearm_when")
GUARD_FIELDS = ("max_daily_loss", "max_contracts", "paused_at", "paused_reason", "updated")
REARM_SIDES = ("at_or_below", "at_or_above")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ascii(text: str) -> str:
    return str(text).encode("ascii", "replace").decode("ascii")


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


# -- formatting (ASCII; the page formats its own copy with the contract's decimals) ---------------


def decimals_for(tick_size: object) -> int:
    tick = _finite(tick_size)
    if not tick or tick <= 0:
        return 2
    text = f"{tick:.10f}".rstrip("0")
    return min(len(text.split(".")[1]) if "." in text else 0, 6)


def fmt_price(value: object, tick_size: object = None) -> str:
    number = _finite(value)
    return "-" if number is None else f"{number:,.{decimals_for(tick_size)}f}"


def fmt_money(value: object) -> str:
    number = _finite(value)
    if number is None:
        return "-"
    return ("-" if number < 0 else "") + f"{abs(number):,.2f}"


# -- input validation (ValueError -> 400 with the message) ----------------------------------------


def _number(value: object, name: str) -> float:
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            raise ValueError(f"{name} must be a number")
        try:
            value = float(text)
        except ValueError:
            raise ValueError(f"{name} must be a number") from None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return value


def parse_price(value: object) -> float:
    price = _number(value, "price")
    if price <= 0 or price > MAX_PRICE:
        raise ValueError("price must be greater than 0")
    return price


def parse_direction(value: object) -> str:
    if not isinstance(value, str) or value.strip().lower() not in DIRECTIONS:
        raise ValueError("direction must be one of: above, below, cross")
    return value.strip().lower()


def parse_note(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("note must be text")
    text = " ".join(value.split())   # one line
    if len(text) > MAX_NOTE:
        raise ValueError(f"note can be at most {MAX_NOTE} characters")
    return text


def parse_bool(value: object, name: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ValueError(f"{name} must be true or false")


def _blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def parse_loss_limit(value: object) -> float | None:
    """The most the trader is willing to lose today, as a positive amount; blank/null = off."""
    if _blank(value):
        return None
    amount = _number(value, "max_daily_loss")
    if amount <= 0 or amount > MAX_LOSS_LIMIT:
        raise ValueError("max_daily_loss is the most you are willing to lose today, as a positive amount "
                         "(for example 500), or null to turn it off")
    return amount


def parse_contract_limit(value: object) -> int | None:
    """The most open contracts the trader allows; blank/null = off."""
    if _blank(value):
        return None
    message = f"max_contracts must be a whole number from 1 to {MAX_CONTRACT_LIMIT:,}, or null to turn it off"
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            raise ValueError(message) from None
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_CONTRACT_LIMIT:
        raise ValueError(message)
    return value


def parse_after(value: object) -> int:
    if value is None or value == "":
        return 0
    try:
        after = int(str(value).strip())
    except ValueError:
        raise ValueError("after must be a whole number") from None
    if after < 0:
        raise ValueError("after must be 0 or more")
    return after


def _alert_id(body: object) -> str:
    alert_id = body.get("id") if isinstance(body, dict) else None
    if not isinstance(alert_id, str) or not alert_id.strip():
        raise ValueError("id must be the id of one of your alerts")
    return alert_id.strip()


# -- crossing rules (pure) ------------------------------------------------------------------------


def crossing(direction: str, previous: float | None, price: float | None, level: float) -> str | None:
    """"up" or "down" when the move from ``previous`` to ``price`` fires an alert of ``direction``.

    above: previous <= level < price.  below: previous >= level > price.  cross: either one.
    Comparing the two observed prices (not the new price alone) means a gap through the level fires.
    """
    if previous is None or price is None:
        return None
    up = previous <= level < price
    down = previous >= level > price
    if direction == "above":
        return "up" if up else None
    if direction == "below":
        return "down" if down else None
    if direction == "cross":
        return "up" if up else "down" if down else None
    return None


def rearm_side(moved: str) -> str:
    """After firing on a move up, a repeating alert re-arms once price is back at or below its level."""
    return "at_or_below" if moved == "up" else "at_or_above"


def on_rearm_side(side: str | None, price: float, level: float) -> bool:
    if side == "at_or_below":
        return price <= level
    if side == "at_or_above":
        return price >= level
    return False


def observed_price(last: float | None, bid: float | None, ask: float | None) -> float | None:
    """The price alerts are checked against: last, else the bid/ask midpoint."""
    if last is not None:
        return last
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    return None


# -- guard rules (pure) ---------------------------------------------------------------------------


def default_guard() -> dict:
    return {"max_daily_loss": None, "max_contracts": None, "paused_at": None, "paused_reason": None, "updated": None}


def guard_breach(guard: dict, totals: dict) -> str | None:
    """The pause reason when a limit the trader set is reached, else None.

    max_daily_loss: today's total P&L (realized + unrealized - fees) at or below -max_daily_loss.
    max_contracts: more open contracts than max_contracts.
    """
    loss = _finite(guard.get("max_daily_loss"))
    total = _finite((totals or {}).get("total"))
    if loss is not None and total is not None and total <= -loss:
        return (f"Order entry is paused by your own guard: today's P&L (approx. {fmt_money(total)}) reached "
                f"your max daily loss of {fmt_money(loss)}. Change or clear the guard under Alerts to resume.")
    cap = guard.get("max_contracts")
    held = _finite((totals or {}).get("open_contracts"))
    if isinstance(cap, int) and held is not None and held > cap:
        return (f"Order entry is paused by your own guard: {int(held)} open contracts is above your max of "
                f"{cap}. Change or clear the guard under Alerts to resume.")
    return None


# -- persistence (pure save/load pair) ---------------------------------------------------------------


def handoff_dir(environ: dict | None = None, platform: str | None = None, home: str | Path | None = None) -> Path:
    """Folder for the dashboard's small settings files: %LOCALAPPDATA%\topstepx on Windows, ~/.topstepx elsewhere."""
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    home_path = Path(home) if home is not None else Path.home()
    if platform.startswith("win"):
        base = environ.get("LOCALAPPDATA")
        return (Path(base) if base else home_path / "AppData" / "Local") / "topstepx"
    return home_path / ".topstepx"


def default_path(environ: dict | None = None, platform: str | None = None, home: str | Path | None = None) -> Path:
    """``TOPSTEPX_ALERTS_FILE`` if set, else alerts.json in %LOCALAPPDATA%\\topstepx or ~/.topstepx."""
    environ = os.environ if environ is None else environ
    override = str(environ.get(FILE_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    return handoff_dir(environ, platform, home) / FILE_NAME


def clean_alert(raw: object) -> dict | None:
    """A stored alert with every field validated, or None when it cannot be used."""
    if not isinstance(raw, dict):
        return None
    try:
        alert_id = raw.get("id")
        contract_id = raw.get("contract_id")
        if not isinstance(alert_id, str) or not alert_id or not isinstance(contract_id, str) or not contract_id:
            return None
        armed = parse_bool(raw.get("armed"), "armed", True)
        rearm = raw.get("rearm_when") if raw.get("rearm_when") in REARM_SIDES else None
        count = raw.get("trigger_count")
        return {
            "id": alert_id[:40],
            "contract_id": contract_id[:80],
            "price": parse_price(raw.get("price")),
            "direction": parse_direction(raw.get("direction")),
            "note": parse_note(raw.get("note"))[:MAX_NOTE],
            "once": parse_bool(raw.get("once"), "once", True),
            "created": str(raw.get("created") or "")[:40] or None,
            "armed": armed,
            "last_triggered": str(raw.get("last_triggered") or "")[:40] or None,
            "trigger_count": count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else 0,
            "rearm_when": None if armed else rearm,
        }
    except ValueError:
        return None


def clean_guard(raw: object) -> dict:
    guard = default_guard()
    if not isinstance(raw, dict):
        return guard
    with contextlib.suppress(ValueError):
        guard["max_daily_loss"] = parse_loss_limit(raw.get("max_daily_loss"))
    with contextlib.suppress(ValueError):
        guard["max_contracts"] = parse_contract_limit(raw.get("max_contracts"))
    paused_at = raw.get("paused_at")
    if isinstance(paused_at, str) and paused_at:
        guard["paused_at"] = paused_at[:40]
        reason = raw.get("paused_reason")
        guard["paused_reason"] = _ascii(reason)[:400] if isinstance(reason, str) and reason else (
            "Order entry is paused by your own guard. Change or clear the guard under Alerts to resume.")
    if isinstance(raw.get("updated"), str):
        guard["updated"] = raw["updated"][:40]
    return guard


def save_settings(path: str | Path, alerts: list[dict], guard: dict) -> None:
    """Write the alerts and the guard (and nothing else) atomically to ``path``."""
    path = Path(path)
    record = {
        "version": FILE_VERSION,
        "alerts": [{key: alert.get(key) for key in ALERT_FIELDS} for alert in alerts],
        "guard": {key: guard.get(key) for key in GUARD_FIELDS},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=1)
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def load_settings(path: str | Path) -> tuple[list[dict], dict]:
    """``(alerts, guard)`` from ``path``; empty defaults when the file does not exist.

    ValueError when the file exists but is not a settings file this version understands. Alerts
    that fail validation are dropped one by one, never the whole file.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], default_guard()
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{path.name} is not valid JSON ({exc})") from None
    if not isinstance(data, dict) or not isinstance(data.get("alerts", []), list):
        raise ValueError(f"{path.name} is not an alerts settings file")
    alerts, seen = [], set()
    for raw in data.get("alerts", [])[:MAX_ALERTS]:
        alert = clean_alert(raw)
        if alert is not None and alert["id"] not in seen:
            seen.add(alert["id"])
            alerts.append(alert)
    return alerts, clean_guard(data.get("guard"))


# -- the store -------------------------------------------------------------------------------------


class AlertStore:
    """Alerts, the guard and the events ring, behind one lock (hub callbacks on the event loop write,
    HTTP threads read and edit). ``path`` None keeps everything in memory (tests)."""

    def __init__(self, path: str | Path | None = None, *, capacity: int = MAX_EVENTS,
                 describe: Callable[[str], dict | None] | None = None):
        self._lock = threading.RLock()
        self.path = Path(path) if path is not None else None
        self.describe = describe or (lambda contract_id: None)
        self.alerts: dict[str, dict] = {}
        self.guard = default_guard()
        self.events: deque[dict] = deque(maxlen=capacity)
        self.seq = 0
        self.version = 0                  # bumps on every alert or guard change (the page reloads its list)
        self.quotes: dict[str, dict] = {}  # contract id -> last seen {"last", "bid", "ask"}
        self.prices: dict[str, float] = {}  # contract id -> last observed price
        self._seen_fills: deque = deque(maxlen=SEEN_FILLS)
        self.dirty = False
        self.load_error: str | None = None
        self.save_error: str | None = None
        self.task: asyncio.Task | None = None

    # -- persistence ----------------------------------------------------------------------------

    def load(self) -> None:
        if self.path is None:
            return
        try:
            alerts, guard = load_settings(self.path)
        except (OSError, ValueError) as exc:
            self.load_error = f"{exc}"
            aside = self.path.with_name(self.path.name + ".bad")
            with contextlib.suppress(OSError):
                os.replace(self.path, aside)   # keep the unreadable file for the trader, start clean
                self.load_error += f" It was renamed to {aside.name} and a new file will be written."
            print(f"[dashboard] alerts: could not read {_ascii(self.path)}: {_ascii(self.load_error)}", file=sys.stderr)
            return
        with self._lock:
            self.alerts = {a["id"]: a for a in alerts}
            self.guard = guard
            self.version += 1

    def save(self) -> bool:
        """Write now (trader edits). Returns False and keeps ``save_error`` when the disk refuses."""
        if self.path is None:
            with self._lock:
                self.dirty = False
            return True
        with self._lock:
            alerts = [dict(a) for a in self.alerts.values()]
            guard = dict(self.guard)
            self.dirty = False
        try:
            save_settings(self.path, alerts, guard)
        except OSError as exc:
            with self._lock:
                self.save_error = f"Could not save {self.path}: {exc}"
                self.dirty = True
            return False
        with self._lock:
            self.save_error = None
        return True

    def flush(self) -> bool:
        """Save if a trigger or a guard pause changed something since the last save."""
        with self._lock:
            if not self.dirty:
                return True
        return self.save()

    # -- events ------------------------------------------------------------------------------------

    def add_event(self, kind: str, **fields: object) -> dict:
        with self._lock:
            self.seq += 1
            event = {"seq": self.seq, "time": now_iso(), "kind": kind, **fields}
            self.events.append(event)
            return dict(event)

    def since(self, after: int) -> tuple[list[dict], int]:
        with self._lock:
            return [dict(e) for e in self.events if e["seq"] > after], self.seq

    # -- alerts ------------------------------------------------------------------------------------

    def _new_id(self) -> str:
        while True:
            alert_id = "a" + secrets.token_hex(4)
            if alert_id not in self.alerts:
                return alert_id

    def add(self, contract_id: str, price: float, direction: str, note: str = "", once: bool = True) -> dict:
        with self._lock:
            if len(self.alerts) >= MAX_ALERTS:
                raise ValueError(f"You already have {MAX_ALERTS} alerts. Delete some before adding more.")
            alert = {
                "id": self._new_id(), "contract_id": contract_id, "price": price, "direction": direction,
                "note": note, "once": once, "created": now_iso(), "armed": True, "last_triggered": None,
                "trigger_count": 0, "rearm_when": None,
            }
            self.alerts[alert["id"]] = alert
            self.version += 1
            return dict(alert)

    def update(self, alert_id: str, changes: dict) -> dict:
        """Apply validated ``changes`` (price, direction, note, once, armed). KeyError if unknown.

        A new price or direction re-arms the alert unless ``armed`` is given too; ``armed`` true or
        false always ends any wait for an automatic re-arm.
        """
        with self._lock:
            alert = self.alerts.get(alert_id)
            if alert is None:
                raise KeyError(alert_id)
            moved = any(k in changes and changes[k] != alert[k] for k in ("price", "direction"))
            for key in ("price", "direction", "note", "once"):
                if key in changes:
                    alert[key] = changes[key]
            if "armed" in changes:
                alert["armed"] = bool(changes["armed"])
                alert["rearm_when"] = None
            elif moved:
                alert["armed"] = True
                alert["rearm_when"] = None
            self.version += 1
            return dict(alert)

    def delete(self, alert_id: str) -> bool:
        with self._lock:
            removed = self.alerts.pop(alert_id, None) is not None
            if removed:
                self.version += 1
            return removed

    def list(self) -> list[dict]:
        with self._lock:
            return [dict(a) for a in self.alerts.values()]

    # -- prices ------------------------------------------------------------------------------------

    def observe_quote(self, contract_id: str, last: float | None = None, bid: float | None = None,
                      ask: float | None = None) -> list[dict]:
        """Merge a partial quote (fields that did not change are None) and check that contract's alerts."""
        with self._lock:
            merged = self.quotes.setdefault(contract_id, {"last": None, "bid": None, "ask": None})
            for key, value in (("last", last), ("bid", bid), ("ask", ask)):
                value = _finite(value)
                if value is not None:
                    merged[key] = value
            price = observed_price(merged["last"], merged["bid"], merged["ask"])
        if price is None:
            return []
        return self.observe_price(contract_id, price)

    def observe_price(self, contract_id: str, price: float) -> list[dict]:
        """Check every alert on ``contract_id`` against the move from the previous observed price."""
        with self._lock:
            previous = self.prices.get(contract_id)
            self.prices[contract_id] = price
            if previous == price:
                return []
            fired, changed = [], False
            for alert in self.alerts.values():
                if alert["contract_id"] != contract_id:
                    continue
                level = alert["price"]
                if not alert["armed"] and alert["rearm_when"] and on_rearm_side(alert["rearm_when"], price, level):
                    alert["armed"], alert["rearm_when"], changed = True, None, True
                if not alert["armed"]:
                    continue
                moved = crossing(alert["direction"], previous, price, level)
                if moved is None:
                    continue
                when = now_iso()
                alert["last_triggered"] = when
                alert["trigger_count"] = int(alert.get("trigger_count") or 0) + 1
                alert["armed"] = False
                alert["rearm_when"] = None if alert["once"] else rearm_side(moved)
                changed = True
                fired.append(self._alert_event(alert, previous, price, moved))
            if changed:
                self.version += 1
                self.dirty = True
            return fired

    def _alert_event(self, alert: dict, previous: float, price: float, moved: str) -> dict:
        info = self.describe(alert["contract_id"]) or {}
        name = info.get("name") or alert["contract_id"]
        tick = info.get("tick_size")
        word = "above" if moved == "up" else "below"
        text = f"{name}: price crossed {word} {fmt_price(alert['price'], tick)} (now {fmt_price(price, tick)})"
        if alert["note"]:
            text += f". {alert['note']}"
        return self.add_event(
            "alert", alert_id=alert["id"], contract_id=alert["contract_id"], name=name, price=alert["price"],
            direction=alert["direction"], moved=moved, previous=previous, observed=price, note=alert["note"],
            once=alert["once"], text=_ascii(text),
        )

    # -- fills -------------------------------------------------------------------------------------

    def record_fill(self, payload: object, account_id: int | None) -> dict | None:
        """A fill event for a user-hub trade on ``account_id``; None for other accounts, voided or repeated trades."""
        t = unwrap(payload)
        if not t or t.get("voided"):
            return None
        trade_account = t.get("accountId")
        if account_id is not None and trade_account is not None and trade_account != account_id:
            return None
        trade_id = t.get("id")
        with self._lock:
            if trade_id is not None:
                if trade_id in self._seen_fills:
                    return None
                self._seen_fills.append(trade_id)
        contract_id = t.get("contractId")
        info = (self.describe(contract_id) if contract_id else None) or {}
        name = info.get("name") or contract_id or "?"
        side = enum_name(OrderSide, t.get("side"))
        size, price = t.get("size"), _finite(t.get("price"))
        text = f"Filled {side} {size if size is not None else '?'} {name} @ {fmt_price(price, info.get('tick_size'))}"
        return self.add_event(
            "fill", side=side, size=size, price=price, contract_id=contract_id, name=name, trade_id=trade_id,
            account_id=trade_account, fill_time=t.get("creationTimestamp"), text=_ascii(text),
        )

    # -- the guard -----------------------------------------------------------------------------------

    def guard_copy(self) -> dict:
        with self._lock:
            return dict(self.guard)

    def guard_active(self) -> bool:
        with self._lock:
            g = self.guard
            return bool(g["paused_at"]) or g["max_daily_loss"] is not None or g["max_contracts"] is not None

    def paused_reason(self) -> str | None:
        with self._lock:
            return self.guard["paused_reason"] if self.guard["paused_at"] else None

    def set_guard(self, max_daily_loss: float | None, max_contracts: int | None) -> dict:
        """The trader's new limits. Changing the guard ends a pause; the next check may pause again."""
        with self._lock:
            self.guard.update(max_daily_loss=max_daily_loss, max_contracts=max_contracts,
                              paused_at=None, paused_reason=None, updated=now_iso())
            self.version += 1
            return dict(self.guard)

    def clear_pause(self) -> dict:
        with self._lock:
            self.guard.update(paused_at=None, paused_reason=None, updated=now_iso())
            self.version += 1
            return dict(self.guard)

    def check_guard(self, totals: dict) -> str | None:
        """The pause reason (new or still standing), or None. A new pause is recorded and announced once."""
        with self._lock:
            if self.guard["paused_at"]:
                return self.guard["paused_reason"]
            reason = guard_breach(self.guard, totals)
            if reason is None:
                return None
            self.guard["paused_at"] = now_iso()
            self.guard["paused_reason"] = reason
            self.version += 1
            self.dirty = True
        self.add_event("guard", text=reason, totals={k: (totals or {}).get(k) for k in ("total", "open_contracts")})
        return reason

    # -- read side ------------------------------------------------------------------------------------

    def summary(self, blocked: str | None) -> dict:
        with self._lock:
            return {
                "alerts_count": len(self.alerts),
                "armed_count": sum(1 for a in self.alerts.values() if a["armed"]),
                "guard": dict(self.guard),
                "blocked": blocked,
                "last_event_seq": self.seq,
                "version": self.version,
                "error": self.save_error or self.load_error,
            }

    def last_price(self, contract_id: str) -> float | None:
        with self._lock:
            return self.prices.get(contract_id)


# -- per-session wiring -----------------------------------------------------------------------------


def _store(session) -> AlertStore:
    store = getattr(session, "_alerts_store", None)
    if store is None:
        def describe(contract_id: str) -> dict | None:
            try:
                return session.state.contract(contract_id)
            except Exception:  # noqa: BLE001 - a name is a nicety
                return None

        store = AlertStore(default_path(), describe=describe)
        store.load()
        setattr(session, "_alerts_store", store)
    return store


def _selected_account_id(session) -> int | None:
    try:
        account = session.state.selected_account()
    except Exception:  # noqa: BLE001
        return None
    return account.get("id") if account else None


def order_entry_blocked(session) -> str | None:
    store = _store(session)
    standing = store.paused_reason()
    if standing:
        return standing
    if not store.guard_active():
        return None
    totals = (session.state.snapshot() or {}).get("totals") or {}
    return store.check_guard(totals)


def snapshot(session) -> dict:
    store = _store(session)
    return store.summary(order_entry_blocked(session))


# -- hub callbacks --------------------------------------------------------------------------------


def _on_quote(session, contract_id, quote) -> None:
    _store(session).observe_quote(contract_id, getattr(quote, "last_price", None),
                                  getattr(quote, "best_bid", None), getattr(quote, "best_ask", None))


def _on_user_trade(session, payload) -> None:
    store = _store(session)
    store.record_fill(payload, _selected_account_id(session))
    order_entry_blocked(session)   # a fill changes realized P&L and open contracts


def _on_position(session, payload) -> None:
    order_entry_blocked(session)


async def _watch(session, store: AlertStore) -> None:
    reported = False
    while True:
        await asyncio.sleep(LOOP_SECONDS)
        try:
            order_entry_blocked(session)
            store.flush()
            reported = False
        except Exception as exc:  # noqa: BLE001 - keep checking; report once
            if not reported:
                print(f"[dashboard] alerts check failed: {type(exc).__name__}: {_ascii(exc)}", file=sys.stderr)
                reported = True


async def start(session) -> None:
    store = _store(session)
    # Quotes that arrived before this hook ran: they become the "previous" price for the first check.
    with contextlib.suppress(Exception):
        for contract_id, quote in ((session.state.snapshot() or {}).get("quotes") or {}).items():
            store.observe_quote(contract_id, quote.get("last"), quote.get("bid"), quote.get("ask"))
    session.market.on_quote(lambda contract_id, quote: _on_quote(session, contract_id, quote))
    session.user.on_user_trade(lambda payload: _on_user_trade(session, payload))
    session.user.on_position(lambda payload: _on_position(session, payload))
    store.task = asyncio.create_task(_watch(session, store))


async def stop(session) -> None:
    store = getattr(session, "_alerts_store", None)
    if store is None:
        return
    if store.task is not None:
        store.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await store.task
        store.task = None
    store.flush()


# -- routes ------------------------------------------------------------------------------------------


def _listing(session, store: AlertStore) -> dict:
    blocked = order_entry_blocked(session)
    snap = session.state.snapshot() or {}
    totals = snap.get("totals") or {}
    alerts = []
    for alert in store.list():
        info = store.describe(alert["contract_id"]) or {}
        alerts.append({**alert, "name": info.get("name") or alert["contract_id"],
                       "watched": bool(info), "last_price": store.last_price(alert["contract_id"])})
    return {
        "alerts": alerts,
        "guard": store.guard_copy(),
        "status": {
            "blocked": blocked,
            "totals": {"total": totals.get("total"), "open_contracts": totals.get("open_contracts")},
            "file": str(store.path) if store.path else None,
            "load_error": store.load_error,
            "save_error": store.save_error,
        },
        "version": store.version,
        "last_event_seq": store.since(10 ** 12)[1],
        "limits": {"max_alerts": MAX_ALERTS, "max_note": MAX_NOTE, "directions": list(DIRECTIONS)},
    }


def _saved(store: AlertStore, payload: dict) -> tuple[int, dict]:
    if not store.save():
        payload = {**payload, "warning": store.save_error}
    return 200, payload


def routes(session) -> dict:
    def get_alerts(query: dict) -> tuple[int, dict]:
        return 200, _listing(session, _store(session))

    def get_events(query: dict) -> tuple[int, dict]:
        events, last_seq = _store(session).since(parse_after(query.get("after")))
        return 200, {"events": events, "last_seq": last_seq}

    def add(body: dict) -> tuple[int, dict]:
        contract_id = body.get("contract_id")
        if not isinstance(contract_id, str) or not contract_id.strip():
            raise ValueError("contract_id is required")
        contract_id = contract_id.strip()
        if not session.state.has_contract(contract_id):
            raise ValueError(f"{contract_id} is not on the watch list. Add it to the watch list first.")
        price = parse_price(body.get("price"))
        direction = parse_direction(body.get("direction"))
        note = parse_note(body.get("note"))
        once = parse_bool(body.get("once"), "once", True)
        store = _store(session)
        alert = store.add(contract_id, price, direction, note, once)
        return _saved(store, {"ok": True, "alert": alert})

    def update(body: dict) -> tuple[int, dict]:
        alert_id = _alert_id(body)
        changes: dict = {}
        if "price" in body:
            changes["price"] = parse_price(body["price"])
        if "direction" in body:
            changes["direction"] = parse_direction(body["direction"])
        if "note" in body:
            changes["note"] = parse_note(body["note"])
        if "once" in body:
            changes["once"] = parse_bool(body["once"], "once", True)
        if "armed" in body:
            changes["armed"] = parse_bool(body["armed"], "armed", True)
        if "contract_id" in body:
            raise ValueError("an alert's contract cannot change; delete it and add a new one")
        if not changes:
            raise ValueError("nothing to change: send price, direction, note, once or armed")
        store = _store(session)
        try:
            alert = store.update(alert_id, changes)
        except KeyError:
            return 404, {"error": f"no alert with id {alert_id}"}
        return _saved(store, {"ok": True, "alert": alert})

    def delete(body: dict) -> tuple[int, dict]:
        alert_id = _alert_id(body)
        store = _store(session)
        if not store.delete(alert_id):
            return 404, {"error": f"no alert with id {alert_id}"}
        return _saved(store, {"ok": True, "deleted": alert_id})

    def set_guard(body: dict) -> tuple[int, dict]:
        store = _store(session)
        current = store.guard_copy()
        if "max_daily_loss" not in body and "max_contracts" not in body:
            raise ValueError("send max_daily_loss and/or max_contracts (a number, or null to turn it off)")
        loss = parse_loss_limit(body["max_daily_loss"]) if "max_daily_loss" in body else current["max_daily_loss"]
        cap = parse_contract_limit(body["max_contracts"]) if "max_contracts" in body else current["max_contracts"]
        store.set_guard(loss, cap)
        blocked = order_entry_blocked(session)   # a limit that is already reached pauses right away
        return _saved(store, {"ok": True, "guard": store.guard_copy(), "blocked": blocked})

    def clear_guard(body: dict) -> tuple[int, dict]:
        store = _store(session)
        store.clear_pause()
        blocked = order_entry_blocked(session)
        payload = {"ok": True, "guard": store.guard_copy(), "blocked": blocked}
        if blocked:
            payload["note"] = ("Your limit is still reached, so order entry paused again. "
                               "Change or remove the limit to resume.")
        return _saved(store, payload)

    def test(body: dict) -> tuple[int, dict]:
        event = _store(session).add_event(
            "test", text="Test notification from the dashboard. If you can see (or hear) this, your alert "
                         "notifications work.")
        return 200, {"ok": True, "event": event}

    return {
        "GET": {"/api/alerts": get_alerts, "/api/alerts/events": get_events},
        "POST": {
            "/api/alerts/add": add,
            "/api/alerts/update": update,
            "/api/alerts/delete": delete,
            "/api/alerts/guard": set_guard,
            "/api/alerts/guard/clear": clear_guard,
            "/api/alerts/test": test,
        },
    }
