import json
from datetime import date, datetime, timedelta

from app.parsers.docx import HEADING_TYPES, block_text
from app.services.learning_clock import local_date, utc_bounds_for_local_days
from app.services.wiki_sources import source_prefix

HEATMAP_DAYS = 182


def module_tree(connection, question_type='theory', preserved_scope=None):
    if question_type == 'theory':
        return _theory_module_tree(connection, preserved_scope)
    roots = {}

    def add(path, field=None, count=0):
        if question_type == 'code':
            if path == '刷题':
                return
            path = '代码 > ' + path.removeprefix('刷题 > ')
        branch, parts = roots, []
        for label in path.split(" > "):
            if not label:
                continue
            parts.append(label)
            node = branch.setdefault(label, {"label": label, "path": " > ".join(parts), "total": 0,
                                             "available": 0, "pending": 0, "children": {}})
            if field:
                node[field] += count
            branch = node["children"]

    for row in connection.execute("SELECT ss.blocks_json,st.source_id FROM source_sync_state st JOIN source_snapshot ss ON ss.id=st.snapshot_id "
                                  "JOIN source s ON s.id=st.source_id WHERE s.question_type=? AND s.enabled=1", (question_type,)):
        path = {}
        prefix = source_prefix(connection, row['source_id'])
        if prefix:
            add(prefix)
        if question_type == 'theory':
            continue
        for block in json.loads(row[0]):
            level = HEADING_TYPES.get(block.get("block_type"))
            if level:
                path = {key: value for key, value in path.items() if key < level}
                path[level] = block_text(block)
                suffix = " > ".join(value for _, value in sorted(path.items()) if value)
                add(prefix + ' > ' + suffix if prefix else suffix)
    for row in connection.execute("SELECT v.category_path,COUNT(*) total,SUM(v.material_status IN ('complete','verified','text_complete') "
                                  "AND NOT EXISTS(SELECT 1 FROM task t WHERE t.question_id=q.id AND t.status IN ('pending','in_progress'))) available "
                                  "FROM question q JOIN question_version v ON v.id=q.current_version_id "
                                  "WHERE q.question_type=? AND q.source_kind='feishu' AND q.source_status='active' GROUP BY v.category_path", (question_type,)):
        add(row["category_path"], "total", row["total"])
        add(row["category_path"], "available", row["available"])
    for row in connection.execute("SELECT draft_json FROM parser_candidate c JOIN source s ON s.id=c.source_id "
                                  "WHERE c.status='pending' AND s.question_type=? AND s.enabled=1", (question_type,)):
        add(json.loads(row[0])["category_path"], "pending", 1)
    return list(roots.values())


def theory_document_options(connection, preserved_scope=None):
    """Return one compact daily-scope option per registered theory document."""
    documents = []
    for node in _theory_module_tree(connection, preserved_scope):
        full_path = node['path']
        label = full_path.rsplit(' > ', 1)[-1]
        if full_path == label == '八股':
            label = '八股主页'
        documents.append({**node, 'label': label, 'full_path': full_path, 'children': {}})
    return documents


