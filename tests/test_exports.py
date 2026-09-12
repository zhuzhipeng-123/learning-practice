import sqlite3
from pathlib import Path

from app.services.exports import export_learning_data, verify_export


def test_export_and_restore_include_database_and_media(
    database: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    database.execute(
        "INSERT INTO question(id, question_type, source_kind, created_at) "
        "VALUES ('q1', 'code', 'derived', '2026-09-11T00:00:00Z')"
    )
    database.commit()
    media = tmp_path / "media"
    media.mkdir()
    (media / "example.png").write_bytes(b"safe-media")
    archive = tmp_path / "export.zip"

    export_learning_data(database, archive, media)
    result = verify_export(archive, tmp_path / "restore")

    assert result == {"question_count": 1, "media_count": 1}
