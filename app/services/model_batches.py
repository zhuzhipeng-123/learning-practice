"""Freeze every input and model setting before executing a multi-request operation."""

import hashlib
from datetime import UTC, datetime

from app.services.llm_config import freeze_request
from app.services.tasks import _load_idempotent, _save_idempotent
from app.storage.ids import new_id
from app.storage.transactions import transaction


def prepare_batches(connection, module, request_key, payload, build_contexts):
    plan_key = f'model-batches:{module}:{request_key}'
    with transaction(connection):
        saved = _load_idempotent(connection, plan_key, 'model_batches', payload)
        if saved is not None:
            return saved['batches']
        # Build and validate the complete plan before creating or sending any jobs.
        contexts = build_contexts()
        batches = []
        digest = hashlib.sha256(request_key.encode()).hexdigest()
        now = datetime.now(UTC).isoformat()
        for index, context in enumerate(contexts):
            key = f'parts:{module}:{digest}:{index}'
            job_id = new_id('job')
            connection.execute("INSERT INTO model_job VALUES (?,?,?,'pending',0,NULL,NULL,NULL,?,?)",
                               (job_id, f'{module}:{key}', module, now, now))
            freeze_request(connection, job_id, module, context)
            batches.append({'key': key, 'job_id': job_id, 'target': context['source_id']})
        _save_idempotent(connection, plan_key, 'model_batches', payload, {'batches': batches}, now)
    return batches


def prepare_format_retry(connection, module, batch):
    """Keep the rejected request and clone its frozen settings for one format retry."""
    key = batch['key'] + ':format-retry'
    with transaction(connection):
        existing = connection.execute('SELECT id FROM model_job WHERE business_key=?',
                                      (f'{module}:{key}',)).fetchone()
        if existing:
            return {**batch, 'key': key, 'job_id': existing[0]}
        now, job_id = datetime.now(UTC).isoformat(), new_id('job')
        connection.execute("INSERT INTO model_job VALUES (?,?,?,'pending',0,NULL,NULL,NULL,?,?)",
                           (job_id, f'{module}:{key}', module, now, now))
        connection.execute('INSERT INTO model_request(job_id,module,config_json,input_json,prompt_hash) '
                           'SELECT ?,module,config_json,input_json,prompt_hash FROM model_request WHERE job_id=?',
                           (job_id, batch['job_id']))
    return {**batch, 'key': key, 'job_id': job_id}
