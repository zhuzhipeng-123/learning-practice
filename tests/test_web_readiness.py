import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.interview import add_turn, start_session
from app.services.interview_conversation import live_conversation
from app.services.model_jobs import ModelJobError
from app.services.module_jobs import run_module_job
from app.services.question_quality import CHECKS, code_examples, validate_quality
from app.storage.database import connect_database
from tests.test_practice_review import make_plan, seed_question


def interview(database):
    seed_question(database, 'theory')
    task = make_plan(database, 'theory')
    session = start_session(database, task['id'], datetime.now(UTC))
    answer = add_turn(database, session, 'user', 'Please explain the assumptions.', datetime.now(UTC))
    return session, answer


def test_completed_followup_can_reconcile_without_model_call_or_exposure(database):
    session, answer = interview(database)
    fake = SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(
        content=json.dumps({'question': 'How would an empty input change your approach?',
                            'reference_text': 'PRIVATE REFERENCE: define and handle the empty input.'}), model='fake'))
    result = run_module_job(database, 'interview_followup', session, 'ignored', fake, expected_revision=answer)
    jobs = database.execute('SELECT COUNT(*) FROM model_job').fetchone()[0]
    for _ in range(3):
        state = live_conversation(database, session, answer)
        assert state['continuation']['status'] == 'complete'
        assert state['continuation']['result_id'] == result['result_id']
        assert state['continuation']['answer_revision'] == answer
        assert len(state['turns']) == 2
        assert 'PRIVATE REFERENCE' not in json.dumps(state)
    assert database.execute('SELECT COUNT(*) FROM model_job').fetchone()[0] == jobs
    assert database.execute('SELECT COUNT(*) FROM question_exposure').fetchone()[0] == 0
    assert live_conversation(database, session, 'another-session-revision')['continuation'] is None


def test_failed_followup_status_never_retries_or_leaks_rejected_answers(database):
    session, answer = interview(database)
    def fail(*a, **k):
        raise RuntimeError('PRIVATE REJECTED REFERENCE')
    with pytest.raises(ModelJobError):
        run_module_job(database, 'interview_followup', session, 'ignored', SimpleNamespace(complete=fail), expected_revision=answer)
    state = live_conversation(database, session, answer)
    assert state['continuation']['status'] == 'failed'
    assert 'PRIVATE REJECTED REFERENCE' not in json.dumps(state)
    assert len(state['turns']) == 1
    assert database.execute("SELECT retry_count FROM model_job WHERE purpose='interview_followup'").fetchone()[0] == 1


def test_abandoned_followup_is_recoverable_after_lease_expiry_and_restart(database):
    from app.services.free_batches import interrupt_batches
    session, answer = interview(database)
    database.execute("INSERT INTO model_job VALUES ('orphan',?,'interview_followup','running',0,NULL,NULL,NULL,'2026-01-01','2026-01-01')",
                     (f'interview_followup:{session}:{answer}',))
    database.commit()
    assert live_conversation(database, session, answer)['continuation']['status'] == 'expired'
    assert database.execute("SELECT status FROM model_job WHERE id='orphan'").fetchone()[0] == 'running'
    interrupt_batches(database)
    assert live_conversation(database, session, answer)['continuation']['status'] == 'failed'


def example_check():
    question = 'Sum the even values.\nExample 1:\nInput: [2, 5, 8]\nOutput: 10\nExample 2:\nInput: []\nOutput: 0'
    context = {'items': [{'id': 'sum', 'examples': code_examples(question)}]}
    value = {'items': [{'id': 'sum', **dict.fromkeys(CHECKS, True), 'issues': [], 'example_checks': [
        {'example_id': 'example_1', 'input_quote': 'Input: [2, 5, 8]', 'computed_output': '10',
         'reason': 'Only 2 and 8 are even; their sum is 10.', 'consistent': True},
        {'example_id': 'example_2', 'input_quote': 'Input: []', 'computed_output': '0',
         'reason': 'The empty input has no values to add, so the sum is zero.', 'consistent': True}]}]}
    return context, value


