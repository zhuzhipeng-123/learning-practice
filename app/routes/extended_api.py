import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.services.candidates import CandidateError, approve_candidate, reject_candidate
from app.services.exports import export_learning_data, verify_export
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
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime


class InterviewStartRequest(BaseModel):
    task_id: str
    created_at: datetime


class InterviewEndRequest(BaseModel):
    ended_at: datetime


class DerivedQuestionRequest(BaseModel):
    prompt: str
    reference_text: str
    category_path: str
    confirmed_by_user: bool
    created_at: datetime


class ReflectionRequest(BaseModel):
    activity_date: date
    content: str
    covered_ids: list[str] = Field(default_factory=list)
    created_at: datetime


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
        raise HTTPException(429 if error.retry_at else 502, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.post('/interviews/prepare')
def prepare_interview(body: InterviewChoice, database: Database, idempotency_key: Annotated[str, Header(alias='Idempotency-Key')]):
    from app.services.interview_setup import prepare_interview
    try:
        return prepare_interview(database, body.question_id, body.custom_question, body.job_focus, idempotency_key)
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
        raise HTTPException(status_code=429 if error.retry_at else 502, detail=str(error)) from error


@router.post("/attempts/{attempt_id}/reevaluate")
def reevaluate(attempt_id: str, database: Database,
               idempotency_key: Annotated[str, Header(alias="Idempotency-Key")]):
    try:
        return {"job_id": create_reevaluation_job(database, attempt_id, idempotency_key)}
    except ModelJobError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/interviews")
def create_interview(body: InterviewStartRequest, database: Database):
    return {"session_id": start_session(database, body.task_id, body.created_at)}


@router.post("/interviews/{session_id}/turns")
def create_turn(session_id: str, body: TurnRequest, database: Database,
                idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None):
    turn_id = add_turn(database, session_id, body.role, body.content, datetime.now(UTC), idempotency_key)
    return {"turn_id": turn_id}


@router.post("/interviews/{session_id}/end")
def close_interview(session_id: str, body: InterviewEndRequest, database: Database):
    end_session(database, session_id, body.ended_at)
    return {"status": "ended"}


@router.post("/interviews/{session_id}/derived")
def create_derived(session_id: str, body: DerivedQuestionRequest, database: Database):
    question_id = create_derived_theory_question(
        database,
        session_id,
        body.prompt,
        body.reference_text,
        body.category_path,
        body.confirmed_by_user,
        body.created_at,
    )
    return {"question_id": question_id}


@router.post("/reflections/{author}")
def create_reflection(author: str, body: ReflectionRequest, database: Database):
    if author == "user":
        reflection_id = save_user_reflection(
            database, body.activity_date, body.content, body.created_at
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
    export_learning_data(database, destination, root / "media")
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
    return verify_export(archive, restore)
