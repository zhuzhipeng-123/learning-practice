import sqlite3
from datetime import UTC, date, datetime

from app.services.tasks import _load_idempotent, _save_idempotent, add_tasks, create_daily_plan
from app.storage.transactions import atomic


@atomic
def start_review_task(
    connection: sqlite3.Connection,
    question_id: str,
    plan_date: date,
    request_key: str,
) -> dict[str, object]:
    """Reuse a compatible pending task or append one review task for today."""
    payload = {"question_id": question_id, "plan_date": plan_date.isoformat()}
    existing = _load_idempotent(connection, request_key, "start_review", payload)
    if existing is not None:
        return existing
    active = connection.execute(
        "SELECT r.review_basis_id FROM review_round r WHERE r.question_id=? AND r.status='active'",
        (question_id,),
    ).fetchone()
    if active is None:
        raise ValueError("question has no active review round")
    pending = connection.execute(
        "SELECT t.id FROM task t JOIN question_version v ON v.id=t.question_version_id "
        "WHERE t.question_id=? AND t.status IN ('pending', 'in_progress') "
        "AND v.review_basis_id=? ORDER BY t.created_at LIMIT 1",
        (question_id, active["review_basis_id"]),
    ).fetchone()
    if pending is not None:
        result = {"task_id": pending["id"], "created": False}
        _save_idempotent(connection, request_key, "start_review", payload, result, datetime.now(UTC).isoformat())
        return result
    version = connection.execute(
        "SELECT id FROM question_version WHERE question_id=? AND review_basis_id=? ORDER BY rowid DESC LIMIT 1",
        (question_id, active["review_basis_id"]),
    ).fetchone()
    plan = _get_or_create_plan(connection, plan_date, request_key)
    result = add_tasks(
        connection,
        plan["plan_id"],
        [question_id],
        "review",
        f"{request_key}:task",
        version_ids={question_id: version["id"]},
    )
    if not result["created_task_ids"]:
        raise ValueError("review task could not be created")
    response = {"task_id": result["created_task_ids"][0], "created": True}
    _save_idempotent(connection, request_key, "start_review", payload, response, datetime.now(UTC).isoformat())
    return response


def _get_or_create_plan(
    connection: sqlite3.Connection,
    plan_date: date,
    request_key: str,
) -> dict[str, object]:
    row = connection.execute(
        "SELECT id FROM daily_plan WHERE plan_date=?",
        (plan_date.isoformat(),),
    ).fetchone()
    if row is not None:
        return {"plan_id": row["id"]}
    return create_daily_plan(
        connection,
        plan_date,
        0,
        0,
        {},
        f"{request_key}:plan:{plan_date.isoformat()}",
    )
