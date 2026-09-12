"""Enumerate all descendants, explicitly distinguishing a partial traversal."""

from collections import deque

from app.adapters.lark_cli import LarkCliError
from app.services.sync import SyncIntegrityError


def read_wiki_tree(client, url, identity):
    root = client.run_read(['wiki', '+node-get', '--node-token', url, '--as', identity]).data
    root = root.get('node', root)
    _validate_node(root)
    nodes, errors, seen = [], [], set()
    queue = deque([(root, root['title'], 0)])
    while queue:
        node, path, depth = queue.popleft()
        token = node['node_token']
        if token in seen:
            errors.append({'path': path, 'error': '目录出现重复节点或循环，未认定完整读取'})
            continue
        seen.add(token)
        nodes.append({**node, 'path': path})
        if len(nodes) > 5000 or depth > 60:
            raise SyncIntegrityError('知识库超过 5000 节点或 60 层，本轮未完整读取')
        if not node['has_child']:
            continue
        try:
            children = _children(client, node, identity)
            queue.extend((child, path + ' > ' + child['title'], depth + 1) for child in children)
        except (LarkCliError, SyncIntegrityError, KeyError, TypeError) as error:
            errors.append({'path': path, 'error': str(error)})
    return {'nodes': nodes, 'errors': errors, 'complete': not errors}


def _validate_node(node):
    if (not isinstance(node, dict) or any(not isinstance(node.get(key), str) or not node[key]
        for key in ('node_token', 'space_id', 'obj_token', 'obj_type')) or not isinstance(node.get('title'), str)
        or type(node.get('has_child')) is not bool):
        raise SyncIntegrityError('目录节点缺少身份、类型或子目录标记')


def _children(client, node, identity):
    result, seen_cursors, cursor = [], set(), None
    for _ in range(100):
        args = ['wiki', '+node-list', '--space-id', node['space_id'], '--parent-node-token', node['node_token'],
                '--page-size', '50', '--as', identity]
        if cursor:
            args += ['--page-token', cursor]
        data = client.run_read(args).data
        if not isinstance(data, dict) or not isinstance(data.get('nodes'), list) or type(data.get('has_more')) is not bool:
            raise SyncIntegrityError('目录分页缺少 nodes / has_more，未认定完整读取')
        for child in data['nodes']:
            _validate_node(child)
            if child.get('parent_node_token') != node['node_token']:
                raise SyncIntegrityError('目录返回了不属于当前父节点的内容')
            result.append(child)
        if not data['has_more']:
            return result
        cursor = data.get('page_token')
        if not cursor or cursor in seen_cursors:
            raise SyncIntegrityError('目录分页游标缺失或重复')
        seen_cursors.add(cursor)
    raise SyncIntegrityError('目录超过 100 页，本轮未完整读取')
