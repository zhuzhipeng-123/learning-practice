import hashlib
import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from app.domain import QuestionDraft
from app.storage.ids import new_id
from app.storage.transactions import atomic, transaction


class SyncIntegrityError(RuntimeError):
    """The fetched source cannot be safely published."""


def utc_now_text() -> str:
    return datetime.now(UTC).isoformat()


def stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_blocks(blocks: list[dict[str, Any]]) -> None:
    """Reject incomplete trees before they can affect published questions."""
    if not blocks:
        raise SyncIntegrityError("source returned no blocks")
    block_ids = [str(block.get("block_id", "")) for block in blocks]
    if "" in block_ids or len(block_ids) != len(set(block_ids)):
        raise SyncIntegrityError("block IDs are missing or duplicated")
    known = set(block_ids)
    for block in blocks:
        for child_id in block.get("children", []):
            if child_id not in known:
                raise SyncIntegrityError(f"missing child block: {child_id}")


@atomic
def publish_snapshot(
    connection: sqlite3.Connection,
    source_id: str,
    revision_before: str,
    revision_after: str,
    blocks: list[dict[str, Any]],
    drafts: list[QuestionDraft],
    parser_version: str,
    dependencies: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Publish one consistent source snapshot and retain all learning facts."""
    if revision_before != revision_after:
        raise SyncIntegrityError("source revision changed during pagination")
    validate_blocks(blocks)
    content_hash = stable_hash(blocks)
    snapshot = connection.execute(
        "SELECT id FROM source_snapshot WHERE source_id=? AND revision=? AND content_hash=? "
        "AND parser_version=? AND dependency_versions_json=?",
        (source_id, revision_after, content_hash, parser_version, json.dumps(dependencies or {}, sort_keys=True)),
    ).fetchone()
    snapshot_id = snapshot["id"] if snapshot else new_id("snapshot")
    now = utc_now_text()
    with transaction(connection):
        if snapshot is None:
            _insert_snapshot(connection, snapshot_id, source_id, revision_after, blocks,
                             dependencies or {}, content_hash, parser_version, now)
        changed = sum(
            _publish_question(connection, draft, snapshot_id, parser_version, now)
            for draft in drafts
        )
        connection.execute(
            "UPDATE source SET last_check_at=?, last_check_success_at=?, "
            "last_content_sync_at=?, last_complete_sync_at=?, last_error=NULL WHERE id=?",
            (now, now, now, now, source_id),
        )
        connection.execute(
            "INSERT INTO source_sync_state VALUES (?,?) ON CONFLICT(source_id) "
            "DO UPDATE SET snapshot_id=excluded.snapshot_id", (source_id, snapshot_id),
        )
    return {"snapshot_id": snapshot_id, "created": snapshot is None, "questions": changed}


def _insert_snapshot(
    connection: sqlite3.Connection,
    snapshot_id: str,
    source_id: str,
    revision: str,
    blocks: list[dict[str, Any]],
    dependencies: dict[str, str],
    content_hash: str,
    parser_version: str,
    now: str,
) -> None:
    connection.execute(
        "INSERT INTO source_snapshot VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
        (
            snapshot_id,
            source_id,
            revision,
            json.dumps(dependencies, sort_keys=True),
            content_hash,
            parser_version,
            json.dumps(blocks, ensure_ascii=False),
            now,
        ),
    )


def _publish_question(
    connection: sqlite3.Connection,
    draft: QuestionDraft,
    snapshot_id: str,
    parser_version: str,
    now: str,
) -> int:
    binding = connection.execute(
        "SELECT question_id,confirmation_status FROM source_binding WHERE source_id=? AND main_anchor_block_id=?",
        (draft.source_id, draft.main_anchor_block_id),
    ).fetchone()
    if binding is None:
        question_id = new_id("question")
        _insert_question_and_binding(connection, question_id, draft, parser_version, now)
    else:
        question_id = binding["question_id"]
    value = asdict(draft)
    if not draft.materials:
        value.pop("materials")
    if not draft.parse_issues:
        value.pop('parse_issues')
    content_hash = stable_hash(value)
    current = connection.execute(
        "SELECT v.id,v.content_hash,v.review_basis_id FROM question q "
        "JOIN question_version v ON v.id=q.current_version_id WHERE q.id=?",
        (question_id,),
    ).fetchone()
    if binding is not None and binding['confirmation_status'] == 'migrated':
        raise SyncIntegrityError('该题已迁移到其他来源，请在候选中明确确认题目归属')
    if connection.execute("SELECT 1 FROM source_binding WHERE question_id=? AND active=1 "
        "AND confirmation_status!='migrated' AND NOT(source_id=? AND main_anchor_block_id=?)",
        (question_id, draft.source_id, draft.main_anchor_block_id)).fetchone():
        raise SyncIntegrityError('该题有多个来源归属，请在候选中明确选择当前来源')
    connection.execute(
        "UPDATE source_binding SET prompt_block_ids_json=?,reference_block_ids_json=?,"
        "parser_version=?,confirmation_status=?,active=1,missing_observation_count=0 "
        "WHERE source_id=? AND main_anchor_block_id=?",
        (json.dumps(draft.prompt_block_ids), json.dumps(draft.reference_block_ids), parser_version,
         draft.confirmation_status, draft.source_id, draft.main_anchor_block_id),
    )
    connection.execute("UPDATE question SET source_status='active' WHERE id=?", (question_id,))
    if current is not None and current["content_hash"] == content_hash:
        return 0
    historical = connection.execute(
        "SELECT id FROM question_version WHERE question_id=? AND content_hash=?", (question_id, content_hash),
    ).fetchone()
    if historical is not None:
        connection.execute("UPDATE question SET current_version_id=? WHERE id=?", (historical[0], question_id))
        return 1
    basis_id = _choose_basis(connection, question_id, current, draft, now)
    version_id = new_id("version")
    connection.execute(
        "INSERT INTO question_version VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            version_id,
            question_id,
            snapshot_id,
            basis_id,
            draft.prompt,
            draft.reference_text,
            draft.category_path,
            draft.material_status,
            content_hash,
            "initial" if current is None else "source content changed",
            now,
        ),
    )
    connection.execute(
        "UPDATE question SET current_version_id=?, source_status='active' WHERE id=?",
        (version_id, question_id),
    )
    connection.execute(
        "INSERT INTO version_resources VALUES (?,?,?)",
        (version_id, json.dumps(draft.reference_block_ids), json.dumps(draft.materials)),
    )
    return 1


def _insert_question_and_binding(
    connection: sqlite3.Connection,
    question_id: str,
    draft: QuestionDraft,
    parser_version: str,
    now: str,
) -> None:
    connection.execute(
        "INSERT INTO question(id, question_type, source_kind, created_at) VALUES (?, ?, ?, ?)",
        (question_id, draft.question_type, draft.source_kind, now),
    )
    connection.execute(
        "INSERT INTO source_binding VALUES (?, ?, ?, ?, ?, ?, '[]', ?, ?, 1, 0)",
        (
            new_id("binding"),
            question_id,
            draft.source_id,
            draft.main_anchor_block_id,
            json.dumps(draft.prompt_block_ids),
            json.dumps(draft.reference_block_ids),
            parser_version,
            draft.confirmation_status,
        ),
    )


def _choose_basis(
    connection: sqlite3.Connection,
    question_id: str,
    current: sqlite3.Row | None,
    draft: QuestionDraft,
    now: str,
) -> str:
    basis = {"prompt": draft.prompt, "reference": draft.reference_text}
    if draft.materials:
        basis["materials"] = [{key: item.get(key) for key in ("block_id", "role", "sha256", "status")}
                              for item in draft.materials]
    basis_hash = stable_hash(basis)
    existing = connection.execute(
        "SELECT id FROM review_basis WHERE question_id=? AND basis_hash=?",
        (question_id, basis_hash),
    ).fetchone()
    if existing is not None:
        return existing["id"]
    basis_id = new_id("basis")
    connection.execute(
        "INSERT INTO review_basis VALUES (?, ?, ?, ?)",
        (basis_id, question_id, basis_hash, now),
    )
    return basis_id
