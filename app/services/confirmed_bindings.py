"""Rebuild confirmed boundaries from the current source, never from fixture text."""

import json
from dataclasses import replace
from difflib import SequenceMatcher

from app.domain import QuestionDraft
from app.parsers.docx import HEADING_TYPES, block_text, is_theory_heading


def rebuild_bindings(connection, source, blocks, parsed):
    by_id = {block["block_id"]: block for block in blocks}
    discovered = {draft.main_anchor_block_id: draft for draft in [*parsed.published, *parsed.candidates]}
    known = connection.execute(
        "SELECT b.*,v.category_path FROM source_binding b JOIN question q ON q.id=b.question_id "
        "JOIN question_version v ON v.id=q.current_version_id WHERE b.source_id=? "
        "AND b.confirmation_status!='migrated'", (source["id"],),
    ).fetchall()
    confirmed, invalid = [], set()
    for binding in known:
        anchor = binding["main_anchor_block_id"]
        if anchor not in by_id:
            continue
        if source['question_type'] == 'theory' and not is_theory_heading(by_id[anchor]):
            continue
        current = discovered.get(anchor)
        # Recompute clear source boundaries so appended/deleted reference blocks are reflected.
        # Ambiguous/manual spans still follow their explicitly confirmed anchors below.
        if current and current.confirmation_status == "confirmed":
            confirmed.append(current)
            continue
        prompt_ids = json.loads(binding["prompt_block_ids_json"])
        reference_ids = json.loads(binding["reference_block_ids_json"])
        if not all(key in by_id for key in [*prompt_ids, *reference_ids]):
            invalid.add(anchor)
            continue
        # Include inserted blocks inside the confirmed reference span, not arbitrary following notes.
        if len(reference_ids) > 1:
            indexes = [index for index, block in enumerate(blocks) if block["block_id"] in reference_ids]
            span = blocks[min(indexes):max(indexes) + 1]
            if any(block.get("block_type") in HEADING_TYPES for block in span):
                invalid.add(anchor)
                continue
            reference_ids = [block["block_id"] for block in span]
        prompt = "\n".join(filter(None, (block_text(by_id[key]) for key in prompt_ids)))
        reference = "\n".join(filter(None, (block_text(by_id[key]) for key in reference_ids)))
        category = discovered[anchor].category_path if anchor in discovered else _category(blocks, anchor)
        confirmed.append(QuestionDraft(
            source["id"], source["document_id"], source["question_type"], "feishu", anchor,
            tuple(prompt_ids), tuple(reference_ids), category or binding["category_path"],
            prompt or block_text(by_id[anchor]), reference or None, "complete",
        ))
    return confirmed, invalid


def _category(blocks, anchor):
    path = {}
    for block in blocks:
        level = HEADING_TYPES.get(block.get("block_type"))
        if level:
            path = {key: value for key, value in path.items() if key < level}
            path[level] = block_text(block)
        if block["block_id"] == anchor:
            break
    return " > ".join(value for _, value in sorted(path.items()) if value)


def identity_matches(connection, draft):
    """Similarity only raises an ambiguity; it never merges identities automatically."""
    matches = []
    rows = connection.execute(
        "SELECT q.id,v.prompt,v.reference_text FROM question q JOIN question_version v "
        "ON v.id=q.current_version_id WHERE q.source_kind='feishu' AND q.question_type=? "
        "AND NOT EXISTS (SELECT 1 FROM source_binding b WHERE b.question_id=q.id "
        "AND b.source_id=? AND b.main_anchor_block_id=?)",
        (draft.question_type, draft.source_id, draft.main_anchor_block_id),
    )
    text = (draft.prompt + "\n" + (draft.reference_text or "")).strip().casefold()
    for row in rows:
        other = (row["prompt"] + "\n" + (row["reference_text"] or "")).strip().casefold()
        if text and (draft.prompt.casefold() == row["prompt"].casefold()
                     or SequenceMatcher(None, text, other).ratio() >= 0.8):
            matches.append(row["id"])
    return matches


def select_drafts(connection, source, blocks, parsed):
    confirmed, invalid = rebuild_bindings(connection, source, blocks, parsed)
    bound = {draft.main_anchor_block_id for draft in confirmed}
    candidates = [draft for draft in parsed.candidates if draft.main_anchor_block_id not in bound]
    published = list(confirmed)
    for draft in parsed.published:
        if draft.main_anchor_block_id in bound:
            continue
        if draft.main_anchor_block_id in invalid or identity_matches(connection, draft):
            candidates.append(replace(draft, confirmation_status="pending"))
        else:
            published.append(draft)
    return published, candidates, invalid
