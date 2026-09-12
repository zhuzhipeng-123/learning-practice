"""Track Wiki membership without deleting any learning facts."""

from urllib.parse import urlsplit

from app.storage.ids import new_id
from app.storage.transactions import atomic


@atomic
def reconcile_tree(connection, root, tree):
    previous = {row['node_token']: dict(row) for row in connection.execute(
        'SELECT * FROM source_tree_member WHERE root_source_id=?', (root['id'],))}
    changes = {'added': [], 'moved': [], 'missing': [], 'restored': [], 'unsupported': []}
    sources, present = {}, set()
    origin = urlsplit(root['wiki_url'])
    for node in tree['nodes']:
        token, path = node['node_token'], node['path']
        present.add(token)
        old = previous.get(token)
        source_id = None
        if node['obj_type'] == 'docx':
            known = connection.execute('SELECT id,question_type FROM source WHERE document_id=?', (node['obj_token'],)).fetchone()
            if known and known['question_type'] != root['question_type']:
                changes['unsupported'].append({'path': path, 'error': '同一文档已按另一题型登记，需确认归属'})
                continue
            source_id = known['id'] if known else new_id('source')
            url = f'{origin.scheme}://{origin.netloc}/wiki/{token}'
            connection.execute('INSERT OR IGNORE INTO source(id,document_id,wiki_url,question_type,identity) VALUES (?,?,?,?,?)',
                               (source_id, node['obj_token'], url, root['question_type'], root['identity']))
            connection.execute('UPDATE source SET enabled=1,wiki_url=? WHERE id=?', (url, source_id))
            sources.setdefault(source_id, path)
        else:
            changes['unsupported'].append({'path': path, 'error': f"暂不支持把 {node['obj_type']} 直接解析为题源"})
        connection.execute('INSERT INTO source_tree_member VALUES (?,?,?,?,?,?,?,0) ON CONFLICT(root_source_id,node_token) '
                           "DO UPDATE SET source_id=excluded.source_id,obj_type=excluded.obj_type,title=excluded.title,path=excluded.path,status='active',missing_count=0",
                           (root['id'], token, source_id, node['obj_type'], node['title'], path, 'active'))
        if old is None:
            changes['added'].append(path)
        elif old['status'] != 'active':
            changes['restored'].append(path)
        elif old['path'] != path:
            changes['moved'].append({'before': old['path'], 'after': path})
    if tree['complete']:
        for token, old in previous.items():
            if token in present:
                continue
            count = old['missing_count'] + 1
            connection.execute("UPDATE source_tree_member SET status='missing',missing_count=? WHERE root_source_id=? AND node_token=?",
                               (count, root['id'], token))
            changes['missing'].append({'path': old['path'], 'confirmed': count >= 2})
            elsewhere = connection.execute("SELECT 1 FROM source_tree_member WHERE source_id=? AND status='active'", (old['source_id'],)).fetchone()
            if old['source_id'] and old['source_id'] != root['id'] and not elsewhere:
                connection.execute('UPDATE source SET enabled=0 WHERE id=?', (old['source_id'],))
                connection.execute('UPDATE question SET source_status=? WHERE id IN (SELECT question_id FROM source_binding WHERE source_id=?)',
                                   ('source_deleted' if count >= 2 else 'missing_pending', old['source_id']))
    return sources, changes


def source_prefix(connection, source_id):
    row = connection.execute("SELECT path FROM source_tree_member WHERE source_id=? AND status='active' ORDER BY root_source_id,path LIMIT 1", (source_id,)).fetchone()
    return row[0] if row else ''
