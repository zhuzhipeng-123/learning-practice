from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.domain import Submission
from app.main import app
from app.services.current_practice import current_daily_batch, draw_daily_batch, retire_expired
from app.services.free_batches import (
    batch_state,
    practice_state,
    queue_batch,
    retry_batch,
    run_batch,
)
from app.services.interview import start_session
from app.services.learning_clock import local_today
from app.services.practice import PracticeError, start_attempt, submit_code
from app.services.review import enter_review
from app.services.tasks import IdempotencyConflictError, create_daily_plan
from app.storage.database import connect_database
from tests.test_free_batches import spec
from tests.test_student_workflow import pool


def test_daily_replacement_retires_started_work_keeps_submissions_and_review(database):
    pool(database, 8, 'code')
    first = draw_daily_batch(database, local_today(), 3, 0, {}, 'first', '')
    done, started, unused = first['task_ids']
    now = datetime.now(UTC)
    start_attempt(database, done, 'daily', now)
    submit_code(database, Submission(done, 'answer', now, 'daily', code_self_result='cannot_solve'))
    start_attempt(database, started, 'daily', now)
    session = start_session(database, unused, now)
    reviews = [tuple(r) for r in database.execute('SELECT * FROM review_round')]
    second = draw_daily_batch(database, local_today(), 2, 0, {}, 'second', 'first')
    assert set(first['task_ids']).isdisjoint(second['task_ids'])
    assert [database.execute('SELECT status FROM task WHERE id=?', (t,)).fetchone()[0]
            for t in first['task_ids']] == ['completed', 'cancelled', 'cancelled']
    assert database.execute('SELECT status FROM interview_session WHERE id=?', (session,)).fetchone()[0] == 'ended'
    assert [tuple(r) for r in database.execute('SELECT * FROM review_round')] == reviews
    assert database.execute('SELECT COUNT(*) FROM attempt WHERE submitted_at IS NOT NULL').fetchone()[0] == 1
    with pytest.raises(PracticeError):
        submit_code(database, Submission(started, 'stale-answer', now, 'daily', code_self_result='can_solve'))
    assert draw_daily_batch(database, local_today(), 3, 0, {}, 'first', '') == first
    assert current_daily_batch(database, first['plan_id']) == second
    with pytest.raises(IdempotencyConflictError):
        draw_daily_batch(database, local_today(), 2, 0, {}, 'stale-tab', 'first')
    assert current_daily_batch(database, first['plan_id']) == second


def test_invalid_new_daily_batch_rolls_back_retirement(database):
    pool(database, 3, 'code')
    first = draw_daily_batch(database, local_today(), 1, 0, {}, 'first', '')
    with pytest.raises(ValueError):
        draw_daily_batch(database, local_today(), 1, 0, {'invalid': 1}, 'invalid', 'first')
    assert database.execute('SELECT status FROM task WHERE id=?', (first['task_ids'][0],)).fetchone()[0] == 'pending'
    assert current_daily_batch(database, first['plan_id']) == first


def test_rollover_retires_daily_and_free_but_preserves_review_work(database):
    pool(database, 5, 'code')
    yesterday = local_today() - timedelta(days=1)
    plan = create_daily_plan(database, yesterday, 3, 0, {}, 'yesterday')
    tasks = plan['tasks']
    database.execute("UPDATE task SET origin='free_practice' WHERE id=?", (tasks[1]['id'],))
    database.execute("UPDATE task SET origin='review',target_kind='added' WHERE id=?", (tasks[2]['id'],))
    basis = database.execute('SELECT review_basis_id FROM question_version WHERE id=?', (tasks[2]['question_version_id'],)).fetchone()[0]
    enter_review(database, tasks[2]['question_id'], basis, 'user', datetime.now(UTC))
    database.commit()
    before = [tuple(r) for r in database.execute('SELECT * FROM review_round')]
    retire_expired(database)
    assert [database.execute('SELECT status FROM task WHERE id=?', (t['id'],)).fetchone()[0]
            for t in tasks] == ['cancelled', 'cancelled', 'pending']
    assert [tuple(r) for r in database.execute('SELECT * FROM review_round')] == before


