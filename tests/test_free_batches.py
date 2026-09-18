import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.free_batches import (
    batch_state,
    interrupt_batches,
    practice_state,
    queue_batch,
    retry_batch,
    run_batch,
)
from app.services.learning_clock import local_today
from app.services.tasks import IdempotencyConflictError
from app.storage.database import connect_database, initialize_database
from tests.test_student_workflow import pool


def spec(kind='code', **values):
    return dict(question_source='variant', question_type=kind, mode='random',
                theme='', count=1, only_new=False, plan_id=None, **values)


@pytest.fixture
def manual_batches(monkeypatch):
    monkeypatch.setattr('app.services.free_batches.schedule_batch', lambda *args: None)


def fake_client(messages, **kwargs):
    value = json.loads(messages[1]['content'])
    return SimpleNamespace(content=json.dumps({'questions': [
        {'base_question_id': q['id'], 'prompt': 'New independent variant: ' + q['prompt'],
         'reference_text': 'Check bounds and derive the result.'} for q in value['questions']]}), model='fake')


def test_code_and_theory_generate_concurrently_without_holding_database(database, monkeypatch, manual_batches):
    pool(database, 3, 'code')
    pool(database, 3, 'theory')
    database.commit()
    code = queue_batch(database, spec(), 'code-request')
    theory = queue_batch(database, spec('theory'), 'theory-request')
    entered, release = Event(), Event()
    def complete(messages, **kwargs):
        if json.loads(messages[1]['content'])['question_type'] == 'code':
            entered.set()
            assert release.wait(8)
        return fake_client(messages, **kwargs)
    monkeypatch.setattr('app.services.module_jobs.client_for_config', lambda _: SimpleNamespace(complete=complete))
    location = Path(database.execute('PRAGMA database_list').fetchone()[2])
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(run_batch, location, code['id'])
        try:
            assert entered.wait(3)
            workers.submit(run_batch, location, theory['id']).result(timeout=3)
            assert batch_state(database, theory['id'])['status'] == 'complete'
            assert batch_state(database, code['id'])['status'] == 'running'
            duplicate = queue_batch(database, spec(), 'another-code-tab')
            assert duplicate['id'] == code['id']
            assert database.execute('SELECT COUNT(*) FROM free_practice_batch').fetchone()[0] == 2
        finally:
            release.set()
        first.result(timeout=3)
    assert batch_state(database, code['id'])['result']['added'] == 1
    assert database.execute('SELECT COUNT(*) FROM task').fetchone()[0] == 2


def test_batch_survives_same_day_response_loss_but_expires_after_midnight(database, monkeypatch, manual_batches):
    pool(database, 2, 'code')
    database.commit()
    batch = queue_batch(database, spec(), 'lost-response')
    assert queue_batch(database, spec(), 'lost-response')['id'] == batch['id']
    changed = spec()
    changed['count'] = 2
    with pytest.raises(IdempotencyConflictError):
        queue_batch(database, changed, 'lost-response')
    location = Path(database.execute('PRAGMA database_list').fetchone()[2])
    monkeypatch.setattr('app.services.module_jobs.client_for_config', lambda _: SimpleNamespace(complete=lambda *a, **k: (_ for _ in ()).throw(RuntimeError('synthetic timeout'))))
    run_batch(location, batch['id'])
    assert batch_state(database, batch['id'])['status'] == 'failed'
    old_plan = database.execute('SELECT plan_id FROM free_practice_batch').fetchone()[0]
    next_day = local_today() + timedelta(days=1)
    monkeypatch.setattr('app.services.module_jobs.client_for_config', lambda _: SimpleNamespace(complete=fake_client))
    retry_batch(database, batch['id'])
    run_batch(location, batch['id'])
    assert queue_batch(database, spec(), 'lost-response')['id'] == batch['id']
    with connect_database(location) as reopened:
        result = batch_state(reopened, batch['id'])
        assert result['status'] == 'complete'
        assert len(result['result']['tasks']) == 1
        assert reopened.execute('SELECT plan_id FROM task').fetchone()[0] == old_plan
    retry_batch(database, batch['id'])
    run_batch(location, batch['id'])
    assert database.execute('SELECT COUNT(*) FROM task').fetchone()[0] == 1
    monkeypatch.setattr('app.services.free_batches.local_today', lambda: next_day)
    with pytest.raises(ValueError, match='过期'):
        retry_batch(database, batch['id'])
    assert practice_state(database)['code']['result_batch'] is None
    new = queue_batch(database, spec(), 'next-day')
    assert new['id'] != batch['id']
    assert database.execute('SELECT plan_id FROM free_practice_batch WHERE id=?', (new['id'],)).fetchone()[0] != old_plan


