import re
from dataclasses import dataclass, replace
from typing import Any

from app.domain import QuestionDraft
from app.parsers.block_tree import ordered_blocks

HEADING_TYPES = {3: 1, 4: 2, 5: 3, 6: 4, 7: 5, 8: 6}
LAYOUT_TYPES = {1, 2, 19, 22, 24, 25, 31, 32, 34}
QUESTION_IMAGE_SUFFIX = re.compile(r"-图片\s*$")
QUESTION_PREFIX = re.compile(r"^\s*问题\s*[:：]\s*")


def heading_level(block):
    if block.get('_content_container') or block.get('_reference_container'):
        return None
    return HEADING_TYPES.get(block.get('block_type'))


@dataclass(frozen=True)
class HeadingClassification:
    roles: dict[str, str]


@dataclass(frozen=True)
class ParseResult:
    published: list[QuestionDraft]
    candidates: list[QuestionDraft]
    covered_block_ids: set[str]
    unsupported_block_ids: set[str]


def classify_theory_headings(blocks: list[dict[str, Any]]) -> HeadingClassification:
    """Use plain H3 questions and explicit ``问题：`` markers in mixed trees."""
    blocks = ordered_blocks(blocks)
    roles: dict[str, str] = {}
    stack: list[tuple[int, str]] = []
    for index, block in enumerate(blocks):
        level = heading_level(block)
        if level is None or not block_text(block):
            continue
        while stack and stack[-1][0] >= level:
            stack.pop()
        anchor = str(block['block_id'])
        title = block_text(block)
        if level <= 2:
            role = 'module'
        elif _marked_question(title):
            role = 'question'
        else:
            parent_role = roles.get(stack[-1][1]) if stack else None
            if parent_role in {'question', 'ambiguous_question'}:
                role = 'reference_heading'
            elif _has_marked_descendant(blocks, index):
                role = 'module'
            elif level == 3:
                role = 'ambiguous_question' if _has_deeper_heading(blocks, index) else 'question'
            else:
                role = 'reference_heading'
        roles[anchor] = role
        stack.append((level, anchor))
    return HeadingClassification(roles)


def parse_docx_blocks(
    source_id: str,
    document_id: str,
    question_type: str,
    blocks: list[dict[str, Any]],
    category_prefix: str = "",
) -> ParseResult:
    blocks = ordered_blocks(blocks)
    classification = classify_theory_headings(blocks) if question_type == 'theory' else None
    paths: dict[int, str] = {0: category_prefix} if category_prefix else {}
    published: list[QuestionDraft] = []
    candidates: list[QuestionDraft] = []
    covered: set[str] = set()
    unsupported: set[str] = set()
    for index, block in enumerate(blocks):
        level = heading_level(block)
        text = block_text(block)
        if level is not None:
            _update_path(paths, level, _visible_title(text, question_type))
            anchor = str(block['block_id'])
            role = classification.roles.get(anchor) if classification else None
            if question_type == 'code' and level == 3:
                draft, used = _parse_code_section(source_id, document_id, paths, blocks, index)
                if draft is not None:
                    (candidates if draft.confirmation_status == 'pending' else published).append(draft)
                    covered.update(used)
            elif question_type == 'theory' and role in {'question', 'ambiguous_question'}:
                draft, used = _parse_theory_candidate(
                    source_id, document_id, {key: value for key, value in paths.items() if key < level},
                    blocks, index, classification,
                )
                if role == 'ambiguous_question':
                    draft = replace(draft, confirmation_status='pending', material_status='candidate_requires_user_confirmation')
                (published if draft.confirmation_status == 'confirmed' else candidates).append(draft)
                covered.update(used)
            continue
        if "sheet" not in block and block.get("block_type") not in {*HEADING_TYPES, 1, 2, 12, 13, 14, 15, 19, 22, 24, 25, 27, 31, 32, 34}:
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


def _visible_title(title: str, question_type: str) -> str:
    if question_type == 'code':
        return QUESTION_IMAGE_SUFFIX.sub('', title).rstrip()
    return QUESTION_PREFIX.sub('', title).strip()


def _update_path(paths: dict[int, str], level: int, title: str) -> None:
    paths[level] = title
    for deeper in range(level + 1, 7):
        paths.pop(deeper, None)


def _category_path(paths: dict[int, str]) -> str:
    return " > ".join(paths[level] for level in sorted(paths) if paths[level])


