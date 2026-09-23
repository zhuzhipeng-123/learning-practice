"""Start from a user-selected original or an explicitly supplied custom question."""

import hashlib
import json
import random
from datetime import UTC, datetime

from app.services.interview import start_session
from app.services.learning_clock import local_today
from app.services.review_tasks import _get_or_create_plan, start_review_task
from app.services.tasks import _load_idempotent, _save_idempotent, add_tasks
from app.storage.ids import new_id
from app.storage.transactions import atomic


@atomic
def prepare_pool_interview(connection, pool, job_focus, request_key):
    payload = {'pool': pool, 'job_focus': job_focus}
    previous = _load_idempotent(connection, request_key, 'interview_pool', payload)
    if previous:
        return previous
    if pool not in {'review', 'classic'}:
        raise ValueError('请选择错题深挖或经典练习')
    rows = connection.execute(
        "SELECT question_id AS id FROM review_round WHERE status='active'" if pool == 'review' else
        "SELECT q.id FROM question q JOIN question_version v ON v.id=q.current_version_id "
        "WHERE q.is_classic=1 AND q.source_status='active' AND v.material_status IN ('complete','verified','text_complete')",
    ).fetchall()
    message = ''
    if not rows:
        rows = connection.execute(
            "SELECT q.id FROM question q JOIN question_version v ON v.id=q.current_version_id "
            "WHERE q.source_kind='feishu' AND q.source_status='active' AND v.material_status IN ('complete','verified','text_complete')",
        ).fetchall()
        message = f"当前没有{'待复习题' if pool == 'review' else '标记的经典题'}，已从可用原题随机选一道。"
    if not rows:
        raise ValueError('本地还没有可用题目，可以按方向开始面试，或先对齐题库')
    question = random.choice(rows)['id']
    if pool == 'review' and not message:
        task = start_review_task(connection, question, local_today(), request_key + ':review')
        session_id = start_session(connection, task['task_id'], datetime.now(UTC))
        connection.execute('INSERT OR IGNORE INTO interview_setup VALUES (?,?)', (session_id, job_focus))
        result = {'session_id': session_id, 'message': task.get('message', '')}
    else:
        result = {**prepare_interview(connection, question, '', job_focus, request_key + ':prepare'), 'message': message}
    _save_idempotent(connection, request_key, 'interview_pool', payload, result, datetime.now(UTC).isoformat())
    return result


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
    else:
        from app.services.question_quality import validate_pair
        validate_pair(value, question_limit=1000)
    return value


def checked_suggestions(connection, context, reply, config, model, job_id, execution_id, messages):
    from app.services.model_diagnostics import log_event, started_at
    from app.services.model_jobs import (
        ModelJobError,
        load_model_request_owned,
        update_model_request_owned,
    )
    from app.services.model_json import ModelJSONError, complete_json, parse_model_json
    from app.storage.transactions import transaction
    try:
        validate_preparation(context, parse_model_json(reply.content))
        return reply
    except (ModelJobError, ModelJSONError) as error:
        instruction = ('上次方向列表未通过格式校验：' + str(error) +
                       '。请重新输出三个不同的简短方向标题，每个8至30字，不附解释。'
                       '严格符合原 job_focus 并避开 avoid，只输出 {"directions":["标题1","标题2","标题3"]}。')
    # Retain the rejected output and exact correction; never silently truncate a direction.
    with transaction(connection):
        stored = json.loads(load_model_request_owned(connection, job_id, execution_id)['config_json'])
        stored['format_repair'] = {'rejected_response': reply.content, 'instruction': instruction, 'max_tokens': config['max_tokens']}
        update_model_request_owned(connection, job_id, execution_id,
                                   config_json=json.dumps(stored, ensure_ascii=False))
    repair_started = started_at()
    fixed = complete_json(model, [*messages, {'role':'assistant', 'content':reply.content},
                                 {'role':'user', 'content':instruction}], config['max_tokens'])
    log_event(job_id, 'interview_preparation', execution_id, 'repair',
              started=repair_started, reply=fixed)
    validate_preparation(context, parse_model_json(fixed.content))
    return fixed


def prepare_direction(connection, mode, direction, job_focus, avoid, request_key, client=None):
    from app.services.interview_conversation import learning_context
    from app.services.model_json import parse_model_json
    from app.services.module_jobs import run_module_job
    direction = direction.strip()
    if mode == 'opening' and not direction:
        raise ValueError('请先填写或选择一个面试方向')
    context = {'mode': mode, 'direction': direction, 'job_focus': job_focus.strip(), 'avoid': avoid}
    target = hashlib.sha256(json.dumps(context, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    context['source_id'] = target
    context['learning_context'] = learning_context(connection)
    result = run_module_job(connection, 'interview_preparation', target, request_key, client, context)
    value = parse_model_json(result['response_text'])
    if mode == 'opening' and isinstance(value, dict) and not value.get('reference_text'):
        legacy = _load_idempotent(connection, request_key + ':session', 'prepare_interview', {
            'question_id': None, 'custom_question': value.get('question', '').strip(),
            'job_focus': f'练习方向：{direction}\n{job_focus.strip()}'})
        if legacy:
            return legacy
    validate_preparation(context, value)
    if mode == 'suggest':
        return {'directions': [item.strip() for item in value['directions']]}
    return prepare_interview(connection, None, value['question'].strip(),
                             f'练习方向：{direction}\n{job_focus.strip()}', request_key + ':session', value['reference_text'].strip())


@atomic
def prepare_interview(connection, question_id, custom_question, job_focus, request_key, reference_text=None):
    payload = {'question_id': question_id, 'custom_question': custom_question, 'job_focus': job_focus}
    if reference_text is not None:
        payload['reference_text'] = reference_text
    previous = _load_idempotent(connection, request_key, 'prepare_interview', payload)
    if previous:
        return previous
    now = datetime.now(UTC)
    if custom_question.strip():
        question_id = _custom_question(connection, custom_question.strip(), now, reference_text)
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


def _custom_question(connection, prompt, now, reference_text=None):
    question_id, basis, version = new_id('question'), new_id('basis'), new_id('version')
    connection.execute("INSERT INTO question(id,question_type,source_kind,created_at) VALUES (?,'theory','derived',?)", (question_id, now.isoformat()))
    connection.execute('INSERT INTO review_basis VALUES (?,?,?,?)', (basis, question_id, 'custom:' + question_id, now.isoformat()))
    connection.execute("INSERT INTO question_version VALUES (?,?,NULL,?,?,?,'我的面试题','text_complete',?,?,?)",
                       (version, question_id, basis, prompt, reference_text, 'custom:' + question_id,
                        'model generated interview question and answer' if reference_text else 'user supplied interview question', now.isoformat()))
    connection.execute('UPDATE question SET current_version_id=? WHERE id=?', (version, question_id))
    return question_id
