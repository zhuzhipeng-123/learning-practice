"""Synthetic reviewer protocol responses; these fixtures do not verify semantics."""

from app.services.question_quality import CHECKS


def quality_result(items, passed=True):
    return {'items': [{'id': item['id'], **dict.fromkeys(CHECKS, passed),
        'issues': [] if passed else ['答案未回应实际问题，示例关系错误。'],
        **({'example_checks': [{'example_id': example['id'], 'input_quote': example['text'][:200],
            'computed_output': 'synthetic result, not a correctness claim',
            'reason': 'Synthetic reviewer evidence used only for isolated flow verification.',
            'consistent': passed} for example in item['examples']]} if item.get('examples') else {})} for item in items]}
