import json
import sqlite3
from datetime import UTC, date, datetime

from app.storage.ids import new_id
from app.storage.transactions import atomic


@atomic
def view_model_reflection(connection, reflection_id, viewed_at):
    row = connection.execute("SELECT * FROM reflection WHERE id=? AND author='model'", (reflection_id,)).fetchone()
    if row is None:
        raise ReflectionError('这份模型复盘不存在')
    covered = json.loads(row['covered_ids_json'])
    # Model feedback may reveal an approach; count its explicit viewing as exposure.
    for record_id in covered:
        questions = connection.execute("SELECT t.question_id FROM attempt a JOIN task t ON t.id=a.task_id "
            "WHERE a.id=? OR a.id IN (SELECT attempt_id FROM evaluation WHERE id=?) "
            "UNION SELECT t.question_id FROM interview_turn it JOIN interview_session s ON s.id=it.session_id "
            "JOIN task t ON t.id=s.task_id WHERE it.id=?", (record_id, record_id, record_id)).fetchall()
        for question in questions:
            connection.execute('INSERT OR IGNORE INTO question_exposure VALUES (?,?)', (question[0], viewed_at.astimezone(UTC).isoformat()))
    return {'content': row['content'], 'stale': bool(row['stale'])}


class ReflectionError(RuntimeError):
    """A reflection update violates author ownership."""


def save_user_reflection(
    connection: sqlite3.Connection,
    activity_date: date,
    content: str,
    created_at: datetime,
) -> str:
    """Create a new user-owned version without model overwrite access."""
    return _save_reflection(
        connection,
        activity_date,
        "user",
        content,
        [],
        created_at,
    )


def save_model_reflection(
    connection: sqlite3.Connection,
    activity_date: date,
    content: str,
    covered_ids: list[str],
    created_at: datetime,
) -> str:
    if not covered_ids:
        raise ReflectionError("model reflection must cite covered records")
    return _save_reflection(
        connection,
        activity_date,
        "model",
        content,
        covered_ids,
        created_at,
    )


def mark_model_reflections_stale(
    connection: sqlite3.Connection,
    activity_date: date,
) -> None:
    connection.execute(
        "UPDATE reflection SET stale=1 WHERE activity_date=? AND author='model'",
        (activity_date.isoformat(),),
    )


def activity_timeline(
    connection: sqlite3.Connection,
    activity_date: date,
) -> dict[str, object]:
    attempts = connection.execute(
        "SELECT a.id, a.task_id, a.submitted_at, a.code_self_result, a.answer_text, "
        "a.note,v.prompt,v.category_path,t.origin, t.question_id FROM attempt a JOIN task t ON t.id=a.task_id "
        "JOIN question_version v ON v.id=a.question_version_id "
        "WHERE a.activity_date=? AND a.submitted_at IS NOT NULL ORDER BY a.submitted_at",
        (activity_date.isoformat(),),
    ).fetchall()
    turns = connection.execute(
        "SELECT it.id, it.session_id, it.role, it.created_at FROM interview_turn it "
        "WHERE date(it.created_at, '+8 hours')=? ORDER BY it.created_at",
        (activity_date.isoformat(),),
    ).fetchall()
    task_activity = {row["task_id"] for row in attempts}
    session_tasks = connection.execute(
        "SELECT DISTINCT s.task_id FROM interview_session s JOIN interview_turn t "
        "ON t.session_id=s.id WHERE date(t.created_at, '+8 hours')=? AND t.role='user'",
        (activity_date.isoformat(),),
    ).fetchall()
    task_activity.update(row["task_id"] for row in session_tasks)
    return {
        "activity_date": activity_date.isoformat(),
        "activity_units": len(task_activity),
        "attempts": [dict(row) for row in attempts],
        "interview_turns": [dict(row) for row in turns],
    }


def _save_reflection(
    connection: sqlite3.Connection,
    activity_date: date,
    author: str,
    content: str,
    covered_ids: list[str],
    created_at: datetime,
) -> str:
    if not content.strip():
        raise ReflectionError("reflection cannot be empty")
    latest = connection.execute(
        "SELECT COALESCE(MAX(version), 0) FROM reflection WHERE activity_date=? AND author=?",
        (activity_date.isoformat(), author),
    ).fetchone()[0]
    reflection_id = new_id("reflection")
    connection.execute(
        "INSERT INTO reflection VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
        (
            reflection_id,
            activity_date.isoformat(),
            author,
            content,
            latest + 1,
            json.dumps(covered_ids),
            created_at.astimezone(UTC).isoformat(),
        ),
    )
    return reflection_id
