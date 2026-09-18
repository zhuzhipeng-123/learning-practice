import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.routes.model_errors import model_error_response
from app.services.candidates import CandidateError, approve_candidate, reject_candidate
from app.services.exports import ExportError, export_learning_data, verify_export
from app.services.interview import (
    add_turn,
    create_derived_theory_question,
    end_session,
    start_session,
)
from app.services.model_jobs import ModelJobError, create_reevaluation_job, run_evaluation_job
from app.services.practice import expose_answer
from app.services.reflections import save_model_reflection, save_user_reflection
from app.storage.dependencies import get_database

router = APIRouter(prefix="/api")
Database = Annotated[sqlite3.Connection, Depends(get_database)]


class TurnRequest(BaseModel):
    role: Literal["user"]
    content: str = Field(min_length=1, max_length=30000)
    created_at: datetime
    expected_revision: str | None = Field(default=None, max_length=200)


class InterviewStartRequest(BaseModel):
    task_id: str
    created_at: datetime


class InterviewEndRequest(BaseModel):
    ended_at: datetime


class DerivedQuestionRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    reference_text: str = Field(min_length=1, max_length=20000)
    category_path: str = Field(max_length=1000)
    confirmed_by_user: bool
    created_at: datetime
    turn_id: str | None = None
    reference_verified: bool = False


class ReviewPreviewRequest(BaseModel):
    turn_id: str


class InterviewPoolRequest(BaseModel):
    pool: Literal['review', 'classic'] = 'review'
    job_focus: str = Field(default='', max_length=2000)


class InterviewCodeRequest(BaseModel):
    result: Literal['can_solve', 'cannot_solve']
    note: str = Field(default='', max_length=20000)


@router.post('/interviews/{session_id}/code-assessment')
def interview_code_assessment(session_id: str, body: InterviewCodeRequest, database: Database,
                              idempotency_key: Annotated[str, Header(alias='Idempotency-Key')]):
    from app.services.interview_assessment import assess_code
    return assess_code(database, session_id, body.result, body.note, idempotency_key)


class ReflectionRequest(BaseModel):
    activity_date: date
    content: str = Field(min_length=1, max_length=30000)
    covered_ids: list[str] = Field(default_factory=list)
    created_at: datetime
    expected_id: str | None = None


class CandidateDecision(BaseModel):
    existing_question_id: str | None = None
    as_new: bool = False


class InterviewChoice(BaseModel):
    question_id: str | None = None
    custom_question: str = Field(default='', max_length=4000)
    job_focus: str = Field(default='', max_length=2000)


class InterviewDirection(BaseModel):
    mode: Literal['suggest', 'opening']
    direction: str = Field(default='', max_length=2000)
    job_focus: str = Field(default='', max_length=2000)
    avoid: list[str] = Field(default_factory=list, max_length=12)


@router.post('/interviews/direction')
def interview_direction(body: InterviewDirection, database: Database,
                        idempotency_key: Annotated[str, Header(alias='Idempotency-Key')]):
    from app.services.interview_setup import prepare_direction
    try:
        return prepare_direction(database, body.mode, body.direction, body.job_focus, body.avoid, idempotency_key)
    except ModelJobError as error:
        return model_error_response(error, 502)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.post('/interviews/prepare')
def prepare_interview(body: InterviewChoice, database: Database, idempotency_key: Annotated[str, Header(alias='Idempotency-Key')]):
    from app.services.interview_setup import prepare_interview
    try:
        return prepare_interview(database, body.question_id, body.custom_question, body.job_focus, idempotency_key)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@router.post('/interviews/pool')
def pool_interview(body: InterviewPoolRequest, database: Database,
                   idempotency_key: Annotated[str, Header(alias='Idempotency-Key')]):
    from app.services.interview_setup import prepare_pool_interview
    try:
        return prepare_pool_interview(database, body.pool, body.job_focus, idempotency_key)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@router.post("/candidates/{candidate_id}/approve")
def approve(candidate_id: str, database: Database, body: CandidateDecision | None = None):
    try:
        decision = body or CandidateDecision()
        return {"question_id": approve_candidate(database, candidate_id, decision.existing_question_id, decision.as_new)}
    except CandidateError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/candidates/{candidate_id}/reject")
def reject(candidate_id: str, database: Database):
    reject_candidate(database, candidate_id)
    return {"status": "rejected"}


@router.post("/model-jobs/{job_id}/run")
def run_model_job(job_id: str, database: Database):
    try:
        evaluation_id = run_evaluation_job(database, job_id)
        row = database.execute("SELECT e.*, a.task_id FROM evaluation e JOIN attempt a ON a.id=e.attempt_id "
                               "WHERE e.id=?", (evaluation_id,)).fetchone()
        expose_answer(database, row["task_id"], datetime.now(UTC))
        return dict(row)
    except ModelJobError as error:
        return model_error_response(error, 502)


