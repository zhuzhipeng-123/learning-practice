import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.parsers.docx import parse_docx_blocks
from app.services.current_practice import draw_daily_batch
from app.services.learning_clock import local_today
from app.services.plan_editing import update_daily_plan
from app.services.sync import publish_snapshot
from app.services.tasks import create_daily_plan, fill_daily_plan
from app.services.theory_scope import (
    ROOT_ANCHOR,
    document_scope_ids,
    module_id,
    resolve_scope,
    theory_catalog,
)
from app.storage.database import connect_database, initialize_database
from tests.helpers import add_source
from tests.test_docx_parser import heading, text_block
from tests.test_student_workflow import pool

DAY = date(2026, 9, 20)


def publish_tree(database, source_id, revision='1', root='同名模块', include_b=True, moved=False):
    if not database.execute('SELECT 1 FROM source WHERE id=?', (source_id,)).fetchone():
        add_source(database, source_id)
        database.execute("UPDATE source SET question_type='theory' WHERE id=?", (source_id,))
    blocks = [heading('root', 1, root)]
    if moved:
        blocks += [heading('parent', 1, '新父级')]
    blocks += [heading('a', 2, '子模块 A'), heading('qa', 3, '问题 A'), text_block('aa', '回答 A')]
    if include_b:
        blocks += [heading('b', 2, '子模块 B'), heading('qb', 3, '问题 B'), text_block('ab', '回答 B')]
    parsed = parse_docx_blocks(source_id, 'document-' + source_id, 'theory', blocks)
    publish_snapshot(database, source_id, revision, revision, blocks, parsed.published, 'scope-test')
    return theory_catalog(database)


def task_sources(database, task_ids):
    if not task_ids:
        return []
    marks = ','.join('?' for _ in task_ids)
    return [row[0] for row in database.execute(
        'SELECT b.source_id FROM task t JOIN source_binding b ON b.question_id=t.question_id '
        f'WHERE t.id IN ({marks}) ORDER BY b.source_id', task_ids)]


def test_legacy_heading_scope_collapses_to_its_document(database):
    catalog = publish_tree(database, 'theory')

    selected = document_scope_ids(catalog['nodes'], [module_id('theory', 'a')])

    assert selected == [module_id('theory', ROOT_ANCHOR)]


def test_today_scope_renders_documents_without_internal_headings(monkeypatch):
    monkeypatch.setattr('app.services.bootstrap.load_initial_sources', list)
    with TestClient(app) as client:
        database = connect_database(app.state.database_path)
        publish_tree(database, 'theory-one')
        publish_tree(database, 'theory-two')
        database.commit()

        response = client.get('/')

        assert response.status_code == 200
        assert response.text.count('class="module-scope"') == 2
        assert 'class="document-picker"' in response.text
        assert '子模块 A' not in response.text
        database.close()


def test_scope_filters_before_sampling_and_distinguishes_same_named_sources(database, monkeypatch):
    first = publish_tree(database, 'theory-one')
    catalog = publish_tree(database, 'theory-two')
    selected = [module_id('theory-one', ROOT_ANCHOR)]
    monkeypatch.setattr('app.services.current_practice.local_today', lambda: DAY)

    batch = draw_daily_batch(database, DAY, 0, 2, {}, 'scoped', '', selected, catalog['version'])

    assert len(batch['task_ids']) == 2
    assert task_sources(database, batch['task_ids']) == ['theory-one', 'theory-one']
    assert batch['shortages'] == {}
    assert next(node for node in first['nodes'] if node['anchor'] == 'root')['label'] == '同名模块'
    assert len([node for node in catalog['nodes'] if node['anchor'] == 'root' and node['label'] == '同名模块']) == 2
    assert module_id('theory-one', 'root') != module_id('theory-two', 'root')


def test_scope_validation_covers_empty_overlap_and_excluded_quota(database):
    catalog = publish_tree(database, 'theory')
    publish_tree(database, 'other-theory')
    catalog = theory_catalog(database)
    root = module_id('theory', ROOT_ANCHOR)
    heading_root = module_id('theory', 'root')
    child = module_id('theory', 'a')
    with pytest.raises(ValueError, match='至少选择'):
        create_daily_plan(database, DAY, 0, 1, {}, 'empty', theory_scope=[],
                          theory_catalog_version=catalog['version'])
    assert create_daily_plan(database, DAY, 0, 0, {}, 'zero', theory_scope=[],
                             theory_catalog_version=catalog['version'])['tasks'] == []
    database.execute('DELETE FROM daily_plan')
    with pytest.raises(ValueError, match='exceed'):
        create_daily_plan(database, DAY, 0, 1, {child: 2}, 'over', theory_scope=[root],
                          theory_catalog_version=catalog['version'])
    with pytest.raises(ValueError, match='overlap'):
        create_daily_plan(database, DAY, 0, 2, {heading_root: 1, child: 1}, 'overlap', theory_scope=[root],
                          theory_catalog_version=catalog['version'])
    with pytest.raises(ValueError, match='排除范围'):
        create_daily_plan(database, DAY, 0, 1, {module_id('other-theory', 'b'): 1}, 'excluded',
                          theory_scope=[root], theory_catalog_version=catalog['version'])


