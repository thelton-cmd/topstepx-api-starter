"""Example 01 — authenticate and inspect your session token.

The kit handles login automatically everywhere else; this example just
shows what's happening under the hood: your username + API key are
exchanged for a JWT session token (valid ~24h) which every other call
sends as a Bearer header.

    python examples/01_authenticate.py
"""

import sys

from topstepx import TokenManager, TopstepXError, load_settings


# ── [login] Trade your key for a session token ──────────────────────
def main() -> None:
    settings = load_settings()
    tokens = TokenManager(settings)

    token = tokens.get_token()  # logs in on first call

    # ── [inspect] Look at what came back ────────────────────────────
    print(f"Logged in as : {settings.username}")
    print(f"Token        : {token[:20]}... ({len(token)} chars, masked)")
    print(f"Expires      : {tokens.expires_at}")
    print()
    print("TokenManager refreshes this automatically ~30 min before expiry,")
    print("so you never need to handle tokens yourself.")


# ── [errors] The safety net ─────────────────────────────────────────
if __name__ == "__main__":
    try:
        main()
    except TopstepXError as exc:
        print(f"Error: {exc}")
        sys.exit(1)