@router.post("/attempts/{attempt_id}/reevaluate")
def reevaluate(attempt_id: str, database: Database,
               idempotency_key: Annotated[str, Header(alias="Idempotency-Key")]):
    try:
        return {"job_id": create_reevaluation_job(database, attempt_id, idempotency_key)}
    except ModelJobError as error:
        return model_error_response(error, 409)


@router.post("/interviews")
def create_interview(body: InterviewStartRequest, database: Database):
    return {"session_id": start_session(database, body.task_id, body.created_at)}


@router.post("/interviews/{session_id}/turns")
def create_turn(session_id: str, body: TurnRequest, database: Database,
                idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None):
    turn_id = add_turn(database, session_id, body.role, body.content, datetime.now(UTC), idempotency_key, body.expected_revision)
    return {"turn_id": turn_id, 'turn_count': database.execute('SELECT COUNT(*) FROM interview_turn WHERE session_id=?', (session_id,)).fetchone()[0]}


@router.post("/interviews/{session_id}/end")
def close_interview(session_id: str, body: InterviewEndRequest, database: Database):
    end_session(database, session_id, body.ended_at)
    return {"status": "ended"}


@router.post("/interviews/{session_id}/derived")
def create_derived(session_id: str, body: DerivedQuestionRequest, database: Database,
                   idempotency_key: Annotated[str, Header(alias='Idempotency-Key')]):
    question_id = create_derived_theory_question(
        database,
        session_id,
        body.prompt,
        body.reference_text,
        body.category_path,
        body.confirmed_by_user,
        datetime.now(UTC),
        body.turn_id,
        idempotency_key,
        body.reference_verified,
    )
    result = json.loads(database.execute('SELECT result_json FROM idempotency_record WHERE request_key=?', (idempotency_key,)).fetchone()[0])
    if 'version_id' not in result:
        result['version_id'] = database.execute('SELECT id FROM question_version WHERE question_id=? ORDER BY rowid LIMIT 1', (question_id,)).fetchone()[0]
    return {**result, 'current_version_id': database.execute('SELECT current_version_id FROM question WHERE id=?', (question_id,)).fetchone()[0]}


@router.post('/interviews/{session_id}/review-preview')
def review_preview(session_id: str, body: ReviewPreviewRequest, database: Database,
                   idempotency_key: Annotated[str, Header(alias='Idempotency-Key')]):
    from app.services.interview_review import preview_review
    try:
        return preview_review(database, session_id, body.turn_id, idempotency_key)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except ModelJobError as error:
        return model_error_response(error, 502)


@router.post('/interviews/{session_id}/review-main')
def review_interview_main(session_id: str, database: Database):
    from app.services.interview_review import review_main_question
    return {'review_round_id': review_main_question(database, session_id)}


@router.post("/reflections/{author}")
def create_reflection(author: str, body: ReflectionRequest, database: Database,
                       idempotency_key: Annotated[str | None, Header(alias='Idempotency-Key')] = None):
    if author == "user":
        reflection_id = save_user_reflection(
            database, body.activity_date, body.content, datetime.now(UTC), idempotency_key, body.expected_id
        )
    elif author == "model":
        reflection_id = save_model_reflection(
            database,
            body.activity_date,
            body.content,
            body.covered_ids,
            body.created_at,
        )
    else:
        raise HTTPException(status_code=422, detail="unsupported reflection author")
    database.commit()
    return {"reflection_id": reflection_id}


@router.post("/exports", response_class=FileResponse)
def create_export(request: Request, database: Database):
    root = Path(request.app.state.database_path).parent
    destination = root / "backups" / f"learning-export-{datetime.now(UTC):%Y%m%d%H%M%S}-{uuid4().hex}.zip"
    try:
        export_learning_data(database, destination, root / "media")
    except ExportError as error:
        raise HTTPException(409, str(error)) from error
    return FileResponse(destination, filename="learning-export.zip")


@router.post("/exports/verify")
def verify_latest_export(request: Request, database: Database):
    del database
    root = Path(request.app.state.database_path).parent
    archives = sorted((root / "backups").glob("learning-export-*.zip"))
    if not archives:
        raise HTTPException(404, "请先导出学习数据")
    archive = archives[-1]
    restore = root / "restore-check"
    try:
        return verify_export(archive, restore)
    except ExportError as error:
        raise HTTPException(409, str(error)) from error
