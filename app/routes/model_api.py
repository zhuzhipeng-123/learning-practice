import sqlite3
from datetime import UTC, date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.routes.model_errors import model_error_response
from app.services.llm_config import (
    DEFAULT_MODELS,
    MODULES,
    get_module_config,
    provider_status,
    save_module_config,
)
from app.services.model_jobs import ModelJobError
from app.services.module_jobs import run_module_job
from app.storage.dependencies import get_database
from app.storage.transactions import transaction

router = APIRouter(prefix="/api")
Database = Annotated[sqlite3.Connection, Depends(get_database)]
RequestKey = Annotated[str, Header(alias="Idempotency-Key")]


class ModuleSettings(BaseModel):
    provider: Literal["agnes", "openrouter"]
    model: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=16_000)
    max_tokens: int = Field(ge=128, le=8192)


class ReflectionGeneration(BaseModel):
    activity_date: date


class GlobalProvider(BaseModel):
    provider: Literal['agnes', 'openrouter']


class InterviewReference(BaseModel):
    turn_id: str | None = Field(default=None, max_length=200)


class InterviewGeneration(BaseModel):
    expected_revision: str | None = Field(default=None, max_length=200)


@router.get('/interviews/{session_id}/conversation')
def read_live_conversation(session_id: str, database: Database, pending_revision: str | None = Query(None, max_length=200)):
    from app.services.interview_conversation import live_conversation
    return live_conversation(database, session_id, pending_revision)


@router.post('/interviews/{session_id}/reference')
def read_interview_reference(session_id: str, body: InterviewReference, database: Database):
    from app.services.interview_answers import view_reference
    return view_reference(database, session_id, body.turn_id)


@router.post('/interviews/{session_id}/reference/generate')
def complete_interview_reference(session_id: str, body: InterviewReference, database: Database):
    from app.services.interview_answers import generate_reference
    try:
        return generate_reference(database, session_id, body.turn_id)
    except ModelJobError as error:
        return model_error_response(error, 409)


@router.post('/model-reflections/{reflection_id}/view')
def read_model_reflection(reflection_id: str, database: Database):
    from app.services.reflections import view_model_reflection
    row = database.execute("SELECT id FROM reflection WHERE id=? AND author='model'", (reflection_id,)).fetchone()
    if not row:
        raise HTTPException(404, '这份模型复盘不存在')
    return view_model_reflection(database, row[0], datetime.now(UTC))


@router.post('/model-provider')
def set_model_provider(body: GlobalProvider, database: Database):
    with transaction(database):
        for module in MODULES:
            config = get_module_config(database, module)
            save_module_config(database, module, body.provider, DEFAULT_MODELS[body.provider], config['prompt'], config['max_tokens'])
    return {'updated': len(MODULES)}


@router.post("/interviews/{session_id}/dialogue")
def read_dialogue(session_id: str, database: Database):
    row = database.execute("SELECT t.question_id FROM task t JOIN interview_session s ON s.task_id=t.id WHERE s.id=?", (session_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "面试会话不存在")
    with transaction(database):
        database.execute("INSERT OR IGNORE INTO question_exposure VALUES (?,?)", (row[0], datetime.now(UTC).isoformat()))
        turns = database.execute("SELECT it.id,it.role,it.content,EXISTS(SELECT 1 FROM model_job j WHERE j.result_id=it.id AND j.purpose='interview_feedback') AS is_feedback "
                                 "FROM interview_turn it WHERE it.session_id=? ORDER BY it.rowid", (session_id,)).fetchall()
    return {"turns": [dict(turn) for turn in turns]}


@router.get("/model-settings")
def model_settings(database: Database):
    return {"modules": [get_module_config(database, module) for module in MODULES], "credentials": provider_status()}


@router.post("/sources/{source_id}/analyze")
def analyze_source(source_id: str, idempotency_key: RequestKey, database: Database):
    try:
        return run_module_job(database, "source_parsing", source_id, idempotency_key)
    except ModelJobError as error:
        return model_error_response(error, 409)


@router.post("/model-settings/{module}")
def update_model_settings(module: str, body: ModuleSettings, database: Database):
    try:
        return save_module_config(database, module, **body.model_dump())
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.post("/interviews/{session_id}/generate/{kind}")
def generate_interview(session_id: str, kind: Literal["followup", "feedback"], idempotency_key: RequestKey, database: Database,
                       body: InterviewGeneration | None = None):
    try:
        result = run_module_job(database, "interview_" + kind, session_id, idempotency_key,
                                expected_revision=body.expected_revision if body else None)
        return {**result, 'turn_count': database.execute('SELECT COUNT(*) FROM interview_turn WHERE session_id=?', (session_id,)).fetchone()[0]}
    except ModelJobError as error:
        return model_error_response(error, 409)


@router.post("/reflections/generate")
def generate_reflection(body: ReflectionGeneration, idempotency_key: RequestKey, database: Database):
    try:
        return run_module_job(database, "daily_reflection", body.activity_date.isoformat(), idempotency_key)
    except ModelJobError as error:
        return model_error_response(error, 409)
