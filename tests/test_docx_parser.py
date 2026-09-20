import pytest

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
        heading("question", 3, "移动0-图片"),
        {"block_id": "image", "block_type": 27, "image": {"alt": "题干"}},
        text_block("answer", "双指针"),
        heading("next", 3, "下一题"),
    ]

    result = parse_docx_blocks("source-code", "doc", "code", blocks)

    assert len(result.published) == 1
    assert result.published[0].main_anchor_block_id == "question"
    assert result.published[0].category_path == "hot100 > 移动0"
    assert result.published[0].prompt == "移动0"
    assert result.published[0].prompt_block_ids == ("question", "image")


def test_compound_code_heading_requires_confirmation() -> None:
    blocks = [
        heading("question", 3, "链表排序&合并k个链表-图片"),
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
        heading("question", 3, "为什么要预热？"),
        text_block("answer", "排除延迟编译时间。"),
    ]

    result = parse_docx_blocks("source-theory", "doc", "theory", blocks)

    assert result.candidates == []
    assert len(result.published) == 1
    assert result.published[0].confirmation_status == "confirmed"
    assert result.published[0].reference_block_ids == ("answer",)


def test_wiki_doc_question_heading_does_not_require_question_mark():
    blocks = [heading('q', 3, '什么是function call'), text_block('a', '模型生成结构化参数以调用工具。')]
    result = parse_docx_blocks('source', 'doc', 'theory', blocks, '八股 > Agent')
    assert len(result.published) == 1
    assert result.published[0].category_path == '八股 > Agent'


def test_mixed_h3_h4_tree_uses_explicit_question_marker():
    blocks = [
        heading('h1', 1, 'Agent'), text_block('module-note', '模块说明'),
        heading('h2', 2, '工具'),
        heading('container', 3, '常见问题'),
        heading('child', 4, '问题：为什么要校验参数？'), text_block('answer', '避免无效调用。'),
    ]

    result = parse_docx_blocks('source', 'doc', 'theory', blocks, '八股')

    assert [item.main_anchor_block_id for item in result.published] == ['child']
    assert result.published[0].prompt == '为什么要校验参数？'
    assert result.published[0].category_path == '八股 > Agent > 工具 > 常见问题'


def test_mixed_h3_h4_tree_does_not_guess_from_question_words():
    blocks = [
        heading('module', 1, 'Agent'), heading('container', 3, '基础概念'),
        heading('child', 4, '问题：什么是工具调用？'), text_block('answer', '模型调用外部工具。'),
    ]

    result = parse_docx_blocks('source', 'doc', 'theory', blocks, '八股')

    assert [item.main_anchor_block_id for item in result.published] == ['child']
    assert result.published[0].reference_block_ids == ('answer',)


@pytest.mark.parametrize('answer_heading', ['原因', '为什么要设置超时时间？'])
def test_marked_h3_question_keeps_deeper_answer_heading_as_reference(answer_heading):
    blocks = [
        heading('module', 1, 'Agent'),
        heading('question', 3, '问题：为什么需要校验参数？'),
        heading('answer-heading', 4, answer_heading),
        text_block('answer', '避免无效调用。'),
    ]

    result = parse_docx_blocks('source', 'doc', 'theory', blocks, '八股')

    assert [item.main_anchor_block_id for item in result.published] == ['question']
    assert result.published[0].prompt == '为什么需要校验参数？'
    assert result.published[0].reference_block_ids == ('answer-heading', 'answer')
    assert result.candidates == []


def test_marker_starts_h4_question_without_guessing_from_wording():
    blocks = [
        heading('module', 1, 'Agent'), heading('parent', 3, '工具调用'),
        heading('child', 4, '问题：校验参数的价值'), text_block('answer', '避免无效调用。'),
    ]

    result = parse_docx_blocks('source', 'doc', 'theory', blocks, '八股')

    assert [item.main_anchor_block_id for item in result.published] == ['child']
    assert result.published[0].prompt == '校验参数的价值'
    assert result.published[0].reference_block_ids == ('answer',)
    assert result.candidates == []


