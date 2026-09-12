import json
from datetime import UTC, datetime

from app.domain import QuestionDraft
from app.services.sync import _publish_question
from app.storage.ids import new_id
from app.storage.transactions import atomic


class CandidateError(RuntimeError):
    """A parser candidate is outdated or requires an identity decision."""


@atomic
def approve_candidate(connection, candidate_id: str, existing_question_id: str | None = None,
                      as_new: bool = False) -> str:
    resolution = connection.execute("SELECT question_id FROM candidate_resolution WHERE candidate_id=?",
                                    (candidate_id,)).fetchone()
    if resolution:
        if existing_question_id and resolution[0] != existing_question_id:
            raise CandidateError("candidate was already resolved differently")
        return resolution[0]
    row = connection.execute(
        "SELECT c.*,ss.parser_version FROM parser_candidate c JOIN source_snapshot ss ON ss.id=c.snapshot_id "
        "JOIN source_sync_state state ON state.source_id=c.source_id AND state.snapshot_id=c.snapshot_id "
        "WHERE c.id=? AND c.status='pending'", (candidate_id,),
    ).fetchone()
    if row is None:
        raise CandidateError("candidate is outdated or already decided; refresh the source page")
    value = json.loads(row["draft_json"])
    matches = value.get("match_question_ids", [])
    if matches and existing_question_id is None and not as_new:
        raise CandidateError("choose an existing question to preserve history, or explicitly create a new one")
    if existing_question_id is not None:
        _bind_existing(connection, row, value, existing_question_id)
    draft = _draft_from_json(row["source_id"], row["draft_json"])
    now = datetime.now(UTC).isoformat()
    _publish_question(connection, draft, row["snapshot_id"], row["parser_version"], now)
    question_id = connection.execute(
        "SELECT question_id FROM source_binding WHERE source_id=? AND main_anchor_block_id=?",
        (row["source_id"], row["main_anchor_block_id"]),
    ).fetchone()[0]
    connection.execute("UPDATE parser_candidate SET status='approved',decided_at=? WHERE id=?", (now, candidate_id))
    connection.execute("INSERT INTO candidate_resolution VALUES (?,?)", (candidate_id, question_id))
    return question_id


def _bind_existing(connection, row, value, question_id):
    question = connection.execute("SELECT question_type,source_kind FROM question WHERE id=?", (question_id,)).fetchone()
    if question is None or question[0] != value["question_type"] or question[1] != "feishu":
        raise CandidateError("existing question must be a Feishu original of the same type")
    current = connection.execute(
        "SELECT question_id FROM source_binding WHERE source_id=? AND main_anchor_block_id=?",
        (row["source_id"], row["main_anchor_block_id"]),
    ).fetchone()
    if current and current[0] != question_id:
        raise CandidateError("anchor already belongs to a different question")
    connection.execute("UPDATE source_binding SET active=0,confirmation_status='migrated' WHERE question_id=?",
                       (question_id,))
    connection.execute(
        "INSERT OR IGNORE INTO source_binding VALUES (?,?,?,?,?,?,'[]',?,'confirmed',1,0)",
        (new_id("binding"), question_id, row["source_id"], row["main_anchor_block_id"],
         json.dumps(value["prompt_block_ids"]), json.dumps(value["reference_block_ids"]), row["parser_version"]),
    )


@atomic
def reject_candidate(connection, candidate_id: str) -> None:
    changed = connection.execute(
        "UPDATE parser_candidate SET status='rejected',decided_at=? WHERE id=? AND status='pending'",
        (datetime.now(UTC).isoformat(), candidate_id),
    ).rowcount
    if changed != 1:
        raise CandidateError("pending candidate was not found")


def _draft_from_json(source_id: str, draft_json: str) -> QuestionDraft:
    value = json.loads(draft_json)
    status = value["material_status"]
    if status == "candidate_requires_user_confirmation":
        status = "complete"
    return QuestionDraft(source_id, value["document_id"], value["question_type"], "feishu",
                         value["main_anchor_block_id"], tuple(value["prompt_block_ids"]),
                         tuple(value["reference_block_ids"]), value["category_path"], value["prompt"],
                         value["reference_text"], status, "confirmed", tuple(value.get("materials", [])))
