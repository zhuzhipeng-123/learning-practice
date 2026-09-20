"""Explicit edits create versions; existing tasks and answers keep their basis."""

import hashlib
import json
from datetime import UTC, datetime

from app.services.reference_corrections import get_correction
from app.services.reference_state import save_verification, verification
from app.services.review import enter_review, get_active_round
from app.services.tasks import IdempotencyConflictError, _load_idempotent, _save_idempotent
from app.storage.ids import new_id
from app.storage.transactions import atomic


@atomic
def read_knowledge(connection, question_id, version_id=None):
    from app.services.materials import materials_for_role
    row = connection.execute('SELECT v.*,q.source_kind FROM question q JOIN question_version v '
        'ON v.id=COALESCE(?,q.current_version_id) AND v.question_id=q.id WHERE q.id=?',
        (version_id, question_id)).fetchone()
    if row is None:
        raise ValueError('题目版本不存在')
    connection.execute('INSERT OR IGNORE INTO question_exposure VALUES (?,?)',
                       (question_id, datetime.now(UTC).isoformat()))
    resource = connection.execute('SELECT materials_json FROM version_resources WHERE version_id=?',
                                  (row['id'],)).fetchone()
    materials = json.loads(resource[0]) if resource else []
    return {'question_id': question_id, 'version_id': row['id'], 'prompt': row['prompt'],
            'reference_text': row['reference_text'] or '', 'category_path': row['category_path'],
            'editable': row['source_kind'] == 'derived', 'reference_verification': verification(connection, row['id']),
            'reference_correction': get_correction(connection, row['id']),
            'prompt_materials': materials_for_role(materials, 'prompt'),
            'reference_materials': materials_for_role(materials, 'reference')}


@atomic
def edit_knowledge(connection, question_id, expected_version_id, prompt, reference_text,
                   category_path, reference_verified, request_key):
    payload = {'question_id': question_id, 'expected_version_id': expected_version_id, 'prompt': prompt,
                   'reference_text': reference_text, 'category_path': category_path, 'reference_verified': reference_verified}
    previous = _load_idempotent(connection, request_key, 'edit_knowledge', payload)
    if previous:
        return previous
    row = connection.execute('SELECT v.*,q.source_kind FROM question q JOIN question_version v '
                             'ON v.id=q.current_version_id WHERE q.id=?', (question_id,)).fetchone()
    if not row or row['source_kind'] != 'derived':
        raise ValueError('原题请在飞书修改；本地可以保存附来源的参考校正')
    if row['id'] != expected_version_id:
        raise IdempotencyConflictError('题目已更新，请重新读取后再编辑；当前草稿仍保留')
    if (not 1 <= len(prompt.strip()) <= 4000 or not 1 <= len(reference_text.strip()) <= 20000
            or len(category_path) > 1000):
        raise ValueError('请填写完整题干和参考答案，并遵守字数限制')
    same_content = prompt.strip() == row['prompt'] and reference_text.strip() == row['reference_text']
    state = verification(connection, row['id'])
    now = datetime.now(UTC).isoformat()
    version_id, basis = row['id'], row['review_basis_id']
    if not (same_content and category_path.strip() == row['category_path'] and reference_verified == state['verified']):
        version_id = new_id('version')
        if not same_content:
            basis = new_id('basis')
            connection.execute('INSERT INTO review_basis VALUES (?,?,?,?)', (basis, question_id, version_id, now))
        digest = hashlib.sha256(json.dumps([payload, version_id], sort_keys=True).encode()).hexdigest()
        connection.execute("INSERT INTO question_version VALUES (?,?,NULL,?,?,?,?,'text_complete',?,'用户修改题干、答案或核验状态',?)",
            (version_id, question_id, basis, prompt.strip(), reference_text.strip(), category_path.strip(), digest, now))
        save_verification(connection, version_id, reference_verified, now)
        connection.execute("INSERT INTO version_resources VALUES (?,?,'[]')", (version_id, json.dumps(['reference:' + version_id])))
        connection.execute('UPDATE question SET current_version_id=? WHERE id=?', (version_id, question_id))
        if get_active_round(connection, question_id):
            enter_review(connection, question_id, basis, 'explicit_knowledge_edit', datetime.fromisoformat(now))
    connection.execute('INSERT OR IGNORE INTO question_exposure VALUES (?,?)', (question_id, now))
    result = {'question_id': question_id, 'version_id': version_id, 'reference_verified': reference_verified}
    _save_idempotent(connection, request_key, 'edit_knowledge', payload, result, now)
    return result
