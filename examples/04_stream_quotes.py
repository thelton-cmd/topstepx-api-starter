"""Example 04 — stream live quotes over SignalR. (The showcase.)

Connects to the market hub and prints bid/ask/last as quotes arrive.
Reconnects and resubscribes automatically if the connection drops.
Press Ctrl+C to stop.

    python examples/04_stream_quotes.py                      # defaults to first "ES" match
    python examples/04_stream_quotes.py CON.F.US.EP.U26      # specific contract
"""

import asyncio
import sys

from topstepx import MarketHub, Quote, TopstepXClient, TopstepXError


# ── [print-quote] The function that runs on every quote ─────────────
def print_quote(contract_id: str, quote: Quote) -> None:
    bid = f"{quote.best_bid:.2f}" if quote.best_bid is not None else "-"
    ask = f"{quote.best_ask:.2f}" if quote.best_ask is not None else "-"
    last = f"{quote.last_price:.2f}" if quote.last_price is not None else "-"
    print(f"{contract_id}  bid {bid:>10}   ask {ask:>10}   last {last:>10}")


# ── [find-contract] Log in and pick a contract ──────────────────────
async def main() -> None:
    client = TopstepXClient()

    if len(sys.argv) > 1:
        contract_id = sys.argv[1]
    else:
        contracts = client.search_contracts("ES")
        if not contracts:
            print("No ES contracts found — pass a contract ID explicitly.")
            return
        contract_id = contracts[0].id

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
