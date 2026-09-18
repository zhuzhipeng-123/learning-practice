import json

from app.parsers.docx import HEADING_TYPES, block_text
from app.services.model_budget import split_inputs
from app.services.model_jobs import ModelJobError


def coverage(connection, source_id):
    row = connection.execute("SELECT ss.blocks_json FROM source_sync_state st JOIN source_snapshot ss ON ss.id=st.snapshot_id "
                             "WHERE st.source_id=?", (source_id,)).fetchone()
    if not row:
        return []
    blocks = json.loads(row[0])
    bindings = {r["main_anchor_block_id"]: dict(r) for r in connection.execute(
        "SELECT b.main_anchor_block_id,v.material_status,q.id FROM source_binding b JOIN question q ON q.id=b.question_id "
        "JOIN question_version v ON v.id=q.current_version_id WHERE b.source_id=? AND b.active=1", (source_id,))}
    pending = {r[0] for r in connection.execute("SELECT main_anchor_block_id FROM parser_candidate WHERE source_id=? AND status='pending'", (source_id,))}
    paths, items = {}, []
    for index, block in enumerate(blocks):
        level = HEADING_TYPES.get(block.get("block_type"))
        if not level:
            continue
        paths = {k:v for k,v in paths.items() if k < level}
        paths[level] = block_text(block)
        end = next((i for i in range(index+1, len(blocks)) if blocks[i].get("block_type") in HEADING_TYPES), len(blocks))
        body = blocks[index+1:end]
        anchor = block["block_id"]
        state = "已入库" if anchor in bindings else "待确认" if anchor in pending else "未识别 / 待核验"
        if anchor in bindings and bindings[anchor]["material_status"] not in {"complete", "verified", "text_complete"}:
            state = "素材未就绪"
        if end < len(blocks) and HEADING_TYPES[blocks[end]["block_type"]] > level and not any(block_text(b) or "image" in b for b in body):
            state = "目录"
        items.append({"anchor": anchor, "path": " > ".join(paths.values()), "title": block_text(block), "state": state,
                      "images": sum("image" in b for b in body), "text_blocks": sum(bool(block_text(b)) for b in body)})
    return items


def parsing_context(connection, source_id, limit=12, offset=0):
    candidates = [_candidate_context(json.loads(row[0])) for row in connection.execute(
        "SELECT draft_json FROM parser_candidate WHERE source_id=? AND status='pending' ORDER BY rowid LIMIT ? OFFSET ?",
        (source_id, limit, offset))]
    if not candidates:
        raise ModelJobError("目前没有待分析的候选")
    return {"source_id": source_id, "candidates": candidates}


def _candidate_context(draft):
    reference = draft['reference_text'] or ''
    return {'anchor_id': draft['main_anchor_block_id'], 'title': draft['prompt'],
            'category': draft['category_path'], 'reference': reference,
            'reference_length': len(reference), 'reference_truncated': False,
            'material_status': draft.get('material_status', 'unknown'),
            'image_count': sum(item['kind'] == 'media' for item in draft.get('materials', []))}


def parsing_batches(connection, source_id, change_note=''):
    candidates = [_candidate_context(json.loads(row[0])) for row in connection.execute(
        "SELECT draft_json FROM parser_candidate WHERE source_id=? AND status='pending' ORDER BY rowid", (source_id,))]
    return split_inputs({'source_id': source_id, 'user_change_description': change_note},
                        'candidates', candidates, max_items=4)


def validate_suggestions(context, payload):
    if context.get('mode') == 'change_review':
        if (not isinstance(payload, dict) or not isinstance(payload.get('review'), str) or not payload['review'].strip()
                or not isinstance(payload.get('unresolved'), list) or any(not isinstance(item, str) for item in payload['unresolved'])):
            raise ModelJobError('改动复核格式不正确')
        return
    allowed = {item["anchor_id"] for item in context["candidates"]}
    if not isinstance(payload, dict) or not isinstance(payload.get("suggestions"), list) or not payload["suggestions"]:
        raise ModelJobError("边界建议格式不正确")
    seen = set()
    for item in payload["suggestions"]:
        # Long opaque IDs are sometimes copied with whitespace. Only accept exact
        # membership after whitespace removal; never fuzzy-match another anchor.
        if isinstance(item, dict) and isinstance(item.get('anchor_id'), str):
            compact = ''.join(item['anchor_id'].split())
            if compact in allowed:
                item['anchor_id'] = compact
        if (not isinstance(item, dict) or item.get("anchor_id") not in allowed or item["anchor_id"] in seen
            or item.get("decision") not in {"single", "split", "note", "missing"}
            or not isinstance(item.get("reason"), str) or not isinstance(item.get("parts"), list)
            or any(not isinstance(part, str) for part in item["parts"])):
            raise ModelJobError("边界建议引用了未知题目或格式不正确")
        seen.add(item["anchor_id"])
    if seen != allowed:
        raise ModelJobError("模型遗漏了部分候选，本批分析不完整，请重试")
