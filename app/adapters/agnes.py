import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import httpx


class AgnesError(RuntimeError):
    """Base error for a failed Agnes request."""


class AgnesConfigurationError(AgnesError):
    """Required local configuration is absent or unsafe."""


class AgnesAuthenticationError(AgnesError):
    """The configured account cannot authenticate or use the model."""


class AgnesRateLimitError(AgnesError):
    """The account is currently rate limited."""

    def __init__(self, message, retry_at=None):
        super().__init__(message)
        self.retry_at = retry_at or datetime.now(UTC) + timedelta(seconds=60)


def retry_after_deadline(value, now=None):
    now = now or datetime.now(UTC)
    try:
        value = (value or "").strip()
        if value.isascii() and value.isdigit():
            return now + timedelta(seconds=int(value))
        deadline = parsedate_to_datetime(value)
        if deadline.tzinfo is not None:
            return max(now, deadline.astimezone(UTC))
    except (ValueError, TypeError, OverflowError):
        pass
    return now + timedelta(seconds=60)


class AgnesResponseError(AgnesError):
    """Agnes returned a malformed or unsuccessful response."""


@dataclass(frozen=True)
class AgnesSettings:
    base_url: str
    api_key: str = field(repr=False)
    model: str = "agnes-2.5-flash"
    connect_timeout: float = 10
    read_timeout: float = 90

    @classmethod
    def from_environment(cls) -> "AgnesSettings":
        base_url = os.getenv("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1")
        api_key = os.getenv("AGNES_API_KEY")
        model = os.getenv("AGNES_MODEL", "agnes-2.5-flash")
        if not api_key:
            raise AgnesConfigurationError("AGNES_API_KEY is not configured")
        return cls(base_url=base_url.rstrip("/"), api_key=api_key, model=model)


@dataclass(frozen=True)
class AgnesReply:
    content: str
    model: str
    usage: dict[str, Any] | None


class AgnesClient:
    """Minimal Chat Completions client with explicit failure states."""

    def __init__(self, settings: AgnesSettings) -> None:
        self.settings = settings

    def complete(self, messages: list[dict[str, str]], max_tokens: int = 256) -> AgnesReply:
        self._validate_messages(messages)
        payload = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        timeout = httpx.Timeout(
            connect=self.settings.connect_timeout,
            read=self.settings.read_timeout,
            write=10,
            pool=10,
        )
        response = self._send(payload, timeout)
        return self._parse_reply(response)

    def _send(self, payload: dict[str, Any], timeout: httpx.Timeout) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = httpx.post(
                f"{self.settings.base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=timeout,
            )
        except httpx.HTTPError as error:
            if isinstance(error, httpx.ReadTimeout):
                raise AgnesResponseError('模型响应超时，已保存的内容仍保留，请稍后重试') from error
            if isinstance(error, httpx.RemoteProtocolError):
                raise AgnesResponseError('模型连接中断，已保存的内容仍保留，请稍后重试') from error
            raise AgnesResponseError(f"模型连接失败：{type(error).__name__}") from error
        if response.status_code in {401, 403, 404}:
            raise AgnesAuthenticationError(f"模型服务拒绝请求，请检查密钥和模型 ID：HTTP {response.status_code}")
        if response.status_code == 429:
            raise AgnesRateLimitError("模型调用达到限额", retry_after_deadline(response.headers.get("Retry-After")))
        if response.status_code >= 400:
            raise AgnesResponseError(f"模型服务返回 HTTP {response.status_code}")
        return response

    def _parse_reply(self, response: httpx.Response) -> AgnesReply:
        try:
            payload = response.json()
            choices = payload["choices"]
            content = choices[0]["message"]["content"]
            model = payload["model"]
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise AgnesResponseError("模型返回格式不正确") from error
        if choices[0].get("finish_reason") == "length":
            raise AgnesResponseError("模型输出被截断，未采用结果；请提高输出上限后按新配置重新生成")
        if not isinstance(content, str) or not content.strip():
            raise AgnesResponseError("模型未返回正文；可能输出上限不足，请检查配置或换一个模型")
        usage = payload.get("usage")
        return AgnesReply(content=content, model=str(model), usage=usage)

    def _validate_messages(self, messages: list[dict[str, str]]) -> None:
        if not messages:
            raise ValueError("at least one message is required")
        for message in messages:
            if message.get("role") not in {"system", "user", "assistant"}:
                raise ValueError("unsupported message role")
            if not isinstance(message.get("content"), str):
                raise TypeError("message content must be text")
