import httpx
import pytest

from topstepx.auth import TokenManager
from topstepx.config import load_settings
from topstepx.exceptions import AuthenticationError, TopstepXError

from .conftest import make_jwt


def test_login_success_populates_token_and_expiry(settings, mock_client_factory):
    token = make_jwt()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/Auth/loginKey"
        return httpx.Response(200, json={"success": True, "token": token})

    tokens = TokenManager(settings, http_client=mock_client_factory(handler))
    assert tokens.get_token() == token
    assert tokens.expires_at is not None


def test_login_rejected_raises_friendly_error(settings, mock_client_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": False, "errorCode": 3})

    tokens = TokenManager(settings, http_client=mock_client_factory(handler))
    with pytest.raises(AuthenticationError, match=".env"):
        tokens.get_token()


def test_near_expiry_triggers_validate(settings, mock_client_factory):
    old_token = make_jwt(expires_in_seconds=60)  # inside the 30-min refresh margin
    new_token = make_jwt()
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": old_token})
        if request.url.path == "/Auth/validate":
            return httpx.Response(200, json={"success": True, "newToken": new_token})
        raise AssertionError(request.url.path)

    tokens = TokenManager(settings, http_client=mock_client_factory(handler))
    tokens.get_token()  # login -> near-expiry token
    assert tokens.get_token() == new_token
    assert "/Auth/validate" in calls


def test_validate_failure_falls_back_to_login(settings, mock_client_factory):
    near_expiry = make_jwt(expires_in_seconds=60)
    fresh = make_jwt()
    login_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal login_count
        if request.url.path == "/Auth/loginKey":
            login_count += 1
            token = near_expiry if login_count == 1 else fresh
            return httpx.Response(200, json={"success": True, "token": token})
        return httpx.Response(200, json={"success": False})  # validate fails

    tokens = TokenManager(settings, http_client=mock_client_factory(handler))
    tokens.get_token()
    assert tokens.get_token() == fresh
    assert login_count == 2


def test_decode_exp_handles_garbage():
    assert TokenManager._decode_exp("not-a-jwt") is None
    assert TokenManager._decode_exp("") is None


def test_missing_env_vars_raise_helpful_message(monkeypatch, tmp_path):
    monkeypatch.delenv("TOPSTEPX_USERNAME", raising=False)
    monkeypatch.delenv("TOPSTEPX_API_KEY", raising=False)
    with pytest.raises(TopstepXError, match=r"\.env"):
        load_settings(dotenv_path=str(tmp_path / "nonexistent.env"))


def test_placeholder_env_values_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("TOPSTEPX_USERNAME", "your_topstepx_username")
    monkeypatch.setenv("TOPSTEPX_API_KEY", "your_api_key_here")
    with pytest.raises(TopstepXError, match="Missing credentials"):
        load_settings(dotenv_path=str(tmp_path / "nonexistent.env"))
