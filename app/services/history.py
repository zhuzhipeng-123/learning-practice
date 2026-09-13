"""Read learning facts across entries without exposing saved answers by default."""


def question_history(connection, question_id, page=1):
    from app.services.reference_state import verification
    question = connection.execute(
        'SELECT q.*,v.prompt,v.category_path FROM question q JOIN question_version v ON v.id=q.current_version_id WHERE q.id=?',
        (question_id,),
    ).fetchone()
    if not question:
        return None
    where = ' FROM attempt a JOIN task t ON t.id=a.task_id WHERE t.question_id=? AND a.submitted_at IS NOT NULL'
    total = connection.execute('SELECT COUNT(*)' + where, (question_id,)).fetchone()[0]
    attempts = connection.execute(
        'SELECT a.id,a.task_id,a.activity_date,a.submitted_at,t.origin, '
        '(SELECT verdict FROM evaluation e WHERE e.attempt_id=a.id AND e.adopted=1) verdict, a.code_self_result' + where +
        ' ORDER BY a.submitted_at DESC,a.rowid DESC LIMIT 40 OFFSET ?', (question_id, (page-1)*40),
    ).fetchall()
    return {'question': question, 'attempts': attempts, 'page': page, 'has_more': page*40 < total,
            'reference_verification': verification(connection, question['current_version_id']),
            'versions': connection.execute('SELECT id,prompt,category_path,created_at,change_summary,material_status FROM question_version WHERE question_id=? ORDER BY rowid DESC', (question_id,)).fetchall(),
            'sessions': connection.execute('SELECT s.* FROM interview_session s JOIN task t ON t.id=s.task_id WHERE t.question_id=? ORDER BY s.created_at DESC', (question_id,)).fetchall(),
            'rounds': connection.execute('SELECT r.*,COUNT(p.id) passes,MIN(p.submitted_at) first_pass,MAX(p.submitted_at) last_pass FROM review_round r LEFT JOIN valid_review_pass p ON p.review_round_id=r.id WHERE r.question_id=? GROUP BY r.id ORDER BY r.started_at DESC', (question_id,)).fetchall(),
            'derived_from': connection.execute('SELECT * FROM interview_derivation WHERE question_id=?', (question_id,)).fetchone(),
            'children': connection.execute('SELECT d.question_id,v.prompt FROM interview_derivation d JOIN question q ON q.id=d.question_id JOIN question_version v ON v.id=q.current_version_id WHERE d.parent_question_id=?', (question_id,)).fetchall()}
