import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from app.domain import QuestionDraft, Submission
from app.services.practice import (
    add_theory_to_review,
    start_attempt,
    submit_code,
    submit_theory,
)
from app.services.review import record_valid_pass
from app.services.sync import publish_snapshot
from app.services.tasks import IdempotencyConflictError, create_daily_plan
from tests.helpers import add_source


def seed_question(database: sqlite3.Connection, question_type: str) -> str:
    source_id = f"source-{question_type}"
    add_source(database, source_id)
    database.execute("UPDATE source SET question_type=? WHERE id=?", (question_type, source_id))
    draft = QuestionDraft(
        source_id=source_id,
        document_id=f"document-{source_id}",
        question_type=question_type,  # type: ignore[arg-type]
        source_kind="feishu",
        main_anchor_block_id=f"anchor-{question_type}",
        prompt_block_ids=(f"anchor-{question_type}",),
        reference_block_ids=(f"reference-{question_type}",),
        category_path="category",
        prompt=f"{question_type} prompt",
        reference_text=f"{question_type} reference",
        material_status="complete",
    )
    blocks = [{"block_id": f"anchor-{question_type}", "children": []}]
    publish_snapshot(database, source_id, "1", "1", blocks, [draft], "v1")
    database.commit()
    return database.execute(
        "SELECT id FROM question WHERE question_type=?",
        (question_type,),
    ).fetchone()[0]


def make_plan(database: sqlite3.Connection, question_type: str) -> sqlite3.Row:
    targets = (1, 0) if question_type == "code" else (0, 1)
    result = create_daily_plan(
        database,
        date(2026, 9, 11),
        targets[0],
        targets[1],
        {},
        "plan-key",
        random_seed=4,
    )
    return database.execute("SELECT * FROM task WHERE id=?", (result["tasks"][0]["id"],)).fetchone()


def test_daily_plan_is_stable_and_idempotent(database: sqlite3.Connection) -> None:
    seed_question(database, "code")

    first = create_daily_plan(database, date(2026, 9, 11), 1, 0, {}, "same-key", 1)
    second = create_daily_plan(database, date(2026, 9, 11), 1, 0, {}, "same-key", 99)

    assert first == second
    assert database.execute("SELECT COUNT(*) FROM task").fetchone()[0] == 1


def test_idempotency_key_rejects_changed_payload(database: sqlite3.Connection) -> None:
    seed_question(database, "code")
    create_daily_plan(database, date(2026, 9, 11), 1, 0, {}, "same-key", 1)

    with pytest.raises(IdempotencyConflictError):
        create_daily_plan(database, date(2026, 9, 11), 0, 1, {}, "same-key", 1)


