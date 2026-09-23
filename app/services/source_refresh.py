"""Non-blocking, per-source refresh; network work owns a separate connection."""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Lock

from app.services.model_batches import prepare_batches, prepare_format_retry
from app.services.model_jobs import ModelJobError
from app.services.model_json import parse_model_json
from app.services.module_jobs import run_module_job
from app.services.source_coverage import parsing_batches, validate_suggestions
from app.services.tasks import IdempotencyConflictError, _payload_hash
from app.storage.database import connect_database
from app.storage.ids import new_id
from app.storage.transactions import transaction

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="source-refresh")
_lock = Lock()
_jobs = {}
REFRESH_COOLDOWN = timedelta(minutes=10)


def source_status(connection):
    location = connection.execute("PRAGMA database_list").fetchone()[2]
    rows = [dict(row) for row in connection.execute(
        "SELECT s.id,s.question_type,s.last_check_at,s.last_check_success_at,s.last_complete_sync_at,s.last_error,"
        "COALESCE((SELECT summary_json FROM alignment_run WHERE source_id=s.id ORDER BY rowid DESC LIMIT 1),"
        "(SELECT summary_json FROM sync_run WHERE source_id=s.id ORDER BY rowid DESC LIMIT 1)) AS summary_json,"
        "(SELECT id FROM alignment_run WHERE source_id=s.id ORDER BY rowid DESC LIMIT 1) AS alignment_run_id,"
        "COALESCE((SELECT status FROM alignment_run WHERE source_id=s.id ORDER BY rowid DESC LIMIT 1),"
        "(SELECT status FROM sync_run WHERE source_id=s.id ORDER BY rowid DESC LIMIT 1)) AS sync_status,"
        "(SELECT COUNT(*) FROM parser_candidate WHERE source_id=s.id AND status='pending') AS pending "
        "FROM source s WHERE s.enabled=1 AND NOT EXISTS(SELECT 1 FROM source_tree_member m WHERE m.source_id=s.id AND m.root_source_id!=s.id) ORDER BY s.id")]
    with _lock:
        for row in rows:
            row['member_source_ids'] = [row['id'], *[item[0] for item in connection.execute(
                'SELECT DISTINCT source_id FROM source_tree_member WHERE root_source_id=? AND source_id IS NOT NULL', (row['id'],))]]
            job = _jobs.get((location, row['id']))
            row['running'] = row['sync_status'] in {'queued', 'running'} and bool(job and not job.done())
            row['recoverable'] = row['sync_status'] == 'recoverable'
    return rows


def queue_source_refresh(connection, source_id=None, force=False, change_note='', request_key=None):
    if source_id:
        parent = connection.execute('SELECT root_source_id FROM source_tree_member WHERE source_id=? AND root_source_id!=? LIMIT 1', (source_id, source_id)).fetchone()
        source_id = parent[0] if parent else source_id
    location = connection.execute("PRAGMA database_list").fetchone()[2]
    now = datetime.now(UTC)
    sources = source_status(connection)
    if source_id and not any(source["id"] == source_id for source in sources):
        raise ValueError("没有找到启用的题源")
    selected = []
    for source in sources:
        if source_id and source['id'] != source_id:
            continue
        checked = datetime.fromisoformat(source['last_check_at']) if source['last_check_at'] else None
        if force or not checked or now - checked >= REFRESH_COOLDOWN:
            selected.append(source)
    if not request_key:
        request_key = new_id('alignment-request')
    if len(request_key) > 200:
        raise ValueError('请求标识过长')
    payload = {'source_id': source_id, 'force': bool(force), 'change_note': change_note}
    created = []
    with transaction(connection):
        prior = connection.execute('SELECT payload_hash FROM alignment_request WHERE request_key=?',
                                   (request_key,)).fetchone()
        if prior and prior['payload_hash'] != _payload_hash(payload):
            raise IdempotencyConflictError('request key was reused with different input')
        if not prior:
            stamp = now.isoformat()
            connection.execute('INSERT INTO alignment_request VALUES (?,?,?,?,?,?)',
                               (request_key, _payload_hash(payload), json.dumps(payload, ensure_ascii=False, sort_keys=True),
                                'accepted', stamp, stamp))
            for source in selected:
                active = connection.execute(
                    "SELECT id,status,summary_json FROM alignment_run WHERE source_id=? "
                    "AND status IN ('queued','running','recoverable') ORDER BY rowid DESC LIMIT 1",
                    (source['id'],),
                ).fetchone()
                if active:
                    run_id, reused = active['id'], 1
                    if active['status'] == 'recoverable':
                        frozen_note = json.loads(active['summary_json'] or '{}').get('change_note', '')
                        created.append((source['id'], run_id, frozen_note))
                else:
                    from app.services.wiki_alignment import initial_alignment_summary
                    run_id, reused = new_id('alignment'), 0
                    summary = initial_alignment_summary(change_note)
                    connection.execute("INSERT INTO alignment_run(id,source_id,started_at,status,summary_json) "
                                       "VALUES (?,?,?,'queued',?)",
                                       (run_id, source['id'], stamp, json.dumps(summary, ensure_ascii=False)))
                    created.append((source['id'], run_id, change_note))
                connection.execute('INSERT INTO alignment_request_run VALUES (?,?,?,?)',
                                   (request_key, source['id'], run_id, reused))
        else:
            for row in connection.execute(
                "SELECT a.source_id,a.run_id,r.summary_json FROM alignment_request_run a "
                "JOIN alignment_run r ON r.id=a.run_id WHERE a.request_key=? AND r.status='recoverable'",
                (request_key,),
            ):
                note = json.loads(row['summary_json'] or '{}').get('change_note', '')
                created.append((row['source_id'], row['run_id'], note))
    for queued_source, run_id, frozen_note in created:
        _schedule_run(location, queued_source, run_id, frozen_note)
    return alignment_request_status(connection, request_key, sources=sources)


