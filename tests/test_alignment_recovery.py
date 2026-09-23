import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.source_refresh import (
    alignment_request_status,
    queue_source_refresh,
    recover_interrupted_alignment_runs,
    source_status,
)
from app.services.tasks import IdempotencyConflictError
from tests.helpers import add_source


def test_acceptance_is_persisted_before_dispatch_and_restart_can_resume(database, monkeypatch):
    add_source(database)
    scheduled = []
    monkeypatch.setattr('app.services.source_refresh._schedule_run', lambda *args: scheduled.append(args))

    accepted = queue_source_refresh(database, 'source-code', force=True, change_note='frozen note',
                                    request_key='accepted-before-worker')

    run_id = accepted['runs'][0]['run_id']
    assert scheduled and scheduled[0][2] == run_id
    assert database.execute('SELECT status FROM alignment_run WHERE id=?', (run_id,)).fetchone()[0] == 'queued'
    assert alignment_request_status(database, 'accepted-before-worker', sources=[])['runs'][0]['run_id'] == run_id

    # Reads are side-effect free. Only single-process startup releases interrupted work.
    before = database.total_changes
    assert source_status(database)[0]['recoverable'] is False
    assert alignment_request_status(database, 'accepted-before-worker')['status'] == 'running'
    assert database.total_changes == before
    assert database.execute('SELECT status FROM alignment_run WHERE id=?', (run_id,)).fetchone()[0] == 'queued'
    assert recover_interrupted_alignment_runs(database) == 1
    assert source_status(database)[0]['recoverable'] is True
    replay = queue_source_refresh(database, 'source-code', force=True, change_note='frozen note',
                                  request_key='accepted-before-worker')
    assert replay['runs'][0]['run_id'] == run_id
    assert len(scheduled) == 2


def test_alignment_request_keys_recover_alias_and_reject_changed_payload(database, monkeypatch):
    add_source(database)
    monkeypatch.setattr('app.services.source_refresh._schedule_run', lambda *args: None)
    first = queue_source_refresh(database, 'source-code', force=True, change_note='original', request_key='first')

    same = queue_source_refresh(database, 'source-code', force=True, change_note='original', request_key='first')
    assert same['runs'][0]['run_id'] == first['runs'][0]['run_id']
    with pytest.raises(IdempotencyConflictError):
        queue_source_refresh(database, 'source-code', force=True, change_note='changed', request_key='first')

    alias = queue_source_refresh(database, 'source-code', force=True, change_note='different note', request_key='second')
    assert alias['runs'][0]['run_id'] == first['runs'][0]['run_id']
    assert alias['runs'][0]['reused'] == 1
    frozen = json.loads(alias['runs'][0]['summary_json'])
    assert frozen['change_note'] == 'original'
    assert database.execute('SELECT COUNT(DISTINCT run_id) FROM alignment_request_run').fetchone()[0] == 1


def test_completed_aliases_return_their_original_run(database, monkeypatch):
    add_source(database)
    monkeypatch.setattr('app.services.source_refresh._schedule_run', lambda *args: None)
    first = queue_source_refresh(database, 'source-code', force=True, request_key='owner')
    alias = queue_source_refresh(database, 'source-code', force=True, request_key='alias')
    run_id = first['runs'][0]['run_id']
    database.execute("UPDATE alignment_run SET status='complete' WHERE id=?", (run_id,))
    database.commit()

    assert alignment_request_status(database, 'owner')['terminal'] is True
    recovered = alignment_request_status(database, 'alias')
    assert recovered['terminal'] is True
    assert recovered['runs'][0]['run_id'] == alias['runs'][0]['run_id'] == run_id


