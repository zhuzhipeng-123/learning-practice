"""Explicit access to stored interview references, including legacy answer completion."""

import json
from datetime import UTC, datetime

from app.services.interview import InterviewError
from app.services.model_json import ModelJSONError, parse_model_json
from app.storage.transactions import transaction


def reference_context(connection, session_id, turn_id=None):
    row = connection.execute('SELECT s.id,s.question_version_id,v.prompt,v.reference_text,v.category_path,t.question_id,q.question_type,q.source_kind '
        'FROM interview_session s JOIN task t ON t.id=s.task_id JOIN question_version v ON v.id=s.question_version_id '
        'JOIN question q ON q.id=t.question_id WHERE s.id=?', (session_id,)).fetchone()
    if not row:
        raise InterviewError('面试会话不存在')
    question = row['prompt']
    if turn_id:
        turn = connection.execute("SELECT content FROM interview_turn WHERE id=? AND session_id=? AND role='assistant' "
            "AND NOT EXISTS(SELECT 1 FROM model_job WHERE result_id=interview_turn.id AND purpose='interview_feedback')",
            (turn_id, session_id)).fetchone()
        if not turn:
            raise InterviewError('请选择本场面试的一条追问')
        question = turn['content']
    target = f'interview-reference:{session_id}:{turn_id or "main"}'
    return {'source_id': target, 'conversation_id': session_id, 'turn_id': turn_id, 'question_id': row['question_id'],
            'question': question, 'question_type': row['question_type'], 'main_question': row['prompt'],
            'source_reference': row['reference_text'], 'source_kind': row['source_kind'], 'category_path': row['category_path'],
            'question_version_id': row['question_version_id']}


def saved_reference(connection, context):
    if not context['turn_id'] and context['source_reference']:
        return context['source_reference'], context['source_kind'] != 'feishu'
    if context['turn_id']:
        row = connection.execute("SELECT r.response_text FROM model_job j JOIN model_request r ON r.job_id=j.id "
            "WHERE j.result_id=? AND j.purpose='interview_followup' AND j.status='complete'", (context['turn_id'],)).fetchone()
        if row:
            try:
                pair = parse_model_json(row[0])
                if isinstance(pair, dict) and pair.get('question') == context['question'] and isinstance(pair.get('reference_text'), str):
                    return pair['reference_text'], True
            except ModelJSONError:
                pass  # Legacy follow-ups stored plain questions, not question/answer pairs.
    row = connection.execute("SELECT r.response_text FROM model_job j JOIN model_request r ON r.job_id=j.id "
        "WHERE r.module='interview_reference' AND j.status='complete' AND json_extract(r.input_json,'$.source_id')=? ORDER BY j.rowid DESC LIMIT 1",
        (context['source_id'],)).fetchone()
    return (parse_model_json(row[0])['reference_text'], True) if row else (None, False)


def view_reference(connection, session_id, turn_id=None):
    from app.services.materials import materials_for_role
    from app.services.reference_corrections import get_correction
    from app.services.reference_state import verification
    context = reference_context(connection, session_id, turn_id)
    reference, generated = saved_reference(connection, context)
    if not reference:
        return {'available': False, 'message': '这是一道旧面试题，当时没有保存参考答案。可以为原问题补充一份答案。'}
    with transaction(connection):
        connection.execute('INSERT OR IGNORE INTO question_exposure VALUES (?,?)', (context['question_id'], datetime.now(UTC).isoformat()))
    materials = []
    if not turn_id:
        resource = connection.execute('SELECT materials_json FROM version_resources WHERE version_id=?',
                                      (context['question_version_id'],)).fetchone()
        stored = json.loads(resource[0]) if resource else []
        materials = materials_for_role(stored, 'reference')
    return {'available': True, 'question': context['question'], 'reference_text': reference, 'model_generated': generated,
            'materials': materials, 'question_id': context['question_id'],
            'question_version_id': context['question_version_id'],
            'reference_correction': get_correction(connection, context['question_version_id']) if not turn_id else None,
            'reference_verification': verification(connection, context['question_version_id']) if not turn_id else {'verified': False}}


def generate_reference(connection, session_id, turn_id=None, client=None):
    from app.services.module_jobs import run_module_job
    context = reference_context(connection, session_id, turn_id)
    if saved_reference(connection, context)[0]:
        return view_reference(connection, session_id, turn_id)
    turns = [dict(row) for row in connection.execute("SELECT id,role,content FROM interview_turn WHERE session_id=? ORDER BY rowid", (session_id,))]
    from app.services.interview_conversation import selected_dialogue
    context['dialogue'], context['context_window'] = selected_dialogue(turns, turn_id)
    # A fixed question has one answer-completion job, also across tabs or a lost response.
    run_module_job(connection, 'interview_reference', context['source_id'], context['source_id'], client, context)
    return view_reference(connection, session_id, turn_id)
