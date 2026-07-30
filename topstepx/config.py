"""Configuration loading.

Credentials live in a `.env` file (never committed) or plain environment
variables. Environment variables win over `.env` values, which is the
python-dotenv default behavior.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from .exceptions import TopstepXError

API_BASE = "https://api.topstepx.com/api"
RTC_BASE = "wss://rtc.topstepx.com/hubs"


@dataclass(frozen=True)
class Settings:
    username: str
    api_key: str
    api_base: str = API_BASE
    rtc_base: str = RTC_BASE


def load_settings(dotenv_path: str | None = None) -> Settings:
    """Load credentials from .env / environment and return a Settings object."""
    load_dotenv(dotenv_path)

    username = os.environ.get("TOPSTEPX_USERNAME", "").strip()
    api_key = os.environ.get("TOPSTEPX_API_KEY", "").strip()

    missing = [
        name
        for name, value in [("TOPSTEPX_USERNAME", username), ("TOPSTEPX_API_KEY", api_key)]
        if not value or value.startswith("your_")
    ]
    if missing:
        raise TopstepXError(
            f"Missing credentials: {', '.join(missing)}. "
            "Copy .env.example to .env and fill in your TopstepX username and API key. "
            "Generate a key in TopstepX under Settings -> API Access."
        )

    return Settings(username=username, api_key=api_key)
