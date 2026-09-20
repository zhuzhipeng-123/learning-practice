import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from threading import Barrier
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.adapters.agnes import (
    AgnesClient,
    AgnesRateLimitError,
    AgnesResponseError,
    AgnesSettings,
    retry_after_deadline,
)
from app.domain import Submission
from app.main import app
from app.services.llm_config import save_module_config
from app.services.model_jobs import ModelJobError, create_reevaluation_job, run_evaluation_job
from app.services.module_jobs import run_module_job
from app.services.practice import (
    add_theory_to_review,
    adopt_theory_evaluation,
    start_attempt,
    submit_theory,
)
from app.storage.database import connect_database, initialize_database
from tests.helpers import remove_migrations_after
from tests.test_model_modules import HEADERS
from tests.test_practice_review import make_plan, seed_question
from tests.test_repair_business import submitted_theory


def evaluator(verdict="aligned"):
    return SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(
        content=json.dumps({"verdict": verdict, "covered_points": [], "missing_points": [],
                            "errors": [], "brief_feedback": "test", "evidence_refs": []}), model="fake"))


def test_reevaluation_preserves_history_and_uses_new_frozen_settings(database):
    result = submitted_theory(database)
    original = run_evaluation_job(database, result["job_id"], evaluator())
    old_config = database.execute("SELECT config_json FROM model_request WHERE job_id=?", (result["job_id"],)).fetchone()[0]
    save_module_config(database, "theory_evaluation", "agnes-new", "new prompt", 1024)
    job = create_reevaluation_job(database, result["attempt_id"], "new-config")
    assert create_reevaluation_job(database, result["attempt_id"], "new-config") == job
    save_module_config(database, "theory_evaluation", "agnes-later", "later prompt", 2048)
    config = json.loads(database.execute("SELECT config_json FROM model_request WHERE job_id=?", (job,)).fetchone()[0])
    assert config["prompt"] == "new prompt" and config["model"] == "agnes-new"
    latest = run_evaluation_job(database, job, evaluator("needs_review"))
    assert run_evaluation_job(database, job, evaluator()) == latest
    assert database.execute("SELECT adopted FROM evaluation WHERE id=?", (original,)).fetchone()[0] == 0
    assert database.execute("SELECT id FROM evaluation WHERE adopted=1").fetchone()[0] == latest
    assert database.execute("SELECT COUNT(*) FROM evaluation").fetchone()[0] == 2
    assert database.execute("SELECT COUNT(*) FROM attempt").fetchone()[0] == 1
    assert database.execute("SELECT COUNT(*) FROM task WHERE status='completed'").fetchone()[0] == 1
    assert database.execute("SELECT config_json FROM model_request WHERE job_id=?", (result["job_id"],)).fetchone()[0] == old_config
    with pytest.raises(ModelJobError, match="不同作答"):
        create_reevaluation_job(database, "other-attempt", "new-config")


def test_reevaluation_keeps_human_adoption(database):
    result = submitted_theory(database)
    manual = adopt_theory_evaluation(database, result["attempt_id"], "needs_review", datetime.now(UTC), True)
    job = create_reevaluation_job(database, result["attempt_id"], "manual")
    run_evaluation_job(database, job, evaluator())
    assert database.execute("SELECT id FROM evaluation WHERE adopted=1").fetchone()[0] == manual


def test_reevaluation_recomputes_one_review_pass_for_original_submission(database):
    question = seed_question(database, "theory")
    now = datetime.now(UTC)
    add_theory_to_review(database, question, "explicit_click", now)
    task = make_plan(database, "theory")
    start_attempt(database, task["id"], "review", now)
    result = submit_theory(database, Submission(task["id"], "review-answer", now, "review", answer_text="answer"))
    run_evaluation_job(database, result["job_id"], evaluator())
    again = create_reevaluation_job(database, result["attempt_id"], "again")
    run_evaluation_job(database, again, evaluator())
    assert database.execute("SELECT COUNT(*) FROM valid_review_pass").fetchone()[0] == 1
    revised = create_reevaluation_job(database, result["attempt_id"], "revised")
    run_evaluation_job(database, revised, evaluator("needs_review"))
    assert database.execute("SELECT COUNT(*) FROM valid_review_pass").fetchone()[0] == 0
    assert database.execute("SELECT submitted_at FROM attempt").fetchone()[0] == now.isoformat()


def test_module_rate_limit_saves_cooldown_and_retries_same_request(database):
    result = submitted_theory(database)
    day = database.execute("SELECT activity_date FROM attempt").fetchone()[0]
    deadline = datetime.now(UTC) + timedelta(minutes=1)

    def limited(*a, **k):
        raise AgnesRateLimitError("limited", deadline)

    with pytest.raises(ModelJobError):
        run_module_job(database, "daily_reflection", day, "retry-reflection", SimpleNamespace(complete=limited))
    assert database.execute("SELECT retry_at FROM provider_cooldown").fetchone()[0] == deadline.isoformat()
    fake = SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(
        content=json.dumps({"content": "reflection", "covered_ids": [result["attempt_id"]]}), model="fake"))
    database.execute("UPDATE provider_cooldown SET retry_at=?", ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),))
    database.commit()
    first = run_module_job(database, "daily_reflection", day, "retry-reflection", fake)
    assert run_module_job(database, "daily_reflection", day, "retry-reflection", fake) == first
    assert database.execute("SELECT COUNT(*) FROM reflection").fetchone()[0] == 1


@pytest.mark.parametrize("header,seconds", [("120", 120), ("0", 0), ("invalid", 60), ("-1", 60), (None, 60)])
def test_retry_after_seconds_and_invalid_fallback(header, seconds):
    now = datetime(2026, 9, 12, tzinfo=UTC)
    assert retry_after_deadline(header, now) == now + timedelta(seconds=seconds)


