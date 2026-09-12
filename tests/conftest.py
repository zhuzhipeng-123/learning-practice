import sqlite3
from pathlib import Path

import pytest

from app.storage.database import connect_database, initialize_database


@pytest.fixture(autouse=True)
def isolated_app_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Lifespan must never open the user's learning database during a test."""
    monkeypatch.setenv("LEARNING_DATA_DIR", str(tmp_path / "app-data"))
    monkeypatch.setenv("AGNES_API_KEY", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setattr('app.services.bootstrap.load_initial_sources', lambda: [
        ('source-code', 'demo-code-document', 'https://example.feishu.cn/wiki/demo-code', 'code'),
        ('source-theory', 'demo-theory-document', 'https://example.feishu.cn/wiki/demo-theory', 'theory'),
    ])
    monkeypatch.setattr("app.routes.api.queue_source_refresh", lambda connection, *a, **k: {"used_cache": True, "sources": [], "refreshing": []})


@pytest.fixture
def database(tmp_path: Path) -> sqlite3.Connection:
    connection = connect_database(tmp_path / "test.db")
    initialize_database(connection)
    yield connection
    connection.close()
