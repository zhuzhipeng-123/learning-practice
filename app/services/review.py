import json
import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.storage.ids import new_id

SHANGHAI = ZoneInfo("Asia/Shanghai")
REQUIRED_VALID_PASSES = 5
REQUIRED_SPAN_SECONDS = 604_800


class ReviewError(RuntimeError):
    """A requested review transition is not allowed."""


def get_active_round(connection: sqlite3.Connection, question_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM review_round WHERE question_id=? AND status='active'",
        (question_id,),
    ).fetchone()


def enter_review(
    connection: sqlite3.Connection,
    question_id: str,
    basis_id: str,
    entered_by: str,
    happened_at: datetime,
) -> str:
    """Create one active round or reuse the matching active round."""
    active = get_active_round(connection, question_id)
    if active is not None:
        if active["review_basis_id"] == basis_id:
            return active["id"]
        _end_round(connection, active["id"], "version_replaced", happened_at)
    round_id = new_id("round")
    connection.execute(
        "INSERT INTO review_round VALUES (?, ?, ?, 'active', ?, ?, NULL, NULL)",
        (round_id, question_id, basis_id, entered_by, happened_at.isoformat()),
    )
    _record_event(connection, round_id, "entered", happened_at, entered_by)
    return round_id


def record_valid_pass(
    connection: sqlite3.Connection,
    attempt_id: str,
    passed: bool,
) -> bool:
    """Project one eligible attempt into its frozen review round."""
    attempt = connection.execute(
        "SELECT a.*, v.review_basis_id FROM attempt a "
        "JOIN question_version v ON v.id=a.question_version_id WHERE a.id=?",
        (attempt_id,),
    ).fetchone()
    if attempt is None or attempt["submitted_at"] is None:
        raise ReviewError("attempt has not been submitted")
    if not passed or attempt["review_round_id"] is None:
        _remove_pass(connection, attempt_id)
        return False
    if not _assessment_eligible(connection, attempt):
        _remove_pass(connection, attempt_id)
        return False
    if attempt["answer_exposed_at"] is not None and datetime.fromisoformat(attempt["answer_exposed_at"]) <= datetime.fromisoformat(attempt["submitted_at"]):
        _remove_pass(connection, attempt_id)
        return False
    review_round = connection.execute(
        "SELECT * FROM review_round WHERE id=?",
        (attempt["review_round_id"],),
    ).fetchone()
    if review_round is None or review_round["review_basis_id"] != attempt["review_basis_id"]:
        _remove_pass(connection, attempt_id)
        return False
    if exposed_recently(
        connection,
        _attempt_question_id(connection, attempt_id),
        datetime.fromisoformat(attempt["submitted_at"]),
        exclude_attempt_id=attempt_id,
    ):
        _remove_pass(connection, attempt_id)
        return False
    _upsert_daily_pass(connection, review_round["id"], attempt)
    return recompute_round(connection, review_round["id"])


def recompute_round(connection: sqlite3.Connection, round_id: str) -> bool:
    passes = connection.execute(
        "SELECT submitted_at FROM valid_review_pass WHERE review_round_id=? ORDER BY julianday(submitted_at)",
        (round_id,),
    ).fetchall()
    qualifies = _passes_qualify(passes)
    review_round = connection.execute(
        "SELECT status, end_reason, question_id FROM review_round WHERE id=?",
        (round_id,),
    ).fetchone()
    if review_round is None:
        raise ReviewError("review round was not found")
    if qualifies and review_round["status"] == "active":
        last = datetime.fromisoformat(passes[-1]["submitted_at"])
        _end_round(connection, round_id, "criteria_met", last)
        _record_event(connection, round_id, "criteria_met", last, str(len(passes)))
    elif not qualifies and review_round["end_reason"] == "criteria_met":
        happened_at = datetime.now().astimezone()
        active = get_active_round(connection, review_round["question_id"])
        if active is None:
            connection.execute(
                "UPDATE review_round SET status='active', ended_at=NULL, end_reason=NULL WHERE id=?",
                (round_id,),
            )
        else:
            connection.execute(
                "UPDATE review_round SET end_reason='criteria_revoked' WHERE id=?", (round_id,),
            )
        _record_event(connection, round_id, "criteria_revoked", happened_at, str(len(passes)))
    return qualifies


def _passes_qualify(passes: list[sqlite3.Row]) -> bool:
    if len(passes) < REQUIRED_VALID_PASSES:
        return False
    first = datetime.fromisoformat(passes[0]["submitted_at"])
    last = datetime.fromisoformat(passes[-1]["submitted_at"])
    return (last - first).total_seconds() > REQUIRED_SPAN_SECONDS


def _assessment_eligible(connection, attempt):
    if attempt['code_self_result'] is not None:
        return True
    from app.services.reference_state import verification
    state = verification(connection, attempt['question_version_id'])
    if state['verified']:
        return True
    return bool(connection.execute("SELECT 1 FROM evaluation WHERE attempt_id=? AND adopted=1 "
                                    "AND corrected_by_user=1 AND verdict='aligned'", (attempt['id'],)).fetchone())


