"""Runtime defaults and environment-backed deployment settings."""

import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOTENV_PATH = PROJECT_ROOT / ".env"

DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_AGNES_BASE_URL = "https://apihub.agnes-ai.com/v1"
DEFAULT_AGNES_MODEL = "agnes-2.5-flash"


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


def agnes_base_url() -> str:
    configured = os.environ.get("AGNES_BASE_URL", DEFAULT_AGNES_BASE_URL).strip()
    return (configured or DEFAULT_AGNES_BASE_URL).rstrip("/")


def agnes_default_model() -> str:
    return os.environ.get("AGNES_MODEL", DEFAULT_AGNES_MODEL).strip() or DEFAULT_AGNES_MODEL
