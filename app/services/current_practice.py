"""Current practice replaces abandoned tasks; review remains the durable collection."""

import json
import random
from datetime import UTC, datetime

from app.config import local_timezone_name
from app.services.learning_clock import local_today
from app.services.tasks import (
    IdempotencyConflictError,
    _load_idempotent,
    _save_idempotent,
    _select_questions,
    _validate_targets,
)
from app.services.theory_scope import canonical_quotas, resolve_scope
from app.storage.ids import new_id
from app.storage.transactions import atomic


def _latest_daily_batch(connection, plan_id):
    if not plan_id:
        return None
    row = connection.execute("SELECT result_json FROM idempotency_record WHERE operation='daily_batch' "
        "AND json_extract(result_json,'$.plan_id')=? ORDER BY rowid DESC LIMIT 1", (plan_id,)).fetchone()
    return json.loads(row[0]) if row else None


def current_daily_batch(connection, plan_id):
    result = _latest_daily_batch(connection, plan_id)
    if result and str(result.get('batch_key', '')).startswith('daily-auto:'):
        return None
    return result


def legacy_daily_batch(connection, plan_id):
    result = _latest_daily_batch(connection, plan_id)
    if result and str(result.get('batch_key', '')).startswith('daily-auto:'):
        return result
    return None


@atomic
def restore_daily(connection):
    day = local_today()
    plan = connection.execute('SELECT id FROM daily_plan WHERE plan_date=?', (day.isoformat(),)).fetchone()
    current = current_daily_batch(connection, plan[0]) if plan else None
    if current:
        return current
    return None


@atomic
def adopt_legacy_daily_batch(connection, day, request_key, expected_batch):
    payload = {'day': day.isoformat(), 'legacy_batch': expected_batch}
    previous = _load_idempotent(connection, request_key, 'daily_batch', payload)
    if previous:
        return previous
    if day != local_today():
        raise ValueError('日期已变化，请回到今天重新选择；旧页面输入仍保留')
    plan = connection.execute('SELECT id FROM daily_plan WHERE plan_date=?', (day.isoformat(),)).fetchone()
    legacy = legacy_daily_batch(connection, plan[0] if plan else None)
    if not legacy or legacy.get('batch_key') != expected_batch:
        raise IdempotencyConflictError('旧题单状态已变化，请重新读取今天页面后再决定')
    task_ids = legacy.get('task_ids') or []
    if not task_ids:
        raise ValueError('旧题单没有可恢复题目，请重新安排')
    placeholders = ','.join('?' for _ in task_ids)
    available = connection.execute(
        "SELECT COUNT(*) FROM task WHERE plan_id=? AND origin='daily' AND status!='cancelled' "
        f"AND id IN ({placeholders})",
        (plan[0], *task_ids),
    ).fetchone()[0]
    if available != len(task_ids):
        raise ValueError('旧题单已不完整，请重新安排；已提交的学习记录仍保留')
    now = datetime.now(UTC).isoformat()
    result = {**legacy, 'batch_key': request_key, 'adopted_from': expected_batch}
    _save_idempotent(connection, request_key, 'daily_batch', payload, result, now)
    return result


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
def draw_daily_batch(connection, day, code_target, theory_target, quotas, request_key, expected_batch=None,
                     theory_scope=None, theory_catalog_version=None):
    payload = {'day': day.isoformat(), 'code_target': code_target, 'theory_target': theory_target,
               'quotas': quotas, 'expected_batch': expected_batch}
    if theory_scope is not None or theory_catalog_version is not None:
        payload.update(theory_scope=theory_scope, theory_catalog_version=theory_catalog_version)
    previous = _load_idempotent(connection, request_key, 'daily_batch', payload)
    if previous:
        return previous
    if day != local_today():
        raise ValueError('日期已变化，请回到今天重新出题')
    plan = connection.execute('SELECT id FROM daily_plan WHERE plan_date=?', (day.isoformat(),)).fetchone()
    saved_plan = connection.execute('SELECT theory_scope_json FROM daily_plan WHERE id=?', (plan[0],)).fetchone() if plan else None
    if not saved_plan:
        saved_plan = connection.execute(
            "SELECT p.theory_scope_json FROM idempotency_record r JOIN daily_plan p "
            "ON p.id=json_extract(r.result_json,'$.plan_id') WHERE r.operation='daily_batch' "
            "AND json_extract(r.result_json,'$.batch_key') NOT LIKE 'daily-auto:%' "
            "ORDER BY p.plan_date DESC,r.rowid DESC LIMIT 1"
        ).fetchone()
    previous_scope = json.loads(saved_plan[0] or '{}') if saved_plan else None
    scope = resolve_scope(connection, theory_scope, theory_catalog_version, theory_target, previous_scope)
    quotas = canonical_quotas(quotas, scope)
    _validate_targets(code_target, theory_target, quotas, scope)
    current = current_daily_batch(connection, plan[0]) if plan else None
    if expected_batch is not None and expected_batch != (current['batch_key'] if current else ''):
        raise IdempotencyConflictError('题单已在其他页面更新，请刷新后再决定是否出新一批')
    retire_expired(connection)
    old = connection.execute("SELECT id FROM task WHERE origin='daily' AND status IN ('pending','in_progress')").fetchall()
    retire_tasks(connection, [row[0] for row in old])
    selected, shortages = _select_questions(connection, code_target, theory_target, quotas, random.Random(), scope=scope)
    if (code_target or theory_target) and not selected:
        raise ValueError('当前范围没有可安排的题目；原题单已保留，请调整范围或先更新题库')
    plan_id, now = plan[0] if plan else new_id('plan'), datetime.now(UTC).isoformat()
    if not plan:
        connection.execute(
            "INSERT INTO daily_plan(id,plan_date,timezone,code_target,theory_target,added_target,allocation_json,created_at,theory_scope_json) "
            "VALUES (?,?,?,?,?,0,?,?,?)",
            (
                plan_id,
                day.isoformat(),
                local_timezone_name(),
                code_target,
                theory_target,
                json.dumps(quotas),
                now,
                json.dumps(scope, ensure_ascii=False, sort_keys=True),
            ),
        )
    else:
        connection.execute('UPDATE daily_plan SET code_target=?,theory_target=?,allocation_json=?,theory_scope_json=? WHERE id=?',
                           (code_target, theory_target, json.dumps(quotas),
                            json.dumps(scope, ensure_ascii=False, sort_keys=True), plan_id))
    task_ids = []
    for question in selected:
        task_id = new_id('task')
        connection.execute("INSERT INTO task VALUES (?,?,?,?,'daily','base','pending',?,NULL,NULL,NULL)",
                           (task_id, plan_id, question['id'], question['current_version_id'], now))
        task_ids.append(task_id)
    result = {'plan_id': plan_id, 'batch_key': request_key, 'task_ids': task_ids, 'shortages': shortages,
              'theory_scope': scope}
    _save_idempotent(connection, request_key, 'daily_batch', payload, result, now)
    return result
