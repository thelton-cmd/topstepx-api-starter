import base64
import json
import time

import httpx
import pytest

from topstepx.config import Settings


def make_jwt(expires_in_seconds: float = 24 * 3600) -> str:
    """Build an unsigned JWT with an exp claim — enough for TokenManager."""

    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    header = b64({"alg": "none", "typ": "JWT"})
    payload = b64({"exp": time.time() + expires_in_seconds, "sub": "test"})
    return f"{header}.{payload}.sig"


@pytest.fixture
def settings() -> Settings:
    return Settings(username="tester", api_key="key-123", api_base="https://api.test")


@pytest.fixture
def mock_client_factory(settings):
    """Build an httpx.Client whose responses come from a handler function."""

    def factory(handler):
        return httpx.Client(
            base_url=settings.api_base, transport=httpx.MockTransport(handler)
        )

    return factory
