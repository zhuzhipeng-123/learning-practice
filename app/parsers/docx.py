from dataclasses import dataclass, replace
from typing import Any

from app.domain import QuestionDraft
from app.parsers.block_tree import ordered_blocks

HEADING_TYPES = {3: 1, 4: 2, 5: 3, 6: 4, 7: 5, 8: 6}


def is_theory_heading(block):
    return not block.get('_reference_container') and HEADING_TYPES.get(block.get('block_type')) in {1, 2, 3} and bool(block_text(block))


@dataclass(frozen=True)
class ParseResult:
    published: list[QuestionDraft]
    candidates: list[QuestionDraft]
    covered_block_ids: set[str]
    unsupported_block_ids: set[str]


def parse_docx_blocks(
    source_id: str,
    document_id: str,
    question_type: str,
    blocks: list[dict[str, Any]],
    category_prefix: str = "",
) -> ParseResult:
    """Produce conservative candidates; only clear code sections auto-publish."""
    blocks = ordered_blocks(blocks)
    paths: dict[int, str] = {0: category_prefix} if category_prefix else {}
    published: list[QuestionDraft] = []
    candidates: list[QuestionDraft] = []
    covered: set[str] = set()
    unsupported: set[str] = set()
    for index, block in enumerate(blocks):
        level = None if block.get('_reference_container') else HEADING_TYPES.get(block.get("block_type"))
        text = block_text(block)
        if level is not None:
            _update_path(paths, level, text)
            if question_type == "code":
                draft, used = _parse_code_section(source_id, document_id, paths, blocks, index)
                if draft is not None:
                    if _requires_code_confirmation(draft, blocks, index):
                        candidates.append(replace(draft, confirmation_status="pending"))
                    else:
                        published.append(draft)
                    covered.update(used)
            if question_type == "theory" and is_theory_heading(block):
                # A heading immediately containing other questions is a module.
                if not _following_content(blocks, index) and not _reference_issues(blocks, index):
                    continue
                draft, used = _parse_theory_candidate(source_id, document_id, {k:v for k,v in paths.items() if k < level}, blocks, index)
                (published if draft.confirmation_status == "confirmed" else candidates).append(draft)
                covered.update(used)
            continue
        if "sheet" not in block and block.get("block_type") not in {*HEADING_TYPES, 1, 2, 12, 13, 14, 15, 19, 22, 24, 25, 27, 34}:
            unsupported.add(str(block.get("block_id")))
    return ParseResult(published, candidates, covered, unsupported)


def block_text(block: dict[str, Any]) -> str:
    for key in ("text", "code", "bullet", "ordered", "quote", "heading1", "heading2", "heading3", "heading4", "heading5", "heading6"):
        content = block.get(key)
        if not isinstance(content, dict):
            continue
        pieces = []
        for element in content.get("elements", []):
            text_run = element.get("text_run") or element.get("equation") or {}
            pieces.append(str(text_run.get("content") or ""))
        return "".join(pieces).strip()
    return ""


def _update_path(paths: dict[int, str], level: int, title: str) -> None:
    paths[level] = title
    for deeper in range(level + 1, 7):
        paths.pop(deeper, None)


def _category_path(paths: dict[int, str]) -> str:
    return " > ".join(paths[level] for level in sorted(paths) if paths[level])


