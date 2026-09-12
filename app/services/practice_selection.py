"""Model-assisted topic matching with a strict existing-question allowlist."""

from app.services.free_practice import find_new_originals
from app.services.model_jobs import ModelJobError
from app.services.model_json import parse_model_json
from app.services.module_jobs import run_module_job


def select_by_description(connection, description, count, request_key, only_new=False, client=None):
    rows = find_new_originals(connection, '', only_new)
    if not rows:
        return []
    context = {'source_id': 'practice:' + request_key, 'description': description, 'count': count,
               'questions': [{'id': r['id'], 'title': r['prompt'], 'module': r['category_path']} for r in rows]}
    reply = run_module_job(connection, 'practice_selection', context['source_id'], request_key, client, context)
    return parse_model_json(reply['response_text'])['question_ids']


def validate_selection(context, payload):
    allowed = {q['id'] for q in context['questions']}
    ids = payload.get('question_ids') if isinstance(payload, dict) else None
    if (not isinstance(ids, list) or len(ids) > context['count'] or any(not isinstance(q, str) or q not in allowed for q in ids)
            or len(set(ids)) != len(ids)):
        raise ModelJobError('选题结果不符合题库范围，请重试或选择完全随机')
    return ids
