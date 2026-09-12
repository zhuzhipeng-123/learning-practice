import sqlite3

from app.storage.transactions import transaction


class TaskVersionConflictError(RuntimeError):
    """A task version cannot change after answering has started."""


def update_unstarted_task_version(
    connection: sqlite3.Connection,
    task_id: str,
    expected_version_id: str,
    new_version_id: str,
) -> None:
    """Use a conditional update so starting and updating cannot race silently."""
    with transaction(connection):
        cursor = connection.execute(
            "UPDATE task SET question_version_id=? WHERE id=? "
            "AND question_version_id=? AND status='pending' AND started_at IS NULL",
            (new_version_id, task_id, expected_version_id),
        )
        if cursor.rowcount != 1:
            raise TaskVersionConflictError("task already started or its version changed")


def current_version_available(connection: sqlite3.Connection, task_id: str) -> bool:
    row = connection.execute(
        "SELECT t.question_version_id, q.current_version_id FROM task t "
        "JOIN question q ON q.id=t.question_id WHERE t.id=?",
        (task_id,),
    ).fetchone()
    return row is not None and row["question_version_id"] != row["current_version_id"]
