"""Explicit plan edits redistribute only untouched tasks."""

import json
from datetime import UTC, datetime

from app.services.tasks import (
    _load_idempotent,
    _save_idempotent,
    _validate_targets,
    fill_daily_plan,
)
from app.services.theory_scope import (
    canonical_quotas,
    is_descendant,
    module_id,
    question_allowed,
    resolve_scope,
)
from app.storage.transactions import atomic


def _matches(row, module, scope):
    question_module = scope.get('question_modules', {}).get(module_id(row['source_id'], row['main_anchor_block_id']))
    return question_module == module or is_descendant(question_module, module, scope)


@atomic
def update_daily_plan(connection, plan_id, code_target, theory_target, module_quotas,
                      request_key=None, expected_state=None, required_date=None,
                      theory_scope=None, theory_catalog_version=None):
    payload = {'plan_id': plan_id, 'code_target': code_target, 'theory_target': theory_target,
                   'module_quotas': module_quotas, 'expected_state': expected_state}
    if theory_scope is not None or theory_catalog_version is not None:
        payload.update(theory_scope=theory_scope, theory_catalog_version=theory_catalog_version)
    if request_key:
        previous = _load_idempotent(connection, request_key, 'edit_plan', payload)
        if previous:
            return previous
    plan = connection.execute('SELECT * FROM daily_plan WHERE id=?', (plan_id,)).fetchone()
    if not plan:
        raise ValueError('今日计划不存在')
    if required_date and plan['plan_date'] != required_date.isoformat():
        raise ValueError('日期已变化，只能调整今天的计划；历史保存可按原请求恢复')
    previous_scope = json.loads(plan['theory_scope_json'] or '{}')
    scope = resolve_scope(connection, theory_scope, theory_catalog_version, theory_target, previous_scope)
    module_quotas = canonical_quotas(module_quotas, scope)
    _validate_targets(code_target, theory_target, module_quotas, scope)
    current = {'code_target': plan['code_target'], 'theory_target': plan['theory_target'],
                   'module_quotas': json.loads(plan['allocation_json'])}
    if expected_state is not None and expected_state != current:
        raise ValueError('计划已在其他页面更新，请重新读取后再保存；当前输入仍保留')
    rows = connection.execute("SELECT t.*,q.question_type,q.source_status,v.category_path,b.source_id,b.main_anchor_block_id,"
        "(t.status!='pending' OR EXISTS(SELECT 1 FROM attempt a WHERE a.task_id=t.id) "
        "OR EXISTS(SELECT 1 FROM interview_session s WHERE s.task_id=t.id)) protected "
        "FROM task t JOIN question q ON q.id=t.question_id JOIN question_version v ON v.id=t.question_version_id "
        "LEFT JOIN source_binding b ON b.question_id=q.id AND b.active=1 "
        "WHERE t.plan_id=? AND t.target_kind='base' AND t.status!='cancelled' ORDER BY t.created_at,t.id", (plan_id,)).fetchall()
    keep = set()
    for kind, target in [('code', code_target), ('theory', theory_target)]:
        group = [r for r in rows if r['question_type'] == kind]
        fixed = [r for r in group if r['protected']]
        if len(fixed) > target:
            raise ValueError(f"{'代码' if kind == 'code' else '八股'}已有 {len(fixed)} 道开始或完成，目标不能少于这个数量")
        keep.update(r['id'] for r in fixed)
        quotas = module_quotas if kind == 'theory' else {}
        needed = {p: max(0, n - sum(_matches(r, p, scope) for r in fixed)) for p, n in quotas.items()}
        if sum(needed.values()) > target - len(fixed):
            raise ValueError('已有作答属于其他模块，请减少指定模块题数，为已开始的题留出数量')
        for path, count in needed.items():
            chosen = [r for r in group if r['id'] not in keep and r['source_status'] == 'active' and
                      _matches(r, path, scope)][:count]
            keep.update(r['id'] for r in chosen)
        flexible = target - len(fixed) - sum(needed.values())
        extras = [r for r in group if r['id'] not in keep and r['source_status'] == 'active' and (
            kind == 'code' or question_allowed(r['question_id'], r['source_id'], r['main_anchor_block_id'], scope)
        )][:flexible]
        keep.update(r['id'] for r in extras)
    cancelled = [r['id'] for r in rows if r['id'] not in keep]
    for task_id in cancelled:
        connection.execute("UPDATE task SET status='cancelled' WHERE id=?", (task_id,))
    connection.execute('UPDATE daily_plan SET code_target=?,theory_target=?,allocation_json=?,theory_scope_json=? WHERE id=?',
                       (code_target, theory_target, json.dumps(module_quotas, ensure_ascii=False),
                        json.dumps(scope, ensure_ascii=False, sort_keys=True), plan_id))
    result = fill_daily_plan(connection, plan_id)
    result = {**result, 'cancelled': len(cancelled)}
    if request_key:
        _save_idempotent(connection, request_key, 'edit_plan', payload, result, datetime.now(UTC).isoformat())
    return result
