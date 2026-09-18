"""Preview an interview weakness before the user chooses to save it."""

import hashlib
import json
from datetime import UTC, datetime

from app.services.interview import InterviewError
from app.services.model_jobs import ModelJobError
from app.services.model_json import parse_model_json
from app.services.practice import add_theory_to_review
from app.storage.transactions import atomic


@atomic
def review_main_question(connection, session_id):
    row = connection.execute(
        "SELECT t.question_id,v.id AS version_id FROM interview_session s JOIN task t ON t.id=s.task_id "
        "JOIN question_version v ON v.id=s.question_version_id JOIN question q ON q.id=t.question_id "
        "WHERE s.id=? AND q.question_type='theory'", (session_id,),
    ).fetchone()
    if not row:
        raise InterviewError('没有可加入复习的八股主问题')
    return add_theory_to_review(connection, row['question_id'], 'interview:' + session_id, datetime.now(UTC), row['version_id'])


def validate_review_preview(value):
    if not isinstance(value, dict):
        raise ModelJobError('整理结果格式不正确，请重试或手动填写')
    for field, limit in [('prompt', 4000), ('reference_text', 20000)]:
        if not isinstance(value.get(field), str) or not 1 <= len(value[field].strip()) <= limit:
            raise ModelJobError('整理结果缺少独立题干或参考要点，请重试或手动填写')
    return value


def preview_review(connection, session_id, turn_id, request_key, client=None):
    existing = connection.execute('SELECT question_id FROM interview_derivation WHERE session_id=? AND turn_id=?', (session_id, turn_id)).fetchone()
    if existing:
        from app.services.knowledge_editing import read_knowledge
        saved = read_knowledge(connection, existing[0])
        return {**saved, 'reference_verified': saved['reference_verification']['verified']}
    from app.services.interview_conversation import selected_dialogue
    from app.services.module_jobs import run_module_job
    frozen = connection.execute('SELECT r.input_json FROM model_job j JOIN model_request r ON r.job_id=j.id '
                                'WHERE j.business_key=?', ('interview_review:' + request_key,)).fetchone()
    frozen = json.loads(frozen[0]) if frozen else None
    if frozen and (frozen.get('conversation_id') != session_id or frozen.get('selected_turn_id') != turn_id):
        raise InterviewError('同一请求不能用于另一场面试或另一条追问')
    row = connection.execute(
        'SELECT v.id AS question_version_id,v.prompt,v.reference_text,v.category_path,t.question_id FROM interview_session s '
        'JOIN task t ON t.id=s.task_id JOIN question_version v ON v.id=s.question_version_id WHERE s.id=?',
        (session_id,),
    ).fetchone()
    if not row:
        raise InterviewError('面试会话不存在')
    turns = [dict(turn) for turn in connection.execute(
        "SELECT it.id,it.role,it.content FROM interview_turn it WHERE it.session_id=? AND NOT EXISTS "
        "(SELECT 1 FROM model_job j WHERE j.result_id=it.id AND j.purpose='interview_feedback') ORDER BY it.rowid",
        (session_id,),
    )]
    if not any(turn['id'] == turn_id and turn['role'] == 'assistant' for turn in turns):
        raise InterviewError('请选择本场面试的一条追问')
    compact, window = selected_dialogue(turns, turn_id)
    context = {'main_question': row['prompt'][:4000], 'reference': (row['reference_text'] or '')[:8000],
               'question_version_id': row['question_version_id'],
               'turns': compact, 'context_window': window, 'selected_turn_id': turn_id, 'conversation_id': session_id}
    from app.services.interview_answers import reference_context, saved_reference
    context['selected_reference'] = (saved_reference(connection, reference_context(connection, session_id, turn_id))[0] or '')[:20000]
    target = frozen['source_id'] if frozen else hashlib.sha256(f'{session_id}:{turn_id}'.encode()).hexdigest()
    context['source_id'] = target
    response = run_module_job(connection, 'interview_review', target, request_key, client, context)
    return {**validate_review_preview(parse_model_json(response['response_text'])),
            'category_path': row['category_path'], 'reference_verified': False}
