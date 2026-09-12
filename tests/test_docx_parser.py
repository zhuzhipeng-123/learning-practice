from app.parsers.docx import parse_docx_blocks


def text_block(block_id: str, content: str) -> dict:
    return {
        "block_id": block_id,
        "block_type": 2,
        "text": {"elements": [{"text_run": {"content": content}}]},
    }


def heading(block_id: str, level: int, content: str) -> dict:
    return {
        "block_id": block_id,
        "block_type": level + 2,
        f"heading{level}": {"elements": [{"text_run": {"content": content}}]},
    }


def test_code_heading_with_image_and_reference_is_published() -> None:
    blocks = [
        heading("category", 1, "hot100"),
        heading("question", 3, "移动0"),
        {"block_id": "image", "block_type": 27, "image": {"alt": "题干"}},
        text_block("answer", "双指针"),
        heading("next", 3, "下一题"),
    ]

    result = parse_docx_blocks("source-code", "doc", "code", blocks)

    assert len(result.published) == 1
    assert result.published[0].main_anchor_block_id == "question"
    assert result.published[0].category_path == "hot100 > 移动0"
    assert result.published[0].prompt == "题干"


def test_compound_code_heading_requires_confirmation() -> None:
    blocks = [
        heading("question", 3, "链表排序&合并k个链表"),
        {"block_id": "image", "block_type": 27, "image": {"alt": "题干"}},
        text_block("answer", "两个独立解法"),
    ]

    result = parse_docx_blocks("source-code", "doc", "code", blocks)

    assert result.published == []
    assert len(result.candidates) == 1
    assert result.candidates[0].confirmation_status == "pending"


def test_clear_theory_question_with_category_and_reference_is_published() -> None:
    blocks = [
        heading("category", 2, "计算效率"),
        text_block("question", "为什么要预热？"),
        text_block("answer", "排除延迟编译时间。"),
    ]

    result = parse_docx_blocks("source-theory", "doc", "theory", blocks)

    assert result.candidates == []
    assert len(result.published) == 1
    assert result.published[0].confirmation_status == "confirmed"
    assert result.published[0].reference_block_ids == ("answer",)


def test_wiki_doc_question_heading_does_not_require_question_mark():
    blocks = [heading('q', 1, '什么是function call'), text_block('a', '模型生成结构化参数以调用工具。')]
    result = parse_docx_blocks('source', 'doc', 'theory', blocks, '八股 > Agent')
    assert len(result.published) == 1
    assert result.published[0].category_path == '八股 > Agent'
