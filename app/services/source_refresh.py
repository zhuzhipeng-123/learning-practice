"""Non-blocking, per-source refresh; network work owns a separate connection."""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Lock

from app.services.model_jobs import ModelJobError
from app.services.model_json import parse_model_json
from app.services.module_jobs import run_module_job
from app.services.source_coverage import parsing_context, validate_suggestions
from app.storage.database import connect_database
from app.storage.transactions import transaction

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="source-refresh")
_lock = Lock()
_jobs = {}


def source_status(connection):
    location = connection.execute("PRAGMA database_list").fetchone()[2]
    rows = [dict(row) for row in connection.execute(
        "SELECT s.id,s.question_type,s.last_check_at,s.last_check_success_at,s.last_error,"
        "COALESCE((SELECT summary_json FROM alignment_run WHERE source_id=s.id ORDER BY rowid DESC LIMIT 1),"
        "(SELECT summary_json FROM sync_run WHERE source_id=s.id ORDER BY rowid DESC LIMIT 1)) AS summary_json,"
        "COALESCE((SELECT status FROM alignment_run WHERE source_id=s.id ORDER BY rowid DESC LIMIT 1),"
        "(SELECT status FROM sync_run WHERE source_id=s.id ORDER BY rowid DESC LIMIT 1)) AS sync_status,"
        "(SELECT COUNT(*) FROM parser_candidate WHERE source_id=s.id AND status='pending') AS pending "
        "FROM source s WHERE s.enabled=1 AND NOT EXISTS(SELECT 1 FROM source_tree_member m WHERE m.source_id=s.id AND m.root_source_id!=s.id) ORDER BY s.id")]
    with _lock:
        for row in rows:
            job = _jobs.get((location, row['id']))
            row['running'] = bool(job and not job.done())
    return rows


def queue_source_refresh(connection, source_id=None, force=False, change_note=''):
    if source_id:
        parent = connection.execute('SELECT root_source_id FROM source_tree_member WHERE source_id=? AND root_source_id!=? LIMIT 1', (source_id, source_id)).fetchone()
        source_id = parent[0] if parent else source_id
    location = connection.execute("PRAGMA database_list").fetchone()[2]
    now = datetime.now(UTC)
    sources = source_status(connection)
    if source_id and not any(source["id"] == source_id for source in sources):
        raise ValueError("没有找到启用的题源")
    queued = []
    for source in sources:
        if source_id and source["id"] != source_id:
            continue
        checked = datetime.fromisoformat(source["last_check_at"]) if source["last_check_at"] else None
        key = (location, source["id"])
        with _lock:
            existing = _jobs.get(key)
            if existing and not existing.done():
                queued.append(source["id"])
                continue
            if not force and checked and now - checked < timedelta(minutes=10):
                continue
            _jobs[key] = _pool.submit(_refresh, *key, change_note) if change_note else _pool.submit(_refresh, *key)
            queued.append(source["id"])
    return {"used_cache": True, "refreshing": queued, "sources": sources}


def _refresh(location, source_id, change_note=''):
    connection = connect_database(location)
    try:
        from app.services.wiki_alignment import align_source_tree
        align_source_tree(connection, source_id, analyze_alignment, change_note=change_note)
    except Exception:
        logging.getLogger(__name__).exception("Source refresh failed: %s", source_id)
    finally:
        connection.close()


def analyze_alignment(connection, source_id, result, client=None):
    """Every explicit alignment includes model analysis of all unresolved candidates."""
    count = connection.execute("SELECT COUNT(*) FROM parser_candidate WHERE source_id=? AND status='pending'", (source_id,)).fetchone()[0]
    analysis = {'status': 'running' if count else 'not_needed', 'total': count, 'processed': 0, 'suggestions': [], 'errors': []}
    result['analysis'] = analysis
    _save_summary(connection, result)
    for offset in range(0, count, 4):
        try:
            context = parsing_context(connection, source_id, 4, offset)
            context['user_change_description'] = result.get('change_note', '')
            key = f"{result['run_id']}:{offset}"
            try:
                reply = run_module_job(connection, 'source_parsing', source_id, key, client, context)
            except ModelJobError as error:
                if not any(marker in str(error) for marker in ('JSON', '边界建议', '遗漏了部分候选')):
                    raise
                # One bounded format retry; preserve both requests for inspection.
                reply = run_module_job(connection, 'source_parsing', source_id, key + ':format-retry', client, context)
            titles = {item['anchor_id']: item['title'] for item in context['candidates']}
            payload = parse_model_json(reply['response_text'])
            validate_suggestions(context, payload)
            items = payload['suggestions']
            analysis['suggestions'].extend({**item, 'title': titles[item['anchor_id']]} for item in items)
            analysis['processed'] += len(items)
        except Exception as error:
            logging.getLogger(__name__).exception("Source model analysis failed: %s", source_id)
            analysis['errors'].append(str(error))
            # A provider failure should not make dozens of repeated calls.
            break
        finally:
            _save_summary(connection, result)
    analysis['status'] = 'partial' if analysis['errors'] else 'complete' if count else 'not_needed'
    _save_summary(connection, result)
    return analysis


def _save_summary(connection, result):
    with transaction(connection):
        connection.execute("UPDATE sync_run SET summary_json=? WHERE id=?", (json.dumps(result, ensure_ascii=False), result['run_id']))
