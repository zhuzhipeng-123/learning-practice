"""One free-practice operation, shared by direct requests and durable batches."""

from dataclasses import asdict

from app.services.free_practice import add_free_practice
from app.services.model_budget import ORIGINAL_LIMIT, VARIANT_LIMIT
from app.services.practice_generation import generate_variants
from app.services.practice_selection import select_by_description


def validate_spec(spec):
    if spec.get('question_type') not in {'code', 'theory'}:
        raise ValueError('请选择代码或八股')
    if spec.get('question_source') not in {'original', 'variant'} or spec.get('mode') not in {'random', 'topic'}:
        raise ValueError('请选择有效的题目来源和练习方式')
    maximum = VARIANT_LIMIT if spec['question_source'] == 'variant' else ORIGINAL_LIMIT
    if type(spec.get('count')) is not int or not 1 <= spec['count'] <= maximum:
        raise ValueError(f'本次题数请填写 1 至 {maximum}')
    if not isinstance(spec.get('theme'), str) or len(spec['theme']) > 2000:
        raise ValueError('练习范围不能超过 2000 字')
    if spec['mode'] == 'topic' and not spec['theme'].strip():
        raise ValueError('请描述想练的内容，或选择完全随机')


def execute_free_request(connection, spec, request_key):
    original = spec['question_source'] == 'original'
    only_new = True if original else spec['only_new']
    theme = spec['theme'] if spec['mode'] == 'topic' else ''
    selected = select_by_description(connection, theme, spec['count'], request_key, only_new,
                                   question_type=spec['question_type']) if spec['mode'] == 'topic' else None
    if original:
        result = asdict(add_free_practice(connection, spec['plan_id'], theme, spec['count'], request_key,
                       False, True, selected_ids=selected, question_type=spec['question_type']))
    else:
        result = generate_variants(connection, spec['plan_id'], theme, spec['count'], spec['question_type'],
                                   only_new, request_key, selected)
    return result


def free_cards(connection, task_ids):
    cards = []
    for task_id in task_ids:
        row = connection.execute(
            'SELECT t.id,t.status,v.prompt,v.category_path,q.question_type,EXISTS '
            '(SELECT 1 FROM question_derivation d WHERE d.question_id=q.id) is_variant, '
            '(SELECT s.id FROM interview_session s WHERE s.task_id=t.id ORDER BY s.rowid DESC LIMIT 1) session_id, '
            'EXISTS(SELECT 1 FROM reference_correction c WHERE c.version_id=t.question_version_id) has_correction FROM task t '
            'JOIN question_version v ON v.id=t.question_version_id JOIN question q ON q.id=t.question_id '
            'WHERE t.id=?', (task_id,),
        ).fetchone()
        if row:
            cards.append(dict(row))
    return cards