def _parse_code_section(
    source_id: str,
    document_id: str,
    paths: dict[int, str],
    blocks: list[dict[str, Any]],
    start: int,
) -> tuple[QuestionDraft | None, set[str]]:
    section = _until_heading(blocks, start)
    images = [block for block in section if block.get("block_type") == 27]
    references = [
        block for block in section[1:] if block_text(block) or block.get("block_type") == 14 or "sheet" in block
    ]
    if not images and not references:
        return None, set()
    issues = [str(block.get('block_id')) for block in section[1:]
              if block not in references and block not in images
              and block.get('block_type') not in {1, 2, 19, 22, 24, 25, 34}]
    title = block_text(section[0])
    prompt = (_image_alt(images[0]) if images else "") or title
    prompt_ids = (str(section[0]["block_id"]), *([str(images[0]["block_id"])] if images else []))
    reference_ids = tuple(str(block["block_id"]) for block in section[1:] if block in references or block in images[1:])
    draft = QuestionDraft(
        source_id=source_id,
        document_id=document_id,
        question_type="code",
        source_kind="feishu",
        main_anchor_block_id=str(section[0]["block_id"]),
        prompt_block_ids=prompt_ids,
        reference_block_ids=reference_ids,
        category_path=_category_path(paths),
        prompt=prompt,
        reference_text="\n".join(filter(None, (block_text(block) for block in references))),
        material_status="incomplete_reference" if issues else "media_required" if images else "candidate_requires_user_confirmation",
        parse_issues=tuple(issues),
    )
    used = {str(block["block_id"]) for block in section}
    return draft, used


def _requires_code_confirmation(
    draft: QuestionDraft,
    blocks: list[dict[str, Any]],
    start: int,
) -> bool:
    title = block_text(blocks[start])
    section = _until_heading(blocks, start)
    image_count = sum(block.get("block_type") == 27 for block in section)
    code_count = sum(block.get("block_type") == 14 for block in section)
    compound_title = any(marker in title for marker in ("&", "＆", "以及", "与", "/"))
    return compound_title or image_count != 1 or code_count > 1 or title in {"定义", "原理", "基础知识", "其他", "其他知识点"}


def _parse_theory_candidate(
    source_id: str,
    document_id: str,
    paths: dict[int, str],
    blocks: list[dict[str, Any]],
    index: int,
) -> tuple[QuestionDraft, set[str]]:
    question = blocks[index]
    following = _following_content(blocks, index)
    reference_ids = tuple(str(block["block_id"]) for block in following)
    reference_text = "\n".join(filter(None, (block_text(block) for block in following)))
    issues = _reference_issues(blocks, index)
    clear = bool(paths and reference_text and len(block_text(question)) <= 300 and not issues)
    draft = QuestionDraft(
        source_id=source_id,
        document_id=document_id,
        question_type="theory",
        source_kind="feishu",
        main_anchor_block_id=str(question["block_id"]),
        prompt_block_ids=(str(question["block_id"]),),
        reference_block_ids=reference_ids,
        category_path=_category_path(paths),
        prompt=block_text(question),
        reference_text=reference_text or None,
        material_status="incomplete_reference" if issues else "complete" if clear else "candidate_requires_user_confirmation",
        confirmation_status="confirmed" if clear else "pending",
        parse_issues=tuple(issues),
    )
    return draft, {str(question["block_id"]), *reference_ids}


def _until_heading(blocks: list[dict[str, Any]], start: int) -> list[dict[str, Any]]:
    result = [blocks[start]]
    for block in blocks[start + 1 :]:
        if block.get("block_type") in HEADING_TYPES and not block.get('_reference_container'):
            break
        result.append(block)
    return result


def _following_content(
    blocks: list[dict[str, Any]],
    start: int,
) -> list[dict[str, Any]]:
    result = []
    for block in blocks[start + 1 :]:
        if is_theory_heading(block):
            break
        if block_text(block) or block.get("block_type") in {14, 27} or "sheet" in block:
            result.append(block)
        if len(result) >= 200:
            break
    return result


def _reference_issues(blocks, start):
    """Retain evidence of skipped or truncated answer content in the candidate."""
    count = 0
    issues = []
    for block in blocks[start + 1:]:
        if is_theory_heading(block):
            break
        kind = block.get('block_type')
        if block_text(block) or kind in {14, 27} or 'sheet' in block:
            count += 1
        elif kind not in {1, 2, 19, 22, 24, 25, 34}:
            issues.append(str(block.get('block_id')))
        if count > 200:
            issues.append('truncated_after_200_blocks')
            break
    return issues


def _image_alt(block: dict[str, Any]) -> str:
    image = block.get("image") or {}
    return str(image.get("alt") or "")
