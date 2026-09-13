import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.interview import add_turn
from app.services.interview_assessment import assess_code
from app.services.interview_setup import prepare_direction, prepare_interview
from app.services.model_jobs import ModelJobError, _claim_job
from app.storage.dependencies import get_database
from tests.test_practice_review import seed_question

HEADERS = {'X-Requested-With': 'learning-practice', 'Idempotency-Key': 'flow-recovery'}


@pytest.mark.parametrize('busy,cooldown,state,status', [
    (False, False, 'failed', 409), (True, False, 'pending', 409), (False, True, 'pending', 429),
])
def test_model_error_exposes_definite_failure_vs_recoverable_request(monkeypatch, busy, cooldown, state, status):
    def fail(*args, **kwargs):
        raise ModelJobError('fixture failure', datetime.now(UTC)+timedelta(minutes=1) if cooldown else None, in_progress=busy)
    monkeypatch.setattr('app.routes.model_api.run_module_job', fail)
    with TestClient(app) as client:
        response = client.post('/api/interviews/example/generate/followup', headers=HEADERS)
    assert response.status_code == status
    assert response.json() == {'detail': 'fixture failure', 'request_state': state}


def test_failed_opening_is_explicit_even_with_http_502(monkeypatch):
    def fail(*args, **kwargs):
        raise ModelJobError('未生成完整问题与答案')
    monkeypatch.setattr('app.services.interview_setup.prepare_direction', fail)
    with TestClient(app) as client:
        response = client.post('/api/interviews/direction', headers=HEADERS,
                               json={'mode':'opening', 'direction':'工具调用恢复', 'job_focus':'', 'avoid':[]})
    assert response.status_code == 502
    assert response.json()['request_state'] == 'failed'


def test_busy_lease_does_not_run_or_clear_existing_request(database):
    now = datetime.now(UTC).isoformat()
    database.execute("INSERT INTO model_job VALUES ('busy','busy','theory_evaluation','running',0,NULL,NULL,NULL,?,?)", (now, now))
    database.commit()
    with pytest.raises(ModelJobError) as error:
        _claim_job(database, 'busy')
    assert error.value.in_progress
    assert database.execute("SELECT status FROM model_job WHERE id='busy'").fetchone()[0] == 'running'


def test_code_assessment_page_restores_result_and_disables_resubmission(database):
    question_id = seed_question(database, 'code')
    session = prepare_interview(database, question_id, '', '', 'setup')
    now = datetime.now(UTC)
    add_turn(database, session['session_id'], 'user', '我的解题思路', now, 'answer')
    result = assess_code(database, session['session_id'], 'cannot_solve', '', 'assessment')
    assert assess_code(database, session['session_id'], 'cannot_solve', '', 'assessment') == result
    app.dependency_overrides[get_database] = lambda: database
    try:
        with TestClient(app) as client:
            page = client.get('/interview/'+session['session_id'])
        assert page.status_code == 200
        assert '已记录不会做，并加入复习库。' in page.text
        assert 'data-interview-code="can_solve" disabled' in page.text
        assert 'data-interview-code="cannot_solve" disabled' in page.text
    finally:
        app.dependency_overrides.clear()


def test_overlong_suggestions_are_regenerated_once_and_original_is_retained(database):
    replies = iter([{'directions':['a'*101,'b','c']}, {'directions':['并发控制','工具恢复','检索评估']}])
    calls = []
    def complete(messages, **kwargs):
        calls.append(messages)
        return SimpleNamespace(content=json.dumps(next(replies)), model='fixture')
    result = prepare_direction(database, 'suggest', '', 'Agent', [], 'repair-once', SimpleNamespace(complete=complete))
    assert result == {'directions':['并发控制','工具恢复','检索评估']}
    assert len(calls) == 2
    config = json.loads(database.execute('SELECT config_json FROM model_request').fetchone()[0])
    assert 'a'*101 in config['format_repair']['rejected_response']


def test_suggestion_repair_is_bounded_and_never_creates_questions(database):
    calls = []
    def complete(messages, **kwargs):
        calls.append(messages)
        return SimpleNamespace(content='{}', model='fixture')
    with pytest.raises(ModelJobError):
        prepare_direction(database, 'suggest', '', '', [], 'still-invalid', SimpleNamespace(complete=complete))
    assert len(calls) == 2
    assert database.execute('SELECT COUNT(*) FROM question').fetchone()[0] == 0


def test_reflection_uses_primary_dialogue_and_explicit_code_assessment(database):
    from app.services.learning_clock import local_today
    from app.services.module_jobs import reflection_context, run_module_job
    from tests.interview_fixtures import feedback_json
    question_id = seed_question(database, 'code')
    session = prepare_interview(database, question_id, '', '', 'reflection-setup')['session_id']
    add_turn(database, session, 'user', '需要练习', datetime.now(UTC), 'reflection-answer')
    assess_code(database, session, 'cannot_solve', '我未找到解法', 'reflection-assessment')
    model = SimpleNamespace(complete=lambda messages, **k:SimpleNamespace(content=feedback_json(messages,'MODEL SUMMARY MUST NOT BECOME PRIMARY EVIDENCE'), model='fixture'))
    run_module_job(database, 'interview_feedback', session, 'feedback-context', model)
    context = reflection_context(database, local_today())
    assert 'MODEL SUMMARY' not in json.dumps(context)
    assert context['wrong_answers'][0]['assessment_status'] == '自评不会，已知薄弱点'
    assert context['wrong_answers'][0]['note'] == '我未找到解法'


def test_reflection_repairs_internal_ids_and_literal_newlines_before_saving(database):
    from app.services.learning_clock import local_today
    from app.services.module_jobs import run_module_job
    question_id = seed_question(database, 'theory')
    session = prepare_interview(database, question_id, '', '', 'reflection-format')['session_id']
    turn = add_turn(database, session, 'user', '我的实际回答', datetime.now(UTC), 'reflection-turn')
    outputs = iter([{'content':turn+'\\n一段不可读的复盘', 'covered_ids':[turn]},
                    {'content':'今天练习了理论题。\n下一次补充解释。', 'covered_ids':[turn]}])
    fake = SimpleNamespace(complete=lambda *a, **k:SimpleNamespace(content=json.dumps(next(outputs)), model='fixture'))
    result = run_module_job(database, 'daily_reflection', local_today().isoformat(), 'reflection-clean', fake)
    assert result['content'] == '今天练习了理论题。\n下一次补充解释。'
    assert database.execute("SELECT COUNT(*) FROM reflection WHERE author='model'").fetchone()[0] == 1


def test_generated_reference_is_not_used_as_an_authoritative_feedback_rubric(database):
    from app.services.module_jobs import interview_context
    session = prepare_interview(database, None, '怎样设计工具超时？', '', 'generated-opening', '尚未人工核实的模型参考')['session_id']
    add_turn(database, session, 'user', '应区分超时和确定失败', datetime.now(UTC), 'generated-answer')
    context = interview_context(database, session, 'interview_feedback')
    assert context['reference'] is None
    assert context['turns'][0]['content'] == '应区分超时和确定失败'


def test_unanswered_followups_are_matched_within_the_same_session():
    from app.services.module_jobs import unanswered_questions
    turns = [{'id':'question-a', 'session_id':'a', 'role':'assistant'},
             {'id':'question-b', 'session_id':'b', 'role':'assistant'},
             {'id':'answer-a', 'session_id':'a', 'role':'user'},
             {'id':'question-a2', 'session_id':'a', 'role':'assistant'}]
    assert unanswered_questions(turns) == ['question-b', 'question-a2']
