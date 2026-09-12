import sqlite3


def add_source(connection: sqlite3.Connection, source_id: str = "source-code") -> None:
    connection.execute(
        "INSERT INTO source(id, document_id, wiki_url, question_type) VALUES (?, ?, ?, 'code')",
        (source_id, f"document-{source_id}", f"https://example.test/{source_id}"),
    )
    connection.commit()
