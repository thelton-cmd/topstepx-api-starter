"""Command line entry point: arguments, startup lines and the error safety net.

    python -m dashboard                                   # watch ES NQ GC CL + micros, pick an account
    python -m dashboard --account-id 123 --watch ES NQ CL
    python -m dashboard --unit minute --unit-number 15 --bars 2000 --port 8080
    python -m dashboard --no-open --seconds 25            # unattended check

The page listens on 127.0.0.1 only, so nothing outside this computer can reach it.
Order entry works only on accounts the API marks simulated (Practice Accounts) and
only after a preflight summary and a typed "yes" in the browser. Nothing here is
trading advice; P&L figures are an approximation built from API data.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from topstepx import TopstepXError

from . import VERSION, app
from .server import UNIT_SECONDS


# Watched by default: the four most traded CME futures and their micro contracts. Symbols resolve
# to the current front-month contract at startup; pass --watch to choose your own list.
DEFAULT_WATCH = ("ES", "NQ", "GC", "CL", "MES", "MNQ", "MGC", "MCL")

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m dashboard",
        description="Local dashboard: quotes, chart with indicators, DOM, tape, positions, orders, fills, "
                    "and an order ticket for Practice Accounts.",
    )
    parser.add_argument("--account-id", type=int, help="account to show first (default: first Practice Account)")
    parser.add_argument("--watch", nargs="+", default=list(DEFAULT_WATCH), metavar="CONTRACT",
                        help="symbols or exact contract IDs to quote and chart (default: ES)")
    parser.add_argument("--unit", choices=list(UNIT_SECONDS), default="minute", help="chart bar unit (default: minute)")
    parser.add_argument("--unit-number", type=int, default=5, help="units per chart bar (default: 5)")
    parser.add_argument("--features", default="all", metavar="LIST",
                        help="feature modules to load from dashboard/features: all (default), none, or a,b,c")
    parser.add_argument("--bars", type=int, default=1500,
                        help="chart bars per contract, 1-5000 (default: 1500; the page can change bar size and history)")
    parser.add_argument("--refresh", type=float, default=60.0,
                        help="seconds between chart refreshes for non-focused contracts (default: 60)")
    parser.add_argument("--refresh-focus", type=float, default=5.0,
                        help="seconds between chart refreshes for the focused contract (default: 5)")
    parser.add_argument("--since", help="count today's fills from this UTC date, YYYY-MM-DD (default: today)")
    parser.add_argument("--port", type=int, default=8765, help="local port to serve on (default: 8765, 0 = any free port)")
    parser.add_argument("--no-open", action="store_true", help="do not open a browser automatically")
    parser.add_argument("--seconds", type=float, help="stop after this many seconds (default: run until Ctrl+C)")
    parser.add_argument("--record", metavar="PATH",
                        help="record every hub message and the lookups the page needs to PATH "
                             "(newline-delimited JSON; a .gz name compresses)")
    parser.add_argument("--replay", metavar="PATH", help="replay a recording made with --record (offline, order entry disabled)")
    parser.add_argument("--speed", type=float, default=None, help="replay speed multiplier, above 0 (default: 1)")
    parser.add_argument("--replay-loop", action="store_true", help="start the replay over when it reaches the end")
    parser.add_argument("--version", action="version", version=f"dashboard {VERSION}")
    args = parser.parse_args(argv)
    if args.unit_number <= 0:
        parser.error("--unit-number must be greater than zero")
    if not 1 <= args.bars <= 5000:
        parser.error("--bars must be between 1 and 5000")
    if args.refresh < 10:
        parser.error("--refresh must be at least 10 seconds (History/retrieveBars allows 50 requests per 30s)")
    if args.refresh_focus < 2:
        parser.error("--refresh-focus must be at least 2 seconds (History/retrieveBars allows 50 requests per 30s)")
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    if args.seconds is not None and args.seconds <= 0:
        parser.error("--seconds must be greater than zero")
    if args.record and args.replay:
        parser.error("--record and --replay cannot be used together (replay is read from a file)")
    if args.record and Path(args.record).exists():
        parser.error(f"--record: {args.record} already exists; choose a new file name")
    if args.replay and not Path(args.replay).is_file():
        parser.error(f"--replay: file not found: {args.replay}")
    if (args.speed is not None or args.replay_loop) and not args.replay:
        parser.error("--speed and --replay-loop need --replay PATH")
    if args.speed is None:
        args.speed = 1.0
    if not 0 < args.speed <= 1000 or args.speed != args.speed:
        parser.error("--speed must be greater than 0 and at most 1000")
    if args.since:
        try:
            datetime.strptime(args.since, "%Y-%m-%d")
        except ValueError:
            parser.error("--since must be a UTC date like 2026-10-26")
    return args


def print_startup(session: "app.Session", url: str) -> None:
    """The lines a user sees when the dashboard is up. ASCII only (Windows cp1252 consoles)."""
    snapshot = session.state.snapshot()
    account = snapshot.get("account") or {}
    args = session.args
    names = {c["id"]: c.get("name") or c["id"] for c in snapshot.get("contracts", [])}
    watched = ", ".join(f"{names.get(cid, cid)} ({cid})" for cid in getattr(session, "watch_ids", [])) or "-"
    bar_label = f"{args.unit_number} {args.unit}{'s' if args.unit_number != 1 else ''}"
    has_practice = any(a.get("simulated") is True and a.get("can_trade") is True for a in snapshot.get("accounts", []))

    print(f"Dashboard for account {account.get('id', '-')} ({account.get('name', '-')}) -> {url}")
    print(f"Watching: {watched}")
    print(f"Chart: {bar_label} x {args.bars} bars; focus refreshed every {args.refresh_focus:.0f}s, "
          f"others every {args.refresh:.0f}s")
    if getattr(session, "replay", None) is not None:
        loop = ", looping" if args.replay_loop else ""
        print(f"Mode: REPLAY of {args.replay} at {args.speed:g}x{loop}. Offline; order entry disabled.")
    elif getattr(args, "record", None):
        print(f"Mode: live, recording to {args.record}")
    if getattr(session, "replay", None) is not None:
        print("Order entry: disabled (replay mode)")
    elif has_practice:
        print("Order entry: enabled for Practice Accounts only")
    else:
        print("Order entry: disabled (no Practice Account)")
    print("P&L figures are an approximation built from API data. Ctrl+C to stop.\n")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        asyncio.run(app.run(args, announce=print_startup))
    except KeyboardInterrupt:
        print("\nStopped.")
    except OSError as exc:
        print(f"Could not start the local server: {exc}. Try another port with --port.")
        sys.exit(1)
    except TopstepXError as exc:
        print(f"Error: {exc}")
        sys.exit(1)
    except ValueError as exc:
        print(f"Cannot continue: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
