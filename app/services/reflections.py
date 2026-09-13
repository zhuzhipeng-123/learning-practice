import json
import sqlite3
from datetime import UTC, date, datetime

from app.services.tasks import _load_idempotent, _save_idempotent
from app.storage.ids import new_id
from app.storage.transactions import atomic


@atomic
def view_model_reflection(connection, reflection_id, viewed_at):
    row = connection.execute("SELECT * FROM reflection WHERE id=? AND author='model'", (reflection_id,)).fetchone()
    if row is None:
        raise ReflectionError('这份模型复盘不存在')
    # Daily reflections contain learning suggestions, not answer references.
    # Merely citing an answer ID does not establish exposure to its solution.
    return {'content': row['content'], 'stale': bool(row['stale'])}


class ReflectionError(RuntimeError):
    """A reflection update violates author ownership."""


@atomic
def save_user_reflection(
    connection: sqlite3.Connection,
    activity_date: date,
    content: str,
    created_at: datetime,
    request_key: str | None = None,
    expected_id: str | None = None,
) -> str:
    """Create a new user-owned version without model overwrite access."""
    payload = {"activity_date": activity_date.isoformat(), "content": content, "expected_id": expected_id}
    if request_key:
        previous = _load_idempotent(connection, request_key, 'user_reflection', payload)
        if previous:
            return previous['reflection_id']
    current = connection.execute("SELECT id FROM reflection WHERE author='user' AND activity_date=? ORDER BY version DESC LIMIT 1",
                                 (activity_date.isoformat(),)).fetchone()
    if expected_id is not None and expected_id != (current[0] if current else ''):
        raise ReflectionError('复盘已在其他页面更新，请重新读取后再保存；当前草稿仍保留')
    result = _save_reflection(
        connection,
        activity_date,
        "user",
        content,
        [],
        created_at,
    )
    if request_key:
        _save_idempotent(connection, request_key, 'user_reflection', payload, {'reflection_id': result}, created_at.isoformat())
    return result


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
    if len(content) > 30000:
        raise ReflectionError('复盘请控制在30000字以内，原文不会被截断')
    previous = connection.execute(
        'SELECT id,content FROM reflection WHERE activity_date=? AND author=? ORDER BY version DESC LIMIT 1',
        (activity_date.isoformat(), author),
    ).fetchone()
    if author == 'user' and previous and previous['content'] == content:
        return previous['id']
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
