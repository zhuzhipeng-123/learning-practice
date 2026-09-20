import hashlib
import json
import random
import sqlite3
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.parsers.docx import parse_docx_blocks
from app.services.exports import ExportError, export_learning_data, verify_export
from app.services.interview import start_session
from app.services.local_reparse import _archived_materials
from app.services.materials import (
    apply_materials,
    collect_materials,
    material_closure,
    normalize_material,
)
from app.services.sync import publish_snapshot
from app.services.tasks import _select_questions
from app.storage.database import connect_database
from tests.helpers import add_source
from tests.test_docx_parser import heading, text_block
from tests.test_practice_review import seed_question


class TableMedia:
    def download_media(self, token, output, identity):
        output.write_bytes(b'\x89PNG\r\n\x1a\ncell-image')


class BrokenTableMedia:
    def download_media(self, token, output, identity):
        output.write_bytes(b'not-an-image')


class TableSheet:
    def run_read(self, args):
        if args[1] == '+workbook-info':
            return SimpleNamespace(data={
                'sheets': [{'sheet_id': 'sheet', 'row_count': 2, 'column_count': 2}],
            })
        return SimpleNamespace(data={
            'has_more': False,
            'annotated_csv': 'name,value\nlatency,42',
        })


def native_table_blocks(unsupported=False):
    tail = [{'block_id': 'gap', 'block_type': 999}] if unsupported else []
    return [
        heading('module', 1, 'Data'), heading('q', 3, 'How is the matrix interpreted?'),
        {'block_id': 'table', 'block_type': 31, 'children': ['c1', 'c2', 'c3', 'c4'],
         'table': {'property': {'row_size': 2, 'column_size': 2,
                                'merge_info': [{'row': 0, 'column': 0, 'row_span': 1, 'column_span': 2}]}}},
        {'block_id': 'c1', 'block_type': 32, 'children': ['t1']}, text_block('t1', 'name'),
        {'block_id': 'c2', 'block_type': 32, 'children': ['t2']}, text_block('t2', 'value'),
        {'block_id': 'c3', 'block_type': 32, 'children': ['t3']}, text_block('t3', 'a,b'),
        {'block_id': 'c4', 'block_type': 32, 'children': ['image', *[item['block_id'] for item in tail]]},
        {'block_id': 'image', 'block_type': 27, 'image': {'token': 'cell-image'}}, *tail,
    ]


def test_native_table_preserves_order_merge_and_nested_attachment(database):
    blocks = native_table_blocks()
    parsed = parse_docx_blocks('source', 'doc', 'theory', blocks, '八股')
    assert parsed.published[0].material_status == 'media_required'

    candidates = [*parsed.published, *parsed.candidates]
    materials = collect_materials(database, {'identity': 'bot'}, blocks, candidates, TableMedia())
    draft = apply_materials(candidates[0], materials, blocks)
    table = next(item for item in draft.materials if item['kind'] == 'table')

    assert draft.material_status == 'complete'
    assert table['structure']['rows'] == table['structure']['columns'] == 2
    assert [cell['content'][0].get('text') for cell in table['structure']['cells'][:3]] == ['name', 'value', 'a,b']
    assert table['structure']['merge_info'][0]['column_span'] == 2
    assert table['structure']['cells'][3]['content'] == [{'kind': 'media', 'block_id': 'image'}]
    assert table['attachments'][0]['status'] == 'complete'
    assert '[结构化表格]' in draft.reference_text and '"columns": 2' in draft.reference_text
    ordered = next(item for item in draft.materials
                   if item['kind'] == 'ordered_content' and item['role'] == 'reference')
    assert [node['kind'] for node in ordered['nodes']] == ['table']
    nested = next(item for item in material_closure(draft.materials) if item.get('block_id') == 'image')
    assert nested['role'] == 'reference'


