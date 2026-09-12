import sqlite3
from pathlib import Path

import pytest

from app.storage.database import backup_database


def test_foreign_keys_are_enabled(database: sqlite3.Connection) -> None:
    enabled = database.execute("PRAGMA foreign_keys").fetchone()[0]

    assert enabled == 1


def test_only_one_active_review_round_per_question(database: sqlite3.Connection) -> None:
    database.execute(
        "INSERT INTO question(id, question_type, source_kind, created_at) "
        "VALUES ('q1', 'code', 'feishu', '2026-09-11T00:00:00Z')"
    )
    database.execute(
        "INSERT INTO review_basis VALUES ('b1', 'q1', 'hash-1', '2026-09-11T00:00:00Z')"
    )
    database.execute(
        "INSERT INTO review_round VALUES "
        "('r1', 'q1', 'b1', 'active', 'test', '2026-09-11T00:00:00Z', NULL, NULL)"
    )

    with pytest.raises(sqlite3.IntegrityError):
        database.execute(
            "INSERT INTO review_round VALUES "
            "('r2', 'q1', 'b1', 'active', 'test', '2026-09-12T00:00:00Z', NULL, NULL)"
        )


def test_backup_is_consistent(database: sqlite3.Connection, tmp_path: Path) -> None:
    database.execute(
        "INSERT INTO question(id, question_type, source_kind, created_at) "
        "VALUES ('q1', 'theory', 'derived', '2026-09-11T00:00:00Z')"
    )
    database.commit()
    destination = tmp_path / "backup.db"

    backup_database(database, destination)

    restored = sqlite3.connect(destination)
    try:
        count = restored.execute("SELECT COUNT(*) FROM question").fetchone()[0]
        violations = restored.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        restored.close()
    assert count == 1
    assert violations == []
