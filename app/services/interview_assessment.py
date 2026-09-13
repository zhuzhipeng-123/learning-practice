"""Record the user's code self-assessment without treating chat as an independent pass."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.domain import Submission
from app.services.interview import InterviewError
from app.services.practice import _complete_attempt
from app.services.review import enter_review
from app.services.tasks import _load_idempotent, _save_idempotent
from app.storage.ids import new_id
from app.storage.transactions import atomic


@atomic
def assess_code(connection, session_id, result, note, request_key):
    if result not in {'can_solve', 'cannot_solve'}:
        raise InterviewError('请选择会做或不会做')
    payload = {'session_id': session_id, 'result': result, 'note': note}
    saved = _load_idempotent(connection, request_key, 'interview_assessment', payload)
    if saved:
        return saved
    session = connection.execute(
        "SELECT s.task_id,s.question_version_id,t.question_id,v.review_basis_id FROM interview_session s "
        "JOIN task t ON t.id=s.task_id JOIN question q ON q.id=t.question_id "
        "JOIN question_version v ON v.id=s.question_version_id WHERE s.id=? AND q.question_type='code' "
        "AND EXISTS(SELECT 1 FROM interview_turn it WHERE it.session_id=s.id AND it.role='user')", (session_id,),
    ).fetchone()
    if not session:
        raise InterviewError('先保存这道代码题的回答，再记录自评')
    if connection.execute('SELECT 1 FROM attempt WHERE task_id=? AND submitted_at IS NOT NULL', (session['task_id'],)).fetchone():
        raise InterviewError('这道主问题已有自评记录，请到作答历史查看')
    now = datetime.now(UTC)
    attempt = connection.execute('SELECT id FROM attempt WHERE task_id=? AND submitted_at IS NULL', (session['task_id'],)).fetchone()
    attempt_id = attempt['id'] if attempt else new_id('attempt')
    if not attempt:
        connection.execute('INSERT INTO attempt(id,task_id,question_version_id,entry_mode,started_at) VALUES (?,?,?,?,?)',
                           (attempt_id, session['task_id'], session['question_version_id'], 'interview', now.isoformat()))
    _complete_attempt(connection, attempt_id, Submission(session['task_id'], request_key, now, 'interview', code_self_result=result, note=note),
                      now, now.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat())
    if result == 'cannot_solve':
        enter_review(connection, session['question_id'], session['review_basis_id'], 'interview_code_cannot_solve', now)
    response = {'attempt_id': attempt_id, 'result': result, 'counted_as_review_pass': False}
    _save_idempotent(connection, request_key, 'interview_assessment', payload, response, now.isoformat())
    return response
