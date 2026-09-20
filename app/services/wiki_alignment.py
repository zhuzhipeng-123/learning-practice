"""One button run: discover every file, refresh originals, then analyze boundaries."""

import json
import logging
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from threading import BoundedSemaphore, Event

from app.adapters.lark_cli import LarkCliClient
from app.adapters.wiki_reader import read_wiki_tree
from app.services.alignment_report import empty_inventory_changes
from app.services.source_sync import sync_registered_source
from app.services.wiki_sources import reconcile_tree
from app.storage.database import connect_database
from app.storage.ids import new_id
from app.storage.transactions import transaction

_analysis_slots = BoundedSemaphore(2)


def initial_alignment_summary(change_note=''):
    return {'phase': 'queued', 'change_note': change_note, 'documents': [], 'tree_errors': [],
            'stage_errors': [], 'tree_complete': False, 'document_changes': {},
            'analysis': {'status': 'running', 'total': 0, 'processed': 0, 'suggestions': [], 'errors': []}}


def align_source_tree(connection, source_id, analyze, client=None, change_note='', run_id=None):
    root = dict(connection.execute('SELECT * FROM source WHERE id=?', (source_id,)).fetchone())
    run_id = run_id or new_id('alignment')
    summary = initial_alignment_summary(change_note)
    summary['phase'] = 'discovery'
    with transaction(connection):
        existing = connection.execute('SELECT source_id,summary_json FROM alignment_run WHERE id=?', (run_id,)).fetchone()
        if existing:
            if existing['source_id'] != source_id:
                raise ValueError('alignment run does not belong to this source')
            saved = json.loads(existing['summary_json'] or '{}')
            if saved.get('change_note', '') != change_note:
                raise ValueError('alignment run payload changed')
            connection.execute("UPDATE alignment_run SET status='running',finished_at=NULL,error=NULL WHERE id=?", (run_id,))
        else:
            connection.execute("INSERT INTO alignment_run(id,source_id,started_at,status) VALUES (?,?,?,'running')",
                               (run_id, source_id, datetime.now(UTC).isoformat()))
    _save(connection, run_id, summary)
    stage = 'discovery'
    try:
        remote = client or LarkCliClient()
        if '/wiki/' in root['wiki_url']:
            tree = read_wiki_tree(remote, root['wiki_url'], root['identity'])
            summary.update(tree_complete=tree['complete'], tree_errors=tree['errors'], discovered_nodes=len(tree['nodes']))
            sources, changes = reconcile_tree(connection, root, tree)
            summary['document_changes'] = changes
        else:
            sources = {source_id: root['document_id']}
            summary['tree_complete'] = True
        summary['documents'] = [{'source_id': key, 'path': path, 'status': 'pending', 'change_note': change_note} for key, path in sources.items()]
        summary['phase'] = 'documents'
        stage = 'documents'
        location = connection.execute('PRAGMA database_list').fetchone()[2]
        model_unavailable = Event()
        with ThreadPoolExecutor(max_workers=3) as pool:
            pending = {pool.submit(_align_document, location, item, analyze, remote, model_unavailable) for item in summary['documents']}
            while pending:
                done, pending = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
                for future in done:
                    future.result()
                stage = 'aggregation'
                _aggregate(summary)
                _save(connection, run_id, summary)
                stage = 'documents'
        if change_note:
            stage = 'review'
            summary['phase'] = 'review'; _save(connection, run_id, summary)
            if model_unavailable.is_set():
                summary['review_error'] = '本轮模型连接异常，原文读取已保留；请稍后重新对齐以完成复核'
            else:
                _review_changes(connection, source_id, run_id, summary)
        incomplete = not summary['tree_complete'] or summary.get('review_error') or summary['document_changes'].get('unsupported') or any(item['status'] != 'complete' for item in summary['documents'])
        stage = 'finalization'
        summary['phase'] = 'finished'
        _save(connection, run_id, summary, 'partial' if incomplete else 'complete')
    except Exception as error:
        logging.getLogger(__name__).exception('Source alignment failed during %s: %s', stage, source_id)
        summary['failure_stage'] = stage
        if stage == 'discovery':
            summary['tree_errors'].append({'path': root['wiki_url'], 'error': str(error)})
        else:
            summary['stage_errors'].append({'stage': stage, 'error': str(error)})
        summary['phase'] = 'failed'
        _save(connection, run_id, summary, 'failed')
    return summary


def _align_document(location, item, analyze, remote, model_unavailable=None):
    connection = connect_database(location)
    try:
        item['status'] = 'reading'
        result = sync_registered_source(connection, item['source_id'], remote)
        result['change_note'] = item.get('change_note', '')
        item['status'] = 'analyzing'
        with _analysis_slots:
            if model_unavailable and model_unavailable.is_set() and result['candidate_count']:
                result['analysis'] = {'status': 'partial', 'total': result['candidate_count'], 'processed': 0, 'suggestions': [],
                                      'errors': ['本轮模型连接异常，这篇候选尚未复核；原文已读取']}
            else:
                result['analysis'] = analyze(connection, item['source_id'], result)
                if model_unavailable and any(any(word in error for word in ('连接', '超时', 'HTTP 5')) for error in result['analysis']['errors']):
                    model_unavailable.set()
        item['result'] = result
        item['status'] = 'partial' if result['partial'] or result['analysis']['errors'] else 'complete'
    except Exception as error:
        logging.getLogger(__name__).exception('Document alignment failed: %s', item['source_id'])
        item.update(status='failed', error=str(error))
    finally:
        connection.close()


def _aggregate(summary):
    summary['changes'] = empty_inventory_changes()
    summary['changes']['unchanged'] = 0
    summary['candidate_count'] = 0
    analysis = {'status': 'complete', 'total': 0, 'processed': 0, 'suggestions': [], 'errors': []}
    for document in summary['documents']:
        result = document.get('result', {})
        summary['candidate_count'] += result.get('candidate_count', 0)
        for key, value in result.get('changes', {}).items():
            summary['changes'][key] += value
        for key in ('total', 'processed', 'suggestions', 'errors'):
            analysis[key] += result.get('analysis', {}).get(key, 0 if key in {'total', 'processed'} else [])
    analysis['status'] = 'partial' if analysis['errors'] else 'complete' if analysis['total'] else 'not_needed'
    summary['analysis'] = analysis


def _save(connection, run_id, summary, status='running'):
    with transaction(connection):
        connection.execute('UPDATE alignment_run SET status=?,summary_json=?,finished_at=? WHERE id=?',
                           (status, json.dumps(summary, ensure_ascii=False), None if status == 'running' else datetime.now(UTC).isoformat(), run_id))


def _review_changes(connection, source_id, run_id, summary):
    from app.services.model_json import parse_model_json
    from app.services.module_jobs import run_module_job
    context = {'source_id': source_id, 'mode': 'change_review', 'user_change_description': summary['change_note'],
               'tree_complete': summary['tree_complete'], 'tree_errors': summary['tree_errors'],
               'document_changes': summary['document_changes'],
               'question_changes': summary['changes'],
               'documents': [{'path': d['path'], 'status': d['status'], 'error': d.get('error'),
                              'candidates': d.get('result', {}).get('candidate_count', 0)} for d in summary['documents']]}
    try:
        with _analysis_slots:
            reply = run_module_job(connection, 'source_parsing', source_id, run_id + ':review', context_override=context)
        summary['user_review'] = parse_model_json(reply['response_text'])
    except Exception as error:
        logging.getLogger(__name__).exception('User change review failed: %s', source_id)
        summary['review_error'] = str(error)