def test_unsupported_native_table_cell_stays_incomplete(database):
    blocks = native_table_blocks(unsupported=True)
    parsed = parse_docx_blocks('source', 'doc', 'theory', blocks, '八股')
    candidates = [*parsed.published, *parsed.candidates]
    materials = collect_materials(database, {'identity': 'bot'}, blocks, candidates, TableMedia())
    draft = apply_materials(candidates[0], materials, blocks)

    assert draft.material_status == 'incomplete_reference'
    table = next(item for item in draft.materials if item['kind'] == 'table')
    assert table['status'] == 'incomplete' and '暂不支持' in table['error']


def test_pending_nested_table_attachment_keeps_version_incomplete(database):
    blocks = native_table_blocks()
    parsed = parse_docx_blocks('source', 'doc', 'theory', blocks, '八股')
    draft = parsed.published[0]
    materials = collect_materials(database, {'identity': 'bot'}, blocks, [draft], BrokenTableMedia())

    archived = apply_materials(draft, materials, blocks)

    table = next(item for item in archived.materials if item['kind'] == 'table')
    assert archived.material_status == 'media_required'
    assert table['status'] == 'incomplete'
    assert '单元格附件 image 未完整归档' in table['error']
    add_source(database, 'source')
    database.execute("UPDATE source SET question_type='theory' WHERE id='source'")
    publish_snapshot(database, 'source', '1', '1', blocks, [archived], 'ordered-v1')

    selected, shortages = _select_questions(database, 0, 1, {}, random.Random(0))

    assert selected == []
    assert shortages == {'unallocated': 1}


def test_native_table_code_cell_preserves_language_and_indentation(database):
    blocks = [
        heading('module', 1, 'Data'), heading('q', 3, 'Show the code'),
        {'block_id': 'table', 'block_type': 31, 'children': ['cell'],
         'table': {'property': {'row_size': 1, 'column_size': 1}}},
        {'block_id': 'cell', 'block_type': 32, 'children': ['code']},
        {'block_id': 'code', 'block_type': 14, 'code': {'style': {'language': 'Python'},
         'elements': [{'text_run': {'content': '  value = 1\n'}}]}},
    ]
    draft = parse_docx_blocks('source', 'doc', 'theory', blocks, '八股').published[0]

    archived = apply_materials(
        draft, collect_materials(database, {'identity': 'bot'}, blocks, [draft], TableMedia()), blocks,
    )

    table = next(item for item in archived.materials if item['kind'] == 'table')
    code = table['structure']['cells'][0]['content'][0]
    assert code == {'kind': 'code', 'block_id': 'code', 'text': '  value = 1', 'language': 'Python'}


def test_native_table_cell_preserves_nested_sheet(database):
    blocks = [
        heading('module', 1, 'Data'), heading('q', 3, 'Read the nested sheet'),
        {'block_id': 'table', 'block_type': 31, 'children': ['cell'],
         'table': {'property': {'row_size': 1, 'column_size': 1}}},
        {'block_id': 'cell', 'block_type': 32, 'children': ['sheet-block']},
        {'block_id': 'sheet-block', 'block_type': 30, 'sheet': {'token': 'book_sheet'}},
    ]
    parsed = parse_docx_blocks('source', 'doc', 'theory', blocks, '八股')
    draft = [*parsed.published, *parsed.candidates][0]

    archived = apply_materials(
        draft, collect_materials(database, {'identity': 'bot'}, blocks, [draft], TableSheet()), blocks,
    )

    table = next(item for item in archived.materials if item['kind'] == 'table')
    nested = table['attachments'][0]
    assert archived.material_status == 'complete'
    assert table['structure']['cells'][0]['content'] == [
        {'kind': 'sheet', 'block_id': 'sheet-block'},
    ]
    assert nested['kind'] == 'sheet'
    assert nested['structure']['cells'] == [['name', 'value'], ['latency', '42']]
    assert next(item for item in material_closure(archived.materials)
                if item.get('block_id') == 'sheet-block')['role'] == 'reference'