def test_retry_after_http_date_and_adapter(monkeypatch):
    deadline = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=10)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(429, headers={"Retry-After": format_datetime(deadline, usegmt=True)}))
    with pytest.raises(AgnesRateLimitError) as caught:
        AgnesClient(AgnesSettings("https://example.test", "fake")).complete([{"role": "user", "content": "test"}])
    assert caught.value.retry_at == deadline


def test_cooldown_covers_new_jobs_other_modules_and_survives_reopen(database):
    result = submitted_theory(database)
    deadline = datetime.now(UTC) + timedelta(minutes=10)
    calls = []

    def limited(*a, **k):
        calls.append(True)
        raise AgnesRateLimitError("limited", deadline)

    fake = SimpleNamespace(complete=limited)
    with pytest.raises(ModelJobError):
        run_evaluation_job(database, result["job_id"], fake)
    assert database.execute("SELECT next_retry_at FROM model_job WHERE id=?", (result["job_id"],)).fetchone()[0] == deadline.isoformat()
    newer = create_reevaluation_job(database, result["attempt_id"], "bypass")
    path = database.execute("PRAGMA database_list").fetchone()[2]
    reopened = connect_database(path)
    try:
        for job in (result["job_id"], newer):
            with pytest.raises(ModelJobError) as caught:
                run_evaluation_job(reopened, job, fake)
            assert caught.value.retry_at == deadline
        day = reopened.execute("SELECT activity_date FROM attempt").fetchone()[0]
        with pytest.raises(ModelJobError, match="限流"):
            run_module_job(reopened, "daily_reflection", day, "reflection", fake)
        assert len(calls) == 1
        reopened.execute("UPDATE provider_cooldown SET retry_at=?", ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),))
        reopened.commit()
        run_evaluation_job(reopened, newer, evaluator())
        run_evaluation_job(reopened, result["job_id"], evaluator())
        assert reopened.execute("SELECT next_retry_at FROM model_job WHERE id=?", (result["job_id"],)).fetchone()[0] is None
    finally:
        reopened.close()


def test_truncated_plain_text_is_not_success(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json={
        "model": "fake", "choices": [{"finish_reason": "length", "message": {"content": "half a sentence"}}]}))
    with pytest.raises(AgnesResponseError, match="截断"):
        AgnesClient(AgnesSettings("https://example.test", "fake")).complete([{"role": "user", "content": "test"}])


def test_v4_upgrade_backfills_existing_evaluation_jobs(database):
    result = submitted_theory(database)
    remove_migrations_after(database, 4)
    database.execute("DROP TABLE evaluation_job_target")
    database.execute("DROP TABLE provider_cooldown")
    database.execute("DROP TABLE source_tree_member")
    database.execute("DROP TABLE alignment_run")
    database.execute("DROP TABLE interview_setup")
    database.execute("DROP TABLE question_derivation")
    database.execute("DROP TABLE interview_derivation")
    database.execute("DROP TABLE free_practice_batch")
    database.execute("DROP TABLE reference_correction")
    database.execute('DROP TABLE reference_correction_history')
    database.execute('DROP TABLE reference_verification')
    database.execute("DROP INDEX task_question_history")
    database.execute("DROP INDEX attempt_activity_history")
    database.execute("DROP INDEX interview_turn_session")
    database.execute("DELETE FROM schema_version WHERE version>=5")
    database.commit()
    initialize_database(database)
    assert database.execute("SELECT attempt_id FROM evaluation_job_target WHERE job_id=?", (result["job_id"],)).fetchone()[0] == result["attempt_id"]
    run_evaluation_job(database, result["job_id"], evaluator())


def test_reevaluation_api_is_idempotent_and_page_recovers_latest_job():
    with TestClient(app) as client:
        connection = connect_database(app.state.database_path)
        try:
            connection.execute("DELETE FROM source WHERE id='source-theory'")
            result = submitted_theory(connection)
            task = connection.execute("SELECT task_id FROM attempt WHERE id=?", (result["attempt_id"],)).fetchone()[0]
        finally:
            connection.close()
        url = f"/api/attempts/{result['attempt_id']}/reevaluate"
        headers = {**HEADERS, "Idempotency-Key": "page-reevaluate"}
        response = client.post(url, headers=headers)
        assert response.status_code == 200
        assert client.post(url, headers=headers).json() == response.json()
        html = client.get(f"/practice/{task}").text
        assert response.json()["job_id"] in html
        assert "按当前设置重新评价" in html
        assert client.post("/api/attempts/missing/reevaluate", headers={**HEADERS, "Idempotency-Key": "missing"}).status_code == 409


def test_independent_jobs_overlap_and_next_request_needs_no_fixed_wait(database, monkeypatch):
    result = submitted_theory(database)
    second = create_reevaluation_job(database, result["attempt_id"], "parallel")
    path = database.execute("PRAGMA database_list").fetchone()[2]
    barrier = Barrier(2)

    def complete(*args, **kwargs):
        barrier.wait(timeout=5)
        return evaluator().complete()

    monkeypatch.setattr("app.services.model_jobs.client_for_config", lambda config: SimpleNamespace(complete=complete))

    def run(job):
        connection = connect_database(path)
        try:
            return run_evaluation_job(connection, job)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(run, [result["job_id"], second]))
    assert len(set(outcomes)) == 2
    assert database.execute("SELECT COUNT(*) FROM evaluation WHERE adopted=1").fetchone()[0] == 1
    monkeypatch.setattr("app.services.model_jobs.client_for_config", lambda config: evaluator())
    third = create_reevaluation_job(database, result["attempt_id"], "immediate-next")
    assert run_evaluation_job(database, third)
