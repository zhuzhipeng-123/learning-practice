"""Apply heading eligibility without treating a rule change as source deletion."""

import json

from app.parsers.block_tree import ordered_blocks
from app.parsers.docx import classify_theory_headings


def exclude_ineligible_anchors(connection, source, blocks):
    if source['question_type'] != 'theory':
        return 0
    by_id = {block['block_id']: block for block in ordered_blocks(blocks)}
    roles = classify_theory_headings(blocks).roles
    excluded = 0
    bindings = connection.execute("SELECT b.question_id,b.main_anchor_block_id FROM source_binding b "
        "WHERE b.source_id=? AND b.confirmation_status!='migrated' AND NOT EXISTS(SELECT 1 FROM source_binding other "
        "WHERE other.question_id=b.question_id AND other.id!=b.id AND other.active=1 AND other.confirmation_status!='migrated')", (source['id'],)).fetchall()
    for binding in bindings:
        role = roles.get(binding['main_anchor_block_id'])
        if binding['main_anchor_block_id'] in by_id and role not in {'question', 'ambiguous_question'}:
            excluded += connection.execute("UPDATE question SET source_status='excluded_by_rule' "
                "WHERE id=? AND source_status!='excluded_by_rule'", (binding['question_id'],)).rowcount
    for candidate in connection.execute("SELECT id,main_anchor_block_id FROM parser_candidate "
                                         "WHERE source_id=? AND status='pending'", (source['id'],)).fetchall():
        role = roles.get(candidate['main_anchor_block_id'])
        if candidate['main_anchor_block_id'] in by_id and role not in {'question', 'ambiguous_question'}:
            connection.execute("UPDATE parser_candidate SET status='superseded' WHERE id=?", (candidate['id'],))
    return excluded


def candidate_heading_allowed(connection, source_id, anchor):
    row = connection.execute("SELECT ss.blocks_json FROM source_sync_state st JOIN source_snapshot ss ON ss.id=st.snapshot_id "
                              "WHERE st.source_id=?", (source_id,)).fetchone()
    if not row:
        return False
    role = classify_theory_headings(json.loads(row[0])).roles.get(anchor)
    return role in {'question', 'ambiguous_question'}
