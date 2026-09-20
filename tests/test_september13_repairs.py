import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.domain import Submission
from app.main import app
from app.services.current_practice import draw_daily_batch, restore_daily, retire_expired
from app.services.dashboard import day_details
from app.services.free_batches import batch_state, queue_batch, restore_free, run_batch
from app.services.interview import add_turn, start_session
from app.services.interview_assessment import assess_code
from app.services.interview_review import preview_review
from app.services.interview_setup import prepare_pool_interview
from app.services.knowledge_editing import edit_knowledge
from app.services.learning_clock import local_today
from app.services.practice import PracticeError, start_attempt, submit_code, submit_theory
from app.services.review import enter_review, get_active_round
from app.services.review_tasks import start_review_task
from app.services.tasks import IdempotencyConflictError, create_daily_plan
from app.storage.database import connect_database
from tests.test_free_batches import spec
from tests.test_practice_review import seed_question
from tests.test_student_workflow import pool


@pytest.mark.parametrize('interview', [False, True])
def test_old_code_cannot_solve_preserves_newer_review_basis(database, interview):
    question = seed_question(database, 'code')
    database.execute("UPDATE question SET source_kind='derived' WHERE id=?", (question,))
    version, basis = database.execute('SELECT current_version_id,v.review_basis_id FROM question q '
        'JOIN question_version v ON v.id=q.current_version_id WHERE q.id=?', (question,)).fetchone()
    now = datetime.now(UTC)
    old_round = enter_review(database, question, basis, 'user', now)
    database.commit()
    task = start_review_task(database, question, local_today(), 'old-task')['task_id']
    if interview:
        session = start_session(database, task, now)
        add_turn(database, session, 'user', 'I cannot find a working approach.', now)
    else:
        start_attempt(database, task, 'review', now)
    edit_knowledge(database, question, version, 'Find two distinct maximum values',
                   'Track the two largest distinct values.', 'Arrays', True, 'edit')
    newer = dict(get_active_round(database, question))
    if not interview:
        with pytest.raises(PracticeError, match='复习依据'):
            submit_code(database, Submission(task, 'answer', now, 'review', code_self_result='cannot_solve'))
        assert dict(get_active_round(database, question)) == newer
        attempt = database.execute('SELECT * FROM attempt WHERE task_id=?', (task,)).fetchone()
        assert attempt['submitted_at'] is None
        assert attempt['review_round_id'] == old_round
        return
    result = assess_code(database, session, 'cannot_solve', '', 'answer')
    assert result['review_basis_conflict'] is True
    assert dict(get_active_round(database, question)) == newer
    attempt = database.execute('SELECT * FROM attempt WHERE id=?', (result['attempt_id'],)).fetchone()
    assert attempt['submitted_at'] and attempt['question_version_id'] == version


def test_manual_assessment_http_replay_preserves_later_adoption(monkeypatch):
    monkeypatch.setattr('app.services.bootstrap.load_initial_sources', list)
    with TestClient(app) as client, connect_database(app.state.database_path) as db:
        seed_question(db, 'theory')
        task = create_daily_plan(db, local_today(), 0, 1, {}, 'plan')['tasks'][0]['id']
        now = datetime.now(UTC)
        start_attempt(db, task, 'web', now)
        answer = submit_theory(db, Submission(task, 'answer', now, 'web', answer_text='My explanation'))
        url = f"/api/attempts/{answer['attempt_id']}/evaluation"
        headers = {'X-Requested-With': 'learning-practice', 'Idempotency-Key': 'correction-a'}
        body = {'verdict': 'aligned', 'expected_adoption': None}
        first = client.post(url, headers=headers, json=body)
        assert first.status_code == 200
        second = client.post(url, headers={**headers, 'Idempotency-Key': 'correction-b'},
                             json={'verdict': 'needs_review', 'expected_adoption': first.json()['evaluation_id']})
        assert second.status_code == 200
        assert client.post(url, headers=headers, json=body).json() == first.json()
        assert db.execute('SELECT id FROM evaluation WHERE adopted=1').fetchone()[0] == second.json()['evaluation_id']
        assert db.execute('SELECT COUNT(*) FROM evaluation').fetchone()[0] == 2
        assert client.post(url, headers={**headers, 'Idempotency-Key': 'stale-new'}, json=body).status_code == 409
        assert client.post(url, headers=headers, json={**body, 'verdict': 'needs_review'}).status_code == 409


def test_deduplicated_free_key_recovers_original_after_later_batches(database, monkeypatch):
    monkeypatch.setattr('app.services.free_batches.schedule_batch', lambda *a: None)
    pool(database, 10, 'code')
    database.commit()
    body = {**spec(), 'question_source': 'original'}
    first = queue_batch(database, body, 'a')
    assert queue_batch(database, {**body, 'count': 2}, 'b')['id'] == first['id']
    location = Path(database.execute('PRAGMA database_list').fetchone()[2])
    run_batch(location, first['id'])
    newer = queue_batch(database, body, 'c')
    run_batch(location, newer['id'])
    before = [tuple(row) for row in database.execute('SELECT * FROM task')]
    assert queue_batch(database, {**body, 'count': 2}, 'b')['id'] == first['id']
    assert [tuple(row) for row in database.execute('SELECT * FROM task')] == before
    assert database.execute('SELECT COUNT(*) FROM free_practice_batch').fetchone()[0] == 2
    with pytest.raises(IdempotencyConflictError):
        queue_batch(database, body, 'b')


