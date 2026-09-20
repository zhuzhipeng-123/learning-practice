from types import SimpleNamespace

import pytest

from app.adapters.docx_reader import DocxReader
from app.parsers.docx import parse_docx_blocks
from app.services.candidates import approve_candidate
from app.services.source_sync import sync_registered_source
from app.services.wiki_sources import reconcile_tree
from tests.test_docx_parser import heading, text_block


def sync_source(database, monkeypatch, source, reference):
    raw = [heading('module', 1, 'Independent topic'), heading('q', 3, 'Explain the conditions'),
           text_block('r', reference)]
    monkeypatch.setattr(DocxReader, 'read_latest', lambda *args: SimpleNamespace(
        revision=reference, blocks=raw, page_count=1))
    return sync_registered_source(database, source, client=object())


def wiki_node(name):
    return {'node_token': name, 'path': 'Root > ' + name, 'obj_type': 'docx',
            'obj_token': 'doc-' + name, 'title': name}


def migrated_question(database, monkeypatch):
    database.execute("INSERT INTO source(id,document_id,wiki_url,question_type) "
                     "VALUES ('root','doc-root','https://example.test/wiki/root','theory')")
    database.commit()
    root = dict(database.execute('SELECT * FROM source').fetchone())
    reconcile_tree(database, root, {'nodes': [wiki_node(n) for n in ('root', 'a', 'b')],
                                    'complete': True})
    sources = {row['document_id']: row['id'] for row in database.execute('SELECT * FROM source')}
    a, b = sources['doc-a'], sources['doc-b']
    sync_source(database, monkeypatch, a, 'Original conditions')
    question = database.execute('SELECT id FROM question').fetchone()[0]
    sync_source(database, monkeypatch, b, 'Original conditions')
    candidate = database.execute("SELECT id FROM parser_candidate WHERE source_id=? AND status='pending'", (b,)).fetchone()[0]
    approve_candidate(database, candidate, existing_question_id=question)
    sync_source(database, monkeypatch, b, 'Corrected conditions')
    return root, a, b, question


@pytest.mark.parametrize('operation', ['restore_old_anchor', 'remove_old_document'])
def test_migrated_binding_has_no_automatic_authority(database, monkeypatch, operation):
    root, a, b, question = migrated_question(database, monkeypatch)
    current = database.execute('SELECT current_version_id FROM question WHERE id=?', (question,)).fetchone()[0]
    if operation == 'restore_old_anchor':
        result = sync_source(database, monkeypatch, a, 'Original conditions')
        assert result['candidate_count'] == 1
    else:
        for _ in range(2):
            reconcile_tree(database, root, {'nodes': [wiki_node('root'), wiki_node('b')],
                                            'complete': True})
    assert tuple(database.execute('SELECT current_version_id,source_status FROM question WHERE id=?', (question,)).fetchone()) == (current, 'active')
    assert database.execute('SELECT COUNT(*) FROM source_binding WHERE question_id=? AND active=1', (question,)).fetchone()[0] == 1
    assert database.execute('SELECT enabled FROM source WHERE id=?', (b,)).fetchone()[0] == 1


def test_retired_anchor_requires_explicit_reassignment(database, monkeypatch):
    _, a, _, question = migrated_question(database, monkeypatch)
    sync_source(database, monkeypatch, a, 'Completely revised independent material')
    candidate = database.execute("SELECT id FROM parser_candidate WHERE source_id=? AND status='pending'", (a,)).fetchone()
    assert candidate is not None
    approve_candidate(database, candidate[0], existing_question_id=question)
    assert database.execute('SELECT COUNT(*) FROM question').fetchone()[0] == 1
    assert database.execute('SELECT source_id FROM source_binding WHERE active=1').fetchone()[0] == a
    assert database.execute('SELECT v.reference_text FROM question q JOIN question_version v ON v.id=q.current_version_id').fetchone()[0] == 'Completely revised independent material'


