"""Runtime defaults and environment-backed deployment settings."""

import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOTENV_PATH = PROJECT_ROOT / ".env"

DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_PROVIDER_BASE_URLS = {
    "agnes": "https://apihub.agnes-ai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}
DEFAULT_PROVIDER_MODELS = {
    "agnes": "agnes-2.5-flash",
    "openrouter": "nex-agi/nex-n2.5-mini:free",
}


def project_path_from_env(name: str, default: Path) -> Path:
    """Resolve an optional path setting consistently from the project root."""
    raw = os.environ.get(name)
    path = Path(raw).expanduser() if raw else default
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def learning_data_directory() -> Path:
    return project_path_from_env("LEARNING_DATA_DIR", PROJECT_ROOT / "data")


def initial_sources_path() -> Path:
    return project_path_from_env(
        "LEARNING_SOURCE_CONFIG",
        PROJECT_ROOT / "local-config.sources.json",
    )


def local_timezone() -> ZoneInfo:
    name = os.environ.get("LEARNING_TIMEZONE", DEFAULT_TIMEZONE).strip()
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as error:
        raise RuntimeError(f"LEARNING_TIMEZONE 不是有效的 IANA 时区：{name}") from error


def local_timezone_name() -> str:
    return local_timezone().key


def provider_base_url(provider: str) -> str:
    default = DEFAULT_PROVIDER_BASE_URLS[provider]
    configured = os.environ.get(f"{provider.upper()}_BASE_URL", default).strip()
    return (configured or default).rstrip("/")


def provider_default_model(provider: str) -> str:
    default = DEFAULT_PROVIDER_MODELS[provider]
    return os.environ.get(f"{provider.upper()}_MODEL", default).strip() or default


def provider_default_models() -> dict[str, str]:
    return {provider: provider_default_model(provider) for provider in DEFAULT_PROVIDER_MODELS}
