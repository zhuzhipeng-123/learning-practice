import sqlite3
from datetime import UTC, datetime

from app.domain import QuestionDraft
from app.services.source_state import observe_missing_questions, record_source_failure
from app.services.sync import publish_snapshot
from tests.helpers import add_source


def seed(database: sqlite3.Connection) -> str:
    add_source(database)
    draft = QuestionDraft(
        source_id="source-code",
        document_id="document-source-code",
        question_type="code",
        source_kind="feishu",
        main_anchor_block_id="anchor",
        prompt_block_ids=("anchor",),
        reference_block_ids=(),
        category_path="hot100",
        prompt="Question",
        reference_text="Reference",
        material_status="complete",
    )
    publish_snapshot(
        database,
        "source-code",
        "1",
        "1",
        [{"block_id": "anchor", "children": []}],
        [draft],
        "v1",
    )
    return database.execute("SELECT id FROM question").fetchone()[0]


def test_failure_does_not_mark_question_missing(database: sqlite3.Connection) -> None:
    question_id = seed(database)

    record_source_failure(database, "source-code", "permission denied", datetime.now(UTC))

    state = database.execute(
        "SELECT source_status FROM question WHERE id=?", (question_id,)
    ).fetchone()
    assert state["source_status"] == "active"


def test_two_independent_complete_reads_confirm_deletion(database: sqlite3.Connection) -> None:
    question_id = seed(database)

    first = observe_missing_questions(database, "source-code", set(), True, False, True)
    first_state = database.execute(
        "SELECT source_status FROM question WHERE id=?", (question_id,)
    ).fetchone()[0]
    second = observe_missing_questions(database, "source-code", set(), True, False, True)
    second_state = database.execute(
        "SELECT source_status FROM question WHERE id=?", (question_id,)
    ).fetchone()[0]

    assert first == {"pending": 1, "deleted": 0}
    assert first_state == "missing_pending"
    assert second == {"pending": 0, "deleted": 1}
    assert second_state == "source_deleted"
    assert (
        database.execute("SELECT COUNT(*) FROM question WHERE id=?", (question_id,)).fetchone()[0]
        == 1
    )


def test_incomplete_read_cannot_advance_deletion(database: sqlite3.Connection) -> None:
    question_id = seed(database)

    result = observe_missing_questions(database, "source-code", set(), False, False, True)

    assert result == {"pending": 0, "deleted": 0}
    state = database.execute(
        "SELECT source_status FROM question WHERE id=?", (question_id,)
    ).fetchone()[0]
    assert state == "active"
