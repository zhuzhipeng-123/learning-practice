"""Reapply parser rules to archived blocks; never claim a fresh remote read."""

import json
from datetime import UTC, datetime
from pathlib import Path

from app.parsers.docx import parse_docx_blocks
from app.services.alignment_report import inventory_changes, source_inventory
from app.services.confirmed_bindings import select_drafts
from app.services.materials import apply_materials
from app.services.source_sync import PARSER_VERSION, _store_candidates
from app.services.sync import publish_snapshot
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
            cached[item['block_id']] = item
    media_root = Path(connection.execute('PRAGMA database_list').fetchone()[2]).parent / 'media'
    result = {}
    for block in blocks:
        kind = 'media' if 'image' in block else 'sheet' if 'sheet' in block else None
        if not kind:
            continue
        item = cached.get(block['block_id'])
        token = block['image' if kind == 'media' else 'sheet'].get('token', '')
        if not item or item.get('token') != token or not (media_root / item.get('path', '__missing__')).is_file():
            item = {'block_id': block['block_id'], 'kind': kind, 'token': token, 'status': 'pending',
                    'error': '本地未归档该素材，请点击对齐飞书读取'}
        result[block['block_id']] = item
    return result


@atomic
def reparse_theory_snapshots(connection):
    reports = []
    sources = connection.execute("SELECT s.*,ss.id snapshot_id,ss.blocks_json,ss.revision,ss.dependency_versions_json "
        "FROM source s JOIN source_sync_state st ON st.source_id=s.id JOIN source_snapshot ss ON ss.id=st.snapshot_id "
        "WHERE s.question_type='theory' AND s.enabled=1").fetchall()
    for source in sources:
        blocks = json.loads(source['blocks_json'])
        before = source_inventory(connection, source['id'])
        parsed = parse_docx_blocks(source['id'], source['document_id'], 'theory', blocks, source_prefix(connection, source['id']))
        drafts, candidates, invalid = select_drafts(connection, source, blocks, parsed)
        materials = _archived_materials(connection, source['id'], blocks)
        drafts = [apply_materials(draft, materials) for draft in drafts]
        candidates = [apply_materials(draft, materials) for draft in candidates]
        published = publish_snapshot(connection, source['id'], source['revision'], source['revision'], blocks, drafts,
                                     PARSER_VERSION, json.loads(source['dependency_versions_json']))
        _store_candidates(connection, source['id'], published['snapshot_id'], candidates)
        for anchor in invalid:
            connection.execute("UPDATE question SET source_status='missing_pending' WHERE id IN "
                               "(SELECT question_id FROM source_binding WHERE source_id=? AND main_anchor_block_id=?)", (source['id'], anchor))
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