@pytest.mark.parametrize('container', [19, 34])
@pytest.mark.parametrize('order', ['preorder', 'children_after_next_question'])
def test_nested_reference_is_complete_and_not_a_question(container, order):
    raw = [heading('module', 1, 'Unfamiliar topic'), heading('q', 3, 'What are the conditions?'),
           text_block('a', 'Necessary condition A.'),
           {'block_id': 'box', 'block_type': container, 'children': ['nested', 'b']},
           heading('nested', 4, 'Important exception'), text_block('b', 'Necessary condition B.'),
           {'block_id': 'divider', 'block_type': 22}, text_block('c', 'Condition C remains necessary.'),
           heading('next', 3, 'Independent next question'), text_block('next-answer', 'Next answer.')]
    if order != 'preorder':
        raw = raw[:4] + raw[6:] + raw[4:6]
    parsed = parse_docx_blocks('s', 'd', 'theory', raw)
    question = next(item for item in parsed.published if item.main_anchor_block_id == 'q')
    assert question.reference_text == 'Necessary condition A.\nImportant exception\nNecessary condition B.\nCondition C remains necessary.'
    assert question.material_status == 'complete'
    assert {item.main_anchor_block_id for item in parsed.published} == {'q', 'next'}


def test_unknown_reference_container_is_visible_as_a_gap():
    parsed = parse_docx_blocks('s', 'd', 'theory', [heading('module', 1, 'Topic'),
        heading('q', 3, 'Explain the mechanism'), {'block_id':'unknown', 'block_type':999}])
    assert parsed.published == []
    assert parsed.candidates[0].material_status == 'incomplete_reference'
    assert 'unknown' in parsed.candidates[0].parse_issues


def test_table_cell_heading_does_not_create_a_question_or_break_order():
    raw = [
        heading('module', 1, 'Topic'), heading('q', 3, 'Explain the mechanism'),
        {'block_id': 'table', 'block_type': 31, 'children': ['cell']},
        {'block_id': 'cell', 'block_type': 32, 'children': ['cell-heading', 'cell-text']},
        heading('cell-heading', 3, 'Cell label'), text_block('cell-text', 'Cell explanation.'),
        text_block('tail', 'Final condition.'),
    ]

    parsed = parse_docx_blocks('s', 'd', 'theory', raw)

    assert [item.main_anchor_block_id for item in parsed.published] == ['q']
    assert parsed.published[0].reference_text == 'Cell label\nCell explanation.\nFinal condition.'
    assert parsed.published[0].material_status == 'media_required'


def test_legacy_multiple_active_bindings_require_explicit_owner(database, monkeypatch):
    import json

    from app.services.candidates import CandidateError
    from app.services.source_state import observe_missing_questions
    _, a, b, question = migrated_question(database, monkeypatch)
    database.execute("UPDATE source_binding SET active=1,confirmation_status='confirmed' WHERE question_id=?", (question,))
    database.commit()
    original = tuple(database.execute('SELECT current_version_id,source_status FROM question WHERE id=?', (question,)).fetchone())
    sync_source(database, monkeypatch, a, 'New content from an ambiguous owner')
    candidate = database.execute("SELECT id,draft_json FROM parser_candidate WHERE source_id=? AND status='pending'", (a,)).fetchone()
    assert question in json.loads(candidate['draft_json'])['match_question_ids']
    with pytest.raises(CandidateError):
        approve_candidate(database, candidate['id'], as_new=True)
    observe_missing_questions(database, a, set(), True, False, True)
    observe_missing_questions(database, a, set(), True, False, True)
    assert tuple(database.execute('SELECT current_version_id,source_status FROM question WHERE id=?', (question,)).fetchone()) == original
    approve_candidate(database, candidate['id'], existing_question_id=question)
    assert database.execute('SELECT source_id FROM source_binding WHERE active=1 AND question_id=?', (question,)).fetchone()[0] == a
    assert database.execute('SELECT active FROM source_binding WHERE question_id=? AND source_id=?', (question, b)).fetchone()[0] == 0


@pytest.mark.parametrize('broken', ['cycle', 'missing_child'])
def test_invalid_block_graph_never_replaces_saved_source(database, monkeypatch, broken):
    _, _, source, question = migrated_question(database, monkeypatch)
    version = database.execute('SELECT current_version_id FROM question WHERE id=?', (question,)).fetchone()[0]
    raw = [heading('module', 1, 'Topic'), heading('q', 3, 'Explain conditions'),
           {'block_id':'box', 'block_type':19, 'children':['box' if broken == 'cycle' else 'absent']}]
    monkeypatch.setattr(DocxReader, 'read_latest', lambda *args: SimpleNamespace(revision='broken', blocks=raw, page_count=1))
    with pytest.raises((ValueError, RuntimeError)):
        sync_registered_source(database, source, client=object())
    assert database.execute('SELECT current_version_id FROM question WHERE id=?', (question,)).fetchone()[0] == version
    assert database.execute("SELECT COUNT(*) FROM sync_run WHERE status='failed'").fetchone()[0] == 1
