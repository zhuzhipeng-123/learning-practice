"""Explicit plan edits redistribute only untouched tasks."""

import json

from app.services.tasks import _validate_targets, fill_daily_plan
from app.storage.transactions import atomic


def _matches(row, path):
    return row['category_path'] == path or row['category_path'].startswith(path + ' > ')


@atomic
def update_daily_plan(connection, plan_id, code_target, theory_target, module_quotas):
    _validate_targets(code_target, theory_target, module_quotas)
    plan = connection.execute('SELECT * FROM daily_plan WHERE id=?', (plan_id,)).fetchone()
    if not plan:
        raise ValueError('今日计划不存在')
    rows = connection.execute("SELECT t.*,q.question_type,q.source_status,v.category_path,"
        "(t.status!='pending' OR EXISTS(SELECT 1 FROM attempt a WHERE a.task_id=t.id) "
        "OR EXISTS(SELECT 1 FROM interview_session s WHERE s.task_id=t.id)) protected "
        "FROM task t JOIN question q ON q.id=t.question_id JOIN question_version v ON v.id=t.question_version_id "
        "WHERE t.plan_id=? AND t.target_kind='base' AND t.status!='cancelled' ORDER BY t.created_at,t.id", (plan_id,)).fetchall()
    keep = set()
    for kind, target in [('code', code_target), ('theory', theory_target)]:
        group = [r for r in rows if r['question_type'] == kind]
        fixed = [r for r in group if r['protected']]
        if len(fixed) > target:
            raise ValueError(f"{'代码' if kind == 'code' else '八股'}已有 {len(fixed)} 道开始或完成，目标不能少于这个数量")
        keep.update(r['id'] for r in fixed)
        quotas = module_quotas if kind == 'theory' else {}
        needed = {p: max(0, n - sum(_matches(r, p) for r in fixed)) for p, n in quotas.items()}
        if sum(needed.values()) > target - len(fixed):
            raise ValueError('已有作答属于其他模块，请减少指定模块题数，为已开始的题留出数量')
        for path, count in needed.items():
            chosen = [r for r in group if r['id'] not in keep and r['source_status'] == 'active' and _matches(r, path)][:count]
            keep.update(r['id'] for r in chosen)
        flexible = target - len(fixed) - sum(needed.values())
        extras = [r for r in group if r['id'] not in keep and r['source_status'] == 'active'][:flexible]
        keep.update(r['id'] for r in extras)
    cancelled = [r['id'] for r in rows if r['id'] not in keep]
    for task_id in cancelled:
        connection.execute("UPDATE task SET status='cancelled' WHERE id=?", (task_id,))
    connection.execute('UPDATE daily_plan SET code_target=?,theory_target=?,allocation_json=? WHERE id=?',
                       (code_target, theory_target, json.dumps(module_quotas, ensure_ascii=False), plan_id))
    result = fill_daily_plan(connection, plan_id)
    return {**result, 'cancelled': len(cancelled)}
