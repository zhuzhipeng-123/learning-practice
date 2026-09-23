import json
import sqlite3
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.storage.database import connect_database, initialize_database
from tests.quality_fixtures import quality_result


@pytest.fixture(autouse=True)
def isolated_app_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Lifespan must never open the user's learning database during a test."""
    monkeypatch.setenv("LEARNING_DATA_DIR", str(tmp_path / "app-data"))
    monkeypatch.setenv("AGNES_API_KEY", "")
    # Most service tests use synthetic September 2026 plans. Freeze the
    # submission guard so the suite stays deterministic after those dates;
    # rollover/expiry tests override this clock explicitly.
    monkeypatch.setattr("app.services.practice.local_today", lambda: date(2026, 9, 1))
    monkeypatch.setattr('app.services.bootstrap.load_initial_sources', lambda: [
        ('source-code', 'demo-code-document', 'https://example.feishu.cn/wiki/demo-code', 'code'),
        ('source-theory', 'demo-theory-document', 'https://example.feishu.cn/wiki/demo-theory', 'theory'),
    ])
    # Mock only the external reviewer, while still exercising its job and validation path.
    def reviewer(messages, **kwargs):
        context = json.loads(messages[1]['content'])
        return SimpleNamespace(content=json.dumps(quality_result(context['items'])), model='fixture-reviewer')
    monkeypatch.setattr('app.services.question_quality.client_for_config', lambda _: SimpleNamespace(complete=reviewer))
    yield
    from app.services.source_refresh import _jobs, _lock
    with _lock:
        owned = [(key, future) for key, future in _jobs.items()
                 if Path(key[0]).is_relative_to(tmp_path)]
    for key, future in owned:
        future.result(timeout=5)
        with _lock:
            if _jobs.get(key) is future:
                _jobs.pop(key, None)


@pytest.fixture
def database(tmp_path: Path) -> sqlite3.Connection:
    connection = connect_database(tmp_path / "test.db")
    initialize_database(connection)
    yield connection
    connection.close()
