import json
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.adapters.lark_cli import LarkCliClient, LarkCliPermissionError
from app.main import app
from app.services.model_jobs import ModelJobError, run_evaluation_job
from app.services.practice import add_theory_to_review
from app.services.review_tasks import start_review_task
from app.services.source_sync import SyncSupersededError, sync_registered_source
from app.services.tasks import create_daily_plan
from app.storage.database import SCHEMA_PATH, connect_database, initialize_database
from app.storage.migrations import CURRENT_VERSION
from tests.test_docx_parser import heading, text_block
from tests.test_practice_review import make_plan, seed_question
from tests.test_repair_business import submitted_theory
from tests.test_repair_sync import live_theory, run_live

HEADERS = {"X-Requested-With": "learning-practice"}


def test_completed_answer_and_correction_across_requests():
    with TestClient(app) as client:
        database = connect_database(app.state.database_path)
        try:
            database.execute("DELETE FROM source WHERE id='source-theory'")
            question = seed_question(database, "theory")
            task = dict(make_plan(database, "theory"))
        finally:
            database.close()
        path = f"/api/tasks/{task['id']}"
        now = datetime.now(UTC).isoformat()
        assert "theory reference" not in client.get(f"/practice/{task['id']}").text
        assert client.post(path + "/attempts", headers=HEADERS, json={"entry_mode": "web", "started_at": now}).status_code == 200
        body = {"entry_mode": "web", "submitted_at": now, "answer_text": "saved answer"}
        headers = {**HEADERS, "Idempotency-Key": "web-answer"}
        response = client.post(path + "/theory-submit", headers=headers, json=body)
        assert response.status_code == 200
        attempt = response.json()["attempt_id"]
        assert client.post(path + "/theory-submit", headers=headers, json=body).json() == response.json()
        page = client.get(f"/practice/{task['id']}")
        assert page.status_code == 200 and 'id="show-saved"' in page.text
        assert "saved answer" not in page.text and "theory reference" not in page.text
        assert client.post(path + "/attempts", headers=HEADERS, json={"entry_mode": "web", "started_at": now}).status_code == 409
        correction = client.post(f"/api/attempts/{attempt}/evaluation", headers=HEADERS, json={"verdict": "needs_review"})
        assert correction.status_code == 200
        saved = client.post(path + "/saved-answer", headers=HEADERS).json()
        assert saved["answer_text"] == "saved answer"
        assert saved["evaluations"][0]["corrected_by_user"] == 1
        assert client.post(f"/api/questions/{question}/review", headers=HEADERS, json={"entered_by": "web", "happened_at": now}).status_code == 200
        review = client.post(f"/api/questions/{question}/start-review", headers={**HEADERS, "Idempotency-Key": "web-review"})
        assert review.status_code == 200 and review.json()["task_id"] != task["id"]
        assert 'data-review-question' in client.get('/review').text
        assert task['id'] in client.get('/history').text
        database = connect_database(app.state.database_path)
        try:
            assert database.execute("SELECT COUNT(*) FROM answer_exposure").fetchone()[0] == 1
            assert database.execute("SELECT COUNT(*) FROM attempt").fetchone()[0] == 1
        finally:
            database.close()


def test_review_placeholder_does_not_block_daily_plan(database):
    question = seed_question(database, "theory")
    seed_question(database, "code")
    add_theory_to_review(database, question, "test", datetime.now(UTC))
    day = date(2026, 9, 12)
    review = start_review_task(database, question, day, "review-placeholder")
    plan = create_daily_plan(database, day, 1, 0, {}, "real-daily")
    assert len(plan["tasks"]) == 2
    assert review["task_id"] in {row["id"] for row in plan["tasks"]}
    assert database.execute("SELECT code_target,added_target FROM daily_plan").fetchone()[:] == (1, 1)