def test_ordered_content_keeps_text_media_order_and_code_metadata(database):
    blocks = [
        heading('module', 1, 'Data'), heading('q', 3, 'How is this evaluated?'),
        text_block('before', 'before'),
        {'block_id': 'image', 'block_type': 27, 'image': {'token': 'cell-image'}},
        {'block_id': 'code', 'block_type': 14, 'code': {
            'style': {'language': 'Python'},
            'elements': [{'text_run': {'content': '  value = 1\n'}}],
        }},
        text_block('after', 'after'),
    ]
    parsed = parse_docx_blocks('source', 'doc', 'theory', blocks, '八股')
    draft = parsed.published[0]
    materials = collect_materials(database, {'identity': 'bot'}, blocks, [draft], TableMedia())

    archived = apply_materials(draft, materials, blocks)

    ordered = next(item for item in archived.materials
                   if item['kind'] == 'ordered_content' and item['role'] == 'reference')
    assert [node['kind'] for node in ordered['nodes']] == ['text', 'media', 'code', 'text']
    assert ordered['nodes'][2]['text'] == '  value = 1'
    assert ordered['nodes'][2]['language'] == 'Python'


def test_local_reparse_recovers_native_table_and_checks_nested_files(database):
    blocks = native_table_blocks()
    add_source(database, 'source')
    database.execute("UPDATE source SET question_type='theory' WHERE id='source'")
    draft = parse_docx_blocks('source', 'doc', 'theory', blocks, '八股').published[0]
    materials = collect_materials(database, {'identity': 'bot'}, blocks, [draft], TableMedia())
    archived = apply_materials(draft, materials, blocks)
    publish_snapshot(database, 'source', '1', '1', blocks, [archived], 'ordered-v1')
    database.commit()

    restored = _archived_materials(database, 'source', blocks)

    assert restored['table']['status'] == 'complete'
    nested = next(item for item in restored['table']['attachments'] if item['block_id'] == 'image')
    media = Path(database.execute('PRAGMA database_list').fetchone()[2]).parent / 'media'
    (media / nested['path']).unlink()
    assert _archived_materials(database, 'source', blocks)['table']['status'] == 'pending'


def test_nested_table_attachment_inherits_reference_authorization(monkeypatch):
    monkeypatch.setattr('app.services.bootstrap.load_initial_sources', list)
    with TestClient(app) as client:
        database = connect_database(app.state.database_path)
        question = seed_question(database, 'theory')
        version = database.execute('SELECT current_version_id FROM question WHERE id=?', (question,)).fetchone()[0]
        data = b'\x89PNG\r\n\x1a\nnested'
        digest = hashlib.sha256(data).hexdigest()
        attachment = {'kind': 'media', 'block_id': 'nested-image', 'status': 'complete',
                      'path': digest + '.png', 'sha256': digest}
        table = {'kind': 'table', 'block_id': 'table', 'role': 'reference', 'status': 'complete',
                 'attachments': [attachment], 'structure': {'schema': 'grid-v1', 'rows': 1,
                 'columns': 1, 'cells': [{'row': 0, 'column': 0, 'content': [
                     {'kind': 'media', 'block_id': 'nested-image'}]}]}}
        database.execute('UPDATE version_resources SET materials_json=? WHERE version_id=?',
                         (json.dumps([table]), version))
        database.execute("INSERT INTO daily_plan(id,plan_date,timezone,theory_target,created_at) "
                         "VALUES ('plan','2026-09-20','Asia/Shanghai',1,'now')")
        database.execute("INSERT INTO task(id,plan_id,question_id,question_version_id,origin,target_kind,status,created_at) "
                         "VALUES ('task','plan',?,?,'daily','base','pending','now')", (question, version))
        media = Path(app.state.database_path).parent / 'media'
        media.mkdir(exist_ok=True)
        (media / attachment['path']).write_bytes(data)
        database.commit()
        headers = {'X-Requested-With': 'learning-practice'}

        assert client.get('/api/tasks/task/materials/nested-image', headers=headers).status_code == 403
        assert client.post('/api/tasks/task/attempts', headers=headers,
                           json={'entry_mode': 'web', 'started_at': '2026-09-20T00:00:00Z'}).status_code == 200
        exposed = client.post('/api/tasks/task/expose-answer', headers=headers,
                              json={'exposed_at': '2026-09-20T00:00:00Z'})
        assert exposed.status_code == 200
        assert client.get('/api/tasks/task/materials/nested-image', headers=headers).status_code == 200
        database.close()


