import json
from types import SimpleNamespace

import pytest

from app.adapters.docx_reader import DocxReader
from app.adapters.lark_cli import LarkCliError
from app.services.candidates import CandidateError, approve_candidate
from app.services.source_state import observe_missing_questions
from app.services.source_sync import sync_registered_source
from app.services.sync import publish_snapshot
from tests.helpers import add_source
from tests.test_docx_parser import heading, text_block
from tests.test_sync import blocks, draft


def live_theory(database):
    add_source(database, "live")
    database.execute("UPDATE source SET question_type='theory' WHERE id='live'")
    database.commit()


def run_live(database, monkeypatch, revision, content, client=None):
    monkeypatch.setattr(DocxReader, "read_latest", lambda *args: SimpleNamespace(
        revision=str(revision), blocks=content, page_count=1,
    ))
    return sync_registered_source(database, "live", client)


@pytest.mark.parametrize("unrelated_changes", [False, True])
def test_source_content_round_trip(database, unrelated_changes):
    add_source(database)
    for index, answer in enumerate(["A", "B", "A"]):
        raw = blocks(str(index) if unrelated_changes else answer)
        publish_snapshot(database, "source-code", answer, answer, raw, [draft(reference=answer)], "v1")
    row = database.execute("SELECT v.reference_text FROM question q JOIN question_version v ON v.id=q.current_version_id").fetchone()
    assert row[0] == "A"
    assert database.execute("SELECT count(*) FROM question_version").fetchone()[0] == 2


def test_confirmed_theory_refreshes_without_reapproval(database, monkeypatch):
    live_theory(database)
    run_live(database, monkeypatch, 1, [text_block("q", "Why?"), text_block("r", "A")])
    candidate = database.execute("SELECT id FROM parser_candidate").fetchone()[0]
    question_id = approve_candidate(database, candidate)
    run_live(database, monkeypatch, 2, [text_block("q", "Why now?"), text_block("r", "B")])
    row = database.execute("SELECT v.prompt,v.reference_text FROM question q JOIN question_version v ON v.id=q.current_version_id WHERE q.id=?", (question_id,)).fetchone()
    assert tuple(row) == ("Why now?", "B")
    assert database.execute("SELECT count(*) FROM question").fetchone()[0] == 1
    assert approve_candidate(database, candidate) == question_id


def test_stale_candidate_cannot_overwrite_new_content(database, monkeypatch):
    live_theory(database)
    run_live(database, monkeypatch, 1, [text_block("q", "Why?"), text_block("r", "A")])
    old = database.execute("SELECT id FROM parser_candidate").fetchone()[0]
    run_live(database, monkeypatch, 2, [text_block("q", "Why?"), text_block("r", "B")])
    fresh = database.execute("SELECT id FROM parser_candidate WHERE status='pending'").fetchone()[0]
    approve_candidate(database, fresh)
    with pytest.raises(CandidateError):
        approve_candidate(database, old)
    assert database.execute("SELECT reference_text FROM question_version").fetchone()[0] == "B"


def test_rebuilt_anchor_requires_explicit_identity_decision(database, monkeypatch):
    live_theory(database)
    run_live(database, monkeypatch, 1, [text_block("old", "Why?"), text_block("r", "A")])
    question_id = approve_candidate(database, database.execute("SELECT id FROM parser_candidate").fetchone()[0])
    for revision in [2, 3]:
        run_live(database, monkeypatch, revision, [text_block("new", "Why?"), text_block("r", "A")])
    candidate = database.execute("SELECT id FROM parser_candidate WHERE status='pending'").fetchone()[0]
    with pytest.raises(CandidateError):
        approve_candidate(database, candidate)
    assert approve_candidate(database, candidate, existing_question_id=question_id) == question_id
    assert database.execute("SELECT count(*) FROM question").fetchone()[0] == 1
    assert database.execute("SELECT source_status FROM question").fetchone()[0] == "active"


def test_deleted_anchor_can_be_restored(database):
    add_source(database)
    publish_snapshot(database, "source-code", "1", "1", blocks(), [draft()], "v1")
    for _ in range(2):
        observe_missing_questions(database, "source-code", set(), True, False, True)
    observe_missing_questions(database, "source-code", {draft().main_anchor_block_id}, True, False, True)
    assert database.execute("SELECT active,missing_observation_count FROM source_binding").fetchone()[:] == (1, 0)
    assert database.execute("SELECT source_status FROM question").fetchone()[0] == "active"


def test_publish_failure_rolls_back_snapshot_and_question(database, monkeypatch):
    live_theory(database)
    def failure(*args):
        raise RuntimeError("injected candidate failure")
    monkeypatch.setattr("app.services.source_sync._store_candidates", failure)
    with pytest.raises(RuntimeError):
        run_live(database, monkeypatch, 1, [text_block("q", "Why?"), text_block("r", "A")])
    assert database.execute("SELECT count(*) FROM source_snapshot").fetchone()[0] == 0
    assert database.execute("SELECT status FROM sync_run").fetchone()[0] == "failed"


def test_image_change_updates_basis_and_keeps_old_archive(database, monkeypatch):
    add_source(database, "live")
    raw = [heading("q", 3, "Move zero"), {"block_id": "image", "block_type": 27,
           "image": {"token": "token"}}, text_block("r", "Use pointers")]
    class Images:
        content = b"\x89PNG\r\n\x1a\nfirst"
        def download_media(self, token, output, identity):
            output.write_bytes(self.content)
    client = Images()
    run_live(database, monkeypatch, 1, raw, client)
    client.content = b"\x89PNG\r\n\x1a\nchanged"
    run_live(database, monkeypatch, 1, raw, client)
    assert database.execute("SELECT count(DISTINCT review_basis_id) FROM question_version").fetchone()[0] == 2
    rows = database.execute("SELECT materials_json FROM version_resources").fetchall()
    assert len({json.loads(row[0])[0]["sha256"] for row in rows}) == 2


def test_failed_image_is_not_assignable(database, monkeypatch):
    from datetime import date

    from app.services.tasks import create_daily_plan
    add_source(database, "live")
    raw = [heading("q", 3, "Move zero"), {"block_id": "image", "block_type": 27,
           "image": {"token": "token"}}, text_block("r", "Use pointers")]
    def fail(*args):
        raise LarkCliError("permission denied")
    run_live(database, monkeypatch, 1, raw, SimpleNamespace(download_media=fail))
    plan = create_daily_plan(database, date(2026, 9, 1), 1, 0, {}, "plan")
    assert plan["tasks"] == []
    assert plan["shortages"]["code"] == 1
