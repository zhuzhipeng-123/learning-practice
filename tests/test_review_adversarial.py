import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.domain import Submission
from app.main import app
from app.services.evaluations import EvaluationValidationError, validate_evaluation
from app.services.interview import (
    InterviewError,
    add_turn,
    create_derived_theory_question,
    start_session,
)
from app.services.interview_assessment import assess_code
from app.services.interview_review import preview_review, review_main_question
from app.services.model_jobs import ModelJobError, run_evaluation_job
from app.services.practice import add_theory_to_review, start_attempt, submit_theory
from app.services.review_tasks import start_review_task
from app.services.task_management import cancel_unstarted_task
from app.services.tasks import create_daily_plan
from app.storage.database import connect_database, initialize_database
from tests.helpers import remove_migrations_after
from tests.test_practice_review import make_plan, seed_question
from tests.test_repair_sync import live_theory, run_live
from tests.test_review_acceptance import text_block

NOW = datetime.now(UTC)
HEADERS = {'X-Requested-With': 'learning-practice', 'Idempotency-Key': 'request'}


def test_interview_main_review_preserves_session_basis(database):
    question = seed_question(database, 'theory')
    task = make_plan(database, 'theory')
    session = start_session(database, task['id'], NOW)
    first = review_main_question(database, session)
    assert review_main_question(database, session) == first
    row = database.execute('SELECT question_id,review_basis_id FROM review_round').fetchone()
    basis = database.execute('SELECT review_basis_id FROM question_version WHERE id=?', (task['question_version_id'],)).fetchone()[0]
    assert tuple(row) == (question, basis)
    assert database.execute('SELECT COUNT(*) FROM review_round').fetchone()[0] == 1


def test_reference_image_cannot_be_graded_as_complete_text(database):
    seed_question(database, 'theory')
    task = make_plan(database, 'theory')
    database.execute('UPDATE version_resources SET materials_json=? WHERE version_id=?',
                     (json.dumps([{'role': 'reference', 'kind': 'media'}]), task['question_version_id']))
    database.commit()
    start_attempt(database, task['id'], 'web', NOW)
    answer = submit_theory(database, Submission(task['id'], 'image-answer', NOW, 'web', answer_text='Partial answer'))
    evaluator = SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(content=json.dumps({
        'verdict': 'aligned', 'covered_points': [], 'missing_points': [], 'errors': [],
        'brief_feedback': 'Looks right', 'evidence_refs': ['reference-theory']}), model='fixture'))
    with pytest.raises(ModelJobError, match='参考'):
        run_evaluation_job(database, answer['job_id'], evaluator)
    assert database.execute('SELECT COUNT(*) FROM evaluation WHERE adopted=1').fetchone()[0] == 0
    assert database.execute('SELECT COUNT(*) FROM attempt WHERE submitted_at IS NOT NULL').fetchone()[0] == 1


def test_model_cannot_adopt_pass_while_listing_core_errors():
    value = {'verdict':'aligned','covered_points':[],'missing_points':[],
             'errors':['The key relationship is reversed'], 'brief_feedback':'Incorrect', 'evidence_refs':['ref']}
    with pytest.raises(EvaluationValidationError, match='未采用'):
        validate_evaluation(value, {'ref'})


def test_interview_code_chat_requires_explicit_self_assessment(database):
    seed_question(database, 'code')
    task = make_plan(database, 'code')
    session = start_session(database, task['id'], NOW)
    with pytest.raises(InterviewError, match='先保存'):
        assess_code(database, session, 'can_solve', '', 'before')
    add_turn(database, session, 'user', 'I can describe the idea but cannot implement it yet.', NOW)
    assert database.execute('SELECT COUNT(*) FROM attempt').fetchone()[0] == 0
    answer = assess_code(database, session, 'cannot_solve', '', 'self')
    assert assess_code(database, session, 'cannot_solve', '', 'self') == answer
    assert database.execute('SELECT COUNT(*) FROM review_round').fetchone()[0] == 1
    assert database.execute('SELECT COUNT(*) FROM attempt').fetchone()[0] == 1
    assert database.execute('SELECT COUNT(*) FROM valid_review_pass').fetchone()[0] == 0