def test_frozen_version_materials_work_across_prompt_and_editor_routes(monkeypatch):
    monkeypatch.setattr('app.services.bootstrap.load_initial_sources', list)
    with TestClient(app) as client:
        database = connect_database(app.state.database_path)
        question = seed_question(database, 'theory')
        version = database.execute('SELECT current_version_id FROM question WHERE id=?', (question,)).fetchone()[0]
        media = Path(app.state.database_path).parent / 'media'
        media.mkdir(exist_ok=True)
        records = []
        for role in ('prompt', 'reference'):
            block = role + '-image'
            data = ('synthetic-' + role).encode()
            digest = hashlib.sha256(data).hexdigest()
            path = digest + '.png'
            (media / path).write_bytes(data)
            image = {'kind': 'media', 'block_id': block, 'role': role, 'status': 'complete',
                     'path': path, 'sha256': digest}
            nodes = [{'kind': 'text', 'block_id': role + '-text', 'text': role},
                     {'kind': 'media', 'block_id': block}]
            ordered_hash = hashlib.sha256(
                json.dumps(nodes, ensure_ascii=False, sort_keys=True).encode(),
            ).hexdigest()
            records.extend([image, {'kind': 'ordered_content', 'role': role, 'status': 'complete',
                                     'schema': 'ordered-v1', 'nodes': nodes, 'sha256': ordered_hash}])
        database.execute('UPDATE version_resources SET materials_json=? WHERE version_id=?',
                         (json.dumps(records), version))
        database.commit()
        headers = {'X-Requested-With': 'learning-practice'}

        prompt = client.get(f'/api/questions/{question}/versions/{version}/prompt-materials', headers=headers)
        assert prompt.status_code == 200
        encoded = json.dumps(prompt.json(), ensure_ascii=False)
        assert 'prompt-image' in encoded and 'reference-image' not in encoded
        assert client.get(
            f'/api/questions/{question}/versions/{version}/materials/prompt-image', headers=headers,
        ).status_code == 200
        assert database.execute('SELECT COUNT(*) FROM question_exposure').fetchone()[0] == 0
        reference_url = f'/api/questions/{question}/versions/{version}/materials/reference-image'
        assert client.get(reference_url, headers=headers).status_code == 403

        knowledge = client.post(f'/api/questions/{question}/knowledge', headers=headers,
                                json={'version_id': version})
        assert knowledge.status_code == 200
        assert knowledge.json()['prompt_materials'][0]['role'] == 'prompt'
        assert knowledge.json()['reference_materials'][0]['role'] == 'reference'
        before = database.execute('SELECT COUNT(*) FROM question_exposure').fetchone()[0]
        assert client.get(reference_url, headers=headers).status_code == 200
        assert database.execute('SELECT COUNT(*) FROM question_exposure').fetchone()[0] == before + 1

        database.execute("INSERT INTO daily_plan(id,plan_date,timezone,theory_target,created_at) "
                         "VALUES ('material-plan','2026-09-20','Asia/Shanghai',1,'now')")
        database.execute("INSERT INTO task(id,plan_id,question_id,question_version_id,origin,target_kind,status,created_at) "
                         "VALUES ('material-task','material-plan',?,?,'interview','base','pending','now')",
                         (question, version))
        database.commit()
        session = start_session(database, 'material-task', datetime.now(UTC))
        interview = client.get(f'/interview/{session}')
        assert interview.status_code == 200
        assert f'data-question-version="{version}"' in interview.text
        answer = client.post(f'/api/interviews/{session}/reference', headers=headers,
                             json={'turn_id': None})
        assert answer.status_code == 200
        assert answer.json()['materials']
        assert answer.json()['materials'][0]['role'] == 'reference'
        database.close()