def test_download_progress_preserves_permission_error(monkeypatch):
    payload = {"ok": False, "error": {"code": 400, "message": 'HTTP 400: {"msg":"Access denied"}'}}
    completed = subprocess.CompletedProcess([], 4, b"", b"Downloading: media safe\n" + json.dumps(payload).encode())
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: completed)
    with pytest.raises(LarkCliPermissionError, match="Access denied"):
        LarkCliClient().download_media("safe", Path("sample.bin"), "bot")


def test_expired_job_retries_and_live_lease_blocks_duplicate(database):
    result = submitted_theory(database)
    payload = {"verdict": "aligned", "covered_points": [], "missing_points": [], "errors": [], "brief_feedback": "ok", "evidence_refs": []}
    fake = SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(content=json.dumps(payload), model="fake"))
    database.execute("UPDATE model_job SET status='running',updated_at=?", (datetime.now(UTC).isoformat(),))
    database.commit()
    with pytest.raises(ModelJobError, match="running"):
        run_evaluation_job(database, result["job_id"], fake)
    database.execute("UPDATE model_job SET updated_at=?", ((datetime.now(UTC) - timedelta(minutes=6)).isoformat(),))
    database.commit()
    evaluation = run_evaluation_job(database, result["job_id"], fake)
    assert run_evaluation_job(database, result["job_id"], fake) == evaluation
    row = database.execute("SELECT raw_json,model_id FROM evaluation").fetchone()
    assert json.loads(row[0]) == payload and row[1] == "fake"


def test_existing_v1_database_migrates_with_backup(tmp_path):
    path = tmp_path / "legacy.db"
    database = connect_database(path)
    try:
        database.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        database.execute("INSERT INTO schema_version VALUES (1,'legacy')")
        database.execute("INSERT INTO source(id,document_id,wiki_url,question_type) VALUES ('legacy','doc','local','code')")
        database.commit()
        initialize_database(database)
        assert database.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == CURRENT_VERSION
        assert database.execute("SELECT id FROM source").fetchone()[0] == 'legacy'
        assert len(list(tmp_path.glob('pre-migration-v1-*.db'))) == 1
        initialize_database(database)
        assert len(list(tmp_path.glob('pre-migration-v1-*.db'))) == 1
    finally:
        database.close()


def test_future_schema_rejected_without_creating_tables(tmp_path):
    database = connect_database(tmp_path / 'future.db')
    try:
        database.execute('CREATE TABLE schema_version(version INTEGER, applied_at TEXT)')
        database.execute("INSERT INTO schema_version VALUES (999,'future')")
        database.commit()
        with pytest.raises(RuntimeError, match='newer'):
            initialize_database(database)
        assert database.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()[0][0] == 'schema_version'
        assert database.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0] == 1
    finally:
        database.close()


def test_older_sync_cannot_replace_newer_published_snapshot(database, monkeypatch):
    live_theory(database)
    old = [heading('q', 1, 'Why?'), text_block('r', 'old')]
    new = [heading('q', 1, 'Why?'), text_block('r', 'new')]
    from app.adapters.docx_reader import DocxReader
    called = False

    def read(*args):
        nonlocal called
        if not called:
            called = True
            sync_registered_source(database, 'live')
            return SimpleNamespace(revision='1', blocks=old, page_count=1)
        return SimpleNamespace(revision='2', blocks=new, page_count=1)

    monkeypatch.setattr(DocxReader, 'read_latest', read)
    with pytest.raises(SyncSupersededError):
        sync_registered_source(database, 'live')
    assert database.execute('SELECT revision FROM source_snapshot').fetchone()[0] == '2'
    assert database.execute("SELECT COUNT(*) FROM sync_run WHERE status='superseded'").fetchone()[0] == 1


def test_rejected_candidate_does_not_keep_source_partial(database, monkeypatch):
    from app.services.candidates import reject_candidate
    live_theory(database)
    blocks = [heading('q', 1, 'Why?'), text_block('r', 'answer')]
    run_live(database, monkeypatch, 1, blocks)
    reject_candidate(database, database.execute('SELECT id FROM parser_candidate').fetchone()[0])
    assert run_live(database, monkeypatch, 1, blocks)['partial'] is False