def exposed_recently(
    connection: sqlite3.Connection,
    question_id: str,
    at: datetime,
    exclude_attempt_id: str | None = None,
) -> bool:
    at = at.astimezone(UTC)
    cutoff = at - timedelta(hours=24)
    if connection.execute("SELECT 1 FROM question_exposure WHERE question_id=? AND julianday(happened_at)>julianday(?) AND julianday(happened_at)<=julianday(?)",
                          (question_id, cutoff.isoformat(), at.isoformat())).fetchone():
        return True
    row = connection.execute(
        "SELECT 1 FROM attempt a JOIN task t ON t.id=a.task_id "
        "WHERE t.question_id=? AND a.id!=COALESCE(?, '') "
        "AND ((julianday(a.answer_exposed_at)>julianday(?) AND julianday(a.answer_exposed_at)<=julianday(?)) OR EXISTS ("
        "SELECT 1 FROM answer_exposure e WHERE e.attempt_id=a.id AND julianday(e.happened_at)>julianday(?) "
        "AND julianday(e.happened_at)<=julianday(?))) LIMIT 1",
        (question_id, exclude_attempt_id, cutoff.isoformat(), at.isoformat(), cutoff.isoformat(), at.isoformat()),
    ).fetchone()
    return row is not None


def _attempt_question_id(connection: sqlite3.Connection, attempt_id: str) -> str:
    row = connection.execute(
        "SELECT t.question_id FROM attempt a JOIN task t ON t.id=a.task_id WHERE a.id=?",
        (attempt_id,),
    ).fetchone()
    if row is None:
        raise ReviewError("attempt question was not found")
    return str(row["question_id"])


def _upsert_daily_pass(
    connection: sqlite3.Connection,
    round_id: str,
    attempt: sqlite3.Row,
) -> None:
    existing = connection.execute(
        "SELECT id, submitted_at FROM valid_review_pass "
        "WHERE review_round_id=? AND activity_date=?",
        (round_id, attempt["activity_date"]),
    ).fetchone()
    if existing is not None and datetime.fromisoformat(existing["submitted_at"]) <= datetime.fromisoformat(attempt["submitted_at"]):
        return
    if existing is not None:
        connection.execute("DELETE FROM valid_review_pass WHERE id=?", (existing["id"],))
    connection.execute(
        "INSERT INTO valid_review_pass VALUES (?, ?, ?, ?, ?)",
        (
            new_id("pass"),
            round_id,
            attempt["id"],
            attempt["activity_date"],
            attempt["submitted_at"],
        ),
    )


def _remove_pass(connection: sqlite3.Connection, attempt_id: str) -> None:
    removed = connection.execute(
        "SELECT review_round_id,activity_date FROM valid_review_pass WHERE attempt_id=?",
        (attempt_id,),
    ).fetchone()
    connection.execute("DELETE FROM valid_review_pass WHERE attempt_id=?", (attempt_id,))
    if removed is None:
        return
    # A corrected assessment may uncover another valid answer on the same local day.
    candidates = connection.execute(
        "SELECT DISTINCT a.* FROM attempt a LEFT JOIN evaluation e ON e.attempt_id=a.id "
        "AND e.adopted=1 WHERE a.review_round_id=? AND a.activity_date=? AND a.id!=? "
        "AND a.submitted_at IS NOT NULL AND (a.answer_exposed_at IS NULL OR a.answer_exposed_at>a.submitted_at) "
        "AND EXISTS (SELECT 1 FROM question_version v JOIN review_round r ON r.id=a.review_round_id "
        "WHERE v.id=a.question_version_id AND v.review_basis_id=r.review_basis_id) "
        "AND (a.code_self_result='can_solve' OR e.verdict='aligned') ORDER BY a.submitted_at,a.id",
        (removed["review_round_id"], removed["activity_date"], attempt_id),
    ).fetchall()
    for candidate in candidates:
        if _assessment_eligible(connection, candidate) and not exposed_recently(connection, _attempt_question_id(connection, candidate["id"]),
                               datetime.fromisoformat(candidate["submitted_at"]), candidate["id"]):
            _upsert_daily_pass(connection, removed["review_round_id"], candidate)
            break
    recompute_round(connection, removed["review_round_id"])


def _end_round(
    connection: sqlite3.Connection,
    round_id: str,
    reason: str,
    happened_at: datetime,
) -> None:
    connection.execute(
        "UPDATE review_round SET status='ended', ended_at=?, end_reason=? WHERE id=?",
        (happened_at.isoformat(), reason, round_id),
    )


def _record_event(
    connection: sqlite3.Connection,
    round_id: str,
    event_type: str,
    happened_at: datetime,
    detail: str,
) -> None:
    connection.execute(
        "INSERT INTO review_event VALUES (?, ?, ?, ?, ?)",
        (
            new_id("event"),
            round_id,
            event_type,
            happened_at.isoformat(),
            json.dumps({"detail": detail}, ensure_ascii=False),
        ),
    )
