import sqlite3
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.services.practice import add_theory_to_review
from app.services.reflections import mark_model_reflections_stale
from app.services.tasks import _load_idempotent, _save_idempotent
from app.storage.ids import new_id
from app.storage.transactions import atomic, transaction


class InterviewError(RuntimeError):
    """An interview session transition is invalid."""


@atomic
def start_session(
    connection: sqlite3.Connection,
    task_id: str,
    created_at: datetime,
) -> str:
    existing = connection.execute("SELECT id FROM interview_session WHERE task_id=? AND status='active'", (task_id,)).fetchone()
    if existing:
        return existing["id"]
    task = connection.execute(
        "SELECT question_version_id,status FROM task WHERE id=?",
        (task_id,),
    ).fetchone()
    if task is None:
        raise InterviewError("task was not found")
    if task["status"] in {"completed", "cancelled"}:
        raise InterviewError("请选择未完成的任务，或创建一次新的复习任务")
    session_id = new_id("interview")
    connection.execute(
        "INSERT INTO interview_session VALUES (?, ?, ?, 'active', ?, NULL)",
        (session_id, task_id, task["question_version_id"], created_at.astimezone(UTC).isoformat()),
    )
    return session_id


@atomic
def add_turn(
    connection: sqlite3.Connection,
    session_id: str,
    role: str,
    content: str,
    created_at: datetime,
    request_key: str | None = None,
) -> str:
    payload = {"session_id": session_id, "role": role, "content": content}
    if request_key:
        existing = _load_idempotent(connection, request_key, "interview_turn", payload)
        if existing:
            return existing["turn_id"]
    if role not in {"user", "assistant"} or not content.strip():
        raise ValueError("a valid interview turn is required")
    session = connection.execute(
        "SELECT status FROM interview_session WHERE id=?",
        (session_id,),
    ).fetchone()
    if session is None or session["status"] != "active":
        raise InterviewError("interview session is not active")
    turn_id = new_id("turn")
    with transaction(connection):
        connection.execute(
            "INSERT INTO interview_turn VALUES (?, ?, ?, ?, ?)",
            (turn_id, session_id, role, content, created_at.astimezone(UTC).isoformat()),
        )
        mark_model_reflections_stale(connection, created_at.astimezone(ZoneInfo("Asia/Shanghai")).date())
        if role == "user":
            connection.execute(
                "UPDATE task SET status='completed', completed_at=COALESCE(completed_at, ?) "
                "WHERE id=(SELECT task_id FROM interview_session WHERE id=?)",
                (created_at.astimezone(UTC).isoformat(), session_id),
            )
            connection.execute("UPDATE question SET first_submitted_at=COALESCE(first_submitted_at,?) "
                               "WHERE id=(SELECT t.question_id FROM task t JOIN interview_session s "
                               "ON s.task_id=t.id WHERE s.id=?)", (created_at.astimezone(UTC).isoformat(), session_id))
        if request_key:
            _save_idempotent(connection, request_key, "interview_turn", payload, {"turn_id": turn_id}, created_at.astimezone(UTC).isoformat())
    return turn_id


@atomic
def end_session(connection: sqlite3.Connection, session_id: str, ended_at: datetime) -> None:
    connection.execute(
        "UPDATE interview_session SET status='ended', ended_at=? WHERE id=?",
        (ended_at.astimezone(UTC).isoformat(), session_id),
    )


def create_derived_theory_question(
    connection: sqlite3.Connection,
    session_id: str,
    prompt: str,
    reference_text: str,
    category_path: str,
    confirmed_by_user: bool,
    created_at: datetime,
) -> str:
    """Create a traceable derived question only after explicit confirmation."""
    if not confirmed_by_user:
        raise InterviewError("derived theory questions require explicit confirmation")
    if not prompt.strip() or not reference_text.strip():
        raise InterviewError("derived question requires standalone prompt and reference")
    session = connection.execute(
        "SELECT question_version_id FROM interview_session WHERE id=?",
        (session_id,),
    ).fetchone()
    if session is None:
        raise InterviewError("interview session was not found")
    question_id = new_id("question")
    basis_id = new_id("basis")
    version_id = new_id("version")
    now = created_at.astimezone(UTC).isoformat()
    with transaction(connection):
        connection.execute(
            "INSERT INTO question(id, question_type, source_kind, created_at) "
            "VALUES (?, 'theory', 'derived', ?)",
            (question_id, now),
        )
        connection.execute(
            "INSERT INTO review_basis VALUES (?, ?, ?, ?)",
            (basis_id, question_id, f"derived:{session_id}:{prompt}", now),
        )
        connection.execute(
            "INSERT INTO question_version VALUES (?, ?, NULL, ?, ?, ?, ?, 'complete', ?, ?, ?)",
            (
                version_id,
                question_id,
                basis_id,
                prompt,
                reference_text,
                category_path,
                f"derived:{session_id}:{prompt}:{reference_text}",
                f"derived from interview session {session_id}",
                now,
            ),
        )
        connection.execute(
            "UPDATE question SET current_version_id=? WHERE id=?",
            (version_id, question_id),
        )
        add_theory_to_review(connection, question_id, f"interview:{session_id}", created_at)
    return question_id
