import sqlite3


def add_source(connection: sqlite3.Connection, source_id: str = "source-code") -> None:
    connection.execute(
        "INSERT INTO source(id, document_id, wiki_url, question_type) VALUES (?, ?, ?, 'code')",
        (source_id, f"document-{source_id}", f"https://example.test/{source_id}"),
    )
    connection.commit()


def remove_migrations_after(connection: sqlite3.Connection, version: int) -> None:
    """Remove newer additive schema objects before replaying an old migration."""
    if version < 16:
        connection.execute("DROP TABLE IF EXISTS model_job_execution")
    if version < 15:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(daily_plan)")}
        if "theory_scope_json" in columns:
            connection.execute("ALTER TABLE daily_plan DROP COLUMN theory_scope_json")
    if version < 14:
        connection.execute("DROP TABLE IF EXISTS mastery_assessment")
    if version < 13:
        connection.execute("DROP TABLE IF EXISTS alignment_request_run")
        connection.execute("DROP TABLE IF EXISTS alignment_request")
