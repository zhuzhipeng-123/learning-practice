import json
import logging
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from app.services.model_jobs import (
    ModelJobError,
    _claim_job,
    job_lease_heartbeat,
    update_model_request_owned,
)
from app.services.module_jobs import run_module_job
from app.storage.database import connect_database


def _pending_job(connection, job_id='lease-job'):
    now = datetime.now(UTC).isoformat()
    connection.execute("INSERT INTO model_job VALUES (?,?,?,'pending',0,NULL,NULL,NULL,?,?)",
                       (job_id, job_id, 'daily_reflection', now, now))
    connection.execute(
        "INSERT INTO model_request(job_id,module,config_json,input_json,prompt_hash) VALUES (?,?,?,?,?)",
        (job_id, 'daily_reflection', json.dumps({'provider': 'agnes'}), '{}', 'prompt-hash'),
    )
    connection.commit()


def test_heartbeat_keeps_long_execution_owned_and_deadline_stops_writes(database, monkeypatch):
    import app.services.model_jobs as jobs

    _pending_job(database)
    claimed = _claim_job(database, 'lease-job', context_loader=lambda connection, job_id: {
        'request': dict(connection.execute('SELECT * FROM model_request WHERE job_id=?', (job_id,)).fetchone())})
    execution_id = claimed['execution_id']
    renewed = Event()
    original = jobs.renew_job_lease

    def observable_renew(*args, **kwargs):
        result = original(*args, **kwargs)
        renewed.set()
        return result

    monkeypatch.setattr(jobs, 'HEARTBEAT_INTERVAL_SECONDS', 0.01)
    monkeypatch.setattr(jobs, 'renew_job_lease', observable_renew)
    location = database.execute('PRAGMA database_list').fetchone()[2]
    with job_lease_heartbeat(database, 'lease-job', execution_id):
        assert renewed.wait(2)
        contender = connect_database(location)
        try:
            with pytest.raises(ModelJobError) as busy:
                _claim_job(contender, 'lease-job', context_loader=lambda connection, job_id: {
                    'request': dict(connection.execute('SELECT * FROM model_request WHERE job_id=?', (job_id,)).fetchone())})
            assert busy.value.in_progress
        finally:
            contender.close()

    database.execute("UPDATE model_job_execution SET deadline_at=? WHERE job_id='lease-job'",
                     ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),))
    database.commit()
    with pytest.raises(ModelJobError, match='执行权'):
        update_model_request_owned(database, 'lease-job', execution_id, response_text='too late')
    assert database.execute("SELECT response_text FROM model_request WHERE job_id='lease-job'").fetchone()[0] is None


def test_late_format_repair_cannot_overwrite_reclaimed_execution(database, monkeypatch):
    import app.services.interview_setup as setup

    context = {'mode': 'suggest', 'direction': '', 'job_focus': 'Agent', 'avoid': [],
               'source_id': 'lease-target', 'learning_context': []}
    entered_repair = Event()
    release_old = Event()
    errors = []
    original = setup.checked_suggestions
    calls = 0

    def gated_repair(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered_repair.set()
            assert release_old.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(setup, 'checked_suggestions', gated_repair)
    old_model = SimpleNamespace(complete=lambda *args, **kwargs: SimpleNamespace(
        content='{}', model='old-model', usage={'total_tokens': 7}))

    def run_old():
        try:
            run_module_job(database, 'interview_preparation', 'lease-target', 'same-key',
                           old_model, context)
        except ModelJobError as error:  # asserted after the competing execution finishes
            errors.append(error)

    worker = Thread(target=run_old)
    worker.start()
    assert entered_repair.wait(5)
    job_id = database.execute("SELECT id FROM model_job WHERE business_key='interview_preparation:same-key'").fetchone()[0]
    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    database.execute("UPDATE model_job_execution SET lease_expires_at=?,deadline_at=? WHERE job_id=?",
                     (expired, expired, job_id))
    database.commit()

    location = database.execute('PRAGMA database_list').fetchone()[2]
    contender = connect_database(location)
    try:
        current_model = SimpleNamespace(complete=lambda *args, **kwargs: SimpleNamespace(
            content=json.dumps({'directions': ['并发恢复', '工具调用', '评估设计']}),
            model='current-model'))
        result = run_module_job(contender, 'interview_preparation', 'lease-target', 'same-key',
                                current_model, context)
        assert json.loads(result['response_text'])['directions'][0] == '并发恢复'
    finally:
        contender.close()
    release_old.set()
    worker.join(5)

    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], ModelJobError)
    row = database.execute("SELECT j.status,r.response_text,r.config_json FROM model_job j "
                           "JOIN model_request r ON r.job_id=j.id WHERE j.id=?", (job_id,)).fetchone()
    assert row['status'] == 'complete'
    assert json.loads(row['response_text'])['directions'][0] == '并发恢复'
    assert 'format_repair' not in json.loads(row['config_json'])


def test_model_diagnostics_keep_usage_but_not_private_content(database, caplog):
    context = {'mode': 'suggest', 'direction': '', 'job_focus': 'PRIVATE PROMPT TEXT', 'avoid': [],
               'source_id': 'diagnostic-target', 'learning_context': []}
    model = SimpleNamespace(complete=lambda *args, **kwargs: SimpleNamespace(
        content=json.dumps({'directions': ['方向甲', '方向乙', '方向丙']}),
        model='fixture-model', usage={'prompt_tokens': 11, 'completion_tokens': 5}))
    with caplog.at_level(logging.INFO, logger='learning.model_job'):
        run_module_job(database, 'interview_preparation', 'diagnostic-target', 'diagnostic-key',
                       model, context)
    output = '\n'.join(caplog.messages)
    assert '"stage": "call"' in output
    assert '"prompt_tokens": 11' in output
    assert 'PRIVATE PROMPT TEXT' not in output
