"""Reapply parser rules to archived blocks; never claim a fresh remote read."""

import json
from datetime import UTC, datetime
from pathlib import Path

from app.parsers.docx import parse_docx_blocks
from app.services.alignment_report import inventory_changes, source_inventory
from app.services.confirmed_bindings import select_drafts
from app.services.materials import apply_materials, material_closure
from app.services.source_sync import PARSER_VERSION, _store_candidates
from app.services.sync import publish_snapshot, stable_hash
from app.services.theory_rules import exclude_ineligible_anchors
from app.services.wiki_sources import source_prefix
from app.storage.ids import new_id
from app.storage.transactions import atomic


def _archived_materials(connection, source_id, blocks):
    cached = {}
    rows = connection.execute("SELECT r.materials_json FROM version_resources r JOIN question_version v ON v.id=r.version_id "
        "JOIN source_snapshot ss ON ss.id=v.snapshot_id JOIN source_sync_state st ON st.snapshot_id=ss.id "
        "WHERE st.source_id=?", (source_id,)).fetchall()
    rows += connection.execute("SELECT draft_json FROM parser_candidate c JOIN source_sync_state st "
                               "ON st.snapshot_id=c.snapshot_id WHERE c.source_id=?", (source_id,)).fetchall()
    for row in rows:
        value = json.loads(row[0])
        for item in value if isinstance(value, list) else value.get('materials', []):
            if isinstance(item, dict) and item.get('block_id'):
                cached[item['block_id']] = item
    media_root = Path(connection.execute('PRAGMA database_list').fetchone()[2]).parent / 'media'
    result = {}
    for block in blocks:
        kind = ('media' if 'image' in block else 'sheet' if 'sheet' in block else
                'table' if block.get('block_type') == 31 else None)
        if not kind:
            continue
        item = cached.get(block['block_id'])
        token = block['image' if kind == 'media' else 'sheet'].get('token', '') if kind != 'table' else ''
        files_complete = all(nested.get('kind') not in {'media', 'sheet'} or nested.get('status') != 'complete'
                             or (media_root / nested.get('path', '__missing__')).is_file()
                             for nested in material_closure([item] if item else []))
        if not item or item.get('token', '') != token or not files_complete:
            item = {'block_id': block['block_id'], 'kind': kind, 'token': token, 'status': 'pending',
                    'error': '本地未归档该素材，请点击对齐飞书读取'}
        result[block['block_id']] = item
    return result


def preview_theory_reparse(connection):
    """Compute the parser-rule migration without publishing any database fact."""
    reports = []
    for source in _theory_sources(connection):
        blocks = json.loads(source['blocks_json'])
        parsed = parse_docx_blocks(source['id'], source['document_id'], 'theory', blocks, source_prefix(connection, source['id']))
        drafts, candidates, invalid = select_drafts(connection, source, blocks, parsed)
        reports.append({
            'source_id': source['id'], 'snapshot_id': source['snapshot_id'],
            'precondition': _preview_precondition(source),
            'published_anchors': [item.main_anchor_block_id for item in drafts],
            'candidate_anchors': [item.main_anchor_block_id for item in candidates],
            'invalid_anchors': sorted(invalid), 'remote_checked': False,
        })
    return reports


@atomic
def reparse_theory_snapshots(connection, expected_preview=None):
    reports = []
    sources = _theory_sources(connection)
    if expected_preview is not None:
        expected = {item['source_id']: item for item in expected_preview}
        if set(expected) != {source['id'] for source in sources}:
            raise ValueError('题源集合已经变化，请重新生成迁移预览')
        if any(expected[source['id']].get('precondition') != _preview_precondition(source) for source in sources):
            raise ValueError('题源快照已经变化，请重新生成迁移预览')
    for source in sources:
        blocks = json.loads(source['blocks_json'])
        before = source_inventory(connection, source['id'])
        parsed = parse_docx_blocks(source['id'], source['document_id'], 'theory', blocks, source_prefix(connection, source['id']))
        drafts, candidates, invalid = select_drafts(connection, source, blocks, parsed)
        materials = _archived_materials(connection, source['id'], blocks)
        drafts = [apply_materials(draft, materials, blocks) for draft in drafts]
        candidates = [apply_materials(draft, materials, blocks) for draft in candidates]
        published = publish_snapshot(connection, source['id'], source['revision'], source['revision'], blocks, drafts,
                                     PARSER_VERSION, json.loads(source['dependency_versions_json']))
        _store_candidates(connection, source['id'], published['snapshot_id'], candidates)
        for anchor in invalid:
            connection.execute("UPDATE question SET source_status='missing_pending' WHERE id IN "
                "(SELECT b.question_id FROM source_binding b WHERE b.source_id=? AND b.main_anchor_block_id=? "
                "AND b.confirmation_status!='migrated' AND NOT EXISTS(SELECT 1 FROM source_binding other WHERE other.question_id=b.question_id "
                "AND other.id!=b.id AND other.active=1 AND other.confirmation_status!='migrated'))", (source['id'], anchor))
        excluded = exclude_ineligible_anchors(connection, source, blocks)
        # publish_snapshot also updates freshness; local reparsing must restore those fields.
        fields = ('last_check_at', 'last_check_success_at', 'last_content_sync_at', 'last_complete_sync_at', 'last_error')
        connection.execute('UPDATE source SET ' + ','.join(field + '=?' for field in fields) + ' WHERE id=?',
                           (*[source[field] for field in fields], source['id']))
        report = {'source_id': source['id'], 'mode': 'local_reparse', 'remote_checked': False,
                  'excluded_by_heading_rule': excluded, 'changes': inventory_changes(before, source_inventory(connection, source['id']))}
        now = datetime.now(UTC).isoformat()
        connection.execute("INSERT INTO sync_run(id,source_id,started_at,finished_at,status,summary_json) VALUES (?,?,?,?,'local_reparse',?)",
                           (new_id('sync'), source['id'], now, now, json.dumps(report, ensure_ascii=False)))
        reports.append(report)
    return reports


def _theory_sources(connection):
    return connection.execute("SELECT s.*,ss.id snapshot_id,ss.blocks_json,ss.revision,ss.content_hash,"
        "ss.parser_version,ss.dependency_versions_json FROM source s JOIN source_sync_state st ON st.source_id=s.id "
        "JOIN source_snapshot ss ON ss.id=st.snapshot_id WHERE s.question_type='theory' AND s.enabled=1 "
        "ORDER BY s.id").fetchall()


def _preview_precondition(source):
    return stable_hash({key: source[key] for key in (
        'id', 'snapshot_id', 'revision', 'content_hash', 'parser_version', 'dependency_versions_json'
    )})
