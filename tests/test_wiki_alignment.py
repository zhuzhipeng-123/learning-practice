from dataclasses import replace
from types import SimpleNamespace

from app.adapters.wiki_reader import read_wiki_tree
from app.services.source_state import observe_missing_questions
from app.services.sync import publish_snapshot
from app.services.wiki_alignment import _aggregate, align_source_tree
from app.services.wiki_sources import reconcile_tree, source_prefix
from tests.helpers import add_source
from tests.test_docx_parser import heading, text_block
from tests.test_docx_reader import FakeClient
from tests.test_sync import blocks, draft


def node(token, parent='', child=False, title=None, kind='docx'):
    return {'node_token': token, 'parent_node_token': parent, 'space_id': 'space',
            'obj_token': f'doc-{token}', 'obj_type': kind, 'title': title or token, 'has_child': child}


class Wiki:
    def run_read(self, args):
        if args[1] == '+node-get':
            return SimpleNamespace(data=node('root', child=True))
        parent = args[args.index('--parent-node-token')+1]
        if parent == 'root':
            if '--page-token' not in args:
                return SimpleNamespace(data={'nodes': [node('folder', 'root', True)], 'has_more': True, 'page_token': 'next'})
            return SimpleNamespace(data={'nodes': [node('sibling', 'root')], 'has_more': False})
        return SimpleNamespace(data={'nodes': [node('nested', 'folder')], 'has_more': False})


def test_recursive_tree_reads_every_page_and_depth():
    tree = read_wiki_tree(Wiki(), 'https://example.feishu.cn/wiki/root', 'bot')
    assert tree['complete']
    assert {item['path'] for item in tree['nodes']} == {'root', 'root > folder', 'root > sibling', 'root > folder > nested'}


def test_partial_tree_reports_branch_and_cannot_prove_deletion(database):
    add_source(database)
    root = dict(database.execute('SELECT * FROM source').fetchone())
    root['wiki_url'] = 'https://example.feishu.cn/wiki/root'
    tree = read_wiki_tree(Wiki(), root['wiki_url'], 'bot')
    sources, _ = reconcile_tree(database, root, tree)
    nested = next(key for key, path in sources.items() if path.endswith('nested'))
    publish_snapshot(database, nested, '1', '1', blocks(), [replace(draft(), source_id=nested)], 'v1')
    partial = {'complete': False, 'nodes': [tree['nodes'][0]], 'errors': [{'path': 'root', 'error': 'denied'}]}
    reconcile_tree(database, root, partial)
    assert database.execute('SELECT enabled FROM source WHERE id=?', (nested,)).fetchone()[0] == 1
    assert database.execute('SELECT source_status FROM question').fetchone()[0] == 'active'
    partial['complete'] = True
    _, missing = reconcile_tree(database, root, partial)
    assert len(missing['missing']) == 3
    assert database.execute('SELECT source_status FROM question').fetchone()[0] == 'missing_pending'
    reconcile_tree(database, root, partial)
    assert database.execute('SELECT source_status FROM question').fetchone()[0] == 'source_deleted'
    reconcile_tree(database, root, tree)
    observe_missing_questions(database, nested, {draft().main_anchor_block_id}, True, False, True)
    assert database.execute('SELECT source_status FROM question').fetchone()[0] == 'active'


def test_document_move_keeps_source_identity_and_full_prefix(database):
    add_source(database)
    root = dict(database.execute('SELECT * FROM source').fetchone())
    tree = read_wiki_tree(Wiki(), root['wiki_url'], 'bot')
    sources, _ = reconcile_tree(database, root, tree)
    nested = next(key for key, path in sources.items() if path.endswith('nested'))
    for item in tree['nodes']:
        if item['node_token'] == 'nested':
            item['path'] = 'root > New folder > New name'
    updated, changes = reconcile_tree(database, root, tree)
    assert nested in updated
    assert len(changes['moved']) == 1 and changes['added'] == []
    assert source_prefix(database, nested) == 'root > New folder > New name'


def test_broken_pagination_and_permission_are_explicit():
    class Broken(Wiki):
        def run_read(self, args):
            if args[1] == '+node-get':
                return super().run_read(args)
            return SimpleNamespace(data={'nodes': [], 'has_more': True, 'page_token': 'same'})
    tree = read_wiki_tree(Broken(), 'https://example.feishu.cn/wiki/root', 'bot')
    assert not tree['complete'] and tree['errors'][0]['path'] == 'root'