def test_anchor_identity_survives_rename_move_and_partial_tree(database):
    catalog = publish_tree(database, 'theory')
    document = module_id('theory', ROOT_ANCHOR)
    frozen = resolve_scope(database, [module_id('theory', 'a')], catalog['version'], 1)
    assert frozen['selected_ids'] == [document]
    publish_tree(database, 'theory', '2', root='改名模块', moved=True)

    resolved = resolve_scope(database, None, None, 1, frozen)
    assert resolved['selected_ids'] == [document]

    publish_tree(database, 'theory', '3', root='改名模块', include_b=True)
    snapshot = database.execute('SELECT snapshot_id FROM source_sync_state WHERE source_id=?', ('theory',)).fetchone()[0]
    blocks = [heading('root', 1, '改名模块'), heading('b', 2, '子模块 B'),
              heading('qb', 3, '问题 B'), text_block('ab', '回答 B')]
    database.execute('UPDATE source_snapshot SET blocks_json=?,complete=0 WHERE id=?',
                     (json.dumps(blocks, ensure_ascii=False), snapshot))
    database.execute("UPDATE source SET last_error='partial tree' WHERE id='theory'")
    partial = resolve_scope(database, None, None, 1, frozen)
    assert partial['selected_ids'] == [document]

    database.execute('UPDATE source_snapshot SET complete=1 WHERE id=?', (snapshot,))
    database.execute("UPDATE source SET last_error=NULL WHERE id='theory'")
    assert resolve_scope(database, None, None, 1, frozen)['selected_ids'] == [document]


def test_old_update_and_fill_paths_preserve_daily_scope(database):
    catalog = publish_tree(database, 'theory')
    selected = module_id('theory', ROOT_ANCHOR)
    plan = create_daily_plan(database, DAY, 0, 2, {}, 'plan', theory_scope=[selected],
                             theory_catalog_version=catalog['version'])
    assert len(plan['tasks']) == 2
    assert fill_daily_plan(database, plan['plan_id'])['filled'] == 0
    updated = update_daily_plan(database, plan['plan_id'], 0, 2, {})
    assert len(updated['tasks']) == 2
    stored = json.loads(database.execute('SELECT theory_scope_json FROM daily_plan').fetchone()[0])
    assert stored['selected_ids'] == [selected]


def test_daily_theory_scope_does_not_filter_code_pool(database, monkeypatch):
    catalog = publish_tree(database, 'theory')
    pool(database, 2, 'code')
    monkeypatch.setattr('app.services.current_practice.local_today', lambda: DAY)
    batch = draw_daily_batch(database, DAY, 2, 0, {}, 'code-only', '', [], catalog['version'])
    kinds = database.execute(
        "SELECT DISTINCT q.question_type FROM task t JOIN question q ON q.id=t.question_id"
    ).fetchall()
    assert len(batch['task_ids']) == 2 and [row[0] for row in kinds] == ['code']


def test_v14_migration_adds_scope_and_model_execution_with_backup(tmp_path):
    path = tmp_path / 'v14.db'
    database = connect_database(path)
    initialize_database(database)
    database.execute('DROP TABLE model_job_execution')
    database.execute('ALTER TABLE daily_plan DROP COLUMN theory_scope_json')
    database.execute('DELETE FROM schema_version WHERE version>=15')
    database.commit()

    initialize_database(database)

    assert database.execute('SELECT MAX(version) FROM schema_version').fetchone()[0] == 16
    assert 'theory_scope_json' in {row['name'] for row in database.execute('PRAGMA table_info(daily_plan)')}
    assert database.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_job_execution'"
    ).fetchone()
    assert len(list(tmp_path.glob('pre-migration-v14-*.db'))) == 1
    database.close()


def test_daily_api_freezes_scope_and_fill_cannot_expand_it(monkeypatch):
    monkeypatch.setattr('app.services.bootstrap.load_initial_sources', list)
    with TestClient(app) as client:
        database = connect_database(app.state.database_path)
        catalog = publish_tree(database, 'api-theory')
        database.commit()
        selected = module_id('api-theory', ROOT_ANCHOR)
        body = {'plan_date': local_today().isoformat(), 'code_target': 0, 'theory_target': 2,
                'module_quotas': {}, 'theory_scope': [selected],
                'theory_catalog_version': catalog['version'], 'fresh_batch': True,
                'expected_batch': ''}
        headers = {'Idempotency-Key': 'api-scope', 'X-Requested-With': 'learning-practice'}
        response = client.post('/api/plans', headers=headers, json=body)
        assert response.status_code == 200
        result = response.json()
        assert len(result['task_ids']) == 2
        assert client.post(f"/api/plans/{result['plan_id']}/fill",
                           headers={'X-Requested-With': 'learning-practice'}).json()['filled'] == 0
        stored = json.loads(database.execute('SELECT theory_scope_json FROM daily_plan').fetchone()[0])
        assert stored['selected_ids'] == [selected]
        database.close()
