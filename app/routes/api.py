import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.domain import Submission
from app.services.bootstrap import register_initial_sources
from app.services.free_practice import add_free_practice
from app.services.learning_clock import local_today
from app.services.model_jobs import ModelJobError
from app.services.plan_editing import update_daily_plan
from app.services.practice import (
    add_theory_to_review,
    adopt_theory_evaluation,
    expose_answer,
    start_attempt,
    submit_code,
    submit_theory,
)
from app.services.practice_selection import select_by_description
from app.services.review_tasks import _get_or_create_plan, start_review_task
from app.services.source_refresh import queue_source_refresh, source_status
from app.services.sources import inspect_and_register_source
from app.services.tasks import IdempotencyConflictError, create_daily_plan, fill_daily_plan
from app.storage.dependencies import get_database

router = APIRouter(prefix="/api")
Database = Annotated[sqlite3.Connection, Depends(get_database)]
RequestKey = Annotated[str, Header(alias="Idempotency-Key")]


class SourceRequest(BaseModel):
    url: str
    question_type: Literal["code", "theory"]
    identity: Literal["bot", "user"] = "bot"


class PlanRequest(BaseModel):
    plan_date: date
    code_target: int = Field(ge=0, le=100)
    theory_target: int = Field(ge=0, le=100)
    module_quotas: dict[str, int] = Field(default_factory=dict)


class FillRequest(BaseModel):
    module_quotas: dict[str, int] | None = None


class StartRequest(BaseModel):
    entry_mode: str
    started_at: datetime


class ExposureRequest(BaseModel):
    exposed_at: datetime


class CodeSubmissionRequest(BaseModel):
    submitted_at: datetime
    entry_mode: str
    code_self_result: Literal["can_solve", "cannot_solve"]
    note: str | None = None


class TheorySubmissionRequest(BaseModel):
    submitted_at: datetime
    entry_mode: str
    answer_text: str


class ReviewRequest(BaseModel):
    entered_by: str
    happened_at: datetime


class FreePracticeRequest(BaseModel):
    plan_id: str | None = None
    theme: str = Field(default='', max_length=2000)
    count: int = Field(gt=0, le=30)
    mode: Literal['random', 'topic'] = 'random'
    only_new: bool = False


class AlignmentRequest(BaseModel):
    change_note: str = Field(default='', max_length=4000)


class EvaluationCorrection(BaseModel):
    verdict: Literal["aligned", "needs_review", "unable_to_assess"]


@router.post("/sources")
def add_source(body: SourceRequest, database: Database):
    try:
        source_id = inspect_and_register_source(
            database,
            body.url,
            body.question_type,
            body.identity,
        )
    except Exception as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"source_id": source_id}


@router.post("/sources/bootstrap")
def bootstrap_sources(database: Database):
    register_initial_sources(database)
    return {"status": "ok", "source_count": 2}


@router.post("/sources/{source_id}/sync")
def sync_source(source_id: str, database: Database, body: AlignmentRequest | None = None):
    try:
        return queue_source_refresh(database, source_id, force=True, change_note=body.change_note if body else '')
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/source-status")
def read_source_status(database: Database):
    return {"sources": source_status(database)}


@router.post("/sources/align-all")
def align_all(database: Database, body: AlignmentRequest | None = None):
    return queue_source_refresh(database, force=True, change_note=body.change_note if body else '')


@router.get('/day-status')
def day_status():
    return {'date': local_today().isoformat()}


@router.put('/plans/{plan_id}')
def edit_plan(plan_id: str, body: PlanRequest, database: Database):
    if body.plan_date != local_today():
        raise HTTPException(409, '日期已变化，请回到今天安排练习')
    plan = database.execute('SELECT plan_date FROM daily_plan WHERE id=?', (plan_id,)).fetchone()
    if not plan or plan[0] != body.plan_date.isoformat():
        raise HTTPException(409, '只可调整今天的计划，历史计划保留')
    try:
        return update_daily_plan(database, plan_id, body.code_target, body.theory_target, body.module_quotas)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@router.post("/plans/{plan_id}/fill")
def fill_plan(plan_id: str, database: Database, body: FillRequest | None = None):
    try:
        return fill_daily_plan(database, plan_id, body.module_quotas if body else None)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@router.post("/plans")