def _theory_module_tree(connection, preserved_scope=None):
    from app.services.theory_scope import module_id, theory_catalog
    catalog = theory_catalog(connection)
    items = list(catalog['nodes'])
    known = {item['id'] for item in items}
    if preserved_scope and not catalog['complete']:
        items.extend({**item, 'label': item['label'] + '（目录暂不完整）', 'stale': True}
                     for item in preserved_scope.get('nodes', []) if item['id'] not in known)
    nodes = {item['id']: {**item, 'total': 0, 'available': 0, 'pending': 0, 'children': {}}
             for item in items}
    for node in nodes.values():
        parent = nodes.get(node['parent_id'])
        if parent:
            parent['children'][node['id']] = node
    rows = connection.execute(
        "SELECT q.id,q.source_status,v.material_status,b.source_id,b.main_anchor_block_id,"
        "EXISTS(SELECT 1 FROM task t WHERE t.question_id=q.id AND t.status IN ('pending','in_progress')) busy "
        "FROM question q JOIN question_version v ON v.id=q.current_version_id "
        "JOIN source_binding b ON b.question_id=q.id AND b.active=1 "
        "WHERE q.question_type='theory' AND q.source_kind='feishu'"
    ).fetchall()
    parents = {item['id']: item['parent_id'] for item in catalog['nodes']}
    for row in rows:
        question_key = module_id(row['source_id'], row['main_anchor_block_id'])
        node_id = catalog['question_modules'].get(question_key)
        while node_id:
            node = nodes.get(node_id)
            if node:
                node['total'] += int(row['source_status'] == 'active')
                node['available'] += int(row['source_status'] == 'active' and not row['busy'] and
                                         row['material_status'] in {'complete', 'verified', 'text_complete'})
            node_id = parents.get(node_id)
    return [node for node in nodes.values() if node['parent_id'] is None]


def latest_reflections(connection, day):
    result = {}
    for author in ('user', 'model'):
        row = connection.execute('SELECT * FROM reflection WHERE activity_date=? AND author=? ORDER BY stale,version DESC LIMIT 1', (day.isoformat(), author)).fetchone()
        result[author] = dict(row) if row else None
    return result


def activity_counts(connection, start, end, scope='all'):
    activity = {}
    for row in connection.execute(
        "SELECT DISTINCT a.activity_date day,a.task_id,q.question_type FROM attempt a "
        "JOIN task t ON t.id=a.task_id JOIN question q ON q.id=t.question_id "
        "WHERE a.submitted_at IS NOT NULL AND a.activity_date BETWEEN ? AND ? "
        "AND (?='all' OR t.origin=?)",
        (start.isoformat(), end.isoformat(), scope, scope),
    ):
        activity[(row["day"], row["task_id"])] = row["question_type"]
    start_at, end_at = utc_bounds_for_local_days(start, end)
    for row in connection.execute(
        "SELECT it.created_at,s.task_id,q.question_type FROM interview_turn it "
        "JOIN interview_session s ON s.id=it.session_id "
        "JOIN task t ON t.id=s.task_id JOIN question q ON q.id=t.question_id "
        "WHERE it.role='user' AND julianday(it.created_at)>=julianday(?) "
        "AND julianday(it.created_at)<julianday(?) AND (?='all' OR t.origin=?)",
        (start_at, end_at, scope, scope),
    ):
        day = local_date(datetime.fromisoformat(row["created_at"]))
        if start <= day <= end:
            activity[(day.isoformat(), row["task_id"])] = row["question_type"]
    counts = {}
    for (day, _), question_type in activity.items():
        value = counts.setdefault(day, {"day": day, "total": 0, "code": 0, "theory": 0})
        value["total"] += 1
        value[question_type] += 1
    return counts


def heatmap(connection, today, scope='all'):
    start = today - timedelta(days=HEATMAP_DAYS - 1)
    start -= timedelta(days=start.weekday())
    counts = activity_counts(connection, start, today, scope)
    cells = []
    for offset in range((today - start).days + 1):
        day = start + timedelta(days=offset)
        value = counts.get(day.isoformat(), {"total": 0, "code": 0, "theory": 0})
        cells.append({"date": day.isoformat(), "weekday": day.weekday(), "month": day.month,
                      **value, "level": min(4, value["total"])})
    return {"scope": scope, "weeks": [cells[i:i + 7] for i in range(0, len(cells), 7)],
            "active_days": len(counts), "activities": sum(row["total"] for row in counts.values())}


