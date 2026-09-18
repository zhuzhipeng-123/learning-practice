import json
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.free_batches import (
    batch_state,
    practice_state,
    queue_batch,
    restore_free,
    run_batch,
)
from app.services.learning_clock import local_today
from app.storage.database import connect_database
from tests.test_free_batches import spec
from tests.test_student_workflow import pool


def legacy_batches(db):
    """The pre-confirmation app stored batches and request keys, but no confirmation."""
    for kind in ('code', 'theory'):
        pool(db, 12, kind)
    db.commit()
    location = Path(db.execute('PRAGMA database_list').fetchone()[2])
    batches = []
    for kind in ('code', 'theory'):
        batch = queue_batch(db, {**spec(kind), 'question_source': 'original'}, 'old-' + kind)
        run_batch(location, batch['id'])
        batches.append(batch_state(db, batch['id']))
    failed = queue_batch(db, spec('theory'), 'old-failed')
    db.execute("UPDATE free_practice_batch SET status='failed',error='Legacy generation failed' WHERE id=?", (failed['id'],))
    db.execute("DELETE FROM idempotency_record WHERE operation='free_batch_confirmation'")
    db.commit()
    return batches


def facts(db):
    return {table: [tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
            for table in ('task', 'attempt', 'review_round', 'free_practice_batch', 'model_job')}


def test_legacy_success_failure_and_requests_do_not_confirm_on_entry(monkeypatch):
    monkeypatch.setattr('app.services.free_batches.schedule_batch', lambda *a: None)
    monkeypatch.setattr('app.services.bootstrap.load_initial_sources', list)
    with TestClient(app) as client, connect_database(app.state.database_path) as db:
        old = legacy_batches(db)
        before = facts(db)
        for _ in range(2):
            html = client.get('/free-practice').text
            assert '先确认今天的题单' in html
            assert 'Legacy generation failed' not in html
            assert all(task['id'] not in html for batch in old for task in batch['result']['tasks'])
            state = client.post('/api/free-practice/restore', headers={'X-Requested-With': 'learning-practice'}).json()
            assert all(not s['batch'] and not s['result_batch'] for s in state.values())
        # Lost-response replay may recover its old result, but must not activate history.
        queue_batch(db, {**spec(), 'question_source': 'original'}, 'old-code')
        assert practice_state(db)['code']['batch'] is None
        assert facts(db) == before


def test_new_confirmation_survives_reopen_without_resurfacing_legacy_results(database, monkeypatch):
    monkeypatch.setattr('app.services.free_batches.schedule_batch', lambda *a: None)
    legacy_batches(database)
    batch = queue_batch(database, spec('theory'), 'new-confirmation')
    database.execute("UPDATE free_practice_batch SET status='failed',error='New failure' WHERE id=?", (batch['id'],))
    database.commit()
    location = Path(database.execute('PRAGMA database_list').fetchone()[2])
    with connect_database(location) as reopened:
        state = restore_free(reopened)['theory']
        assert state['batch']['id'] == batch['id'] and state['batch']['error'] == 'New failure'
        assert state['result_batch'] is None
    confirmed = queue_batch(database, {**spec(), 'question_source': 'original'}, 'new-original')
    run_batch(location, confirmed['id'])
    current = practice_state(database)['code']
    with connect_database(location) as reopened:
        assert restore_free(reopened)['code'] == current
    assert practice_state(database)['theory']['batch']['id'] == batch['id']


@pytest.mark.parametrize('confirmed', [False, True])
def test_next_day_inherits_only_confirmed_settings(database, monkeypatch, confirmed):
    monkeypatch.setattr('app.services.free_batches.schedule_batch', lambda *a: None)
    day = local_today()
    monkeypatch.setattr('app.services.free_batches.local_today', lambda: day)
    legacy_batches(database)
    if confirmed:
        queue_batch(database, {**spec(), 'question_source': 'original', 'count': 2}, 'confirmed-original')
    day += timedelta(days=1)
    before = database.execute('SELECT COUNT(*) FROM free_practice_batch').fetchone()[0]
    state = restore_free(database)
    assert state['theory']['batch'] is None
    if confirmed:
        assert state['code']['batch']['spec']['count'] == 2
        assert restore_free(database)['code']['batch']['id'] == state['code']['batch']['id']
    else:
        assert state['code']['batch'] is None
    assert database.execute('SELECT COUNT(*) FROM free_practice_batch').fetchone()[0] == before + int(confirmed)
    assert all(json.loads(row[0])['question_source'] == 'original' for row in database.execute(
        "SELECT payload_json FROM free_practice_batch WHERE request_key LIKE 'free-auto:%'"))
