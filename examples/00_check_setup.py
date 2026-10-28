"""Setup checker — run this first.

Verifies, step by step, that everything is in place to use the kit:
.env file, credentials, network, a real login, and your accounts. Each
check prints [OK] or [FAIL] with a fix hint. It never prints your API key.

    python examples/00_check_setup.py
"""

import platform
import sys
from pathlib import Path

OK, FAIL = "[OK]  ", "[FAIL]"


def main() -> int:
    failures = 0

    # ── [python] Is this Python new enough? ──────────────────────────
    version = sys.version_info
    if version >= (3, 10):
        print(f"{OK} Python {platform.python_version()} is supported")
    else:
        print(f"{FAIL} Python {platform.python_version()} is too old; version 3.10+ is required")
        if sys.platform == "win32":
            print("       Fix: install current Python from python.org and reopen PowerShell")
        else:
            print("       Fix: install Python 3.10+ and rerun with python3")
        return 1

    # ── [env-file] Is the .env file there? ──────────────────────────
    repo_root = Path(__file__).resolve().parent.parent
    env_file = repo_root / ".env"
    if env_file.exists():
        print(f"{OK} .env file found at {env_file}")
    else:
        print(f"{FAIL} No .env file at {env_file}")
        if sys.platform == "win32":
            print("       Fix: Copy-Item .env.example .env  (then edit .env)")
        else:
            print("       Fix: cp .env.example .env  (then edit .env)")
        return 1

    # ── [credentials] Do the credentials load? ──────────────────────
    try:
        from topstepx import load_settings

        settings = load_settings(str(env_file))
        print(f"{OK} Credentials loaded for user '{settings.username}'")
    except Exception as exc:
        print(f"{FAIL} {exc}")
        return 1

    # ── [network] Can we reach the gateway? ─────────────────────────
    import httpx

    try:
        httpx.get(f"{settings.api_base}/Status/ping", timeout=10.0).raise_for_status()
        print(f"{OK} api.topstepx.com is reachable")
    except httpx.HTTPError as exc:
        print(f"{FAIL} Cannot reach api.topstepx.com: {exc}")
        print("       Fix: check your internet connection and firewall")
        print("       Note: Topstep prohibits API trading through VPNs, VPSs, or remote servers")
        failures += 1

    # ── [login] Does a real login succeed? ──────────────────────────
    try:
        from topstepx import TokenManager

        tokens = TokenManager(settings)
        tokens.login()
        print(f"{OK} Login succeeded — token valid until {tokens.expires_at}")
    except Exception as exc:
        print(f"{FAIL} Login failed: {exc}")
        return 1

    # ── [accounts] Which accounts can the API see? ──────────────────
    try:
        from topstepx import TopstepXClient

        with TopstepXClient(settings) as client:
            accounts = client.search_accounts(only_active=True)
        practice = [a for a in accounts if a.simulated is True and a.can_trade is True]
        print(f"{OK} {len(accounts)} active account(s), {len(practice)} tradable Practice Account(s)")
        for a in accounts:
            kind = "practice" if a.simulated else "funded/live"
            print(f"       [{a.id}] {a.name}  ({kind}, can trade: {a.can_trade})")
        if not practice:
            print("       Note: examples 06 and 07 need a Practice Account. Activate one in the")
            print("       Topstep Dashboard under Accounts > Add-ons. Examples 01-05 and 08-10 work without one.")
    except Exception as exc:
        print(f"{FAIL} Could not list accounts: {exc}")
        failures += 1

    # ── [verdict] The verdict ───────────────────────────────────────
    if failures == 0:
        print("\nAll checks passed. Try:  python examples/04_stream_quotes.py")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