def test_latest_batch_is_restored_by_fresh_entry_without_model_calls(monkeypatch, manual_batches):
    monkeypatch.setattr('app.services.bootstrap.load_initial_sources', list)
    monkeypatch.setattr('app.services.module_jobs.client_for_config', lambda _: SimpleNamespace(complete=fake_client))
    with TestClient(app) as client:
        connection = connect_database(app.state.database_path)
        pool(connection, 3, 'code')
        connection.commit()
        body = spec()
        headers = {'X-Requested-With': 'learning-practice', 'Idempotency-Key': 'page-batch'}
        response = client.post('/api/free-practice/batches', headers=headers, json=body)
        assert response.status_code == 200
        batch = response.json()
        run_batch(app.state.database_path, batch['id'])
        state = client.get('/api/free-practice/state').json()['code']
        task = state['result_batch']['result']['tasks'][0]
        page = client.get('/free-practice')
        assert f'/practice/{task["id"]}' not in page.text
        assert 'New independent variant' in page.text
        assert task['id'] in page.text
        assert 'New independent variant' in client.get('/api/free-practice/batches/' + batch['id']).text
        assert 'free-theme-code' in page.text and 'free-theme-theory' in page.text
        assert 'reference_text' not in json.dumps(state)
        connection.close()


def test_restart_releases_only_interrupted_batch_model_lease(database, monkeypatch, manual_batches):
    pool(database, 2, 'code')
    database.commit()
    batch = queue_batch(database, spec(), 'crash')
    location = Path(database.execute('PRAGMA database_list').fetchone()[2])
    monkeypatch.setattr('app.services.module_jobs.client_for_config', lambda _: SimpleNamespace(complete=lambda *a, **k: (_ for _ in ()).throw(RuntimeError('synthetic outage'))))
    run_batch(location, batch['id'])
    database.execute("UPDATE free_practice_batch SET status='running'")
    database.execute("UPDATE model_job SET status='running'")
    database.commit()
    interrupt_batches(database)
    assert batch_state(database, batch['id'])['status'] == 'interrupted'
    assert database.execute('SELECT status FROM model_job').fetchone()[0] == 'failed'
    monkeypatch.setattr('app.services.module_jobs.client_for_config', lambda _: SimpleNamespace(complete=fake_client))
    retry_batch(database, batch['id'])
    run_batch(location, batch['id'])
    assert batch_state(database, batch['id'])['status'] == 'complete'
    assert database.execute('SELECT COUNT(*) FROM task').fetchone()[0] == 1


def test_empty_new_draw_replaces_and_retires_previous_batch(database, manual_batches):
    pool(database, 1, 'code')
    database.commit()
    body = {**spec(), 'question_source':'original', 'only_new':True}
    location = Path(database.execute('PRAGMA database_list').fetchone()[2])
    first = queue_batch(database, body, 'one-new-question')
    run_batch(location, first['id'])
    second = queue_batch(database, body, 'empty-draw')
    run_batch(location, second['id'])
    state = practice_state(database)['code']
    assert state['batch']['result']['added'] == 0
    assert state['result_batch']['id'] == second['id']
    assert state['result_batch']['result']['tasks'] == []
    assert batch_state(database, first['id'])['result']['tasks'][0]['status'] == 'cancelled'


def test_v9_migration_preserves_legacy_free_tasks_and_backup(database):
    from app.services.free_practice import add_free_practice
    from app.services.tasks import create_daily_plan
    pool(database, 1, 'code')
    plan = create_daily_plan(database, local_today(), 0, 0, {}, 'legacy')
    result = add_free_practice(database, plan['plan_id'], '', 1, 'legacy-free', False, True, question_type='code')
    database.execute('DROP TABLE free_practice_batch')
    database.execute('DROP TABLE reference_correction')
    database.execute('DROP TABLE reference_correction_history')
    database.execute('DROP TABLE reference_verification')
    database.execute('DELETE FROM schema_version WHERE version>=10')
    database.commit()
    initialize_database(database)
    state = practice_state(database)['code']
    assert state['earlier'] == []
    assert database.execute('SELECT id FROM task WHERE id=?', (result.task_ids[0],)).fetchone()
    assert state['result_batch'] is None
    directory = Path(database.execute('PRAGMA database_list').fetchone()[2]).parent
    assert list(directory.glob('pre-migration-v9-*.db'))
    assert database.execute('PRAGMA foreign_key_check').fetchall() == []


