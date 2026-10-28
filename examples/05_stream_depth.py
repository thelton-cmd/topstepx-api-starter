"""Example 05 — stream market depth (DOM) and trade ticks together.

One SignalR connection carries multiple subscription types. Here we
subscribe to both market depth updates and executed-trade ticks for
the same contract. Press Ctrl+C to stop.

    python examples/05_stream_depth.py                      # choose an active "ES" match
    python examples/05_stream_depth.py CON.F.US.EP.Z26      # specific contract
"""

import asyncio
import sys

from topstepx import DepthEntry, MarketHub, MarketTrade, TopstepXClient, TopstepXError
from _safety import choose_active_contract


# ── [callbacks] Two callbacks, two event types ──────────────────────
def print_trades(contract_id: str, trades: list[MarketTrade]) -> None:
    for t in trades:
        price = f"{t.price:.2f}" if t.price is not None else "-"
        print(f"TRADE  {contract_id}  {price:>10} x {t.volume or 0:.0f}")


def print_depth(contract_id: str, entries: list[DepthEntry]) -> None:
    # Depth updates arrive fast; print a compact one-line summary per batch.
    levels = ", ".join(
        f"{e.price:.2f}x{e.volume or 0:.0f}" for e in entries[:5] if e.price is not None
    )
    print(f"DEPTH  {contract_id}  {len(entries)} level update(s): {levels}")


# ── [find-contract] Log in and pick a contract ──────────────────────
async def main() -> None:
    client = TopstepXClient()

    contract = choose_active_contract(client, sys.argv[1] if len(sys.argv) > 1 else "ES")
    contract_id = contract.id

    # ── [subscribe-both] One connection, two subscriptions ──────────
    hub = MarketHub(client.tokens)
    hub.on_trade(print_trades)
    hub.on_depth(print_depth)

    print(f"Streaming depth + trades for {contract_id} — Ctrl+C to stop\n")
    await hub.start()
    await hub.subscribe_trades(contract_id)
    await hub.subscribe_depth(contract_id)

    # ── [run-forever] Run until you say stop ────────────────────────
    try:
        await asyncio.Event().wait()
    finally:
        await hub.stop()
        client.close()


# ── [errors] Exit cleanly ───────────────────────────────────────────
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nDisconnected cleanly.")
    except TopstepXError as exc:
        print(f"Error: {exc}")
        sys.exit(1)
    except ValueError as exc:
        print(f"Cannot continue: {exc}")
        sys.exit(1)