def test_legacy_sheet_text_gets_structured_without_losing_csv_cells():
    item = normalize_material({'kind': 'sheet', 'status': 'complete',
                               'text': 'name,note,empty\n"a,b","line1\nline2",'})

    assert item['structure']['rows'] == 2
    assert item['structure']['cells'][1] == ['a,b', 'line1\nline2', '']


@pytest.mark.parametrize('present', [False, True])
def test_export_checks_attachment_nested_in_table(database, tmp_path, present):
    question = seed_question(database, 'theory')
    version = database.execute('SELECT current_version_id FROM question WHERE id=?', (question,)).fetchone()[0]
    data = b'\x89PNG\r\n\x1a\nnested'
    digest = hashlib.sha256(data).hexdigest()
    attachment = {'kind': 'media', 'block_id': 'nested-image', 'status': 'complete',
                  'path': digest + '.png', 'sha256': digest}
    structure = {'schema': 'grid-v1', 'rows': 1, 'columns': 1,
                 'cells': [{'row': 0, 'column': 0,
                            'content': [{'kind': 'media', 'block_id': 'nested-image'}]}]}
    table = {'kind': 'table', 'block_id': 'table', 'status': 'complete', 'attachments': [attachment],
             'sha256': hashlib.sha256(json.dumps(
                 structure, ensure_ascii=False, sort_keys=True,
             ).encode()).hexdigest(), 'structure': structure}
    database.execute('UPDATE version_resources SET materials_json=? WHERE version_id=?',
                     (json.dumps(['legacy-reference-marker', table]), version))
    database.commit()
    media = Path(database.execute('PRAGMA database_list').fetchone()[2]).parent / 'media'
    media.mkdir(exist_ok=True)
    if present:
        (media / attachment['path']).write_bytes(data)
    archive = tmp_path / 'learning.zip'

    if not present:
        with pytest.raises(ExportError, match='附件缺失'):
            export_learning_data(database, archive, media)
        return
    export_learning_data(database, archive, media)
    assert verify_export(archive, tmp_path / 'restore')['media_count'] == 1
    with zipfile.ZipFile(archive) as bundle:
        restored_database = tmp_path / 'restored-learning.db'
        restored_database.write_bytes(bundle.read('learning.db'))
    with sqlite3.connect(restored_database) as restored:
        frozen = json.loads(restored.execute(
            'SELECT materials_json FROM version_resources WHERE version_id=?', (version,),
        ).fetchone()[0])
    assert frozen == ['legacy-reference-marker', table]


