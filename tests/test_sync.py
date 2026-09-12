import sqlite3
from dataclasses import replace

import pytest

from app.domain import QuestionDraft
from app.services.sync import SyncIntegrityError, publish_snapshot
from tests.helpers import add_source


def draft(prompt: str = "Move zeroes", reference: str = "Use two pointers") -> QuestionDraft:
    return QuestionDraft(
        source_id="source-code",
        document_id="document-source-code",
        question_type="code",
        source_kind="feishu",
        main_anchor_block_id="anchor-move-zero",
        prompt_block_ids=("anchor-move-zero", "image-move-zero"),
        reference_block_ids=("reference-move-zero",),
        category_path="hot100 > Move Zeroes",
        prompt=prompt,
        reference_text=reference,
        material_status="complete",
    )


def blocks(text: str = "Move zeroes") -> list[dict[str, object]]:
    return [
        {"block_id": "root", "children": ["anchor-move-zero"]},
        {"block_id": "anchor-move-zero", "children": [], "text": text},
    ]


def test_repeat_sync_does_not_duplicate_question_or_version(
    database: sqlite3.Connection,
) -> None:
    add_source(database)

    first = publish_snapshot(database, "source-code", "1", "1", blocks(), [draft()], "v1")
    second = publish_snapshot(database, "source-code", "1", "1", blocks(), [draft()], "v1")

    assert first["created"] is True
    assert second["created"] is False
    assert database.execute("SELECT COUNT(*) FROM question").fetchone()[0] == 1
    assert database.execute("SELECT COUNT(*) FROM question_version").fetchone()[0] == 1


def test_changed_reference_keeps_identity_and_creates_new_basis(
    database: sqlite3.Connection,
) -> None:
    add_source(database)
    publish_snapshot(database, "source-code", "1", "1", blocks(), [draft()], "v1")
    question_id = database.execute("SELECT id FROM question").fetchone()[0]
    first_version = database.execute("SELECT id, review_basis_id FROM question_version").fetchone()

    changed = replace(draft(), reference_text="Use a stable write pointer")
    publish_snapshot(database, "source-code", "2", "2", blocks("Updated"), [changed], "v1")

    assert database.execute("SELECT id FROM question").fetchone()[0] == question_id
    versions = database.execute("SELECT id, review_basis_id FROM question_version").fetchall()
    assert len(versions) == 2
    current = database.execute(
        "SELECT v.id, v.review_basis_id FROM question q "
        "JOIN question_version v ON v.id=q.current_version_id WHERE q.id=?", (question_id,)
    ).fetchone()
    assert current["id"] != first_version["id"]
    assert current["review_basis_id"] != first_version["review_basis_id"]


def test_revision_change_rejects_entire_publish(database: sqlite3.Connection) -> None:
    add_source(database)

    with pytest.raises(SyncIntegrityError):
        publish_snapshot(database, "source-code", "1", "2", blocks(), [draft()], "v1")

    assert database.execute("SELECT COUNT(*) FROM source_snapshot").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM question").fetchone()[0] == 0


def test_missing_child_rejects_publish(database: sqlite3.Connection) -> None:
    add_source(database)
    broken = [{"block_id": "root", "children": ["missing"]}]

    with pytest.raises(SyncIntegrityError):
        publish_snapshot(database, "source-code", "1", "1", broken, [draft()], "v1")
