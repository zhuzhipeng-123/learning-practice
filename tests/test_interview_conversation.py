import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.interview import InterviewError, add_turn, end_session, start_session
from app.services.model_jobs import ModelJobError
from app.services.module_jobs import interview_context, run_module_job
from app.storage.database import connect_database
from tests.interview_fixtures import feedback_json
from tests.test_practice_review import make_plan, seed_question

NOW = datetime.now(UTC)
HEADERS = {'X-Requested-With': 'learning-practice'}


def session_with_answer(database):
    seed_question(database, 'theory')
    task = make_plan(database, 'theory')
    session = start_session(database, task['id'], NOW)
    answer = add_turn(database, session, 'user', 'I do not understand the assumptions in this question.', NOW)
    return session, answer


def test_live_conversation_excludes_feedback_and_does_not_expose_answers():
    with TestClient(app) as client:
        database = connect_database(app.state.database_path)
        database.execute("DELETE FROM source WHERE id='source-theory'")
        session, _ = session_with_answer(database)
        run_module_job(database, 'interview_feedback', session, 'summary',
                       SimpleNamespace(complete=lambda messages, **k: SimpleNamespace(content=feedback_json(messages,'PRIVATE MODEL FEEDBACK'), model='fake')))
        exposures = database.execute('SELECT COUNT(*) FROM question_exposure').fetchone()[0]
        response = client.get(f'/api/interviews/{session}/conversation')
        assert response.status_code == 200
        value = response.json()
        assert len(value['turns']) == 1 and value['turns'][0]['role'] == 'user'
        assert 'PRIVATE MODEL FEEDBACK' not in response.text and 'reference_text' not in value
        assert database.execute('SELECT COUNT(*) FROM question_exposure').fetchone()[0] == exposures
        end_session(database, session, NOW)
        assert client.get(f'/api/interviews/{session}/conversation').status_code == 409
        database.close()


def test_stale_tab_answer_is_rejected_but_same_request_replays(database):
    session, answer = session_with_answer(database)
    first = add_turn(database, session, 'user', 'A clarification', NOW, 'answer-key', expected_revision=answer)
    question = add_turn(database, session, 'assistant', 'What would change if the input could be empty?', NOW)
    assert add_turn(database, session, 'user', 'A clarification', NOW, 'answer-key', expected_revision=answer) == first
    with pytest.raises(InterviewError, match='对话已更新'):
        add_turn(database, session, 'user', 'An answer from the stale tab', NOW, 'stale-answer', expected_revision=answer)
    assert database.execute('SELECT id FROM interview_turn ORDER BY rowid DESC LIMIT 1').fetchone()[0] == question


def test_followup_rejects_stale_revision_before_calling_model(database):
    session, answer = session_with_answer(database)
    add_turn(database, session, 'user', 'Newer supplement', NOW)
    def fail(*a, **k):
        pytest.fail('A stale follow-up must not call the model')
    with pytest.raises(ModelJobError, match='对话已更新'):
        run_module_job(database, 'interview_followup', session, 'stale-followup', SimpleNamespace(complete=fail), expected_revision=answer)
    assert database.execute("SELECT COUNT(*) FROM model_job WHERE purpose='interview_followup'").fetchone()[0] == 0


def test_long_conversation_has_bounded_context_and_preserves_latest_answer(database):
    session, _ = session_with_answer(database)
    for index in range(20):
        add_turn(database, session, 'assistant', f'Question {index}: ' + 'context ' * 500, NOW)
        add_turn(database, session, 'user', f'Answer {index}: ' + 'detail ' * 3000, NOW)
    latest = add_turn(database, session, 'user', 'CURRENT ANSWER ' + 'z' * 29000, NOW)
    context = interview_context(database, session, 'interview_followup')
    assert len(json.dumps(context, ensure_ascii=False)) < 100000
    assert context['turns'][-1]['id'] == latest
    assert context['turns'][-1]['content'].endswith('z' * 29000)
    assert context['context_window']['omitted_turns'] > 0
    assert context['conversation_revision'] == latest
    assert database.execute('SELECT COUNT(*) FROM interview_turn').fetchone()[0] == 42


