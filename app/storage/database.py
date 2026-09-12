import sqlite3
from pathlib import Path

from app.storage.migrations import migrate

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect_database(path: Path) -> sqlite3.Connection:
    """Open SQLite with the consistency settings required by the app."""
    # FastAPI can enter, execute and close a sync dependency on different workers.
    # Connections remain request-owned; background jobs open their own connection.
    connection = sqlite3.connect(path, check_same_thread=False, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def initialize_database(connection: sqlite3.Connection) -> None:
    """Apply the initial idempotent schema."""
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone():
        migrate(connection)
        return
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    connection.executescript(schema)
    connection.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, datetime('now'))",
        (1,),
    )
    connection.commit()
    migrate(connection, backup=False)


def backup_database(source: sqlite3.Connection, destination: Path) -> None:
    """Create a transactionally consistent SQLite backup."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
    finally:
        target.close()
