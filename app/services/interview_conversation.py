"""Conversation state and bounded evidence for adaptive interview turns."""

from datetime import UTC, datetime

from app.services.interview import InterviewError


def conversation_turns(connection, session_id):
    return [dict(row) for row in connection.execute(
        "SELECT t.id,t.role,t.content FROM interview_turn t WHERE t.session_id=? AND NOT EXISTS "
        "(SELECT 1 FROM model_job j WHERE j.result_id=t.id AND j.purpose='interview_feedback') ORDER BY t.rowid",
        (session_id,))]


def conversation_revision(connection, session_id):
    row = connection.execute("SELECT t.id FROM interview_turn t WHERE t.session_id=? AND NOT EXISTS "
        "(SELECT 1 FROM model_job j WHERE j.result_id=t.id AND j.purpose='interview_feedback') ORDER BY t.rowid DESC LIMIT 1",
        (session_id,)).fetchone()
    return row[0] if row else ''


def live_conversation(connection, session_id, pending_revision=None):
    session = connection.execute('SELECT status FROM interview_session WHERE id=?', (session_id,)).fetchone()
    if not session or session['status'] != 'active':
        raise InterviewError('会话已结束或不存在，请通过查看已保存对话回顾这场面试')
    turns = conversation_turns(connection, session_id)
    answer = next((turn['id'] for turn in reversed(turns) if turn['role'] == 'user'), '')
    revision = pending_revision if pending_revision is not None else answer
    job = connection.execute(
        'SELECT status,result_id,created_at,updated_at FROM model_job WHERE business_key=?',
        (f'interview_followup:{session_id}:{revision}',)).fetchone() if revision else None
    continuation = {**dict(job), 'answer_revision': revision} if job else None
    if continuation and continuation['status'] == 'running':
        from app.services.model_jobs import LEASE_DURATION
        started = datetime.fromisoformat(continuation['updated_at'])
        if datetime.now(UTC) - started.replace(tzinfo=started.tzinfo or UTC) >= LEASE_DURATION:
            continuation['status'] = 'expired'
    return {'turns': turns, 'revision': turns[-1]['id'] if turns else '', 'status': session['status'],
            'continuation': continuation}


def bounded_dialogue(turns):
    # Keep the latest answer verbatim. Earlier excerpts are labelled, never model summaries.
    selected = turns[-24:]
    compact = []
    for index, turn in enumerate(selected):
        limit = 30000 if index == len(selected) - 1 else 1000
        content = turn['content']
        compact.append({**turn, 'content': content[:limit], **({'excerpt': True} if len(content) > limit else {})})
    return compact, {'total_turns': len(turns), 'omitted_turns': len(turns) - len(selected),
                     'excerpted_turns': sum(bool(turn.get('excerpt')) for turn in compact),
                     'limitations': '较早对话可能只保留片段；未出现的细节不能判定为未回答。完整对话仍保存在本地。'}


def learning_context(connection):
    weak = connection.execute(
        "SELECT t.question_id,substr(v.prompt,1,400) AS question,a.id AS evidence_id,"
        "a.code_self_result,e.verdict,substr(COALESCE(a.answer_text,a.note,''),1,600) AS answer_excerpt "
        "FROM attempt a JOIN task t ON t.id=a.task_id JOIN question_version v ON v.id=a.question_version_id "
        "LEFT JOIN evaluation e ON e.attempt_id=a.id AND e.adopted=1 "
        "WHERE a.submitted_at IS NOT NULL AND (a.code_self_result='cannot_solve' OR e.verdict='needs_review') "
        "AND NOT EXISTS(SELECT 1 FROM attempt newer JOIN task nt ON nt.id=newer.task_id "
        "WHERE nt.question_id=t.question_id AND newer.submitted_at IS NOT NULL AND newer.rowid>a.rowid) "
        "ORDER BY a.rowid DESC LIMIT 8").fetchall()
    unseen = connection.execute(
        "WITH candidates AS (SELECT q.id AS question_id,substr(v.prompt,1,300) AS question,"
        "substr(v.category_path,1,200) AS category,ROW_NUMBER() OVER (PARTITION BY v.category_path ORDER BY q.id) AS n "
        "FROM question q JOIN question_version v ON v.id=q.current_version_id "
        "WHERE q.source_kind='feishu' AND q.source_status='active' AND q.first_submitted_at IS NULL "
        "AND v.material_status IN ('complete','verified','text_complete') "
        "AND NOT EXISTS(SELECT 1 FROM attempt a JOIN task t ON t.id=a.task_id WHERE t.question_id=q.id) "
        "AND NOT EXISTS(SELECT 1 FROM question_exposure e WHERE e.question_id=q.id) "
        "AND NOT EXISTS(SELECT 1 FROM interview_session s JOIN task t ON t.id=s.task_id WHERE t.question_id=q.id)) "
        "SELECT question_id,question,category FROM candidates WHERE n<=2 ORDER BY n,category LIMIT 16").fetchall()
    return {'weak_points': [dict(row) for row in weak], 'unpracticed': [dict(row) for row in unseen],
            'limitations': '只列部分本地记录。未练是无本地作答、查看参考和面试记录，不代表现实中没见过或不会。'
                            '错题仅代表该次自评或已采用评价；结合当前回答重新判断。只选择与岗位或本场方向相关的线索。'}
