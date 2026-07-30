"""Session token management.

The gateway issues a JWT valid for roughly 24 hours. ``TokenManager`` logs
in on first use, then transparently refreshes the token whenever it gets
within 30 minutes of expiry. ``get_token()`` is the single source of truth
for both the REST client and the SignalR hubs.
"""

from __future__ import annotations

import base64
import json
import threading
from datetime import datetime, timedelta, timezone

import httpx

from .config import Settings
from .exceptions import AuthenticationError, TopstepXError


class TokenManager:
    REFRESH_MARGIN = timedelta(minutes=30)

    def __init__(self, settings: Settings, http_client: httpx.Client | None = None):
        self._settings = settings
        # Own client with no auth header — avoids recursion with ApiHttp.
        self._http = http_client or httpx.Client(base_url=settings.api_base, timeout=30.0)
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: datetime | None = None

    def get_token(self) -> str:
        """Return a valid token, logging in or refreshing as needed.

        Safe to call from any thread, and from pysignalr's
        access_token_factory on every (re)connect.
        """
        with self._lock:
            now = datetime.now(timezone.utc)
            if self._token is None:
                return self._login_locked()
            if self._expires_at is not None and now >= self._expires_at - self.REFRESH_MARGIN:
                return self._validate_locked()
            return self._token

    def login(self) -> str:
        with self._lock:
            return self._login_locked()

    def validate(self) -> str:
        with self._lock:
            return self._validate_locked()

    def invalidate(self) -> None:
        """Drop the cached token so the next get_token() performs a fresh login."""
        with self._lock:
            self._token = None
            self._expires_at = None

    # -- internals (caller must hold self._lock) ----------------------------

    def _login_locked(self) -> str:
        response = self._http.post(
            "/Auth/loginKey",
            json={"userName": self._settings.username, "apiKey": self._settings.api_key},
        )
        if response.status_code == 401:
            raise AuthenticationError()
        if response.status_code != 200:
            raise TopstepXError(
                f"Login request failed with HTTP {response.status_code}: {response.text[:200]}"
            )
        body = response.json()
        if not body.get("success") or not body.get("token"):
            raise AuthenticationError(
                f"Login rejected (errorCode={body.get('errorCode')}). {AuthenticationError.DEFAULT_HINT}"
            )
        self._store(body["token"])
        return self._token  # type: ignore[return-value]

    def _validate_locked(self) -> str:
        try:
            response = self._http.post(
                "/Auth/validate",
                headers={"Authorization": f"Bearer {self._token}"},
            )
            body = response.json() if response.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            body = {}
        if body.get("success"):
            # The gateway may return a refreshed token; fall back to the current one.
            self._store(body.get("newToken") or body.get("token") or self._token)
            return self._token  # type: ignore[return-value]
        # Validation failed — fall back to a fresh login.
        return self._login_locked()

    def _store(self, token: str) -> None:
        self._token = token
        self._expires_at = self._decode_exp(token)

    @staticmethod
    def _decode_exp(token: str) -> datetime | None:
        """Read the JWT 'exp' claim without verifying the signature (stdlib only)."""
        try:
            payload_b64 = token.split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)  # restore stripped padding
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            return datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
        except (IndexError, KeyError, ValueError):
            return None

    @property
    def expires_at(self) -> datetime | None:
        return self._expires_at
