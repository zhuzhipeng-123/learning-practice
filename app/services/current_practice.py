"""Current practice replaces abandoned tasks; review remains the durable collection."""

import json
import random
from datetime import UTC, datetime

from app.services.learning_clock import local_today
from app.services.tasks import (
    IdempotencyConflictError,
    _load_idempotent,
    _save_idempotent,
    _select_questions,
    _validate_targets,
)
from app.storage.ids import new_id
from app.storage.transactions import atomic


def current_daily_batch(connection, plan_id):
    row = connection.execute("SELECT result_json FROM idempotency_record WHERE operation='daily_batch' "
        "AND json_extract(result_json,'$.plan_id')=? ORDER BY rowid DESC LIMIT 1", (plan_id,)).fetchone()
    return json.loads(row[0]) if row else None


def retire_tasks(connection, task_ids):
    for task_id in task_ids:
        connection.execute("UPDATE task SET status='cancelled' WHERE id=? AND status IN ('pending','in_progress')", (task_id,))
        connection.execute("UPDATE interview_session SET status='ended',ended_at=? WHERE task_id=? AND status='active' "
                           "AND EXISTS(SELECT 1 FROM task WHERE id=? AND status='cancelled')",
                           (datetime.now(UTC).isoformat(), task_id, task_id))


@atomic
def retire_expired(connection):
    rows = connection.execute("SELECT t.id FROM task t JOIN daily_plan p ON p.id=t.plan_id "
        "WHERE t.origin IN ('daily','free_practice') AND t.status IN ('pending','in_progress') AND p.plan_date<?",
        (local_today().isoformat(),)).fetchall()
    retire_tasks(connection, [row[0] for row in rows])


def replace_free_tasks(connection, kind, keep_ids):
    rows = connection.execute("SELECT t.id FROM task t JOIN question q ON q.id=t.question_id "
        "WHERE t.origin='free_practice' AND q.question_type=? AND t.status IN ('pending','in_progress')", (kind,)).fetchall()
    retire_tasks(connection, [row[0] for row in rows if row[0] not in keep_ids])


@atomic
def draw_daily_batch(connection, day, code_target, theory_target, quotas, request_key, expected_batch=None):
    payload = {'day': day.isoformat(), 'code_target': code_target, 'theory_target': theory_target,
               'quotas': quotas, 'expected_batch': expected_batch}
    previous = _load_idempotent(connection, request_key, 'daily_batch', payload)
    if previous:
        return previous
    if day != local_today():
        raise ValueError('日期已变化，请回到今天重新出题')
    _validate_targets(code_target, theory_target, quotas)
    plan = connection.execute('SELECT id FROM daily_plan WHERE plan_date=?', (day.isoformat(),)).fetchone()
    current = current_daily_batch(connection, plan[0]) if plan else None
    if expected_batch is not None and expected_batch != (current['batch_key'] if current else ''):
        raise IdempotencyConflictError('题单已在其他页面更新，请刷新后再决定是否出新一批')
    retire_expired(connection)
    old = connection.execute("SELECT id FROM task WHERE origin='daily' AND status IN ('pending','in_progress')").fetchall()
    retire_tasks(connection, [row[0] for row in old])
    selected, shortages = _select_questions(connection, code_target, theory_target, quotas, random.Random())
    plan_id, now = plan[0] if plan else new_id('plan'), datetime.now(UTC).isoformat()
    if not plan:
        connection.execute("INSERT INTO daily_plan VALUES (?,?,'Asia/Shanghai',?,?,0,?,?)",
                           (plan_id, day.isoformat(), code_target, theory_target, json.dumps(quotas), now))
    else:
        connection.execute('UPDATE daily_plan SET code_target=?,theory_target=?,allocation_json=? WHERE id=?',
                           (code_target, theory_target, json.dumps(quotas), plan_id))
    task_ids = []
    for question in selected:
        task_id = new_id('task')
        connection.execute("INSERT INTO task VALUES (?,?,?,?,'daily','base','pending',?,NULL,NULL,NULL)",
                           (task_id, plan_id, question['id'], question['current_version_id'], now))
        task_ids.append(task_id)
    result = {'plan_id': plan_id, 'batch_key': request_key, 'task_ids': task_ids, 'shortages': shortages}
    _save_idempotent(connection, request_key, 'daily_batch', payload, result, now)
    return result
