import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.source_refresh import (
    alignment_request_status,
    queue_source_refresh,
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

    # A new process has no in-memory future. A read marks the durable run as recoverable.
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
