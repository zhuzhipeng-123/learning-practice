"""Start from a user-selected original or an explicitly supplied custom question."""

import hashlib
import json
from datetime import UTC, datetime

from app.services.interview import start_session
from app.services.learning_clock import local_today
from app.services.review_tasks import _get_or_create_plan
from app.services.tasks import _load_idempotent, _save_idempotent, add_tasks
from app.storage.ids import new_id
from app.storage.transactions import atomic


def validate_preparation(context, value):
    from app.services.model_jobs import ModelJobError
    if not isinstance(value, dict):
        raise ModelJobError('面试准备格式错误，请重试')
    if context['mode'] == 'suggest':
        items = value.get('directions')
        if (not isinstance(items, list) or len(items) != 3
                or any(not isinstance(item, str) or not 1 <= len(item.strip()) <= 100 for item in items)
                or len({item.strip().casefold() for item in items}) != 3):
            raise ModelJobError('模型没有返回三个不同的方向，请重试')
        if all(item.strip().casefold() in {old.strip().casefold() for old in context['avoid']} for item in items):
            raise ModelJobError('模型重复了上一组方向，请点击重试')
    elif not isinstance(value.get('question'), str) or not 1 <= len(value['question'].strip()) <= 1000:
        raise ModelJobError('模型没有返回有效的开场问题，请重试')
    return value


def prepare_direction(connection, mode, direction, job_focus, avoid, request_key, client=None):
    from app.services.model_json import parse_model_json
    from app.services.module_jobs import run_module_job
    direction = direction.strip()
    if mode == 'opening' and not direction:
        raise ValueError('请先填写或选择一个面试方向')
    context = {'mode': mode, 'direction': direction, 'job_focus': job_focus.strip(), 'avoid': avoid}
    target = hashlib.sha256(json.dumps(context, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    context['source_id'] = target
    result = run_module_job(connection, 'interview_preparation', target, request_key, client, context)
    value = validate_preparation(context, parse_model_json(result['response_text']))
    if mode == 'suggest':
        return {'directions': [item.strip() for item in value['directions']]}
    return prepare_interview(connection, None, value['question'].strip(),
                             f'练习方向：{direction}\n{job_focus.strip()}', request_key + ':session')


@atomic
def prepare_interview(connection, question_id, custom_question, job_focus, request_key):
    payload = {'question_id': question_id, 'custom_question': custom_question, 'job_focus': job_focus}
    previous = _load_idempotent(connection, request_key, 'prepare_interview', payload)
    if previous:
        return previous
    now = datetime.now(UTC)
    if custom_question.strip():
        question_id = _custom_question(connection, custom_question.strip(), now)
    else:
        valid = connection.execute("SELECT 1 FROM question q JOIN question_version v ON v.id=q.current_version_id "
            "WHERE q.id=? AND q.source_status='active' AND v.material_status IN ('complete','verified','text_complete')", (question_id,)).fetchone()
        if not valid:
            raise ValueError('请选择一道可用题目，或填写你自己的面试题')
    pending = connection.execute("SELECT id FROM task WHERE question_id=? AND status IN ('pending','in_progress') ORDER BY rowid LIMIT 1", (question_id,)).fetchone()
    if pending:
        task_id = pending[0]
    else:
        plan = _get_or_create_plan(connection, local_today(), request_key)
        task_id = add_tasks(connection, plan['plan_id'], [question_id], 'interview', request_key + ':task')['created_task_ids'][0]
    session_id = start_session(connection, task_id, now)
    connection.execute('INSERT INTO interview_setup VALUES (?,?) ON CONFLICT(session_id) DO UPDATE SET job_focus=excluded.job_focus', (session_id, job_focus))
    result = {'session_id': session_id}
    _save_idempotent(connection, request_key, 'prepare_interview', payload, result, now.isoformat())
    return result


def _custom_question(connection, prompt, now):
    question_id, basis, version = new_id('question'), new_id('basis'), new_id('version')
    connection.execute("INSERT INTO question(id,question_type,source_kind,created_at) VALUES (?,'theory','derived',?)", (question_id, now.isoformat()))
    connection.execute('INSERT INTO review_basis VALUES (?,?,?,?)', (basis, question_id, 'custom:' + question_id, now.isoformat()))
    connection.execute("INSERT INTO question_version VALUES (?,?,NULL,?,?,NULL,'我的面试题','text_complete',?,'user supplied interview question',?)",
                       (version, question_id, basis, prompt, 'custom:' + question_id, now.isoformat()))
    connection.execute('UPDATE question SET current_version_id=? WHERE id=?', (version, question_id))
    return question_id
