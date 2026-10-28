"""Example 04 — stream live quotes over SignalR. (The showcase.)

Connects to the market hub and prints bid/ask/last as quotes arrive.
Reconnects and resubscribes automatically if the connection drops.
Press Ctrl+C to stop.

    python examples/04_stream_quotes.py                      # choose an active "ES" match
    python examples/04_stream_quotes.py CON.F.US.EP.Z26      # specific contract
"""

import asyncio
import sys

from topstepx import MarketHub, Quote, TopstepXClient, TopstepXError
from _safety import choose_active_contract


# ── [print-quote] The function that runs on every quote ─────────────
latest: dict[str, float | None] = {"bid": None, "ask": None, "last": None}


def fmt(price: float | None) -> str:
    return f"{price:.2f}" if price is not None else "-"


def print_quote(contract_id: str, quote: Quote) -> None:
    # Quotes arrive as partial updates: each one carries only the fields
    # that changed, so keep the last value seen for each field.
    if quote.best_bid is not None:
        latest["bid"] = quote.best_bid
    if quote.best_ask is not None:
        latest["ask"] = quote.best_ask
    if quote.last_price is not None:
        latest["last"] = quote.last_price
    print(f"{contract_id}  bid {fmt(latest['bid']):>10}   ask {fmt(latest['ask']):>10}   last {fmt(latest['last']):>10}")


# ── [find-contract] Log in and pick a contract ──────────────────────
async def main() -> None:
    client = TopstepXClient()

    contract = choose_active_contract(client, sys.argv[1] if len(sys.argv) > 1 else "ES")
    contract_id = contract.id

    # ── [connect] Connect and subscribe ─────────────────────────────
    hub = MarketHub(client.tokens)
    hub.on_quote(print_quote)
    hub.on_connection_change(
        lambda up: print("[connected]" if up else "[disconnected — auto-reconnecting]")
    )

    print(f"Streaming quotes for {contract_id} — Ctrl+C to stop\n")
    await hub.start()
    await hub.subscribe_quotes(contract_id)

    # ── [run-forever] Run until you say stop ────────────────────────
    try:
        await asyncio.Event().wait()  # run until Ctrl+C
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
