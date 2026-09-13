import sqlite3
from collections.abc import Iterator
from pathlib import Path

from fastapi import Request

from app.storage.database import connect_database


def get_database(request: Request) -> Iterator[sqlite3.Connection]:
    """Give each request its own SQLite connection and always close it."""
    database_path: Path = request.app.state.database_path
    connection = connect_database(database_path)
    try:
        from app.services.current_practice import retire_expired
        retire_expired(connection)
        yield connection
    finally:
        connection.close()
