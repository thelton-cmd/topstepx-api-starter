import httpx
import pytest

import topstepx.http as http_module
from topstepx.auth import TokenManager
from topstepx.exceptions import ApiError, AuthenticationError, RateLimitError
from topstepx.http import ApiHttp

from .conftest import make_jwt


@pytest.fixture
def tokens(settings, mock_client_factory):
    def login_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "token": make_jwt()})

    return TokenManager(settings, http_client=mock_client_factory(login_handler))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(http_module.time, "sleep", sleeps.append)
    return sleeps


def make_api(settings, tokens, handler) -> ApiHttp:
    return ApiHttp(settings, tokens, transport=httpx.MockTransport(handler))


def test_success_envelope_returned(settings, tokens):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"].startswith("Bearer ")
        return httpx.Response(200, json={"success": True, "contracts": []})

    body = make_api(settings, tokens, handler).post("/Contract/search", {"searchText": "ES"})
    assert body["success"] is True


def test_401_relogins_once_then_succeeds(settings, tokens):
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(401)
        return httpx.Response(200, json={"success": True})

    body = make_api(settings, tokens, handler).post("/Account/search")
    assert body["success"] is True
    assert len(attempts) == 2


def test_persistent_401_raises_auth_error(settings, tokens):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    with pytest.raises(AuthenticationError):
        make_api(settings, tokens, handler).post("/Account/search")


def test_429_backs_off_then_succeeds(settings, tokens, no_sleep):
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) <= 2:
            return httpx.Response(429)
        return httpx.Response(200, json={"success": True})

    body = make_api(settings, tokens, handler).post("/History/retrieveBars")
    assert body["success"] is True
    assert no_sleep == [1.0, 2.0]  # exponential backoff


def test_429_honors_retry_after(settings, tokens, no_sleep):
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"success": True})

    make_api(settings, tokens, handler).post("/History/retrieveBars")
    assert no_sleep == [7.0]


def test_429_exhausted_raises_rate_limit_error(settings, tokens):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    with pytest.raises(RateLimitError, match="retrieveBars"):
        make_api(settings, tokens, handler).post("/History/retrieveBars")


def test_error_envelope_raises_api_error(settings, tokens):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"success": False, "errorCode": 8, "errorMessage": "bad contract"}
        )

    with pytest.raises(ApiError, match="errorCode=8"):
        make_api(settings, tokens, handler).post("/Order/place")
