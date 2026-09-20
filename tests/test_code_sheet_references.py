import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.adapters.lark_cli import LarkCliError
from app.services.source_sync import sync_registered_source


class SheetSource:
    def __init__(self, mixed=False, fail=False, gap=False):
        self.csv = 'input,result\nempty,zero'
        self.fail = fail
        self.revision = 1
        self.sheet_reads = 0
        self.blocks = [
            {'block_id': 'question', 'block_type': 5,
             'heading3': {'elements': [{'text_run': {'content': 'Find the maximum value-图片'}}]}},
            {'block_id': 'prompt-image', 'block_type': 27,
             'image': {'token': 'synthetic-image', 'alt': 'Find the largest value; return zero for empty input.'}},
            {'block_id': 'answer-sheet', 'block_type': 30, 'sheet': {'token': 'synthetic-sheet_sheetA'}},
        ]
        if mixed:
            self.blocks.append({'block_id': 'answer-text', 'block_type': 2,
                                'text': {'elements': [{'text_run': {'content': 'Track the maximum.'}}]}})
        if gap:
            self.blocks.append({'block_id': 'unsupported-answer', 'block_type': 999})

    def run_read(self, args):
        if args[0] == 'api':
            data = ({'items': self.blocks, 'has_more': False} if args[2].endswith('/blocks')
                    else {'document': {'revision_id': self.revision}})
        else:
            self.sheet_reads += 1
            if self.fail:
                raise LarkCliError('Synthetic sheet read failure')
            if args[:2] == ['sheets', '+workbook-info']:
                data = {'sheets': [{'sheet_id': 'sheetA', 'row_count': 2, 'column_count': 2}]}
            else:
                assert args[:2] == ['sheets', '+csv-get']
                data = {'has_more': False, 'annotated_csv': self.csv}
        return SimpleNamespace(data=data)

    def download_media(self, token, output, identity):
        output.write_bytes(b'\x89PNG\r\n\x1a\nsynthetic-fixture')


def register(db):
    db.execute("INSERT INTO source(id,document_id,wiki_url,question_type) VALUES "
               "('sheet-source','sheet-document','https://example.feishu.cn/docx/synthetic','code')")
    db.commit()


@pytest.mark.parametrize('mixed', [False, True])
def test_code_sheet_reference_is_read_archived_and_versioned(database, mixed):
    register(database)
    source = SheetSource(mixed=mixed)
    sync_registered_source(database, 'sheet-source', source)
    first = database.execute('SELECT v.* FROM question q JOIN question_version v ON v.id=q.current_version_id').fetchone()
    assert first and first['material_status'] == 'complete'
    assert 'empty,zero' in first['reference_text']
    assert ('Track the maximum.' in first['reference_text']) == mixed
    assert source.sheet_reads >= 2
    resources = json.loads(database.execute('SELECT materials_json FROM version_resources WHERE version_id=?', (first['id'],)).fetchone()[0])
    sheet = next(item for item in resources if item['kind'] == 'sheet')
    assert sheet['role'] == 'reference' and sheet['status'] == 'complete'
    assert sheet['structure'] == {'schema': 'grid-v1', 'rows': 2, 'columns': 2,
                                  'cells': [['input', 'result'], ['empty', 'zero']]}
    location = Path(database.execute('PRAGMA database_list').fetchone()[2]).parent
    assert (location / 'media' / sheet['path']).read_text(encoding='utf-8') == source.csv
    source.csv = 'input,result\nempty,None'
    source.revision += 1
    sync_registered_source(database, 'sheet-source', source)
    latest = database.execute('SELECT v.* FROM question q JOIN question_version v ON v.id=q.current_version_id').fetchone()
    assert latest['id'] != first['id'] and 'empty,None' in latest['reference_text']
    assert database.execute('SELECT reference_text FROM question_version WHERE id=?', (first['id'],)).fetchone()[0] == first['reference_text']


@pytest.mark.parametrize('failure', ['sheet', 'unsupported'])
def test_code_reference_gaps_cannot_publish_complete(database, failure):
    register(database)
    source = SheetSource(fail=failure == 'sheet', gap=failure == 'unsupported')
    sync_registered_source(database, 'sheet-source', source)
    assert not database.execute("SELECT 1 FROM question q JOIN question_version v ON v.id=q.current_version_id WHERE v.material_status='complete'").fetchone()
