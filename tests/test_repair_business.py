import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.domain import Submission
from app.services.free_practice import add_free_practice
from app.services.interview import end_session, start_session
from app.services.model_jobs import ModelJobError, run_evaluation_job
from app.services.practice import (
    add_theory_to_review,
    adopt_theory_evaluation,
    expose_answer,
    start_attempt,
    submit_code,
    submit_theory,
)
from app.services.review import enter_review
from app.services.tasks import create_daily_plan
from app.storage.database import connect_database, initialize_database
from tests.test_later_phases import seed_questions
from tests.test_practice_review import make_plan, seed_question

NOW = datetime(2026, 9, 1, tzinfo=UTC)


def submitted_theory(database):
    seed_question(database, "theory")
    task = make_plan(database, "theory")
    start_attempt(database, task["id"], "daily", NOW)
    result = submit_theory(database, Submission(
        task["id"], "answer", NOW, "daily", answer_text="answer",
    ))
    return result


def test_exposure_survives_normal_submission(database):
    question_id = seed_question(database, "code")
    task = make_plan(database, "code")
    basis = database.execute("SELECT review_basis_id FROM question_version").fetchone()[0]
    enter_review(database, question_id, basis, "test", NOW)
    database.commit()
    start_attempt(database, task["id"], "review", NOW)
    expose_answer(database, task["id"], NOW)
    submit_code(database, Submission(
        task["id"], "exposed", NOW, "review", code_self_result="can_solve",
    ))
    assert database.execute("SELECT answer_exposed_at FROM attempt").fetchone()[0]
    assert database.execute("SELECT count(*) FROM valid_review_pass").fetchone()[0] == 0


def test_correction_revokes_completed_round(database):
    question_id = seed_question(database, "theory")
    round_id = add_theory_to_review(database, question_id, "test", NOW)
    task = make_plan(database, "theory")
    for index, days in enumerate([0, 1, 2, 3, 8]):
        moment = NOW + timedelta(days=days)
        database.execute(
            "INSERT INTO attempt(id,task_id,question_version_id,review_round_id,entry_mode,"
            "started_at,submitted_at,activity_date,answer_text) VALUES (?,?,?,?,?,?,?,?,?)",
            (str(index), task["id"], task["question_version_id"], round_id, "review",
             moment.isoformat(), moment.isoformat(), moment.date().isoformat(), "answer"),
        )
        adopt_theory_evaluation(database, str(index), "aligned", moment)
    adopt_theory_evaluation(database, "4", "needs_review", NOW + timedelta(days=9), True)
    row = database.execute("SELECT status,end_reason FROM review_round").fetchone()
    assert tuple(row) == ("active", None)


def test_free_request_retry_returns_persisted_selection(database):
    seed_questions(database)
    plan = create_daily_plan(database, date(2026, 9, 11), 0, 0, {}, "plan")
    args = (database, plan["plan_id"], "Adam", 1, "free", True, False)
    first = add_free_practice(*args, random_seed=1)
    second = add_free_practice(*args, random_seed=99)
    assert first == second
    assert database.execute("SELECT added_target FROM daily_plan").fetchone()[0] == 1


@pytest.mark.parametrize("content", ["[]", "null", '{"verdict": []}'])
def test_invalid_model_shape_is_retryable(database, content):
    result = submitted_theory(database)
    fake = SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(content=content, model="fake"))
    with pytest.raises(ModelJobError):
        run_evaluation_job(database, result["job_id"], fake)
    assert database.execute("SELECT status FROM model_job").fetchone()[0] == "failed"
    assert database.execute("SELECT answer_text FROM attempt").fetchone()[0] == "answer"


def test_late_model_preserves_manual_adoption(database):
    result = submitted_theory(database)

    def complete(*args, **kwargs):
        adopt_theory_evaluation(database, result["attempt_id"], "needs_review", NOW, True)
        payload = {"verdict": "aligned", "covered_points": [], "missing_points": [], "errors": [],
                   "brief_feedback": "ok", "evidence_refs": []}
        return SimpleNamespace(content=json.dumps(payload), model="fake")

    run_evaluation_job(database, result["job_id"], SimpleNamespace(complete=complete))
    row = database.execute("SELECT verdict,corrected_by_user FROM evaluation WHERE adopted=1").fetchone()
    assert tuple(row) == ("needs_review", 1)


def test_interview_transactions_survive_connection_close(tmp_path):
    path = tmp_path / "interview.db"
    database = connect_database(path)
    initialize_database(database)
    seed_question(database, "theory")
    task = make_plan(database, "theory")
    session_id = start_session(database, task["id"], NOW)
    database.close()
    database = connect_database(path)
    assert database.execute("SELECT count(*) FROM interview_session").fetchone()[0] == 1
    end_session(database, session_id, NOW)
    database.close()
    with sqlite3.connect(path) as fresh:
        assert fresh.execute("SELECT status FROM interview_session").fetchone()[0] == "ended"
