import json
from dataclasses import asdict
from datetime import UTC, datetime

from app.adapters.docx_reader import DocxReader
from app.adapters.lark_cli import LarkCliClient
from app.parsers.docx import parse_docx_blocks
from app.services.alignment_report import inventory_changes, source_inventory
from app.services.confirmed_bindings import identity_matches, select_drafts
from app.services.materials import apply_materials, collect_materials, store_dependencies
from app.services.source_state import observe_missing_questions, record_source_failure
from app.services.sync import SyncIntegrityError, publish_snapshot, validate_blocks
from app.services.wiki_sources import source_prefix
from app.storage.ids import new_id
from app.storage.transactions import transaction

PARSER_VERSION = "wiki-heading-question-boundaries-v4"


class SyncSupersededError(SyncIntegrityError):
    """A newer sync has already published for this source."""


def sync_registered_source(connection, source_id: str, client=None):
    source = connection.execute("SELECT * FROM source WHERE id=?", (source_id,)).fetchone()
    if source is None or not source["enabled"]:
        raise ValueError("enabled source was not found")
    started_at = datetime.now(UTC).isoformat()
    run_id = new_id("sync")
    with transaction(connection):
        connection.execute(
            "INSERT INTO sync_run(id,source_id,started_at,status) VALUES (?,?,?,'running')",
            (run_id, source_id, started_at),
        )
    try:
        before = source_inventory(connection, source_id)
        remote = client or LarkCliClient()
        result = DocxReader(remote, source["identity"]).read_latest(source["document_id"])
        validate_blocks(result.blocks)
        prefix = source_prefix(connection, source_id)
        parsed = parse_docx_blocks(source_id, source["document_id"], source["question_type"], result.blocks, prefix)
        drafts, candidates, invalid = select_drafts(connection, source, result.blocks, parsed)
        materials = collect_materials(connection, source, result.blocks, [*drafts, *candidates], remote)
        drafts = [apply_materials(draft, materials) for draft in drafts]
        candidates = [apply_materials(draft, materials) for draft in candidates]
        ambiguous = bool(invalid or parsed.unsupported_block_ids or candidates)
        incomplete = any(item["status"] != "complete" for item in materials.values())
        with transaction(connection):
            if connection.execute("SELECT 1 FROM sync_run WHERE source_id=? AND status IN ('complete','partial') "
                                  "AND rowid>(SELECT rowid FROM sync_run WHERE id=?)", (source_id, run_id)).fetchone():
                raise SyncSupersededError("a newer sync has already published; retry if needed")
            published = publish_snapshot(connection, source_id, result.revision, result.revision,
                                         result.blocks, drafts, PARSER_VERSION,
                                         {key: item.get("sha256", "missing") for key, item in materials.items()})
            store_dependencies(connection, source_id, materials)
            _store_candidates(connection, source_id, published["snapshot_id"], candidates)
            pending_count = connection.execute("SELECT COUNT(*) FROM parser_candidate WHERE source_id=? "
                                               "AND snapshot_id=? AND status='pending'", (source_id, published["snapshot_id"])).fetchone()[0]
            ambiguous = bool(invalid or parsed.unsupported_block_ids or pending_count)
            missing = observe_missing_questions(connection, source_id,
                {block["block_id"] for block in result.blocks}, True,
                ambiguous or incomplete, True)
            for anchor in invalid:
                connection.execute("UPDATE question SET source_status='missing_pending' WHERE id IN "
                                   "(SELECT question_id FROM source_binding WHERE source_id=? AND main_anchor_block_id=?)",
                                   (source_id, anchor))
            summary = {**published, "revision": result.revision, "candidate_count": pending_count,
                       "published_count": connection.execute("SELECT COUNT(*) FROM question q WHERE q.source_status='active' AND EXISTS "
                            "(SELECT 1 FROM source_binding b WHERE b.question_id=q.id AND b.source_id=?)", (source_id,)).fetchone()[0],
                       "block_count": len(result.blocks),
                       "changes": inventory_changes(before, source_inventory(connection, source_id)),
                       "unsupported_block_count": len(parsed.unsupported_block_ids), "missing": missing,
                       "material_failures": sum(item["status"] != "complete" for item in materials.values()),
                       "material_errors": sorted({item["error"] for item in materials.values() if "error" in item}),
                       "boundary_failures": sorted(invalid)}
            partial = ambiguous or incomplete
            summary["partial"] = partial
            connection.execute(
                "UPDATE sync_run SET finished_at=?,status=?,revision_before=?,revision_after=?,"
                "page_count=?,block_count=?,complete=?,summary_json=? WHERE id=?",
                (datetime.now(UTC).isoformat(), "partial" if partial else "complete", result.revision,
                 result.revision, result.page_count, len(result.blocks), int(not partial), json.dumps(summary), run_id),
            )
            if partial:
                connection.execute("UPDATE source SET last_complete_sync_at=?,last_error=? WHERE id=?",
                                   (source["last_complete_sync_at"], "部分内容需要确认或素材读取失败", source_id))
        return {"run_id": run_id, **summary, "page_count": result.page_count}
    except Exception as error:
        with transaction(connection):
            message = f"{type(error).__name__}: {error}"
            connection.execute("UPDATE sync_run SET finished_at=?,status='failed',error=? WHERE id=?",
                               (datetime.now(UTC).isoformat(), message, run_id))
            if isinstance(error, SyncSupersededError):
                connection.execute("UPDATE sync_run SET status='superseded' WHERE id=?", (run_id,))
            else:
                record_source_failure(connection, source_id, message, datetime.now(UTC))
        raise


def _store_candidates(connection, source_id, snapshot_id, candidates):
    connection.execute(
        "UPDATE parser_candidate SET status='superseded' WHERE source_id=? "
        "AND snapshot_id!=? AND status='pending'", (source_id, snapshot_id),
    )
    for draft in candidates:
        value = asdict(draft)
        value["match_question_ids"] = identity_matches(connection, draft)
        status, decided = 'pending', None
        rejected = connection.execute("SELECT draft_json,decided_at FROM parser_candidate WHERE source_id=? AND main_anchor_block_id=? AND status='rejected' ORDER BY rowid DESC", (source_id, draft.main_anchor_block_id)).fetchall()
        fields = ('prompt', 'reference_text', 'prompt_block_ids', 'reference_block_ids', 'materials')
        normalized = json.loads(json.dumps(value))
        for previous in rejected:
            old = json.loads(previous['draft_json'])
            if all(old.get(field) == normalized.get(field) for field in fields):
                status, decided = 'rejected', previous['decided_at']
                break
        connection.execute(
            "INSERT OR IGNORE INTO parser_candidate VALUES (?,?,?,?,?,?,?)",
            (new_id("candidate"), source_id, snapshot_id, draft.main_anchor_block_id,
             json.dumps(value, ensure_ascii=False), status, decided),
        )
