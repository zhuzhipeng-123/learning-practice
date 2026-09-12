"""Apply heading eligibility without treating a rule change as source deletion."""

import json

from app.parsers.docx import is_theory_heading


def exclude_ineligible_anchors(connection, source, blocks):
    if source['question_type'] != 'theory':
        return 0
    by_id = {block['block_id']: block for block in blocks}
    excluded = 0
    bindings = connection.execute("SELECT question_id,main_anchor_block_id FROM source_binding "
                                  "WHERE source_id=? AND confirmation_status!='migrated'", (source['id'],)).fetchall()
    for binding in bindings:
        block = by_id.get(binding['main_anchor_block_id'])
        if block is not None and not is_theory_heading(block):
            excluded += connection.execute("UPDATE question SET source_status='excluded_by_rule' "
                "WHERE id=? AND source_status!='excluded_by_rule'", (binding['question_id'],)).rowcount
    for candidate in connection.execute("SELECT id,main_anchor_block_id FROM parser_candidate "
                                         "WHERE source_id=? AND status='pending'", (source['id'],)).fetchall():
        block = by_id.get(candidate['main_anchor_block_id'])
        if block is not None and not is_theory_heading(block):
            connection.execute("UPDATE parser_candidate SET status='superseded' WHERE id=?", (candidate['id'],))
    return excluded


def candidate_heading_allowed(connection, source_id, anchor):
    row = connection.execute("SELECT ss.blocks_json FROM source_sync_state st JOIN source_snapshot ss ON ss.id=st.snapshot_id "
                              "WHERE st.source_id=?", (source_id,)).fetchone()
    return bool(row and any(block['block_id'] == anchor and is_theory_heading(block) for block in json.loads(row[0])))
