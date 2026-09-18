import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.domain import Submission
from app.routes.model_errors import model_error_response
from app.services.bootstrap import register_initial_sources
from app.services.free_practice import add_free_practice
from app.services.free_requests import ORIGINAL_LIMIT, VARIANT_LIMIT, free_cards
from app.services.learning_clock import local_today
from app.services.model_jobs import ModelJobError
from app.services.plan_editing import update_daily_plan
from app.services.practice import (
    add_theory_to_review,
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
    expected_state: dict | None = None
    fresh_batch: bool = False
    expected_batch: str | None = Field(None, max_length=200)


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
    note: str | None = Field(default=None, max_length=20000)


class TheorySubmissionRequest(BaseModel):
    submitted_at: datetime
    entry_mode: str
    answer_text: str = Field(min_length=1, max_length=30000)


class ReviewRequest(BaseModel):
    entered_by: str
    happened_at: datetime
    task_id: str | None = None


class FreePracticeRequest(BaseModel):
    question_source: Literal['original', 'variant'] = 'original'
    question_type: Literal['code', 'theory'] | None = None
    plan_id: str | None = None
    theme: str = Field(default='', max_length=2000)
    count: int = Field(gt=0, le=ORIGINAL_LIMIT)
    mode: Literal['random', 'topic'] = 'random'
    only_new: bool = True


class AlignmentRequest(BaseModel):
    change_note: str = Field(default='', max_length=4000)


class EvaluationCorrection(BaseModel):
    expected_adoption: str | None = None
    verdict: Literal["aligned", "needs_review", "unable_to_assess"]


class ClassicRequest(BaseModel):
    is_classic: bool


class KnowledgeReadRequest(BaseModel):
    version_id: str | None = None


class KnowledgeEditRequest(BaseModel):
    expected_version_id: str
    prompt: str = Field(min_length=1, max_length=4000)
    reference_text: str = Field(min_length=1, max_length=20000)
    category_path: str = Field(max_length=1000)
    reference_verified: bool = False


class ReferenceCorrectionRequest(BaseModel):
    content: str = Field(min_length=10, max_length=8000)
    sources: list[str] = Field(min_length=1, max_length=10)
    expected_id: str


@router.post('/questions/{question_id}/knowledge')
def read_question_knowledge(question_id: str, body: KnowledgeReadRequest, database: Database):
    from app.services.knowledge_editing import read_knowledge
    try:
        return read_knowledge(database, question_id, body.version_id)
    except ValueError as error:
        raise HTTPException(404, str(error)) from error


@router.put('/questions/{question_id}/knowledge')
def edit_question_knowledge(question_id: str, body: KnowledgeEditRequest, database: Database, idempotency_key: RequestKey):
    from app.services.knowledge_editing import edit_knowledge
    try:
        result = edit_knowledge(database, question_id, **body.model_dump(), request_key=idempotency_key)
        return {**result, 'current_version_id': database.execute('SELECT current_version_id FROM question WHERE id=?', (question_id,)).fetchone()[0]}
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.put('/versions/{version_id}/correction')
def edit_reference_correction(version_id: str, body: ReferenceCorrectionRequest, database: Database, idempotency_key: RequestKey):
    from app.services.reference_corrections import save_correction
    try:
        return save_correction(database, version_id, body.content, body.sources, idempotency_key, body.expected_id)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.post('/questions/{question_id}/classic')
def mark_classic(question_id: str, body: ClassicRequest, database: Database):
    if not database.execute('UPDATE question SET is_classic=? WHERE id=?', (int(body.is_classic), question_id)).rowcount:
        raise HTTPException(404, '题目不存在')
    database.commit()
    return {'is_classic': body.is_classic}


@router.post('/tasks/{task_id}/cancel')
def cancel_task(task_id: str, database: Database):
    from app.services.task_management import cancel_unstarted_task
    try:
        return cancel_unstarted_task(database, task_id)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


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
def edit_plan(plan_id: str, body: PlanRequest, database: Database,
              idempotency_key: Annotated[str | None, Header(alias='Idempotency-Key')] = None):
    plan = database.execute('SELECT plan_date FROM daily_plan WHERE id=?', (plan_id,)).fetchone()
    if not plan or plan[0] != body.plan_date.isoformat():
        raise HTTPException(409, '只可调整今天的计划，历史计划保留')
    try:
        if body.fresh_batch:
            from app.services.current_practice import draw_daily_batch
            if not idempotency_key:
                raise ValueError('新题单需要请求标识，请刷新后重试')
            return draw_daily_batch(database, body.plan_date, body.code_target, body.theory_target,
                                    body.module_quotas, idempotency_key, body.expected_batch)
        return update_daily_plan(database, plan_id, body.code_target, body.theory_target, body.module_quotas,
                                 idempotency_key, body.expected_state, local_today())
    except (ValueError, IdempotencyConflictError) as error:
        raise HTTPException(409, str(error)) from error


@router.post("/plans/{plan_id}/fill")
def fill_plan(plan_id: str, database: Database, body: FillRequest | None = None):
    _require_today_plan(database, plan_id)
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
        if body.fresh_batch:
            from app.services.current_practice import draw_daily_batch
            return draw_daily_batch(database, body.plan_date, body.code_target, body.theory_target,
                                    body.module_quotas, idempotency_key, body.expected_batch)
        result = create_daily_plan(
            database,
            body.plan_date,
            body.code_target,
            body.theory_target,
            body.module_quotas,
            idempotency_key,
            required_date=local_today(),
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


@router.post('/practice/restore')
def restore_today(database: Database):
    from app.services.current_practice import restore_daily
    return {'batch': restore_daily(database)}


@router.post("/attempts/{attempt_id}/evaluation")
def correct_evaluation(attempt_id: str, body: EvaluationCorrection, database: Database, idempotency_key: RequestKey):
    attempt = database.execute("SELECT a.id FROM attempt a JOIN task t ON t.id=a.task_id "
                               "JOIN question q ON q.id=t.question_id WHERE a.id=? "
                               "AND a.submitted_at IS NOT NULL AND q.question_type='theory'",
                               (attempt_id,)).fetchone()
    if attempt is None:
        raise HTTPException(404, "没有可更正的理论作答")
    from app.services.practice import correct_theory_evaluation
    try:
        return correct_theory_evaluation(database, attempt_id, body.verdict, idempotency_key, body.expected_adoption)
    except IdempotencyConflictError as error:
        raise HTTPException(409, str(error)) from error


@router.post("/tasks/{task_id}/expose-answer")
def mark_answer_exposed(task_id: str, body: ExposureRequest, database: Database):
    from app.services.reference_corrections import get_correction
    from app.services.reference_state import verification
    attempt_id = expose_answer(database, task_id, datetime.now(UTC))
    row = database.execute("SELECT v.reference_text,v.id FROM task t JOIN question_version v "
                           "ON v.id=t.question_version_id WHERE t.id=?", (task_id,)).fetchone()
    resource = database.execute("SELECT materials_json FROM version_resources WHERE version_id=?", (row["id"],)).fetchone()
    materials = json.loads(resource[0]) if resource else []
    return {"attempt_id": attempt_id, "answer_exposed": True, "reference_text": row["reference_text"],
            'reference_correction': get_correction(database, row['id']), 'reference_verification': verification(database, row['id']),
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
    try:
        return start_review_task(database, question_id, local_today(), idempotency_key)
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
    version_id = None
    if body.task_id:
        task = database.execute('SELECT question_version_id FROM task WHERE id=? AND question_id=?',
                                (body.task_id, question_id)).fetchone()
        if not task:
            raise HTTPException(409, '题目与任务不匹配')
        version_id = task[0]
    round_id = add_theory_to_review(database, question_id, body.entered_by, datetime.now(UTC), version_id)
    return {"review_round_id": round_id}


@router.post("/free-practice")
def create_free_practice(
    body: FreePracticeRequest,
    idempotency_key: RequestKey,
    database: Database,
):
    if body.plan_id:
        _require_today_plan(database, body.plan_id)
    only_new = True if body.question_source == 'original' else body.only_new
    if body.mode == 'topic' and not body.theme.strip():
        raise HTTPException(422, '请描述想练的内容，或选择完全随机')
    if body.question_source == 'variant' and (not body.question_type or body.count > VARIANT_LIMIT):
        raise HTTPException(422, f'变种题请选择代码或八股，每次生成1至{VARIANT_LIMIT}道')
    try:
        selected = select_by_description(database, body.theme, body.count, idempotency_key, only_new, question_type=body.question_type) if body.mode == 'topic' else None
    except (ValueError, ModelJobError) as error:
        raise HTTPException(409, str(error)) from error
    plan_id = body.plan_id or _get_or_create_plan(database, local_today(), idempotency_key)['plan_id']
    if body.question_source == 'variant':
        from app.services.practice_generation import generate_variants
        try:
            result = generate_variants(database, plan_id, body.theme if body.mode == 'topic' else '', body.count,
                                       body.question_type, body.only_new, idempotency_key, selected)
        except ModelJobError as error:
            return model_error_response(error, 502)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return {**result, 'tasks': _free_cards(database, result['task_ids'])}
    result = add_free_practice(
        database,
        plan_id,
        body.theme if body.mode == 'topic' else '',
        body.count,
        idempotency_key,
        source_checked=False,
        used_cache=True,
        selected_ids=selected,
        only_new=only_new,
        question_type=body.question_type,
    )
    return {**result.__dict__, 'tasks': _free_cards(database, result.task_ids),
            'freshness': {'used_cache': True, 'sources': source_status(database)}}


def _require_today_plan(database, plan_id):
    row = database.execute('SELECT plan_date FROM daily_plan WHERE id=?', (plan_id,)).fetchone()
    if not row or row[0] != local_today().isoformat():
        raise HTTPException(409, '这个计划不属于今天，请返回今天再安排；历史记录会保留')


def _free_cards(database, task_ids):
    return free_cards(database, task_ids)