def test_alignment_api_returns_durable_run_and_conflicts_on_changed_payload(monkeypatch):
    from app.services.source_refresh import queue_source_refresh as real_queue

    monkeypatch.setattr('app.routes.api.queue_source_refresh', real_queue)
    monkeypatch.setattr('app.services.source_refresh._schedule_run', lambda *args: None)
    headers = {'X-Requested-With': 'learning-practice', 'Idempotency-Key': 'browser-alignment'}
    with TestClient(app) as client:
        first = client.post('/api/sources/source-code/sync', headers=headers,
                            json={'change_note': 'frozen note'})
        assert first.status_code == 200
        run_id = first.json()['runs'][0]['run_id']
        recovered = client.get('/api/alignment-requests/browser-alignment')
        assert recovered.status_code == 200
        assert recovered.json()['runs'][0]['run_id'] == run_id
        replay = client.post('/api/sources/source-code/sync', headers=headers,
                             json={'change_note': 'frozen note'})
        assert replay.status_code == 200 and replay.json()['runs'][0]['run_id'] == run_id
        conflict = client.post('/api/sources/source-code/sync', headers=headers,
                               json={'change_note': 'changed note'})
        assert conflict.status_code == 409


def test_http_alignment_uses_real_scheduler_and_status_reads_are_read_only(monkeypatch):
    from app.services.source_refresh import _jobs, _lock
    from app.services.source_refresh import queue_source_refresh as real_queue
    from tests.test_docx_parser import heading, text_block
    from tests.test_docx_reader import FakeClient

    raw = [heading('module', 2, 'Agent'), heading('question', 3, 'Why use tools?'),
           text_block('answer', 'Tools let a model act on external systems.')]
    document_client = FakeClient([
        {'document': {'revision_id': 1}},
        {'items': raw, 'has_more': False},
        {'document': {'revision_id': 1}},
    ])

    class ExternalClient:
        def run_read(self, arguments):
            if arguments[0] == 'wiki':
                return SimpleNamespace(data={'node': {
                    'node_token': 'demo-code', 'parent_node_token': '', 'space_id': 'space',
                    'obj_token': 'demo-code-document', 'obj_type': 'docx',
                    'title': '代码题', 'has_child': False,
                }})
            return document_client.run_read(arguments)

    monkeypatch.setattr('app.routes.api.queue_source_refresh', real_queue)
    monkeypatch.setattr('app.services.wiki_alignment.LarkCliClient', ExternalClient)
    headers = {'X-Requested-With': 'learning-practice', 'Idempotency-Key': 'real-dispatch'}
    with TestClient(app) as client:
        from app.storage.database import connect_database
        with connect_database(app.state.database_path) as connection:
            connection.execute("UPDATE source SET question_type='theory' WHERE id='source-code'")
            connection.commit()
        accepted = client.post('/api/sources/source-code/sync', headers=headers,
                               json={})
        assert accepted.status_code == 200
        with _lock:
            future = _jobs[(str(app.state.database_path), 'source-code')]
        future.result(timeout=5)
        status = client.get('/api/alignment-requests/real-dispatch')
        assert status.status_code == 200
        assert status.json()['terminal'] is True
        database = app.state.database_path
        with connect_database(database) as connection:
            before = connection.total_changes
            source_status(connection)
            alignment_request_status(connection, 'real-dispatch')
            assert connection.total_changes == before
            assert connection.execute('SELECT COUNT(*) FROM question').fetchone()[0] == 1


def test_duplicate_dispatch_cannot_restart_a_terminal_alignment(database, monkeypatch):
    from app.services.source_refresh import _refresh
    from app.storage.transactions import transaction

    add_source(database)
    database.execute("INSERT INTO alignment_run(id,source_id,started_at,status) "
                     "VALUES ('one-run','source-code',datetime('now'),'queued')")
    database.commit()
    calls = []

    def finish(connection, source_id, analyze, client=None, change_note='', run_id=None):
        calls.append(run_id)
        with transaction(connection):
            connection.execute("UPDATE alignment_run SET status='complete' WHERE id=?", (run_id,))

    monkeypatch.setattr('app.services.wiki_alignment.align_source_tree', finish)
    location = database.execute('PRAGMA database_list').fetchone()[2]
    _refresh(location, 'source-code', 'one-run')
    _refresh(location, 'source-code', 'one-run')
    assert calls == ['one-run']
    assert database.execute("SELECT status FROM alignment_run WHERE id='one-run'").fetchone()[0] == 'complete'