def _parse_code_section(source_id, document_id, paths, blocks, start):
    section = _question_section(blocks, start, None)
    if len(section) == 1:
        return None, set()
    raw_title = block_text(section[0])
    marked = bool(QUESTION_IMAGE_SUFFIX.search(raw_title))
    prompt_images, image_issues = _code_prompt_image(section[1:], marked)
    prompt_image_ids = {str(block['block_id']) for block in prompt_images}
    references = [block for block in section[1:] if str(block['block_id']) not in prompt_image_ids
                  and (block_text(block) or block.get('block_type') in {14, 27, 31} or 'sheet' in block)]
    issues = _unsupported_ids(section[1:])
    first_content = next((block for block in section[1:] if _meaningful(block)), None)
    unmarked_leading_image = bool(first_content and first_content.get('block_type') == 27 and not marked)
    visible_title = _visible_title(raw_title, 'code')
    compound_title = any(marker in visible_title for marker in ('&', '＆', '以及', '与', '/'))
    generic_title = visible_title in {'定义', '原理', '基础知识', '其他', '其他知识点'}
    confirmation = 'pending' if image_issues or unmarked_leading_image or issues or compound_title or generic_title else 'confirmed'
    structured = any(block.get('block_type') == 31 or 'sheet' in block for block in references)
    status = ('incomplete_reference' if issues else 'candidate_requires_user_confirmation'
              if confirmation == 'pending' else 'media_required' if structured else 'complete')
    prompt_ids = (str(section[0]['block_id']), *(str(block['block_id']) for block in prompt_images))
    reference_ids = tuple(str(block['block_id']) for block in references)
    draft = QuestionDraft(
        source_id, document_id, 'code', 'feishu', str(section[0]['block_id']), prompt_ids, reference_ids,
        _category_path(paths), visible_title,
        "\n".join(filter(None, (block_text(block) for block in references))) or None,
        status, confirmation_status=confirmation, parse_issues=(*issues, *image_issues),
    )
    return draft, {str(block['block_id']) for block in section}


def _code_prompt_image(blocks, marked):
    if not marked:
        return [], []
    content = [block for block in blocks if _meaningful(block)]
    if not content or content[0].get('block_type') != 27:
        return [], ['marked_heading_missing_prompt_image']
    extra_images = [block for block in content[1:] if block.get('block_type') == 27]
    issues = ['multiple_code_images_require_confirmation'] if extra_images else []
    return [content[0]], issues


def _parse_theory_candidate(source_id, document_id, paths, blocks, index, classification):
    question = blocks[index]
    following = _question_section(blocks, index, classification)[1:]
    reference_ids = tuple(str(block['block_id']) for block in following if _meaningful(block) or heading_level(block))
    reference_text = "\n".join(filter(None, (block_text(block) for block in following)))
    issues = _unsupported_ids(following)
    structured = any(block.get('block_type') in {27, 31} or 'sheet' in block for block in following)
    has_reference = any(_meaningful(block) or heading_level(block) for block in following)
    clear = bool(paths and has_reference and len(block_text(question)) <= 300 and not issues)
    material_status = ('incomplete_reference' if issues else 'media_required' if structured else
                       'complete' if clear else 'candidate_requires_user_confirmation')
    draft = QuestionDraft(
        source_id, document_id, 'theory', 'feishu', str(question['block_id']),
        (str(question['block_id']),), reference_ids, _category_path(paths), _visible_title(block_text(question), 'theory'),
        reference_text or None, material_status,
        confirmation_status='confirmed' if clear else 'pending', parse_issues=tuple(issues),
    )
    return draft, {str(question['block_id']), *reference_ids}


def _question_section(blocks, start, classification):
    result = [blocks[start]]
    start_level = heading_level(blocks[start]) or 6
    for block in blocks[start + 1:]:
        level = heading_level(block)
        if level is not None:
            role = classification.roles.get(str(block['block_id'])) if classification else None
            if level <= start_level or role in {'question', 'ambiguous_question'}:
                break
        result.append(block)
    return result


def _has_deeper_heading(blocks, index):
    level = heading_level(blocks[index]) or 6
    for block in blocks[index + 1:]:
        nested_level = heading_level(block)
        if nested_level is not None:
            return nested_level > level
    return False


def _has_marked_descendant(blocks, index):
    level = heading_level(blocks[index]) or 6
    for block in blocks[index + 1:]:
        nested_level = heading_level(block)
        if nested_level is None:
            continue
        if nested_level <= level:
            return False
        if _marked_question(block_text(block)):
            return True
    return False


def _marked_question(title):
    return bool(QUESTION_PREFIX.match(title))


def _meaningful(block):
    return bool(block_text(block) or block.get('block_type') in {14, 27, 31} or 'sheet' in block)


def _unsupported_ids(blocks):
    issues = []
    meaningful = 0
    for block in blocks:
        if _meaningful(block):
            meaningful += 1
            if meaningful > 200 and 'truncated_after_200_blocks' not in issues:
                issues.append('truncated_after_200_blocks')
        kind = block.get('block_type')
        unsupported = not _meaningful(block) and heading_level(block) is None and kind not in LAYOUT_TYPES
        if unsupported:
            issues.append(str(block.get('block_id')))
    return issues
