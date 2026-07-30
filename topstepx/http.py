"""Thin REST transport with auth, retry, and error handling.

Every gateway endpoint is a POST returning a ``{success, errorCode,
errorMessage, ...}`` envelope, so a single ``post()`` method covers the
whole API:

- attaches a fresh Bearer token from TokenManager
- on 401: forces one re-login and retries once
- on 429: exponential backoff (1s, 2s, 4s), honoring Retry-After
  (limits: History/retrieveBars 50 req/30s, all other endpoints 200 req/60s)
- on success=false: raises ApiError with the gateway's error code
"""

from __future__ import annotations

import time

import httpx

from .auth import TokenManager
from .config import Settings
from .exceptions import ApiError, AuthenticationError, RateLimitError, TopstepXError

MAX_RATE_LIMIT_RETRIES = 3


class ApiHttp:
    def __init__(
        self,
        settings: Settings,
        tokens: TokenManager,
        transport: httpx.BaseTransport | None = None,  # injection point for tests
    ):
        self._tokens = tokens
        self._client = httpx.Client(base_url=settings.api_base, timeout=30.0, transport=transport)

    def post(self, path: str, json: dict | None = None) -> dict:
        retried_auth = False
        rate_limit_tries = 0

        while True:
            response = self._client.post(
                path,
                json=json or {},
                headers={"Authorization": f"Bearer {self._tokens.get_token()}"},
            )

            if response.status_code == 401:
                if retried_auth:
                    raise AuthenticationError(
                        f"Still unauthorized on {path} after re-login. {AuthenticationError.DEFAULT_HINT}"
                    )
                retried_auth = True
                self._tokens.invalidate()
                continue

            if response.status_code == 429:
                if rate_limit_tries >= MAX_RATE_LIMIT_RETRIES:
                    raise RateLimitError(path, _retry_after(response))
                delay = _retry_after(response) or float(2**rate_limit_tries)
                rate_limit_tries += 1
                time.sleep(delay)
                continue

            if response.status_code != 200:
                raise TopstepXError(
                    f"HTTP {response.status_code} from {path}: {response.text[:200]}"
                )

            body = response.json()
            if isinstance(body, dict) and body.get("success") is False:
                raise ApiError.from_envelope(path, body)
            return body

    def close(self) -> None:
        self._client.close()


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    try:
        return float(value) if value else None
    except ValueError:
        return None