def test_empty_answer_repair_duplicate_key_and_cross_tab_submit(monkeypatch):
    monkeypatch.setattr('app.services.current_practice.local_today', lambda: date(2026, 9, 11))
    monkeypatch.setattr('app.services.bootstrap.load_initial_sources', list)
    with TestClient(app) as client:
        connection = connect_database(app.state.database_path)
        seed_question(connection, 'theory')
        task = make_plan(connection, 'theory')['id']
        connection.close()
        start = f'/api/tasks/{task}/attempts'
        submit = f'/api/tasks/{task}/theory-submit'
        assert client.post(start, headers=HEADERS, json={'entry_mode': 'web', 'started_at': NOW.isoformat()}).status_code == 200
        body = {'entry_mode': 'web', 'submitted_at': NOW.isoformat(), 'answer_text': '   '}
        assert client.post(submit, headers=HEADERS, json=body).status_code == 409
        body['answer_text'] = 'A corrected answer with <script>window.attacked=true</script>'
        first = client.post(submit, headers=HEADERS, json=body)
        assert first.status_code == 200
        assert client.post(submit, headers=HEADERS, json=body).json() == first.json()
        assert client.post(submit, headers={**HEADERS, 'Idempotency-Key': 'other-tab'}, json=body).status_code == 409
        body['answer_text'] = 'Changed payload'
        assert client.post(submit, headers=HEADERS, json=body).status_code == 409
        connection = connect_database(app.state.database_path)
        assert connection.execute('SELECT COUNT(*) FROM attempt WHERE submitted_at IS NOT NULL').fetchone()[0] == 1
        connection.close()
        assert '<script>window.attacked=true</script>' not in client.get(f'/practice/{task}').text


def test_old_plan_and_forged_plan_stop_before_model_request(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError('invalid plan must be rejected before using the model')
    monkeypatch.setattr('app.routes.api.select_by_description', forbidden)
    with TestClient(app) as client:
        connection = connect_database(app.state.database_path)
        plan = create_daily_plan(connection, date(2000, 1, 1), 0, 0, {}, 'old')
        connection.close()
        for plan_id in [plan['plan_id'], 'forged']:
            assert client.post('/api/free-practice', headers=HEADERS, json={
                'plan_id': plan_id, 'mode': 'topic', 'theme': 'agent', 'count': 1,
            }).status_code == 409
            assert client.post(f'/api/plans/{plan_id}/fill', headers=HEADERS, json={}).status_code == 409


def test_interview_derivation_preview_confirmation_and_reference_guard(database):
    parent = seed_question(database, 'theory')
    task = make_plan(database, 'theory')
    session = start_session(database, task['id'], NOW)
    turn = add_turn(database, session, 'assistant', 'Why is a retry key stable?', NOW)
    add_turn(database, session, 'user', 'I am not sure.', NOW)
    fake = SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(content=json.dumps({
        'prompt': 'How does a stable key prevent duplicate writes?', 'reference_text': 'Reuse the key to identify the same operation.'}), model='fixture'))
    preview = preview_review(database, session, turn, 'preview', fake)
    assert preview['reference_verified'] is False
    assert database.execute('SELECT COUNT(*) FROM review_round').fetchone()[0] == 0
    with pytest.raises(InterviewError):
        create_derived_theory_question(database, session, preview['prompt'], preview['reference_text'], 'Agent', False, NOW, turn)
    with pytest.raises(InterviewError):
        create_derived_theory_question(database, session, 'Injected', 'Answer', 'Agent', True, NOW, 'another-session-turn')
    first = create_derived_theory_question(database, session, preview['prompt'], preview['reference_text'], 'Agent', True, NOW, turn, 'confirm-1')
    second = create_derived_theory_question(database, session, preview['prompt'], preview['reference_text'], 'Agent', True, NOW, turn, 'confirm-2')
    assert first == second != parent
    assert database.execute('SELECT COUNT(*) FROM review_round').fetchone()[0] == 1
    provenance = database.execute('SELECT parent_question_id,session_id,turn_id,reference_verified FROM interview_derivation').fetchone()
    assert tuple(provenance) == (parent, session, turn, 0)
    review_task = start_review_task(database, first, NOW.date(), 'review')['task_id']
    start_attempt(database, review_task, 'review', NOW)
    answer = submit_theory(database, Submission(review_task, 'answer', NOW, 'review', answer_text='Do not duplicate the write.'))
    illegal = {'verdict': 'aligned', 'covered_points': [], 'missing_points': [], 'errors': [], 'brief_feedback': 'Trust me', 'evidence_refs': []}
    evaluator = SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(content=json.dumps(illegal), model='fixture'))
    with pytest.raises(ModelJobError, match='参考'):
        run_evaluation_job(database, answer['job_id'], evaluator)
    assert database.execute('SELECT COUNT(*) FROM valid_review_pass').fetchone()[0] == 0