def create_plan(
    body: PlanRequest,
    idempotency_key: RequestKey,
    database: Database,
):
    try:
        result = create_daily_plan(
            database,
            body.plan_date,
            body.code_target,
            body.theory_target,
            body.module_quotas,
            idempotency_key,
        )
        return {**result, "freshness": {"used_cache": True, "sources": source_status(database), "refreshing": []}}
    except (ValueError, IdempotencyConflictError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/tasks/{task_id}/attempts")
def start_task_attempt(
    task_id: str,
    body: StartRequest,
    database: Database,
):
    return {"attempt_id": start_attempt(database, task_id, body.entry_mode, datetime.now(UTC))}


@router.post("/tasks/{task_id}/saved-answer")
def saved_answer(task_id: str, database: Database):
    attempt = database.execute("SELECT * FROM attempt WHERE task_id=? AND submitted_at IS NOT NULL "
                               "ORDER BY rowid DESC LIMIT 1", (task_id,)).fetchone()
    if attempt is None:
        raise HTTPException(404, "没有已提交的作答")
    expose_answer(database, task_id, datetime.now(UTC))
    evaluations = database.execute("SELECT * FROM evaluation WHERE attempt_id=? ORDER BY rowid DESC",
                                   (attempt["id"],)).fetchall()
    return {"answer_text": attempt["answer_text"], "code_self_result": attempt["code_self_result"],
            "note": attempt["note"], "evaluations": [dict(row) for row in evaluations]}


@router.post("/attempts/{attempt_id}/evaluation")
def correct_evaluation(attempt_id: str, body: EvaluationCorrection, database: Database):
    attempt = database.execute("SELECT a.id FROM attempt a JOIN task t ON t.id=a.task_id "
                               "JOIN question q ON q.id=t.question_id WHERE a.id=? "
                               "AND a.submitted_at IS NOT NULL AND q.question_type='theory'",
                               (attempt_id,)).fetchone()
    if attempt is None:
        raise HTTPException(404, "没有可更正的理论作答")
    return {"evaluation_id": adopt_theory_evaluation(database, attempt_id, body.verdict,
                                                      datetime.now(UTC), corrected_by_user=True)}


@router.post("/tasks/{task_id}/expose-answer")
def mark_answer_exposed(task_id: str, body: ExposureRequest, database: Database):
    attempt_id = expose_answer(database, task_id, datetime.now(UTC))
    row = database.execute("SELECT v.reference_text,v.id FROM task t JOIN question_version v "
                           "ON v.id=t.question_version_id WHERE t.id=?", (task_id,)).fetchone()
    resource = database.execute("SELECT materials_json FROM version_resources WHERE version_id=?", (row["id"],)).fetchone()
    materials = json.loads(resource[0]) if resource else []
    return {"attempt_id": attempt_id, "answer_exposed": True, "reference_text": row["reference_text"],
            "materials": [item for item in materials if item["role"] == "reference"]}


@router.get("/tasks/{task_id}/materials/{block_id}")
def task_material(task_id: str, block_id: str, request: Request, database: Database):
    row = database.execute("SELECT r.materials_json FROM task t JOIN version_resources r "
                           "ON r.version_id=t.question_version_id WHERE t.id=?", (task_id,)).fetchone()
    items = json.loads(row[0]) if row else []
    item = next((item for item in items if item["block_id"] == block_id and item["status"] == "complete"), None)
    if item is None:
        raise HTTPException(404, "material not found")
    if item["role"] == "reference" and not database.execute(
        "SELECT 1 FROM attempt WHERE task_id=? AND answer_exposed_at IS NOT NULL", (task_id,),
    ).fetchone():
        raise HTTPException(403, "open the reference first")
    root = Path(request.app.state.database_path).parent / "media"
    path = (root / item["path"]).resolve()
    if path.parent != root.resolve() or not path.is_file():
        raise HTTPException(404, "archived material is missing")
    return FileResponse(path, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.post("/questions/{question_id}/start-review")
def start_review(question_id: str, idempotency_key: RequestKey, database: Database):
    from zoneinfo import ZoneInfo
    try:
        return start_review_task(database, question_id, datetime.now(ZoneInfo("Asia/Shanghai")).date(), idempotency_key)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@router.post("/tasks/{task_id}/code-submit")
def submit_code_answer(
    task_id: str,
    body: CodeSubmissionRequest,
    idempotency_key: RequestKey,
    database: Database,
):
    submission = Submission(
        task_id=task_id,
        request_key=idempotency_key,
        submitted_at=datetime.now(UTC),
        entry_mode=body.entry_mode,
        code_self_result=body.code_self_result,
        note=body.note,
    )
    return submit_code(database, submission)


@router.post("/tasks/{task_id}/theory-submit")
def submit_theory_answer(
    task_id: str,
    body: TheorySubmissionRequest,
    idempotency_key: RequestKey,
    database: Database,
):
    submission = Submission(
        task_id=task_id,
        request_key=idempotency_key,
        submitted_at=datetime.now(UTC),
        entry_mode=body.entry_mode,
        answer_text=body.answer_text,
    )
    return submit_theory(database, submission)


@router.post("/questions/{question_id}/review")
def enter_theory_review(
    question_id: str,
    body: ReviewRequest,
    database: Database,
):
    round_id = add_theory_to_review(database, question_id, body.entered_by, datetime.now(UTC))
    return {"review_round_id": round_id}


@router.post("/free-practice")
def create_free_practice(
    body: FreePracticeRequest,
    idempotency_key: RequestKey,
    database: Database,
):
    if body.mode == 'topic' and not body.theme.strip():
        raise HTTPException(422, '请描述想练的内容，或选择完全随机')
    try:
        selected = select_by_description(database, body.theme, body.count, idempotency_key, body.only_new) if body.mode == 'topic' else None
    except (ValueError, ModelJobError) as error:
        raise HTTPException(409, str(error)) from error
    plan_id = body.plan_id or _get_or_create_plan(database, local_today(), idempotency_key)['plan_id']
    result = add_free_practice(
        database,
        plan_id,
        body.theme if body.mode == 'topic' else '',
        body.count,
        idempotency_key,
        source_checked=True,
        used_cache=True,
        selected_ids=selected,
        only_new=body.only_new,
    )
    cards = [dict(database.execute('SELECT t.id,v.prompt,v.category_path,q.question_type FROM task t JOIN question_version v ON v.id=t.question_version_id JOIN question q ON q.id=t.question_id WHERE t.id=?', (task_id,)).fetchone()) for task_id in result.task_ids]
    return {**result.__dict__, 'tasks': cards}
