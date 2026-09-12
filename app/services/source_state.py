import sqlite3
from datetime import UTC, datetime

from app.storage.transactions import transaction


class SourceStateError(RuntimeError):
    """A source state transition lacks trustworthy evidence."""


def record_source_failure(
    connection: sqlite3.Connection,
    source_id: str,
    message: str,
    checked_at: datetime,
) -> None:
    """Record a failed check without changing question availability."""
    connection.execute(
        "UPDATE source SET last_check_at=?, last_error=? WHERE id=?",
        (checked_at.astimezone(UTC).isoformat(), message, source_id),
    )


def observe_missing_questions(
    connection: sqlite3.Connection,
    source_id: str,
    present_anchor_ids: set[str],
    complete_read: bool,
    has_ambiguity: bool,
    independent_run: bool,
) -> dict[str, int]:
    """Advance deletion state only after independent complete observations."""
    if not complete_read:
        return {"pending": 0, "deleted": 0}
    bindings = connection.execute(
        "SELECT id, question_id, main_anchor_block_id, missing_observation_count "
        "FROM source_binding WHERE source_id=? AND confirmation_status!='migrated'",
        (source_id,),
    ).fetchall()
    pending = 0
    deleted = 0
    with transaction(connection):
        for binding in bindings:
            if binding["main_anchor_block_id"] in present_anchor_ids:
                _restore_binding(connection, binding["id"], binding["question_id"])
                continue
            count = binding["missing_observation_count"]
            if independent_run and not has_ambiguity:
                count += 1
            if count >= 2 and not has_ambiguity:
                _mark_deleted(connection, binding["id"], binding["question_id"], count)
                deleted += 1
            else:
                _mark_pending(connection, binding["id"], binding["question_id"], count)
                pending += 1
    return {"pending": pending, "deleted": deleted}


def _restore_binding(
    connection: sqlite3.Connection,
    binding_id: str,
    question_id: str,
) -> None:
    connection.execute(
        "UPDATE source_binding SET missing_observation_count=0,active=1 WHERE id=?",
        (binding_id,),
    )
    connection.execute(
        "UPDATE question SET source_status='active' WHERE id=?",
        (question_id,),
    )


def _mark_pending(
    connection: sqlite3.Connection,
    binding_id: str,
    question_id: str,
    count: int,
) -> None:
    connection.execute(
        "UPDATE source_binding SET missing_observation_count=? WHERE id=?",
        (count, binding_id),
    )
    connection.execute(
        "UPDATE question SET source_status='missing_pending' WHERE id=?",
        (question_id,),
    )


def _mark_deleted(
    connection: sqlite3.Connection,
    binding_id: str,
    question_id: str,
    count: int,
) -> None:
    connection.execute(
        "UPDATE source_binding SET missing_observation_count=?, active=0 WHERE id=?",
        (count, binding_id),
    )
    connection.execute(
        "UPDATE question SET source_status='source_deleted' WHERE id=?",
        (question_id,),
    )
