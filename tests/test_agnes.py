import httpx
import pytest

from app.adapters.agnes import (
    AgnesAuthenticationError,
    AgnesClient,
    AgnesConfigurationError,
    AgnesRateLimitError,
    AgnesResponseError,
    AgnesSettings,
)


def test_settings_require_environment_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGNES_API_KEY", raising=False)

    with pytest.raises(AgnesConfigurationError):
        AgnesSettings.from_environment()


def test_client_parses_valid_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    response = httpx.Response(
        200,
        json={
            "model": "agnes-2.5-flash",
            "choices": [{"message": {"content": "OK"}}],
            "usage": {"total_tokens": 3},
        },
    )
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: response)
    client = AgnesClient(AgnesSettings("https://example.test/v1", "secret"))

    reply = client.complete([{"role": "user", "content": "test"}])

    assert reply.content == "OK"
    assert reply.model == "agnes-2.5-flash"


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (401, AgnesAuthenticationError),
        (403, AgnesAuthenticationError),
        (429, AgnesRateLimitError),
        (500, AgnesResponseError),
    ],
)
def test_client_classifies_http_errors(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    error_type: type[Exception],
) -> None:
    response = httpx.Response(status_code, json={"error": "hidden"})
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: response)
    client = AgnesClient(AgnesSettings("https://example.test/v1", "secret"))

    with pytest.raises(error_type):
        client.complete([{"role": "user", "content": "test"}])
