from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.current_practice import draw_daily_batch
from app.services.free_batches import batch_state, queue_batch, run_batch
from app.services.interview import add_turn, start_session
from app.services.learning_clock import local_today
from app.services.practice import add_theory_to_review
from app.storage.database import connect_database
from tests.test_free_batches import spec
from tests.test_student_workflow import pool


@pytest.fixture
def saved_work(monkeypatch):
    monkeypatch.setattr('app.services.bootstrap.load_initial_sources', list)
    monkeypatch.setattr('app.services.free_batches.schedule_batch', lambda *a: None)
    with TestClient(app) as client:
        db = connect_database(app.state.database_path)
        pool(db, 8, 'code')
        pool(db, 8, 'theory')
        db.commit()
        daily = draw_daily_batch(db, local_today(), 2, 1, {}, 'old-daily', '')
        free = queue_batch(db, {**spec(), 'question_source': 'original'}, 'old-free')
        run_batch(app.state.database_path, free['id'])
        theory = db.execute("SELECT * FROM task t JOIN question q ON q.id=t.question_id WHERE t.id IN (?,?,?) AND q.question_type='theory'", daily['task_ids']).fetchone()
        session = start_session(db, theory['id'], datetime.now(UTC))
        add_turn(db, session, 'user', 'A saved answer must remain available in this conversation.', datetime.now(UTC))
        add_theory_to_review(db, theory['question_id'], 'user', datetime.now(UTC), theory['question_version_id'])
        db.commit()
        yield client, db, daily, free, session, theory['question_id']
        db.close()


@pytest.mark.parametrize('path', ['/', '/free-practice', '/interview'])
def test_fresh_entry_never_renders_saved_question_lists(saved_work, path):
    client, db, daily, free, session, _ = saved_work
    ids = daily['task_ids'] + batch_state(db, free['id'])['result']['task_ids'] + [session]
    before = {table: [tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
              for table in ['task', 'interview_turn', 'review_round', 'valid_review_pass', 'model_job']}
    for _ in range(2):
        html = client.get(path).text
        assert all(identifier not in html for identifier in ids)
        assert 'class="task-row"' not in html
        assert '最近一批' not in html and '继续最近的面试' not in html
        assert 'href="/history' not in html
    assert before == {table: [tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                      for table in before}


def test_explicit_daily_result_can_render_but_bare_entry_stays_empty(saved_work):
    client, db, daily, _, _, _ = saved_work
    latest = draw_daily_batch(db, local_today(), 1, 0, {}, 'clicked-now', daily['batch_key'])
    assert latest['task_ids'][0] in client.get('/?batch=clicked-now').text
    assert latest['task_ids'][0] not in client.get('/').text
    assert latest['task_ids'][0] not in client.get('/?batch=old-daily').text


def test_history_redirects_to_review_and_collected_records_remain(saved_work):
    client, db, _, _, session, question = saved_work
    response = client.get('/history?page=7&search=old', follow_redirects=False)
    assert response.status_code == 307 and response.headers['location'] == '/review'
    page = client.get('/review').text
    assert f'/questions/{question}/history' in page
    assert 'review-code' in page and 'review-theory' in page
    assert 'href="/history' not in page
    record = client.get(f'/questions/{question}/history').text
    assert f'/interview/{session}' in record and '返回复习库' in record
    assert 'A saved answer' not in record
    assert client.get(f'/interview/{session}').status_code == 200
    assert db.execute('SELECT COUNT(*) FROM question_exposure').fetchone()[0] == 0