def _schedule_run(location, source_id, run_id, change_note):
    key = (location, source_id)
    with _lock:
        existing = _jobs.get(key)
        if existing and not existing.done():
            return
        _jobs[key] = _pool.submit(_refresh, location, source_id, run_id, change_note)


def _refresh(location, source_id, run_id, change_note=''):
    connection = connect_database(location)
    try:
        with transaction(connection):
            claimed = connection.execute(
                "UPDATE alignment_run SET status='running',finished_at=NULL,error=NULL "
                "WHERE id=? AND source_id=? AND status IN ('queued','recoverable')",
                (run_id, source_id),
            ).rowcount
        if not claimed:
            return
        from app.services.wiki_alignment import align_source_tree
        align_source_tree(connection, source_id, analyze_alignment, change_note=change_note, run_id=run_id)
    except Exception as error:
        logging.getLogger(__name__).exception("Source refresh failed: %s", source_id)
        with transaction(connection):
            connection.execute("UPDATE alignment_run SET status='failed',finished_at=?,error=? "
                               "WHERE id=? AND status='running'",
                               (datetime.now(UTC).isoformat(), str(error), run_id))
    finally:
        connection.close()


def alignment_request_status(connection, request_key, sources=None):
    request = connection.execute('SELECT * FROM alignment_request WHERE request_key=?', (request_key,)).fetchone()
    if not request:
        raise ValueError('没有找到这次对齐请求')
    rows = [dict(row) for row in connection.execute(
        "SELECT a.source_id,a.run_id,a.reused,r.status,r.summary_json,r.error FROM alignment_request_run a "
        "JOIN alignment_run r ON r.id=a.run_id WHERE a.request_key=? ORDER BY a.source_id", (request_key,))]
    terminal = all(row['status'] in {'complete', 'partial', 'failed'} for row in rows)
    status = 'complete' if terminal else 'recoverable' if any(row['status'] == 'recoverable' for row in rows) else 'running'
    return {'request_key': request_key, 'status': status, 'terminal': terminal, 'runs': rows,
            'used_cache': True,
            'refreshing': [row['source_id'] for row in rows if row['status'] not in {'complete', 'partial', 'failed'}],
            'sources': sources if sources is not None else source_status(connection)}


def recover_interrupted_alignment_runs(connection):
    """Release durable runs only at single-process startup, never during a status read."""
    with transaction(connection):
        return connection.execute(
            "UPDATE alignment_run SET status='recoverable',finished_at=NULL,"
            "error='服务重启中断了本轮对齐，请按原请求继续' "
            "WHERE status IN ('queued','running')"
        ).rowcount


def analyze_alignment(connection, source_id, result, client=None):
    """Every explicit alignment includes model analysis of all unresolved candidates."""
    count = connection.execute("SELECT COUNT(*) FROM parser_candidate WHERE source_id=? AND status='pending'", (source_id,)).fetchone()[0]
    analysis = {'status': 'running' if count else 'not_needed', 'total': count, 'processed': 0, 'suggestions': [], 'errors': []}
    result['analysis'] = analysis
    _save_summary(connection, result)
    try:
        payload = {'source_id': source_id, 'change_note': result.get('change_note', '')}
        batches = prepare_batches(connection, 'source_parsing', result['run_id'], payload,
                                  lambda: parsing_batches(connection, source_id, payload['change_note']))
    except Exception as error:
        logging.getLogger(__name__).exception('Source analysis plan could not be prepared: %s', source_id)
        analysis.update(status='partial', errors=[str(error)])
        _save_summary(connection, result)
        return analysis
    contexts = [json.loads(connection.execute('SELECT input_json FROM model_request WHERE job_id=?',
                                             (batch['job_id'],)).fetchone()[0]) for batch in batches]
    analysis['total'] = sum(len(context['candidates']) for context in contexts)
    for batch, context in zip(batches, contexts, strict=True):
        try:
            try:
                reply = run_module_job(connection, 'source_parsing', source_id, batch['key'], client)
            except ModelJobError as error:
                if not any(marker in str(error) for marker in ('JSON', '边界建议', '遗漏了部分候选')):
                    raise
                # One bounded format retry; preserve both requests for inspection.
                retry = prepare_format_retry(connection, 'source_parsing', batch)
                reply = run_module_job(connection, 'source_parsing', source_id, retry['key'], client)
            titles = {item['anchor_id']: item['title'] for item in context['candidates']}
            payload = parse_model_json(reply['response_text'])
            validate_suggestions(context, payload)
            items = payload['suggestions']
            analysis['suggestions'].extend({**item, 'title': titles[item['anchor_id']]} for item in items)
            analysis['processed'] += len(items)
        except Exception as error:
            logging.getLogger(__name__).exception("Source model analysis failed: %s", source_id)
            analysis['errors'].append(str(error))
            # A model-service failure should not make dozens of repeated calls.
            break
        finally:
            _save_summary(connection, result)
    analysis['status'] = 'partial' if analysis['errors'] else 'complete' if analysis['total'] else 'not_needed'
    _save_summary(connection, result)
    return analysis


def _save_summary(connection, result):
    with transaction(connection):
        connection.execute("UPDATE sync_run SET summary_json=? WHERE id=?", (json.dumps(result, ensure_ascii=False), result['run_id']))