def test_interview_learning_context_distinguishes_unseen_from_unsubmitted(database):
    from app.services.interview_conversation import learning_context
    session, _ = session_with_answer(database)
    seed_question(database, 'code')
    question = database.execute("SELECT id FROM question WHERE question_type='code'").fetchone()[0]
    assert any(item['question_id'] == question for item in learning_context(database)['unpracticed'])
    database.execute('INSERT INTO question_exposure VALUES (?,?)', (question, NOW.isoformat()))
    database.commit()
    assert not any(item['question_id'] == question for item in learning_context(database)['unpracticed'])
    assert interview_context(database, session, 'interview_followup')['learning_context']['limitations']


def test_failed_continuation_keeps_config_across_new_browser_request_keys(database):
    from app.services.llm_config import save_module_config
    session, answer = session_with_answer(database)
    save_module_config(database, 'interview_followup', 'old-model', 'My interview style', 2048)
    def failure(*a, **k):
        raise RuntimeError('model offline')
    with pytest.raises(ModelJobError):
        run_module_job(database, 'interview_followup', session, 'browser-key-1', SimpleNamespace(complete=failure), expected_revision=answer)
    save_module_config(database, 'interview_followup', 'new-model', 'Changed style', 3072)
    calls = []
    def success(messages, **kwargs):
        calls.append(messages)
        assert 'My interview style' in messages[0]['content'] and 'Changed style' not in messages[0]['content']
        assert '请求澄清' in messages[0]['content']
        return SimpleNamespace(content=json.dumps({'question':'What assumption would you like to clarify?', 'reference_text':'State the input constraints explicitly before choosing an algorithm.'}), model='fake')
    first = run_module_job(database, 'interview_followup', session, 'browser-key-2', SimpleNamespace(complete=success), expected_revision=answer)
    assert run_module_job(database, 'interview_followup', session, 'browser-key-3', SimpleNamespace(complete=success), expected_revision=answer) == first
    assert len(calls) == 1
    assert database.execute("SELECT COUNT(*) FROM model_job WHERE purpose='interview_followup'").fetchone()[0] == 1
    assert database.execute("SELECT COUNT(*) FROM interview_turn WHERE role='assistant'").fetchone()[0] == 1
    assert 'reference_text' not in first['response_text']


def test_other_tab_cannot_start_second_model_for_same_conversation(database):
    session, answer = session_with_answer(database)
    def outer(*a, **k):
        with pytest.raises(ModelJobError) as caught:
            run_module_job(database, 'interview_followup', session, 'other-tab', SimpleNamespace(complete=lambda *a, **k: pytest.fail('Duplicate model call')), expected_revision=answer)
        assert caught.value.in_progress
        return SimpleNamespace(content=json.dumps({'question':'How would you test the empty input case?', 'reference_text':'Define the expected result for empty input, then assert that result in a focused test.'}), model='fake')
    run_module_job(database, 'interview_followup', session, 'first-tab', SimpleNamespace(complete=outer), expected_revision=answer)
    assert database.execute("SELECT COUNT(*) FROM model_job WHERE purpose='interview_followup'").fetchone()[0] == 1


def test_api_rejects_stale_draft_without_inserting_it():
    with TestClient(app) as client:
        database = connect_database(app.state.database_path)
        database.execute("DELETE FROM source WHERE id='source-theory'")
        session, answer = session_with_answer(database)
        newer = add_turn(database, session, 'assistant', 'What about an empty input?', NOW)
        response = client.post(f'/api/interviews/{session}/turns', headers={**HEADERS,'Idempotency-Key':'stale-draft'},
                               json={'role':'user','content':'My old draft','created_at':NOW.isoformat(),'expected_revision':answer})
        assert response.status_code == 409 and '对话已更新' in response.text
        assert database.execute('SELECT id FROM interview_turn ORDER BY rowid DESC LIMIT 1').fetchone()[0] == newer
        database.close()


def test_weak_point_is_removed_after_adopted_assessment_is_corrected(database):
    from app.services.interview_conversation import learning_context
    from app.services.practice import adopt_theory_evaluation
    from tests.test_repair_business import submitted_theory
    result = submitted_theory(database)
    assert learning_context(database)['weak_points'] == []
    adopt_theory_evaluation(database, result['attempt_id'], 'needs_review', NOW, True)
    assert learning_context(database)['weak_points'][0]['evidence_id'] == result['attempt_id']
    adopt_theory_evaluation(database, result['attempt_id'], 'aligned', NOW, True)
    assert learning_context(database)['weak_points'] == []