def test_code_cannot_solve_completes_and_enters_review(
    database: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_question(database, "code")
    task = make_plan(database, "code")
    now = datetime(2026, 9, 11, 8, tzinfo=UTC)
    monkeypatch.setattr('app.services.practice.local_today', lambda: now.date())
    start_attempt(database, task["id"], "daily", now)
    submission = Submission(
        task_id=task["id"],
        request_key="submit-code",
        submitted_at=now,
        entry_mode="daily",
        code_self_result="cannot_solve",
    )

    first = submit_code(database, submission)
    second = submit_code(database, submission)

    assert first == second
    assert first["task_completed"] is True
    assert first["passed"] is False
    assert first["review_round_id"] is not None
    assert database.execute("SELECT status FROM task").fetchone()[0] == "completed"


def test_theory_submission_is_saved_without_auto_review(
    database: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question_id = seed_question(database, "theory")
    task = make_plan(database, "theory")
    now = datetime(2026, 9, 11, 8, tzinfo=UTC)
    monkeypatch.setattr('app.services.practice.local_today', lambda: now.date())
    start_attempt(database, task["id"], "daily", now)

    result = submit_theory(
        database,
        Submission(
            task_id=task["id"],
            request_key="submit-theory",
            submitted_at=now,
            entry_mode="daily",
            answer_text="my answer",
        ),
    )

    assert result["evaluation_status"] == "pending"
    assert result["review_membership_changed"] is False
    assert database.execute("SELECT answer_text FROM attempt").fetchone()[0] == "my answer"
    assert database.execute("SELECT COUNT(*) FROM review_round").fetchone()[0] == 0
    add_theory_to_review(database, question_id, "explicit_click", now)
    assert database.execute("SELECT COUNT(*) FROM review_round").fetchone()[0] == 1


def test_review_requires_five_passes_and_strictly_more_than_seven_days(
    database: sqlite3.Connection,
) -> None:
    question_id = seed_question(database, "theory")
    basis_id = database.execute("SELECT review_basis_id FROM question_version").fetchone()[0]
    start = datetime(2026, 9, 1, 0, tzinfo=UTC)
    round_id = add_theory_to_review(database, question_id, "explicit_click", start)
    plan = create_daily_plan(database, date(2026, 9, 1), 0, 1, {}, "review-plan", 1)
    task = plan["tasks"][0]

    offsets = [0, 1, 2, 3, 7]
    attempt_ids = []
    for index, days in enumerate(offsets):
        submitted = start + timedelta(days=days)
        attempt_id = f"attempt-{index}"
        attempt_ids.append(attempt_id)
        database.execute(
            "INSERT INTO attempt(id, task_id, question_version_id, review_round_id, entry_mode, "
            "started_at, submitted_at, activity_date, answer_text) VALUES (?, ?, ?, ?, 'review', ?, ?, ?, 'answer')",
            (
                attempt_id,
                task["id"],
                task["question_version_id"],
                round_id,
                submitted.isoformat(),
                submitted.isoformat(),
                submitted.astimezone().date().isoformat(),
            ),
        )
        record_valid_pass(database, attempt_id, True)
    assert database.execute("SELECT status FROM review_round").fetchone()[0] == "active"

    sixth = start + timedelta(days=7, seconds=1)
    database.execute(
        "INSERT INTO attempt(id, task_id, question_version_id, review_round_id, entry_mode, "
        "started_at, submitted_at, activity_date, answer_text) VALUES (?, ?, ?, ?, 'review', ?, ?, ?, 'answer')",
        (
            "attempt-sixth",
            task["id"],
            task["question_version_id"],
            round_id,
            sixth.isoformat(),
            sixth.isoformat(),
            "2026-09-09",
        ),
    )
    record_valid_pass(database, "attempt-sixth", True)

    review_round = database.execute("SELECT * FROM review_round").fetchone()
    assert review_round["status"] == "ended"
    assert review_round["end_reason"] == "criteria_met"
    assert (
        basis_id
        == database.execute(
            "SELECT review_basis_id FROM review_round WHERE id=?", (round_id,)
        ).fetchone()[0]
    )


def test_recent_answer_exposure_blocks_a_later_pass(database: sqlite3.Connection) -> None:
    question_id = seed_question(database, "theory")
    start = datetime(2026, 9, 1, 0, tzinfo=UTC)
    round_id = add_theory_to_review(database, question_id, "explicit_click", start)
    plan = create_daily_plan(database, date(2026, 9, 1), 0, 1, {}, "exposure-plan", 1)
    task = plan["tasks"][0]
    database.execute(
        "INSERT INTO attempt(id, task_id, question_version_id, review_round_id, entry_mode, "
        "started_at, submitted_at, activity_date, answer_text, answer_exposed_at) "
        "VALUES ('exposed', ?, ?, ?, 'review', ?, ?, '2026-09-01', 'answer', ?)",
        (
            task["id"],
            task["question_version_id"],
            round_id,
            start.isoformat(),
            start.isoformat(),
            start.isoformat(),
        ),
    )
    later = start + timedelta(hours=23)
    database.execute(
        "INSERT INTO attempt(id, task_id, question_version_id, review_round_id, entry_mode, "
        "started_at, submitted_at, activity_date, answer_text) "
        "VALUES ('later', ?, ?, ?, 'review', ?, ?, '2026-09-01', 'answer')",
        (task["id"], task["question_version_id"], round_id, later.isoformat(), later.isoformat()),
    )

    record_valid_pass(database, "later", True)

    count = database.execute("SELECT COUNT(*) FROM valid_review_pass").fetchone()[0]
    assert count == 0