@pytest.mark.parametrize('fault', ['missing', 'duplicate', 'invented_quote', 'missing_reason', 'bad_type'])
def test_code_check_requires_grounded_evidence_for_every_example(fault):
    context, value = example_check()
    evidence = value['items'][0]['example_checks']
    if fault == 'missing': evidence.pop()
    if fault == 'duplicate': evidence[1] = evidence[0]
    if fault == 'invented_quote': evidence[0]['input_quote'] = 'Input: [100]'
    if fault == 'missing_reason': evidence[0]['reason'] = ''
    if fault == 'bad_type': evidence[0]['example_id'] = []
    with pytest.raises(ModelJobError):
        validate_quality(context, value)


def test_example_failure_overrides_optimistic_summary_flags():
    context, value = example_check()
    assert validate_quality(context, value)[0]['examples_consistent']
    value['items'][0]['example_checks'][0]['consistent'] = False
    checked = validate_quality(context, value)[0]
    assert not checked['examples_consistent'] and checked['issues']
    # Old frozen contracts without examples remain recoverable.
    assert validate_quality({'items': [{'id': 'old'}]}, {'items': [
        {'id': 'old', **dict.fromkeys(CHECKS, True), 'issues': []}]})


def test_recomputed_output_mismatch_cannot_be_approved_by_model():
    context, value = example_check()
    context['items'][0]['examples'][0]['text'] = 'Example 1:\nInput: [2, 5, 8]\nOutput: 8\nCorrection: the output should be 10.'
    checked = validate_quality(context, value)[0]
    assert not checked['examples_consistent']
    assert not checked['example_checks'][0]['consistent']
    assert any('最初列出的输出' in issue for issue in checked['issues'])


def test_saved_interview_remains_readable_without_a_history_module():
    with TestClient(app) as client:
        db = connect_database(app.state.database_path)
        db.execute("DELETE FROM source WHERE id='source-theory'")
        session, _ = interview(db)
        response = client.get('/history')
        assert response.status_code == 200
        assert response.url.path == '/review'
        assert f'/interview/{session}' not in response.text
        assert client.get(f'/interview/{session}').status_code == 200
        assert '未完成的练习' not in response.text
        assert db.execute('SELECT COUNT(*) FROM attempt').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM question_exposure').fetchone()[0] == 0
        db.close()


@pytest.mark.parametrize('fixed', [True, False])
def test_quality_format_repair_is_bounded_and_retains_rejected_output(database, fixed):
    context, value = example_check()
    context['source_id'] = 'format-evidence'
    calls = []
    def complete(messages, **kwargs):
        calls.append(messages)
        payload = value if fixed and len(calls) == 2 else {'items': []}
        return SimpleNamespace(content=json.dumps(payload), model='fake')
    if fixed:
        result = run_module_job(database, 'question_quality', 'format-evidence', 'format-evidence',
                                SimpleNamespace(complete=complete), context)
        assert json.loads(result['response_text'])['items'][0]['example_checks']
    else:
        with pytest.raises(ModelJobError):
            run_module_job(database, 'question_quality', 'format-evidence', 'format-evidence',
                           SimpleNamespace(complete=complete), context)
    assert len(calls) == 2
    config = json.loads(database.execute('SELECT config_json FROM model_request').fetchone()[0])
    assert json.loads(config['format_repair']['rejected_response']) == {'items': []}


def test_correction_banner_does_not_reveal_answer_or_record_exposure():
    from app.services.reference_corrections import save_correction
    with TestClient(app) as client:
        db = connect_database(app.state.database_path)
        db.execute("DELETE FROM source WHERE id='source-theory'")
        seed_question(db, 'theory')
        task = make_plan(db, 'theory')
        version = db.execute('SELECT question_version_id FROM task WHERE id=?', (task['id'],)).fetchone()[0]
        save_correction(db, version, 'PRIVATE ANSWER CORRECTION DETAIL', ['https://example.test/evidence'])
        response = client.get('/practice/' + task['id'])
        assert response.status_code == 200 and '本版本有已核对的内容校正' in response.text
        assert 'PRIVATE ANSWER CORRECTION DETAIL' not in response.text
        assert db.execute('SELECT COUNT(*) FROM question_exposure').fetchone()[0] == 0
        db.close()