@pytest.mark.parametrize('corruption', ['ordered-hash', 'ordered-reference', 'cell-reference', 'merge-range'])
def test_export_rejects_broken_structured_material_graph(database, tmp_path, corruption):
    question = seed_question(database, 'theory')
    version = database.execute('SELECT current_version_id FROM question WHERE id=?', (question,)).fetchone()[0]
    text_nodes = [{'kind': 'text', 'block_id': 'text', 'text': 'kept in order'}]
    ordered = {'kind': 'ordered_content', 'role': 'reference', 'status': 'complete',
               'schema': 'ordered-v1', 'nodes': text_nodes,
               'sha256': hashlib.sha256(json.dumps(
                   text_nodes, ensure_ascii=False, sort_keys=True,
               ).encode()).hexdigest()}
    table_structure = {'schema': 'grid-v1', 'rows': 1, 'columns': 1,
                       'cells': [{'row': 0, 'column': 0,
                                  'content': [{'kind': 'text', 'text': 'cell'}]}],
                       'merge_info': []}
    table = {'kind': 'table', 'block_id': 'table', 'role': 'reference', 'status': 'complete',
             'attachments': [], 'structure': table_structure,
             'sha256': hashlib.sha256(json.dumps(
                 table_structure, ensure_ascii=False, sort_keys=True,
             ).encode()).hexdigest()}
    values = [ordered, table]
    if corruption == 'ordered-hash':
        ordered['sha256'] = 'wrong'
    elif corruption == 'ordered-reference':
        ordered['nodes'] = [{'kind': 'media', 'block_id': 'missing'}]
        ordered['sha256'] = hashlib.sha256(json.dumps(
            ordered['nodes'], ensure_ascii=False, sort_keys=True,
        ).encode()).hexdigest()
    elif corruption == 'cell-reference':
        table['structure']['cells'][0]['content'] = [{'kind': 'media', 'block_id': 'missing'}]
    else:
        table['structure']['merge_info'] = [
            {'row': 0, 'column': 0, 'row_span': 2, 'column_span': 1},
        ]
    database.execute('UPDATE version_resources SET materials_json=? WHERE version_id=?',
                     (json.dumps(values), version))
    database.commit()

    with pytest.raises(ExportError, match='备份未通过完整性检查'):
        export_learning_data(database, tmp_path / 'broken.zip', tmp_path / 'media')


def test_export_rejects_wrong_table_structure_hash(database, tmp_path):
    question = seed_question(database, 'theory')
    version = database.execute(
        'SELECT current_version_id FROM question WHERE id=?', (question,),
    ).fetchone()[0]
    structure = {'schema': 'grid-v1', 'rows': 1, 'columns': 1,
                 'cells': [{'row': 0, 'column': 0,
                            'content': [{'kind': 'text', 'text': 'cell'}]}]}
    table = {'kind': 'table', 'block_id': 'table', 'role': 'reference',
             'status': 'complete', 'structure': structure, 'attachments': [],
             'sha256': 'definitely-wrong'}
    database.execute('UPDATE version_resources SET materials_json=? WHERE version_id=?',
                     (json.dumps([table]), version))
    database.commit()

    with pytest.raises(ExportError, match='表格结构哈希不匹配'):
        export_learning_data(database, tmp_path / 'broken-table.zip', tmp_path / 'media')


def test_export_rejects_corrupt_nested_sheet_grid(database, tmp_path):
    question = seed_question(database, 'theory')
    version = database.execute(
        'SELECT current_version_id FROM question WHERE id=?', (question,),
    ).fetchone()[0]
    text = 'name,value\nlatency,42'
    digest = hashlib.sha256(text.encode()).hexdigest()
    sheet = {'kind': 'sheet', 'block_id': 'nested-sheet', 'status': 'complete',
             'path': digest + '.csv', 'sha256': digest, 'text': text,
             'structure': {'schema': 'grid-v1', 'rows': 2, 'columns': 2,
                           'cells': [['wrong', 'value'], ['latency', '42']]}}
    table_structure = {'schema': 'grid-v1', 'rows': 1, 'columns': 1,
                       'cells': [{'row': 0, 'column': 0,
                                  'content': [{'kind': 'sheet', 'block_id': 'nested-sheet'}]}]}
    table = {'kind': 'table', 'block_id': 'table', 'role': 'reference',
             'status': 'complete', 'attachments': [sheet], 'structure': table_structure,
             'sha256': hashlib.sha256(json.dumps(
                 table_structure, ensure_ascii=False, sort_keys=True,
             ).encode()).hexdigest()}
    database.execute('UPDATE version_resources SET materials_json=? WHERE version_id=?',
                     (json.dumps([table]), version))
    database.commit()
    media = tmp_path / 'media'
    media.mkdir()
    (media / sheet['path']).write_text(text, encoding='utf-8', newline='')

    with pytest.raises(ExportError, match='Sheets 结构'):
        export_learning_data(database, tmp_path / 'broken-sheet.zip', media)