@pytest.mark.parametrize('patch', [{'count':0}, {'count':4}, {'question_type':'all'}, {'mode':'topic', 'theme':' '}])
def test_bad_batch_does_not_create_jobs_or_block_other_panel(monkeypatch, manual_batches, patch):
    with TestClient(app) as client:
        response = client.post('/api/free-practice/batches', headers={
            'X-Requested-With':'learning-practice', 'Idempotency-Key':'invalid'}, json={**spec(), **patch})
        assert response.status_code == 422
        connection = connect_database(app.state.database_path)
        assert connection.execute('SELECT COUNT(*) FROM free_practice_batch').fetchone()[0] == 0
        assert connection.execute('SELECT COUNT(*) FROM model_job').fetchone()[0] == 0
        connection.close()

@pytest.mark.parametrize('source', ['original', 'variant'])
def test_committed_tasks_recover_after_batch_receipt_crash(database, monkeypatch, manual_batches, source):
    from app.services.free_requests import execute_free_request
    pool(database, 3, 'code')
    database.commit()
    body = {**spec(), 'question_source': source}
    calls = []
    def complete(*args, **kwargs):
        calls.append(1)
        return fake_client(*args, **kwargs)
    monkeypatch.setattr('app.services.module_jobs.client_for_config', lambda _: SimpleNamespace(complete=complete))
    def fail_after_commit(*args):
        execute_free_request(*args)
        raise RuntimeError('process stopped before batch receipt was saved')
    monkeypatch.setattr('app.services.free_batches.execute_free_request', fail_after_commit)
    batch = queue_batch(database, body, 'commit-then-crash')
    location = Path(database.execute('PRAGMA database_list').fetchone()[2])
    run_batch(location, batch['id'])
    task_id = database.execute('SELECT id FROM task').fetchone()[0]
    calls_before = len(calls)
    database.execute("UPDATE free_practice_batch SET status='running',result_json=NULL")
    database.commit()
    interrupt_batches(database)
    monkeypatch.setattr('app.services.free_batches.execute_free_request', execute_free_request)
    retry_batch(database, batch['id'])
    run_batch(location, batch['id'])
    result = batch_state(database, batch['id'])
    assert result['status'] == 'complete'
    assert result['result']['task_ids'] == [task_id]
    assert database.execute('SELECT COUNT(*) FROM task').fetchone()[0] == 1
    assert len(calls) == calls_before


def test_exported_batches_restore_results_and_interrupted_requests(database, monkeypatch, manual_batches, tmp_path):
    import zipfile

    from app.services.exports import export_learning_data, verify_export
    pool(database, 3, 'code')
    pool(database, 3, 'theory')
    database.commit()
    monkeypatch.setattr('app.services.module_jobs.client_for_config', lambda _: SimpleNamespace(complete=fake_client))
    batch = queue_batch(database, spec(), 'saved-batch')
    run_batch(Path(database.execute('PRAGMA database_list').fetchone()[2]), batch['id'])
    pending = queue_batch(database, spec('theory'), 'queued-batch')
    archive = tmp_path / 'with-batches.zip'
    export_learning_data(database, archive, tmp_path / 'media')
    assert verify_export(archive, tmp_path / 'verify')['question_count'] == 7
    restored = tmp_path / 'restored'
    with zipfile.ZipFile(archive) as saved:
        saved.extractall(restored)
    connection = connect_database(restored / 'learning.db')
    try:
        interrupt_batches(connection)
        state = practice_state(connection)
        assert state['code']['result_batch']['id'] == batch['id']
        assert len(state['code']['result_batch']['result']['tasks']) == 1
        assert state['theory']['batch']['status'] == 'interrupted'
        retry_batch(connection, pending['id'])
        run_batch(restored / 'learning.db', pending['id'])
        assert practice_state(connection)['theory']['batch']['status'] == 'complete'
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
    finally:
        connection.close()
