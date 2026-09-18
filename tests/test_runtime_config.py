from datetime import date

import pytest

from app.config import (
    initial_sources_path,
    local_timezone_name,
    provider_base_url,
    provider_default_model,
)
from app.services.learning_clock import utc_bounds_for_local_days
from app.services.tasks import create_daily_plan


def test_runtime_settings_use_environment_overrides(monkeypatch, tmp_path):
    source_config = tmp_path / "sources.json"
    monkeypatch.setenv("LEARNING_TIMEZONE", "Asia/Tokyo")
    monkeypatch.setenv("LEARNING_SOURCE_CONFIG", str(source_config))
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://gateway.example/v1/")
    monkeypatch.setenv("OPENROUTER_MODEL", "example/model")

    assert local_timezone_name() == "Asia/Tokyo"
    assert initial_sources_path() == source_config.resolve()
    assert provider_base_url("openrouter") == "https://gateway.example/v1"
    assert provider_default_model("openrouter") == "example/model"


def test_invalid_runtime_timezone_is_rejected(monkeypatch):
    monkeypatch.setenv("LEARNING_TIMEZONE", "not-a-timezone")

    with pytest.raises(RuntimeError, match="LEARNING_TIMEZONE"):
        local_timezone_name()


def test_daily_plan_records_configured_timezone(database, monkeypatch):
    monkeypatch.setenv("LEARNING_TIMEZONE", "Asia/Tokyo")

    result = create_daily_plan(database, date(2030, 1, 1), 0, 0, {}, "timezone-plan")
    row = database.execute(
        "SELECT timezone FROM daily_plan WHERE id=?",
        (result["plan_id"],),
    ).fetchone()

    assert row["timezone"] == "Asia/Tokyo"


def test_local_day_bounds_follow_configured_timezone(monkeypatch):
    monkeypatch.setenv("LEARNING_TIMEZONE", "Asia/Tokyo")

    start_at, end_at = utc_bounds_for_local_days(date(2026, 7, 1), date(2026, 7, 1))

    assert start_at == "2026-06-30T15:00:00+00:00"
    assert end_at == "2026-07-01T15:00:00+00:00"
