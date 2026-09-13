"""Durable free-practice batches; code and theory use independent worker slots."""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from app.services.free_requests import execute_free_request, free_cards, validate_spec
from app.services.learning_clock import local_today
from app.services.review_tasks import _get_or_create_plan
from app.services.tasks import IdempotencyConflictError
from app.storage.database import connect_database
from app.storage.ids import new_id
from app.storage.transactions import transaction

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='free-practice')


def batch_state(connection, batch_id):
    row = connection.execute('SELECT * FROM free_practice_batch WHERE id=?', (batch_id,)).fetchone()
    if not row:
        raise ValueError('这批练习不存在')
    result = json.loads(row['result_json']) if row['result_json'] else None
    if result is not None:
        result['tasks'] = free_cards(connection, result['task_ids'])
    return {'id': row['id'], 'question_type': row['question_type'], 'status': row['status'],
            'spec': json.loads(row['payload_json']), 'result': result,
            'error': row['error'], 'created_at': row['created_at']}


def practice_state(connection):
    states = {}
    for kind in ('code', 'theory'):
        latest = connection.execute('SELECT b.id FROM free_practice_batch b JOIN daily_plan p ON p.id=b.plan_id '
            "WHERE b.question_type=? AND p.plan_date=? ORDER BY b.rowid DESC LIMIT 1", (kind, local_today().isoformat())).fetchone()
        complete = connection.execute("SELECT b.id FROM free_practice_batch b JOIN daily_plan p ON p.id=b.plan_id "
            "WHERE b.question_type=? AND b.status='complete' AND p.plan_date=? ORDER BY b.rowid DESC LIMIT 1",
            (kind, local_today().isoformat())).fetchone()
        states[kind] = {'batch': batch_state(connection, latest['id']) if latest else None,
                        'result_batch': batch_state(connection, complete['id']) if complete else None}
        states[kind]['earlier'] = []  # Old tasks are retired, not a hidden backlog.
    return states


def queue_batch(connection, spec, request_key):
    if connection.in_transaction:
        raise ValueError('当前数据还在保存，请稍后再试')
    validate_spec(spec)
    if not request_key or len(request_key) > 200:
        raise ValueError('请求标识不能为空或超过 200 字符')
    payload = json.dumps(spec, sort_keys=True, ensure_ascii=False)
    with transaction(connection):
        previous = connection.execute('SELECT id,payload_json FROM free_practice_batch WHERE request_key=?', (request_key,)).fetchone()
        if previous:
            if previous['payload_json'] != payload:
                raise IdempotencyConflictError('上次请求的内容不同，请先恢复上次提交')
            return batch_state(connection, previous['id'])
        plan_id = spec.get('plan_id')
        if plan_id:
            plan = connection.execute('SELECT plan_date FROM daily_plan WHERE id=?', (plan_id,)).fetchone()
            if not plan or plan['plan_date'] != local_today().isoformat():
                raise ValueError('日期已变化，请回到今天重新出题；旧待办已作废')
        connection.execute("UPDATE free_practice_batch SET status='interrupted',error='日期已变化，请重新出题' "
            "WHERE status IN ('queued','running') AND plan_id IN (SELECT id FROM daily_plan WHERE plan_date<?)", (local_today().isoformat(),))
        active = connection.execute("SELECT id FROM free_practice_batch WHERE question_type=? AND status IN ('queued','running')", (spec['question_type'],)).fetchone()
        if active:
            return {**batch_state(connection, active['id']), 'reused_active': True}
        batch_id, now = new_id('batch'), datetime.now(UTC).isoformat()
        plan_id = plan_id or _get_or_create_plan(connection, local_today(), 'batch:' + batch_id)['plan_id']
        connection.execute("INSERT INTO free_practice_batch(id,request_key,question_type,payload_json,plan_id,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,'queued',?,?)", (batch_id, request_key, spec['question_type'], payload, plan_id, now, now))
    schedule_batch(connection, batch_id)
    return batch_state(connection, batch_id)


def retry_batch(connection, batch_id):
    if connection.in_transaction:
        raise ValueError('当前数据还在保存，请稍后再试')
    with transaction(connection):
        current = batch_state(connection, batch_id)
        latest = practice_state(connection)[current['question_type']]['batch']
        if not latest or latest['id'] != batch_id:
            raise ValueError('这批练习已过期或被新一批替换，请重新出题')
        if current['status'] not in {'failed', 'interrupted'}:
            return current
        active = connection.execute("SELECT id FROM free_practice_batch WHERE question_type=? AND status IN ('queued','running')", (current['question_type'],)).fetchone()
        if active:
            return {**batch_state(connection, active['id']), 'reused_active': True}
        connection.execute("UPDATE free_practice_batch SET status='queued',error=NULL,updated_at=? WHERE id=?",
                           (datetime.now(UTC).isoformat(), batch_id))
    schedule_batch(connection, batch_id)
    return batch_state(connection, batch_id)


def schedule_batch(connection, batch_id):
    location = Path(connection.execute('PRAGMA database_list').fetchone()[2])
    _pool.submit(run_batch, location, batch_id)


def run_batch(location, batch_id):
    connection = connect_database(location)
    try:
        with transaction(connection):
            if not connection.execute("UPDATE free_practice_batch SET status='running',updated_at=? WHERE id=? AND status='queued'",
                                      (datetime.now(UTC).isoformat(), batch_id)).rowcount:
                return
            row = connection.execute('SELECT * FROM free_practice_batch WHERE id=?', (batch_id,)).fetchone()
            spec = {**json.loads(row['payload_json']), 'plan_id': row['plan_id']}
        # No transaction spans selection or generation. The original plan remains frozen over midnight.
        result = execute_free_request(connection, spec, 'free-batch:' + batch_id)
        with transaction(connection):
            from app.services.current_practice import replace_free_tasks, retire_tasks
            current = connection.execute('SELECT b.status,p.plan_date FROM free_practice_batch b '
                'JOIN daily_plan p ON p.id=b.plan_id WHERE b.id=?', (batch_id,)).fetchone()
            if current['status'] != 'running' or current['plan_date'] != local_today().isoformat():
                retire_tasks(connection, result['task_ids'])
                connection.execute("UPDATE free_practice_batch SET status='interrupted',error='这批已过期，请使用当前题单' WHERE id=?", (batch_id,))
                return
            replace_free_tasks(connection, spec['question_type'], result['task_ids'])
            connection.execute("UPDATE free_practice_batch SET status='complete',result_json=?,error=NULL,updated_at=? WHERE id=?",
                (json.dumps(result, ensure_ascii=False), datetime.now(UTC).isoformat(), batch_id))
    except Exception as error:
        logging.getLogger(__name__).exception('Free-practice batch failed')
        with transaction(connection):
            connection.execute("UPDATE free_practice_batch SET status='failed',error=?,updated_at=? WHERE id=?",
                (str(error)[:1000] or '生成未完成，可以按原设置重试。', datetime.now(UTC).isoformat(), batch_id))
    finally:
        connection.close()


def interrupt_batches(connection):
    """A restarted single-process app must not leave an old worker marked running."""
    with transaction(connection):
        connection.execute("UPDATE model_job SET status='failed',error='服务重启中断了模型请求，请按原请求重试；已保存的回答仍保留' "
                           "WHERE status='running'")
        connection.execute("UPDATE free_practice_batch SET status='interrupted',error='服务重启中断了这批练习，可以按原设置重试。',updated_at=? "
                           "WHERE status IN ('queued','running')", (datetime.now(UTC).isoformat(),))
