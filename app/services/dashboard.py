import json
from datetime import date, timedelta

from app.parsers.docx import HEADING_TYPES, block_text
from app.services.wiki_sources import source_prefix


def module_tree(connection, question_type='theory'):
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


def latest_reflections(connection, day):
    result = {}
    for author in ('user', 'model'):
        row = connection.execute('SELECT * FROM reflection WHERE activity_date=? AND author=? ORDER BY stale,version DESC LIMIT 1', (day.isoformat(), author)).fetchone()
        result[author] = dict(row) if row else None
    return result


def activity_counts(connection, start, end, scope='all'):
    rows = connection.execute(
        "WITH activity(day,task_id) AS (SELECT activity_date,task_id FROM attempt WHERE submitted_at IS NOT NULL "
        "UNION SELECT date(it.created_at,'+8 hours'),s.task_id FROM interview_turn it "
        "JOIN interview_session s ON s.id=it.session_id WHERE it.role='user') "
        "SELECT a.day,COUNT(*) total,SUM(q.question_type='code') code,SUM(q.question_type='theory') theory "
        "FROM activity a JOIN task t ON t.id=a.task_id JOIN question q ON q.id=t.question_id "
        "WHERE a.day BETWEEN ? AND ? AND (?='all' OR t.origin=?) GROUP BY a.day", (start.isoformat(), end.isoformat(), scope, scope))
    return {row["day"]: dict(row) for row in rows}


def heatmap(connection, today, scope='all'):
    start = today - timedelta(days=181)
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
        "SELECT a.id,a.task_id,a.submitted_at,a.code_self_result,t.origin,q.question_type,v.prompt,v.category_path,e.verdict FROM attempt a "
        "JOIN task t ON t.id=a.task_id JOIN question q ON q.id=t.question_id JOIN question_version v ON v.id=a.question_version_id "
        "LEFT JOIN evaluation e ON e.attempt_id=a.id AND e.adopted=1 WHERE a.activity_date=? AND a.submitted_at IS NOT NULL "
        "AND (?='all' OR t.origin=?) ORDER BY a.submitted_at", (value, scope, scope))]
    interviews = [dict(row) for row in connection.execute(
        "SELECT s.id,v.prompt,COUNT(*) answers FROM interview_session s JOIN interview_turn it ON it.session_id=s.id "
        "JOIN question_version v ON v.id=s.question_version_id WHERE it.role='user' AND date(it.created_at,'+8 hours')=? "
        "GROUP BY s.id", (value,))]
    if scope not in {'all', 'interview'}:
        interviews = []
    reflections = [row for row in latest_reflections(connection, day).values() if row]
    groups = {'can': [], 'cannot': [], 'pending': [], 'unknown': []}
    latest = {attempt['task_id']: attempt for attempt in attempts}
    for attempt in latest.values():
        outcome = attempt['code_self_result'] if attempt['question_type'] == 'code' else attempt['verdict']
        key = {'can_solve': 'can', 'cannot_solve': 'cannot', 'aligned': 'can', 'needs_review': 'cannot', 'unable_to_assess': 'unknown'}.get(outcome, 'pending')
        groups[key].append(attempt)
    return {"day": value, "scope": scope, "groups": groups, "activity": activity_counts(connection, day, day, scope).get(value, {"total": 0, "code": 0, "theory": 0}),
            "attempts": attempts, "interviews": interviews, "reflections": reflections,
            "completed": connection.execute("SELECT COUNT(*) FROM task WHERE date(completed_at,'+8 hours')=? AND (?='all' OR origin=?)", (value, scope, scope)).fetchone()[0],
            "valid_passes": connection.execute("SELECT COUNT(*) FROM valid_review_pass p JOIN attempt a ON a.id=p.attempt_id JOIN task t ON t.id=a.task_id WHERE date(p.submitted_at,'+8 hours')=? AND (?='all' OR t.origin=?)", (value, scope, scope)).fetchone()[0]}
