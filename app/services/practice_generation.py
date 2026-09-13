"""Generate explicit variants while preserving source and model provenance."""

import json
import random
from datetime import UTC, datetime

from app.services.free_practice import find_new_originals
from app.services.model_jobs import ModelJobError
from app.services.model_json import parse_model_json
from app.services.module_jobs import run_module_job
from app.services.tasks import _load_idempotent, _save_idempotent, add_tasks
from app.storage.ids import new_id
from app.storage.transactions import atomic


def validate_variants(context, value):
    items = value.get('questions') if isinstance(value, dict) else None
    originals = {q['id']: q for q in context['questions']}
    if not isinstance(items, list) or len(items) != len(originals):
        raise ModelJobError('变种题数量不完整，请重试；没有创建练习任务')
    seen, prompts = set(), set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('base_question_id'), str):
            raise ModelJobError('变种题缺少有效来源')
        key, prompt, reference = item['base_question_id'], item.get('prompt'), item.get('reference_text')
        if key not in originals or key in seen:
            raise ModelJobError('变种题引用了未知或重复的原题')
        if (not isinstance(prompt, str) or not 5 <= len(prompt.strip()) <= 4000
                or not isinstance(reference, str) or not 5 <= len(reference.strip()) <= 12000):
            raise ModelJobError('变种题题干或参考不完整，请重试')
        if prompt.strip() == originals[key]['prompt'].strip() or prompt.strip() in prompts:
            raise ModelJobError('模型重复了原题或返回了重复题目，请重试')
        seen.add(key)
        prompts.add(prompt.strip())
    return items


def generate_variants(connection, plan_id, theme, count, question_type, only_new, request_key, selected_ids=None, client=None):
    if question_type not in {'code', 'theory'} or not 1 <= count <= 3:
        raise ValueError('变种题请选择代码或八股，每次生成1至3道')
    spec = {'plan_id': plan_id, 'theme': theme, 'count': count, 'question_type': question_type, 'only_new': only_new}
    previous = _load_idempotent(connection, request_key, 'practice_variants', spec)
    if previous:
        return previous
    saved = connection.execute("SELECT r.input_json FROM model_request r JOIN model_job j ON j.id=r.job_id "
                               "WHERE j.business_key=?", ('practice_generation:' + request_key,)).fetchone()
    if saved:
        context = json.loads(saved[0])
        if context['spec'] != spec:
            raise ValueError('同一请求标识不能用于不同的变种要求')
    else:
        rows = find_new_originals(connection, '', only_new, question_type)
        rows = [dict(row) for row in rows if row['reference_text'] and len(row['prompt']) + len(row['reference_text']) <= 24000
                and (selected_ids is None or row['id'] in selected_ids)]
        chosen = random.sample(rows, min(count, len(rows)))
        from app.services.reference_corrections import attach_corrections
        context = {'source_id': 'variant:' + request_key, 'spec': spec, 'question_type': question_type,
                   'questions': attach_corrections(connection, chosen)}
    if not context['questions']:
        return {'requested': count, 'added': 0, 'missing': count, 'task_ids': []}
    response = run_module_job(connection, 'practice_generation', context['source_id'], request_key, client, context)
    items = validate_variants(context, parse_model_json(response['response_text']))
    return _publish_variants(connection, context, items, response['job_id'], request_key)


@atomic
def _publish_variants(connection, context, items, job_id, request_key):
    spec = context['spec']
    previous = _load_idempotent(connection, request_key, 'practice_variants', spec)
    if previous:
        return previous
    originals = {q['id']: q for q in context['questions']}
    question_ids = []
    now = datetime.now(UTC).isoformat()
    for item in items:
        original = originals[item['base_question_id']]
        question_id, version, basis = new_id('question'), new_id('version'), new_id('basis')
        connection.execute("INSERT INTO question(id,question_type,source_kind,created_at) VALUES (?,?,'derived',?)",
                           (question_id, spec['question_type'], now))
        connection.execute('INSERT INTO review_basis VALUES (?,?,?,?)', (basis, question_id, 'variant:' + question_id, now))
        connection.execute("INSERT INTO question_version VALUES (?,?,NULL,?,?,?,?,'text_complete',?,'model generated variant',?)",
            (version, question_id, basis, item['prompt'].strip(), item['reference_text'].strip(), original['category_path'], 'variant:' + question_id, now))
        connection.execute('UPDATE question SET current_version_id=? WHERE id=?', (version, question_id))
        connection.execute('INSERT INTO question_derivation VALUES (?,?,?)', (question_id, original['version_id'], job_id))
        connection.execute("INSERT INTO version_resources VALUES (?,?,'[]')", (version, json.dumps(['variant-reference:' + version])))
        question_ids.append(question_id)
    result = add_tasks(connection, spec['plan_id'], question_ids, 'free_practice', 'variant-tasks:' + request_key)
    value = {'requested': spec['count'], 'added': result['added'], 'missing': spec['count'] - result['added'], 'task_ids': result['created_task_ids']}
    _save_idempotent(connection, request_key, 'practice_variants', spec, value, now)
    return value