def day_details(connection, day: date, scope='all'):
    value = day.isoformat()
    attempts = [dict(row) for row in connection.execute(
        "SELECT a.id,a.task_id,a.submitted_at,a.code_self_result,t.origin,q.question_type,v.prompt,v.category_path,e.verdict,"
        "EXISTS(SELECT 1 FROM mastery_assessment ma WHERE ma.attempt_id=a.id) AS explicit_unable,"
        "(SELECT ma.level FROM mastery_assessment ma WHERE ma.question_id=t.question_id "
        "AND ma.review_basis_id=v.review_basis_id ORDER BY ma.rowid DESC LIMIT 1) AS mastery_level FROM attempt a "
        "JOIN task t ON t.id=a.task_id JOIN question q ON q.id=t.question_id JOIN question_version v ON v.id=a.question_version_id "
        "LEFT JOIN evaluation e ON e.attempt_id=a.id AND e.adopted=1 WHERE a.activity_date=? AND a.submitted_at IS NOT NULL "
        "AND (?='all' OR t.origin=?) ORDER BY a.submitted_at", (value, scope, scope))]
    start_at, end_at = utc_bounds_for_local_days(day, day)
    interview_rows = connection.execute(
        "SELECT s.id,s.task_id,t.origin,q.question_type,v.prompt,v.category_path,it.created_at "
        "FROM interview_session s JOIN interview_turn it ON it.session_id=s.id "
        "JOIN task t ON t.id=s.task_id JOIN question q ON q.id=t.question_id "
        "JOIN question_version v ON v.id=s.question_version_id WHERE it.role='user' "
        "AND julianday(it.created_at)>=julianday(?) AND julianday(it.created_at)<julianday(?) "
        "AND (?='all' OR t.origin=?)",
        (start_at, end_at, scope, scope),
    )
    interviews_by_id = {}
    for row in interview_rows:
        if local_date(datetime.fromisoformat(row["created_at"])) != day:
            continue
        item = interviews_by_id.setdefault(row["id"], {**dict(row), "answers": 0})
        item["answers"] += 1
    interviews = list(interviews_by_id.values())
    reflections = [row for row in latest_reflections(connection, day).values() if row]
    groups = {'can': [], 'cannot': [], 'pending': [], 'unknown': []}
    latest = {attempt['task_id']: attempt for attempt in attempts}
    for interview in interviews:
        if interview['task_id'] not in latest:
            groups['pending'].append({**interview, 'session_id': interview['id'], 'verdict': None})
    for attempt in latest.values():
        outcome = 'explicit_unable' if attempt['explicit_unable'] else (
            attempt['code_self_result'] if attempt['question_type'] == 'code' else attempt['verdict'])
        key = {'can_solve': 'can', 'cannot_solve': 'cannot', 'explicit_unable': 'cannot',
               'aligned': 'can', 'needs_review': 'cannot', 'unable_to_assess': 'unknown'}.get(outcome, 'pending')
        groups[key].append(attempt)
    return {"day": value, "scope": scope, "groups": groups, "activity": activity_counts(connection, day, day, scope).get(value, {"total": 0, "code": 0, "theory": 0}),
            "attempts": attempts, "interviews": interviews, "reflections": reflections,
            "completed": _completed_count(connection, day, scope),
            "valid_passes": connection.execute("SELECT COUNT(*) FROM valid_review_pass p JOIN attempt a ON a.id=p.attempt_id JOIN task t ON t.id=a.task_id WHERE a.activity_date=? AND (?='all' OR t.origin=?)", (value, scope, scope)).fetchone()[0]}


def _completed_count(connection, day, scope):
    start_at, end_at = utc_bounds_for_local_days(day, day)
    rows = connection.execute(
        "SELECT completed_at FROM task WHERE completed_at IS NOT NULL "
        "AND julianday(completed_at)>=julianday(?) AND julianday(completed_at)<julianday(?) "
        "AND (?='all' OR origin=?)",
        (start_at, end_at, scope, scope),
    )
    return sum(local_date(datetime.fromisoformat(row[0])) == day for row in rows)
