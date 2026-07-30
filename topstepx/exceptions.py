"""Exception types raised by the TopstepX starter kit.

Every exception carries an actionable message so a first-time user can
fix the problem without digging through source code.
"""

from __future__ import annotations


class TopstepXError(Exception):
    """Base class for all errors raised by this package."""


class AuthenticationError(TopstepXError):
    """Login or token validation failed (bad credentials, revoked key, 401)."""

    DEFAULT_HINT = (
        "Check TOPSTEPX_USERNAME and TOPSTEPX_API_KEY in your .env file. "
        "Generate a fresh API key in TopstepX under Settings -> API Access "
        "(see README -> Prerequisites)."
    )

    def __init__(self, message: str | None = None):
        super().__init__(message or f"Authentication failed. {self.DEFAULT_HINT}")


class RateLimitError(TopstepXError):
    """The API returned HTTP 429 even after automatic retries with backoff."""

    def __init__(self, endpoint: str, retry_after: float | None = None):
        self.endpoint = endpoint
        self.retry_after = retry_after
        hint = f" Server suggested waiting {retry_after:.0f}s." if retry_after else ""
        super().__init__(
            f"Rate limit hit on {endpoint} and automatic retries were exhausted.{hint} "
            "Slow down your request rate (History/retrieveBars: 50 requests per 30s; "
            "all other endpoints: 200 per 60s)."
        )


class ApiError(TopstepXError):
    """The API returned an error envelope (success=false) or an unexpected status."""

    def __init__(self, endpoint: str, message: str, error_code: int | None = None, body: dict | None = None):
        self.endpoint = endpoint
        self.error_code = error_code
        self.body = body
        code = f" (errorCode={error_code})" if error_code is not None else ""
        super().__init__(f"API error on {endpoint}{code}: {message}")

    @classmethod
    def from_envelope(cls, endpoint: str, body: dict) -> "ApiError":
        return cls(
            endpoint=endpoint,
            message=body.get("errorMessage") or "request was not successful",
            error_code=body.get("errorCode"),
            body=body,
        )
