import sqlite3
from datetime import UTC, date, datetime

from app.domain import QuestionDraft, Submission
from app.services.practice import start_attempt, submit_code
from app.services.sync import publish_snapshot
from app.services.tasks import create_daily_plan
from tests.helpers import add_source


def test_two_syncs_keep_task_history_and_old_basis(database: sqlite3.Connection) -> None:
    add_source(database)
    first = QuestionDraft(
        source_id="source-code",
        document_id="document-source-code",
        question_type="code",
        source_kind="feishu",
        main_anchor_block_id="stable-anchor",
        prompt_block_ids=("stable-anchor",),
        reference_block_ids=("answer",),
        category_path="hot100",
        prompt="Original prompt",
        reference_text="Original answer",
        material_status="complete",
    )
    publish_snapshot(
        database,
        "source-code",
        "1",
        "1",
        [{"block_id": "stable-anchor", "children": []}],
        [first],
        "v1",
    )
    question_id = database.execute("SELECT id FROM question").fetchone()[0]
    first_version = database.execute("SELECT * FROM question_version").fetchone()
    plan = create_daily_plan(database, date(2026, 9, 11), 1, 0, {}, "milestone-plan", 1)
    task = plan["tasks"][0]
    submitted_at = datetime(2026, 9, 11, 8, tzinfo=UTC)
    start_attempt(database, task["id"], "daily", submitted_at)
    result = submit_code(
        database,
        Submission(
            task_id=task["id"],
            request_key="failed-practice",
            submitted_at=submitted_at,
            entry_mode="daily",
            code_self_result="cannot_solve",
        ),
    )
    active_round = result["review_round_id"]

    changed = QuestionDraft(
        **{**first.__dict__, "reference_text": "Corrected answer"},
    )
    new_question = QuestionDraft(
        **{
            **first.__dict__,
            "main_anchor_block_id": "new-anchor",
            "prompt_block_ids": ("new-anchor",),
            "prompt": "New question",
        },
    )
    publish_snapshot(
        database,
        "source-code",
        "2",
        "2",
        [
            {"block_id": "stable-anchor", "children": []},
            {"block_id": "new-anchor", "children": []},
        ],
        [changed, new_question],
        "v1",
    )

    stored_task = database.execute("SELECT * FROM task WHERE id=?", (task["id"],)).fetchone()
    stored_attempt = database.execute("SELECT * FROM attempt").fetchone()
    stored_round = database.execute(
        "SELECT * FROM review_round WHERE id=?", (active_round,)
    ).fetchone()
    assert database.execute("SELECT COUNT(*) FROM question").fetchone()[0] == 2
    assert database.execute("SELECT COUNT(*) FROM question_version").fetchone()[0] == 3
    assert (
        database.execute(
            "SELECT question_id FROM source_binding WHERE main_anchor_block_id='stable-anchor'"
        ).fetchone()[0]
        == question_id
    )
    assert stored_task["question_version_id"] == first_version["id"]
    assert stored_attempt["question_version_id"] == first_version["id"]
    assert stored_round["review_basis_id"] == first_version["review_basis_id"]
    assert stored_task["status"] == "completed"