def test_unsupported_child_is_reported_not_silently_skipped(database):
    add_source(database)
    root = dict(database.execute('SELECT * FROM source').fetchone())
    child = {**node('sheet', kind='sheet'), 'path': 'root > sheet'}
    sources, report = reconcile_tree(database, root, {'nodes': [child], 'complete': True})
    assert not sources and report['unsupported'][0]['path'] == 'root > sheet'


def test_aggregate_supports_empty_and_nonempty_excluded_changes():
    summary = {'documents': [
        {'result': {'candidate_count': 0, 'changes': {'excluded': []}}},
        {'result': {'candidate_count': 0, 'changes': {'excluded': [{'id': 'question'}]}}},
    ]}

    _aggregate(summary)

    assert summary['changes']['excluded'] == [{'id': 'question'}]
    assert summary['changes']['added'] == []


def test_alignment_combines_real_document_sync_and_aggregate(database):
    add_source(database)
    database.execute("UPDATE source SET question_type='theory' WHERE id='source-code'")
    database.commit()
    raw = [heading('module', 2, 'Agent'), heading('question', 3, 'Why use tools?'),
           text_block('answer', 'Tools let a model act on external systems.')]
    client = FakeClient([
        {'document': {'revision_id': 1}},
        {'items': raw, 'has_more': False},
        {'document': {'revision_id': 1}},
    ])

    summary = align_source_tree(database, 'source-code', _no_analysis, client)

    assert summary['phase'] == 'finished'
    assert summary['documents'][0]['status'] == 'complete'
    assert summary['changes']['excluded'] == []
    assert database.execute('SELECT COUNT(*) FROM question').fetchone()[0] == 1


def test_aggregate_failure_preserves_saved_document_and_reports_stage(database, monkeypatch):
    add_source(database)
    database.execute("UPDATE source SET question_type='theory' WHERE id='source-code'")
    database.commit()
    raw = [heading('module', 2, 'Agent'), heading('question', 3, 'Why use tools?'),
           text_block('answer', 'Tools let a model act on external systems.')]
    client = FakeClient([
        {'document': {'revision_id': 1}},
        {'items': raw, 'has_more': False},
        {'document': {'revision_id': 1}},
    ])
    monkeypatch.setattr('app.services.source_sync.inventory_changes', lambda before, after: {'excluded': 1})

    summary = align_source_tree(database, 'source-code', _no_analysis, client)

    assert summary['phase'] == 'failed'
    assert summary['failure_stage'] == 'aggregation'
    assert summary['tree_errors'] == []
    assert summary['stage_errors'][0]['stage'] == 'aggregation'
    assert summary['documents'][0]['status'] == 'complete'
    assert database.execute('SELECT COUNT(*) FROM question').fetchone()[0] == 1
    assert database.execute('SELECT status FROM alignment_run').fetchone()[0] == 'failed'


def test_mixed_documents_and_partial_tree_keep_safe_results_separate(database, monkeypatch):
    add_source(database)
    add_source(database, 'source-child')
    database.execute("UPDATE source SET wiki_url='https://example.test/wiki/root' WHERE id='source-code'")
    database.commit()
    tree = {'complete': False, 'nodes': [], 'errors': [{'path': 'root > denied', 'error': 'denied'}]}
    monkeypatch.setattr('app.services.wiki_alignment.read_wiki_tree', lambda *args: tree)
    monkeypatch.setattr('app.services.wiki_alignment.reconcile_tree', lambda *args: (
        {'source-code': 'root', 'source-child': 'root > child'},
        {'added': [], 'moved': [], 'missing': [], 'restored': [], 'unsupported': []},
    ))

    def align_document(location, item, analyze, remote, model_unavailable):
        if item['source_id'] == 'source-child':
            item.update(status='failed', error='synthetic document failure')
            return
        item.update(status='partial', result={
            'candidate_count': 1,
            'changes': {'added': [{'id': 'safe-question'}]},
            'analysis': {'status': 'partial', 'total': 1, 'processed': 0,
                         'suggestions': [], 'errors': ['synthetic model failure']},
        })

    monkeypatch.setattr('app.services.wiki_alignment._align_document', align_document)
    summary = align_source_tree(database, 'source-code', _no_analysis, SimpleNamespace())

    assert summary['tree_errors'] == tree['errors']
    assert summary['changes']['added'] == [{'id': 'safe-question'}]
    assert summary['analysis']['errors'] == ['synthetic model failure']
    assert next(item for item in summary['documents'] if item['source_id'] == 'source-child')['status'] == 'failed'
    assert database.execute('SELECT status FROM alignment_run').fetchone()[0] == 'partial'


def _no_analysis(connection, source_id, result):
    return {'status': 'not_needed', 'total': 0, 'processed': 0, 'suggestions': [], 'errors': []}
