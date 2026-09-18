"""Model-assisted topic matching with a strict existing-question allowlist."""

import json

from app.services.free_practice import find_new_originals
from app.services.llm_config import get_module_config
from app.services.model_batches import prepare_batches
from app.services.model_budget import split_inputs
from app.services.model_jobs import ModelJobError
from app.services.model_json import parse_model_json
from app.services.module_jobs import run_module_job
from app.services.tasks import IdempotencyConflictError


def select_by_description(connection, description, count, request_key, only_new=True, client=None, question_type=None):
    # Old, already frozen single requests remain recoverable without another pool read.
    legacy = connection.execute('SELECT r.input_json FROM model_request r JOIN model_job j ON j.id=r.job_id '
                                'WHERE j.business_key=?', ('practice_selection:' + request_key,)).fetchone()
    if legacy:
        context = json.loads(legacy[0])
        if (context['source_id'] != 'practice:' + request_key
                or context['description'] != description or context['count'] != count):
            raise IdempotencyConflictError('同一请求标识不能用于不同的选题条件')
        reply = run_module_job(connection, 'practice_selection', context['source_id'], request_key, client)
        return validate_selection(context, parse_model_json(reply['response_text']))
    payload = {'description': description, 'count': count, 'only_new': only_new, 'question_type': question_type}
    batches = prepare_batches(connection, 'practice_selection', request_key, payload,
                              lambda: _selection_inputs(connection, payload, request_key))
    selected = []
    for batch in batches:
        reply = run_module_job(connection, 'practice_selection', batch['target'], batch['key'], client)
        context = json.loads(connection.execute('SELECT input_json FROM model_request WHERE job_id=?',
                                                (batch['job_id'],)).fetchone()[0])
        selected.extend(validate_selection(context, parse_model_json(reply['response_text'])))
    return list(dict.fromkeys(selected))


def _selection_inputs(connection, payload, request_key):
    rows = find_new_originals(connection, '', payload['only_new'], payload['question_type'])
    questions = [{'id': r['id'], 'version_id': r['version_id'], 'title': r['prompt'], 'module': r['category_path']} for r in rows]
    # A large matching pool must fit the reply too, even if the user only wants one task.
    tokens = get_module_config(connection, 'practice_selection')['max_tokens']
    def output_fits(items):
        return len(json.dumps({'question_ids': [item['id'] for item in items]}).encode()) + 32 <= tokens
    base = {'source_id': 'practice:' + request_key, 'description': payload['description'], 'count': payload['count']}
    return split_inputs(base, 'questions', questions, output_fits=output_fits)


def validate_selection_sources(connection, request_key, candidates):
    """Before task allocation, reject applying an old match to updated source text."""
    plan = connection.execute("SELECT result_json FROM idempotency_record WHERE operation='model_batches' AND request_key=?",
                              ('model-batches:practice_selection:' + request_key,)).fetchone()
    if not plan:
        return
    frozen = {}
    for batch in json.loads(plan[0])['batches']:
        context = json.loads(connection.execute('SELECT input_json FROM model_request WHERE job_id=?',
                                                (batch['job_id'],)).fetchone()[0])
        frozen.update({item['id']: item for item in context['questions']})
    for candidate in candidates:
        original = frozen.get(candidate['id'])
        if (not original or candidate['version_id'] != original.get('version_id')
                or candidate['prompt'] != original['title'] or candidate['category_path'] != original['module']):
            raise ValueError('题库在选题期间发生变化，请重新确认出题；未将旧的选题判断用于新版本。')


def validate_selection(context, payload):
    allowed = {q['id'] for q in context['questions']}
    ids = payload.get('question_ids') if isinstance(payload, dict) else None
    if (not isinstance(ids, list) or len(ids) > len(allowed) or any(not isinstance(q, str) or q not in allowed for q in ids)
            or len(set(ids)) != len(ids)):
        raise ModelJobError('选题结果不符合题库范围，请重试或选择完全随机')
    return ids