def test_confirmed_daily_and_free_batches_restore_once_per_day(database, monkeypatch):
    monkeypatch.setattr('app.services.free_batches.schedule_batch', lambda *a: None)
    day = local_today()
    for module in ('current_practice', 'free_batches'):
        monkeypatch.setattr(f'app.services.{module}.local_today', lambda: day)
    pool(database, 30, 'code')
    pool(database, 30, 'theory')
    database.commit()
    assert restore_daily(database) is None
    assert restore_free(database)['code']['batch'] is None
    daily = draw_daily_batch(database, day, 1, 1, {}, 'confirmed')
    body = {**spec(), 'question_source': 'original'}
    free = queue_batch(database, body, 'free-confirmed')
    # A model-only configuration must never become an automatic model call on entry.
    queue_batch(database, spec('theory'), 'model-confirmed')
    location = Path(database.execute('PRAGMA database_list').fetchone()[2])
    run_batch(location, free['id'])
    for offset in range(1, 4):
        with connect_database(location) as reopened:
            assert restore_daily(reopened) == daily
            assert restore_free(reopened)['code']['batch']['id'] == free['id']
        old_ids = daily['task_ids'] + batch_state(database, free['id'])['result']['task_ids']
        day += timedelta(days=1)
        retire_expired(database)
        daily = restore_daily(database)
        free = restore_free(database)['code']['batch']
        assert daily is None
        assert restore_free(database)['theory']['batch'] is None
        run_batch(location, free['id'])
        assert all(database.execute('SELECT status FROM task WHERE id=?', (t,)).fetchone()[0] == 'cancelled' for t in old_ids)
        assert database.execute('SELECT COUNT(*) FROM model_job').fetchone()[0] == 0
        daily = draw_daily_batch(database, day, 1, 1, {}, f'confirmed-{offset}', '')
        assert database.execute('SELECT COUNT(*) FROM daily_plan').fetchone()[0] == offset + 1


def test_concurrent_first_daily_open_does_not_draw(database, monkeypatch):
    day = local_today()
    monkeypatch.setattr('app.services.current_practice.local_today', lambda: day)
    pool(database, 10, 'code')
    database.commit()
    draw_daily_batch(database, day, 2, 0, {}, 'first')
    day += timedelta(days=1)
    location = Path(database.execute('PRAGMA database_list').fetchone()[2])
    def restore():
        with connect_database(location) as db:
            return restore_daily(db)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: restore(), range(2)))
    assert results == [None, None]
    assert database.execute("SELECT COUNT(*) FROM daily_plan").fetchone()[0] == 1
    assert database.execute("SELECT COUNT(*) FROM task WHERE status='pending'").fetchone()[0] == 2


@pytest.mark.parametrize('origin', ['daily', 'free_practice', 'review'])
def test_interview_in_reused_task_has_matching_day_scope(database, origin):
    question = seed_question(database, 'code')
    task = create_daily_plan(database, local_today(), 1, 0, {}, 'plan')['tasks'][0]['id']
    database.execute('UPDATE task SET origin=? WHERE id=?', (origin, task))
    database.execute('UPDATE question SET is_classic=1 WHERE id=?', (question,))
    database.commit()
    session = prepare_pool_interview(database, 'classic', '', 'classic')['session_id']
    add_turn(database, session, 'user', 'Here is my approach.', datetime.now(UTC))
    detail = day_details(database, local_today(), origin)
    assert detail['activity']['total'] == 1
    assert len(detail['groups']['pending']) == len(detail['interviews']) == 1
    assert detail['groups']['pending'][0]['session_id'] == session
    assert not day_details(database, local_today(), 'interview')['interviews']
    assess_code(database, session, 'can_solve', '', 'assessment')
    detail = day_details(database, local_today(), origin)
    assert len(detail['groups']['can']) == 1 and not detail['groups']['pending']


def test_long_review_preview_freezes_selected_pair_and_replays_after_new_turn(database):
    seed_question(database, 'theory')
    task = create_daily_plan(database, local_today(), 0, 1, {}, 'plan')['tasks'][0]['id']
    now = datetime.now(UTC)
    session = start_session(database, task, now)
    add_turn(database, session, 'user', 'My initial answer', now)
    selected = add_turn(database, session, 'assistant', 'What happens if the response is lost?', now)
    selected_answer = add_turn(database, session, 'user', 'Use the stored request result. ' * 500, now)
    for index in range(12):
        add_turn(database, session, 'assistant', f'Follow up {index}', now)
        add_turn(database, session, 'user', 'A long legal answer. ' * 700, now)
    calls = []
    def complete(messages, **kwargs):
        calls.append(json.loads(messages[1]['content']))
        return SimpleNamespace(content=json.dumps({'prompt': 'How should a retry recover a lost response?',
            'reference_text': 'Read the stored result by its request key.'}), model='local-fake')
    fake = SimpleNamespace(complete_json=complete)
    first = preview_review(database, session, selected, 'preview', fake)
    assert {selected, selected_answer}.issubset({turn['id'] for turn in calls[0]['turns']})
    assert calls[0]['context_window']['omitted_turns'] > 0
    assert len(json.dumps(calls[0], ensure_ascii=False)) < 120000
    frozen = database.execute("SELECT input_json FROM model_request r JOIN model_job j ON j.id=r.job_id WHERE j.business_key='interview_review:preview'").fetchone()[0]
    add_turn(database, session, 'assistant', 'A later follow up', now)
    assert preview_review(database, session, selected, 'preview', fake) == first
    assert len(calls) == 1
    assert database.execute("SELECT input_json FROM model_request r JOIN model_job j ON j.id=r.job_id WHERE j.business_key='interview_review:preview'").fetchone()[0] == frozen
    with pytest.raises(Exception, match='另一条追问'):
        preview_review(database, session, selected_answer, 'preview', fake)