def test_review_concurrent_entry_creates_only_one_task(database):
    question = seed_question(database, 'theory')
    add_theory_to_review(database, question, 'test', NOW)
    location = Path(database.execute('PRAGMA database_list').fetchone()[2])
    def enter(index):
        connection = connect_database(location)
        try:
            return start_review_task(connection, question, NOW.date(), f'parallel-{index}')['task_id']
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(enter, range(4)))
    assert len(set(result)) == 1
    assert database.execute('SELECT COUNT(*) FROM task').fetchone()[0] == 1


def test_old_tasks_cancel_only_before_any_answer(database):
    seed_question(database, 'theory')
    task = make_plan(database, 'theory')
    assert cancel_unstarted_task(database, task['id']) == {'status': 'cancelled'}
    assert cancel_unstarted_task(database, task['id']) == {'status': 'cancelled'}
    plan = create_daily_plan(database, date(2026, 9, 12), 0, 1, {}, 'later')
    task = plan['tasks'][0]['id']
    start_attempt(database, task, 'web', NOW)
    with pytest.raises(ValueError, match='已经开始'):
        cancel_unstarted_task(database, task)
    assert database.execute('SELECT COUNT(*) FROM attempt').fetchone()[0] == 1


def test_incomplete_update_cannot_reuse_old_complete_binding(database, monkeypatch):
    live_theory(database)
    good = [text_block('module', 'Module', 3), text_block('question', 'Question?', 5), text_block('answer', 'A')]
    run_live(database, monkeypatch, 1, good)
    question = database.execute('SELECT id FROM question').fetchone()[0]
    bad = [*good, {'block_id': 'essential-table', 'block_type': 999}]
    result = run_live(database, monkeypatch, 2, bad)
    assert result['partial']
    assert database.execute('SELECT source_status FROM question WHERE id=?', (question,)).fetchone()[0] != 'active'
    assert database.execute('SELECT COUNT(*) FROM question_version').fetchone()[0] == 1
    assert create_daily_plan(database, NOW.date(), 0, 1, {}, 'incomplete')['tasks'] == []


def test_history_can_reach_record_101_without_exposing_answers(monkeypatch):
    monkeypatch.setattr('app.services.bootstrap.load_initial_sources', list)
    with TestClient(app) as client:
        connection = connect_database(app.state.database_path)
        question = seed_question(connection, 'theory')
        task = make_plan(connection, 'theory')
        for index in range(101):
            when = NOW - timedelta(days=index)
            connection.execute('INSERT INTO attempt(id,task_id,question_version_id,entry_mode,started_at,submitted_at,activity_date,answer_text) VALUES (?,?,?,?,?,?,?,?)',
                (f'old-{index}', task['id'], task['question_version_id'], 'web', when.isoformat(), when.isoformat(), when.date().isoformat(), 'protected-answer'))
        connection.commit()
        connection.close()
        for path in [f'/questions/{question}/history?page=3']:
            response = client.get(path)
            assert response.status_code == 200
            assert (NOW - timedelta(days=100)).date().isoformat() in response.text
            assert 'protected-answer' not in response.text
        assert client.get(f'/questions/{question}/history?page=0').status_code == 422


def test_additive_migration_preserves_records_and_creates_backup(database):
    seed_question(database, 'theory')
    task = make_plan(database, 'theory')
    start_attempt(database, task['id'], 'web', NOW)
    submit_theory(database, Submission(task['id'], 'submitted', NOW, 'web', answer_text='Preserved history'))
    remove_migrations_after(database, 8)
    database.execute('DROP TABLE interview_derivation')
    database.execute('DROP TABLE free_practice_batch')
    database.execute('DROP TABLE reference_correction')
    database.execute('DROP TABLE reference_correction_history')
    database.execute('DROP TABLE reference_verification')
    for index in ['task_question_history', 'attempt_activity_history', 'interview_turn_session']:
        database.execute(f'DROP INDEX {index}')
    database.execute('DELETE FROM schema_version WHERE version>=9')
    database.commit()
    initialize_database(database)
    assert database.execute('SELECT answer_text FROM attempt').fetchone()[0] == 'Preserved history'
    assert database.execute('PRAGMA foreign_key_check').fetchall() == []
    directory = Path(database.execute('PRAGMA database_list').fetchone()[2]).parent
    backup = next(directory.glob('pre-migration-v8-*.db'))
    connection = connect_database(backup)
    assert connection.execute('SELECT MAX(version) FROM schema_version').fetchone()[0] == 8
    assert connection.execute('SELECT answer_text FROM attempt').fetchone()[0] == 'Preserved history'
    connection.close()
