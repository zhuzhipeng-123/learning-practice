"""Source-only changes; learning records are deliberately outside this comparison."""

import json

from app.parsers.block_tree import ordered_blocks
from app.parsers.docx import block_text, heading_level

CHANGE_LIST_KEYS = (
    'added', 'updated', 'removed', 'missing', 'restored', 'excluded',
    'modules_added', 'modules_removed', 'modules_updated',
)


def empty_inventory_changes():
    return {name: [] for name in CHANGE_LIST_KEYS}


def source_inventory(connection, source_id):
    questions = {row['id']: dict(row) for row in connection.execute(
        "SELECT q.id,q.current_version_id,q.source_status,v.prompt,v.reference_text,v.category_path,v.material_status "
        "FROM question q JOIN question_version v ON v.id=q.current_version_id "
        "WHERE EXISTS(SELECT 1 FROM source_binding b WHERE b.question_id=q.id AND b.source_id=? AND b.confirmation_status!='migrated')", (source_id,))}
    snapshot = connection.execute("SELECT ss.blocks_json FROM source_sync_state st JOIN source_snapshot ss "
                                  "ON ss.id=st.snapshot_id WHERE st.source_id=?", (source_id,)).fetchone()
    modules, path = {}, {}
    blocks = ordered_blocks(json.loads(snapshot[0])) if snapshot else []
    for block in blocks:
        level = heading_level(block)
        if level:
            path = {key: value for key, value in path.items() if key < level}
            path[level] = block_text(block)
            modules[block['block_id']] = ' > '.join(path.values())
    return {'questions': questions, 'modules': modules}


def inventory_changes(before, after):
    result = empty_inventory_changes()
    unchanged = 0
    for key, item in after['questions'].items():
        old = before['questions'].get(key)
        detail = {'id': key, 'title': item['prompt'], 'module': item['category_path']}
        if old is None:
            result['added'].append(detail)
            continue
        changed = False
        if item['current_version_id'] != old['current_version_id']:
            fields = [label for key, label in (('prompt', '题干'), ('reference_text', '参考内容'), ('category_path', '模块归属'), ('material_status', '素材状态')) if item[key] != old[key]]
            result['updated'].append({**detail, 'previous_title': old['prompt'], 'previous_module': old['category_path'],
                                      'changed_fields': fields or ['图片 / 表格或引用边界']})
            changed = True
        if item['source_status'] != old['source_status']:
            kind = {'source_deleted': 'removed', 'missing_pending': 'missing', 'active': 'restored', 'excluded_by_rule': 'excluded'}.get(item['source_status'])
            if kind:
                result[kind].append(detail)
                changed = True
        unchanged += not changed
    for key, path in after['modules'].items():
        old = before['modules'].get(key)
        if old is None:
            result['modules_added'].append(path)
        elif old != path:
            result['modules_updated'].append({'before': old, 'after': path})
    result['modules_removed'] = [path for key, path in before['modules'].items() if key not in after['modules']]
    return {**result, 'unchanged': unchanged}
