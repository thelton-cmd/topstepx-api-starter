"""Example 06 — stream your own account/order/position/trade events.

Connects to the user hub and prints events as they happen. To see it in
action: leave this running, then place or cancel an order on your Practice
Account in the TopstepX platform — the events appear here in real time.
Press Ctrl+C to stop.

    python examples/06_user_events.py
    python examples/06_user_events.py --account-id 123456   # skip the account prompt
"""

import argparse
import asyncio
import sys

from topstepx import TopstepXClient, TopstepXError, UserHub
from _safety import choose_practice_account


# ── [find-account] Find your account ────────────────────────────────
async def main() -> None:
    parser = argparse.ArgumentParser(description="Stream your own Practice Account events")
    parser.add_argument("--account-id", type=int, help="Practice Account to watch (default: choose)")
    args = parser.parse_args()

    client = TopstepXClient()

    try:
        account = choose_practice_account(client, args.account_id)
    except ValueError:
        client.close()
        raise
    print(f"\nSubscribing to Practice Account {account.id} ({account.name})")

    # ── [subscribe] Subscribe to your own events ────────────────────
    hub = UserHub(client.tokens)
    hub.on_account(lambda data: print(f"ACCOUNT  {data}"))
    hub.on_order(lambda data: print(f"ORDER    {data}"))
    hub.on_position(lambda data: print(f"POSITION {data}"))
    hub.on_user_trade(lambda data: print(f"TRADE    {data}"))

    await hub.start()
    await hub.subscribe_accounts()
    await hub.subscribe_orders(account.id)
    await hub.subscribe_positions(account.id)
    await hub.subscribe_trades(account.id)

    # ── [run-forever] Listen until you say stop ─────────────────────
    print("Listening — place/cancel an order in the TopstepX platform to see events. Ctrl+C to stop.\n")
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