def test_unmarked_mixed_tree_waits_for_confirmation_instead_of_guessing():
    answer_heading = [heading('module', 1, 'Agent'), heading('q', 3, '工具调用'),
                      heading('detail', 4, '为什么需要校验？'), text_block('answer', '先校验参数。')]
    parsed = parse_docx_blocks('source', 'doc', 'theory', answer_heading, '八股')
    assert parsed.published == []
    assert [item.main_anchor_block_id for item in parsed.candidates] == ['q']
    assert parsed.candidates[0].reference_block_ids == ('detail', 'answer')


def test_h3_h4_h5_tree_uses_marker_at_actual_question():
    blocks = [
        heading('module', 1, 'Agent'), heading('group', 3, '工具调用'),
        heading('subgroup', 4, '参数'), heading('question', 5, '问题：为什么校验参数？'),
        text_block('answer', '避免无效调用。'),
    ]

    result = parse_docx_blocks('source', 'doc', 'theory', blocks, '八股')

    assert [item.main_anchor_block_id for item in result.published] == ['question']
    assert result.published[0].category_path == '八股 > Agent > 工具调用 > 参数'
    assert result.published[0].prompt == '为什么校验参数？'


@pytest.mark.parametrize('answer_block', [
    {'block_id': 'answer-image', 'block_type': 27, 'image': {'token': 'answer-image'}},
    {'block_id': 'answer-table', 'block_type': 31, 'children': [],
     'table': {'property': {'row_size': 1, 'column_size': 1}}},
])
def test_theory_question_is_text_while_answer_can_be_image_or_table(answer_block):
    blocks = [heading('module', 1, 'Agent'), heading('question', 3, '工具调用流程'), answer_block]

    result = parse_docx_blocks('source', 'doc', 'theory', blocks, '八股')

    assert len(result.published) == 1
    question = result.published[0]
    assert question.prompt == '工具调用流程'
    assert question.prompt_block_ids == ('question',)
    assert question.reference_block_ids == (answer_block['block_id'],)
    assert question.material_status == 'media_required'


def test_code_question_uses_only_first_image_and_flags_extra_images():
    blocks = [
        heading('question', 3, '移动零-图片   '),
        {'block_id': 'p1', 'block_type': 27, 'image': {'alt': '题图一'}},
        {'block_id': 'p2', 'block_type': 27, 'image': {'alt': '题图二'}},
        text_block('answer', '双指针。'),
        {'block_id': 'answer-image', 'block_type': 27, 'image': {'alt': '答案图'}},
    ]

    result = parse_docx_blocks('source', 'doc', 'code', blocks)

    assert result.published == []
    question = result.candidates[0]
    assert question.prompt == '移动零'
    assert question.prompt_block_ids == ('question', 'p1')
    assert question.reference_block_ids == ('p2', 'answer', 'answer-image')
    assert 'multiple_code_images_require_confirmation' in question.parse_issues


def test_code_question_uses_one_image_then_text_answer():
    blocks = [
        heading('question', 3, '移动零-图片'),
        {'block_id': 'prompt-image', 'block_type': 27, 'image': {'alt': '题图'}},
        text_block('answer', '使用双指针。'),
    ]

    result = parse_docx_blocks('source', 'doc', 'code', blocks)

    assert len(result.published) == 1
    assert result.published[0].prompt_block_ids == ('question', 'prompt-image')
    assert result.published[0].reference_block_ids == ('answer',)


def test_unmarked_leading_image_and_marked_missing_image_require_confirmation():
    unmarked = parse_docx_blocks('source', 'doc', 'code', [
        heading('q', 3, '移动零'), {'block_id': 'image', 'block_type': 27, 'image': {'alt': '未知角色'}},
        text_block('answer', '双指针。'),
    ])
    missing = parse_docx_blocks('source', 'doc', 'code', [
        heading('q', 3, '移动零-图片'), text_block('answer', '双指针。'),
    ])

    assert unmarked.published == [] and unmarked.candidates[0].prompt_block_ids == ('q',)
    assert missing.published == []
    assert 'marked_heading_missing_prompt_image' in missing.candidates[0].parse_issues
