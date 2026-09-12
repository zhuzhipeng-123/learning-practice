import sqlite3
from datetime import UTC, date, datetime

import pytest

from app.domain import QuestionDraft
from app.services.free_practice import add_free_practice, find_new_originals
from app.services.interview import (
    InterviewError,
    add_turn,
    create_derived_theory_question,
    start_session,
)
from app.services.reflections import (
    activity_timeline,
    mark_model_reflections_stale,
    save_model_reflection,
    save_user_reflection,
)
from app.services.sync import publish_snapshot
from app.services.tasks import create_daily_plan
from tests.helpers import add_source


def seed_questions(database: sqlite3.Connection) -> list[str]:
    add_source(database, "source-theory")
    database.execute("UPDATE source SET question_type='theory' WHERE id='source-theory'")
    drafts = []
    blocks = []
    for index, prompt in enumerate(["AdamW 是什么？", "SGD 如何更新？", "Adam 显存如何估算？"]):
        anchor = f"anchor-{index}"
        blocks.append({"block_id": anchor, "children": []})
        drafts.append(
            QuestionDraft(
                source_id="source-theory",
                document_id="document-source-theory",
                question_type="theory",
                source_kind="feishu",
                main_anchor_block_id=anchor,
                prompt_block_ids=(anchor,),
                reference_block_ids=(),
                category_path="计算效率问题 > 优化器",
                prompt=prompt,
                reference_text="reference",
                material_status="complete",
            )
        )
    publish_snapshot(database, "source-theory", "1", "1", blocks, drafts, "v1")
    database.commit()
    return [row[0] for row in database.execute("SELECT id FROM question ORDER BY id")]


def test_free_practice_only_uses_unsubmitted_originals(database: sqlite3.Connection) -> None:
    question_ids = seed_questions(database)
    database.execute(
        "UPDATE question SET first_submitted_at='2026-09-01T00:00:00Z' WHERE id=?",
        (question_ids[0],),
    )
    plan = create_daily_plan(database, date(2026, 9, 11), 0, 0, {}, "empty-plan", 1)

    result = add_free_practice(
        database,
        plan["plan_id"],
        "优化器",
        3,
        "free-request",
        source_checked=True,
        used_cache=False,
        random_seed=2,
    )

    assert result.added == 2
    assert result.missing == 1
    selected = database.execute("SELECT question_id FROM task").fetchall()
    assert question_ids[0] not in {row[0] for row in selected}


def test_pending_question_is_excluded_from_free_pool(database: sqlite3.Connection) -> None:
    seed_questions(database)
    create_daily_plan(database, date(2026, 9, 11), 0, 1, {}, "daily", 2)

    candidates = find_new_originals(database, "优化器")

    assert len(candidates) == 2


def test_interview_followups_do_not_create_more_tasks(database: sqlite3.Connection) -> None:
    seed_questions(database)
    plan = create_daily_plan(database, date(2026, 9, 11), 0, 1, {}, "daily", 2)
    task_id = plan["tasks"][0]["id"]
    now = datetime(2026, 9, 11, 8, tzinfo=UTC)
    session_id = start_session(database, task_id, now)

    for index in range(4):
        add_turn(database, session_id, "assistant", f"follow-up {index}", now)
        add_turn(database, session_id, "user", f"answer {index}", now)

    assert database.execute("SELECT COUNT(*) FROM task").fetchone()[0] == 1
    assert database.execute("SELECT COUNT(*) FROM interview_turn").fetchone()[0] == 8
    assert database.execute("SELECT status FROM task").fetchone()[0] == "completed"


def test_derived_theory_requires_confirmation_and_is_isolated(
    database: sqlite3.Connection,
) -> None:
    seed_questions(database)
    plan = create_daily_plan(database, date(2026, 9, 11), 0, 1, {}, "daily", 2)
    now = datetime(2026, 9, 11, 8, tzinfo=UTC)
    session_id = start_session(database, plan["tasks"][0]["id"], now)

    with pytest.raises(InterviewError):
        create_derived_theory_question(
            database, session_id, "standalone prompt", "reference", "interview", False, now
        )
    question_id = create_derived_theory_question(
        database, session_id, "standalone prompt", "reference", "interview", True, now
    )

    question = database.execute(
        "SELECT source_kind FROM question WHERE id=?", (question_id,)
    ).fetchone()
    review = database.execute(
        "SELECT entered_by FROM review_round WHERE question_id=?", (question_id,)
    ).fetchone()
    assert question["source_kind"] == "derived"
    assert review["entered_by"].startswith("interview:")


def test_reflections_keep_authors_separate_and_model_becomes_stale(
    database: sqlite3.Connection,
) -> None:
    day = date(2026, 9, 11)
    now = datetime(2026, 9, 11, 10, tzinfo=UTC)
    save_user_reflection(database, day, "我的反思", now)
    save_model_reflection(database, day, "基于一次作答的总结", ["attempt-1"], now)

    mark_model_reflections_stale(database, day)

    rows = database.execute(
        "SELECT author, content, stale FROM reflection ORDER BY author"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("model", "基于一次作答的总结", 1),
        ("user", "我的反思", 0),
    ]
    assert activity_timeline(database, day)["activity_units"] == 0