def test_feedback_rejects_interviewer_quotes_and_fabricated_user_quotes(database):
    from app.services.interview_feedback import validate_feedback
    session, user = session_with_answer(database)
    assistant = add_turn(database, session, 'assistant', 'Retries may duplicate side effects.', NOW)
    context = interview_context(database, session, 'interview_feedback')
    for turn, quote in [(assistant,'Retries may duplicate side effects.'),(user,'I understand all risks now')]:
        payload = {'observations':[{'kind':'strength','turn_id':turn,'quote':quote,'comment':'Learner identified retry risks'}],
                   'next_steps':['Try another problem.']}
        with pytest.raises(ModelJobError):
            validate_feedback(context, payload)


def test_feedback_formats_real_evidence_and_leaves_unanswered_question_untested(database):
    session, user = session_with_answer(database)
    add_turn(database, session, 'assistant', 'What would change for an empty input?', NOW)
    result = run_module_job(database,'interview_feedback',session,'quoted-summary',
                            SimpleNamespace(complete=lambda messages, **k:SimpleNamespace(content=feedback_json(messages),model='fake')))
    assert '你的原话：I do not understand' in result['response_text']
    assert '尚未作答，不能判断对错' in result['response_text']
    assert user not in result['response_text']
    assert 'reference_text' not in result['response_text']
    assert len(interview_context(database,session,'interview_feedback')['turns']) == 2


def test_feedback_invalid_evidence_has_one_repair_and_no_published_summary(database):
    session, _ = session_with_answer(database)
    calls=[]
    def invalid(messages, **k):
        calls.append(messages)
        return SimpleNamespace(content=json.dumps({'observations':[{'kind':'strength','turn_id':'invented','quote':'never said','comment':'invented skill'}],
                                                  'next_steps':['practice']}),model='fake')
    with pytest.raises(ModelJobError):
        run_module_job(database,'interview_feedback',session,'bad-evidence',SimpleNamespace(complete=invalid))
    assert len(calls) == 2
    assert database.execute("SELECT COUNT(*) FROM interview_turn WHERE role='assistant'").fetchone()[0] == 0
    assert database.execute('SELECT COUNT(*) FROM question_exposure').fetchone()[0] == 0


def test_feedback_correction_requires_earlier_user_evidence(database):
    from app.services.interview_feedback import render_feedback, validate_feedback
    session, first = session_with_answer(database)
    second = add_turn(database,session,'user','I can now state the required assumptions.',NOW)
    context = interview_context(database,session,'interview_feedback')
    item = {'kind':'correction','turn_id':second,'quote':'I can now state the required assumptions.',
            'comment':'A revised understanding','prior_turn_id':first,'prior_quote':'I do not understand the assumptions in this question.'}
    assert validate_feedback(context,{'observations':[item],'next_steps':['Try a concrete example.']})
    prior = {'kind':'mistake','turn_id':first,'quote':item['prior_quote'],'comment':'Earlier uncertainty'}
    rendered = render_feedback(context,{'observations':[prior,item],'next_steps':['Try a concrete example.']})
    assert '此前的回答（后续已修正）' in rendered and '已经修正的认识' in rendered
    item['prior_turn_id'] = second
    with pytest.raises(ModelJobError):
        validate_feedback(context,{'observations':[item],'next_steps':['Try a concrete example.']})


def test_legacy_feedback_request_keeps_its_frozen_plain_text_contract(database):
    session, _ = session_with_answer(database)
    def fail(*a, **k):
        raise RuntimeError('offline')
    with pytest.raises(ModelJobError):
        run_module_job(database,'interview_feedback',session,'legacy-summary',SimpleNamespace(complete=fail))
    request = database.execute("SELECT * FROM model_request WHERE module='interview_feedback'").fetchone()
    config, context = json.loads(request['config_json']), json.loads(request['input_json'])
    config.pop('feedback_format'); config.pop('response_format'); context.pop('feedback_format')
    config['system_prompt'] = 'Legacy plain text feedback contract'
    database.execute('UPDATE model_request SET config_json=?,input_json=? WHERE job_id=?',
                     (json.dumps(config),json.dumps(context),request['job_id']))
    database.commit()
    result = run_module_job(database,'interview_feedback',session,'legacy-summary',
                            SimpleNamespace(complete=lambda *a, **k:SimpleNamespace(content='Existing plain text format',model='fake')))
    assert result['response_text'] == 'Existing plain text format'
