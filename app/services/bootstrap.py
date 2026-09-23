import json
import sqlite3

from app.config import initial_sources_path


def load_initial_sources():
    path = initial_sources_path()
    if not path.exists():
        return []
    sources = json.loads(path.read_text(encoding='utf-8'))['sources']
    if not isinstance(sources, list) or any(not isinstance(row, list) or len(row) != 4
            or not all(isinstance(value, str) and value for value in row)
            or row[3] not in {'code', 'theory'} for row in sources):
        raise ValueError('本地题源配置格式不正确')
    return sources


def register_initial_sources(connection: sqlite3.Connection) -> int:
    connection.executemany(
        "INSERT OR IGNORE INTO source(id, document_id, wiki_url, question_type) "
        "VALUES (?, ?, ?, ?)",
        load_initial_sources(),
    )
    connection.commit()
    return connection.execute("SELECT COUNT(*) FROM source").fetchone()[0]