def test_new_daily_api_progress_and_stale_page_are_current_batch_only(monkeypatch):
    monkeypatch.setattr('app.services.bootstrap.load_initial_sources', list)
    with TestClient(app) as client:
        db = connect_database(app.state.database_path)
        pool(db, 5, 'code')
        db.commit()
        headers = {'X-Requested-With': 'learning-practice', 'Idempotency-Key': 'first'}
        body = {'plan_date': local_today().isoformat(), 'code_target': 2, 'theory_target': 0,
                'fresh_batch': True, 'expected_batch': ''}
        first = client.post('/api/plans', headers=headers, json=body).json()
        path = '/api/plans/' + first['plan_id']
        second = client.put(path, headers={**headers, 'Idempotency-Key': 'second'}, json={**body, 'expected_batch': 'first'}).json()
        assert second['batch_key'] == 'second'
        stale = client.put(path, headers={**headers, 'Idempotency-Key': 'stale'}, json={**body, 'expected_batch': 'first'})
        assert stale.status_code == 409 and '其他页面更新' in stale.text
        assert client.post('/api/plans', headers=headers, json=body).json() == first
        assert all(t not in client.get('/').text for t in second['task_ids'])
        html = client.get('/?batch=second').text
        assert all(t in html for t in second['task_ids'])
        assert all(t not in html for t in first['task_ids'])
        assert all(t not in client.get('/history').text for t in first['task_ids'])
        result = client.post('/api/tasks/' + first['task_ids'][0] + '/attempts', headers=headers,
                             json={'entry_mode': 'daily', 'started_at': datetime.now(UTC).isoformat()})
        assert result.status_code == 409
        db.close()


def test_free_replacement_is_per_kind_and_failed_new_request_keeps_current(database, monkeypatch):
    monkeypatch.setattr('app.services.free_batches.schedule_batch', lambda *a: None)
    pool(database, 6, 'code')
    pool(database, 6, 'theory')
    database.commit()
    location = Path(database.execute('PRAGMA database_list').fetchone()[2])
    code = queue_batch(database, {**spec(), 'question_source': 'original'}, 'code')
    run_batch(location, code['id'])
    theory = queue_batch(database, {**spec('theory'), 'question_source': 'original'}, 'theory')
    run_batch(location, theory['id'])
    code_next = queue_batch(database, {**spec(), 'question_source': 'original'}, 'code-next')
    run_batch(location, code_next['id'])
    assert batch_state(database, code['id'])['result']['tasks'][0]['status'] == 'cancelled'
    assert batch_state(database, theory['id'])['result']['tasks'][0]['status'] == 'pending'
    with pytest.raises(ValueError, match='替换'):
        retry_batch(database, code['id'])
    monkeypatch.setattr('app.services.free_batches.execute_free_request', lambda *a: (_ for _ in ()).throw(RuntimeError('synthetic outage')))
    failed = queue_batch(database, spec(), 'failed')
    run_batch(location, failed['id'])
    assert practice_state(database)['code']['result_batch']['id'] == code_next['id']
    assert batch_state(database, code_next['id'])['result']['tasks'][0]['status'] == 'pending'


def test_late_free_result_after_midnight_cannot_become_current(database, monkeypatch):
    from app.services.free_requests import execute_free_request
    monkeypatch.setattr('app.services.free_batches.schedule_batch', lambda *a: None)
    pool(database, 4, 'code')
    database.commit()
    location = Path(database.execute('PRAGMA database_list').fetchone()[2])
    batch = queue_batch(database, {**spec(), 'question_source': 'original'}, 'late')
    tomorrow = local_today() + timedelta(days=1)
    def midnight(*args):
        result = execute_free_request(*args)
        monkeypatch.setattr('app.services.free_batches.local_today', lambda: tomorrow)
        return result
    monkeypatch.setattr('app.services.free_batches.execute_free_request', midnight)
    run_batch(location, batch['id'])
    assert batch_state(database, batch['id'])['status'] == 'interrupted'
    assert practice_state(database)['code']['result_batch'] is None
    assert database.execute('SELECT status FROM task').fetchone()[0] == 'cancelled'


def test_review_page_has_separate_code_and_theory_collections(monkeypatch):
    monkeypatch.setattr('app.services.bootstrap.load_initial_sources', list)
    with TestClient(app) as client:
        db = connect_database(app.state.database_path)
        for kind in ['code', 'theory']:
            pool(db, 1, kind)
            row = db.execute('SELECT q.id,v.review_basis_id FROM question q JOIN question_version v ON v.id=q.current_version_id WHERE q.question_type=?', (kind,)).fetchone()
            enter_review(db, row[0], row[1], 'user', datetime.now(UTC))
        db.commit()
        html = client.get('/review').text
        assert '代码复习 · 1 题' in html and '八股复习 · 1 题' in html
        assert 'data-review-kind="code"' in html and 'data-review-kind="theory"' in html
        assert html.count('data-review-question=') == 2
        assert db.execute('SELECT COUNT(*) FROM question_exposure').fetchone()[0] == 0
        db.close()
