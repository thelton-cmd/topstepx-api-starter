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
from datetime import datetime, timezone
from typing import Any, Callable

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
        self._observers: list[Callable[[dict], Any]] = []

    def add_observer(self, callback: Callable[[dict], Any]) -> None:
        """Register ``callback(record)`` to run after every REST call, successful or not.

        ``record`` carries ``time`` (ISO, UTC), ``path``, ``request`` (the JSON body as sent),
        ``status`` (HTTP status, or None if the request never got out), ``response`` (the parsed
        envelope, or the raw text for a non-JSON/non-200 reply, cut to 2000 chars), ``error``
        (message or None) and ``duration_ms``. The Authorization header is never included.
        Observer exceptions are swallowed so an observer can never break a call.
        """
        self._observers.append(callback)

    def post(self, path: str, json: dict | None = None) -> dict:
        payload = json or {}
        meta: dict = {"status": None, "response": None}
        started = time.perf_counter()
        error: str | None = None
        try:
            return self._post(path, payload, meta)
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            raise
        finally:
            if self._observers:
                record = {
                    "time": datetime.now(timezone.utc).isoformat(),
                    "path": path,
                    "request": payload,
                    "status": meta["status"],
                    "response": meta["response"],
                    "error": error,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                }
                for callback in list(self._observers):
                    try:
                        callback(record)
                    except Exception:  # noqa: BLE001 - observers never break a call
                        pass

    def _post(self, path: str, payload: dict, meta: dict) -> dict:
        retried_auth = False
        rate_limit_tries = 0

        while True:
            try:
                response = self._client.post(
                    path,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._tokens.get_token()}"},
                )
            except httpx.TransportError as exc:
                raise TopstepXError(
                    f"Could not reach the TopstepX API while calling {path}: {exc}. "
                    "Check your internet connection and try again."
                ) from exc

            meta["status"] = response.status_code
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
                meta["response"] = response.text[:2000]
                raise TopstepXError(
                    f"HTTP {response.status_code} from {path}: {response.text[:200]}"
                )

            try:
                body = response.json()
            except ValueError as exc:
                meta["response"] = response.text[:2000]
                raise ApiError(
                    path,
                    "the API returned HTTP 200 but the response was not valid JSON; "
                    "retry the request, and check the API status if it continues",
                ) from exc

            meta["response"] = body
            if not isinstance(body, dict):
                raise ApiError(
                    path,
                    f"expected a JSON object envelope, received {type(body).__name__}",
                )
            if body.get("success") is False:
                raise ApiError.from_envelope(path, body)
            if body.get("success") is not True:
                raise ApiError(
                    path,
                    "response envelope is missing the required success=true field",
                    body=body,
                )
            return body

    def close(self) -> None:
        self._client.close()


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    try:
        return float(value) if value else None
    except ValueError:
        return None
