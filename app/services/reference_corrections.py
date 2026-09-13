"""Cited corrections apply only to their reviewed source version, never to history."""

import json
from datetime import UTC, datetime
from urllib.parse import urlparse

from app.services.tasks import IdempotencyConflictError, _load_idempotent, _save_idempotent
from app.storage.ids import new_id
from app.storage.transactions import atomic


@atomic
def save_correction(connection, version_id, content, sources, request_key=None, expected_id=None):
    payload = {'version_id': version_id, 'content': content, 'sources': sources, 'expected_id': expected_id}
    if request_key:
        previous = _load_idempotent(connection, request_key, 'reference_correction', payload)
        if previous:
            return previous
    current = get_correction(connection, version_id)
    if expected_id is not None and expected_id != (current['id'] if current else ''):
        raise IdempotencyConflictError('校正已更新，请重新读取再保存；当前草稿仍保留')
    if (not isinstance(content, str) or not 10 <= len(content.strip()) <= 8000
            or not isinstance(sources, list) or not 1 <= len(sources) <= 10
            or any(not isinstance(url, str) or urlparse(url).scheme != 'https' or not urlparse(url).hostname for url in sources)):
        raise ValueError('校正需要明确内容和可核对的 HTTPS 来源')
    if not connection.execute('SELECT 1 FROM question_version WHERE id=?', (version_id,)).fetchone():
        raise ValueError('题目版本不存在')
    now, correction_id = datetime.now(UTC).isoformat(), new_id('correction')
    connection.execute('INSERT INTO reference_correction_history VALUES (?,?,?,?,?)',
                       (correction_id, version_id, content.strip(), json.dumps(sources), now))
    connection.execute('INSERT INTO reference_correction VALUES (?,?,?,?) ON CONFLICT(version_id) DO UPDATE SET '
        'content=excluded.content,sources_json=excluded.sources_json,created_at=excluded.created_at',
        (version_id, content.strip(), json.dumps(sources), now))
    result = get_correction(connection, version_id)
    if request_key:
        _save_idempotent(connection, request_key, 'reference_correction', payload, result, now)
    return result


def get_correction(connection, version_id):
    row = connection.execute('SELECT * FROM reference_correction WHERE version_id=?', (version_id,)).fetchone()
    if row:
        record = connection.execute('SELECT id FROM reference_correction_history WHERE version_id=? ORDER BY rowid DESC LIMIT 1', (version_id,)).fetchone()
        return {'id': record[0] if record else 'legacy:' + version_id, 'version_id': row['version_id'], 'content': row['content'], 'sources': json.loads(row['sources_json']),
                'reviewed_at': row['created_at']}
    return None


def attach_corrections(connection, questions):
    return [{**dict(q), 'reference_correction': get_correction(connection, q['version_id'])} for q in questions]
