import json
import sqlite3
from datetime import date, datetime
from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services.dashboard import (
    day_details,
    heatmap,
    latest_reflections,
    module_tree,
    theory_document_options,
)
from app.services.free_requests import ORIGINAL_LIMIT, VARIANT_LIMIT
from app.services.learning_clock import local_now, local_today
from app.services.llm_config import MODULES, agnes_configured, get_module_config
from app.services.model_budget import OUTPUT_TOKEN_MAX, OUTPUT_TOKEN_MIN
from app.services.review import review_hint_flags
from app.services.source_coverage import coverage
from app.services.source_refresh import source_status
from app.storage.dependencies import get_database

router = APIRouter()
Database = Annotated[sqlite3.Connection, Depends(get_database)]


def page_context(request: Request, **values):
    return {"request": request, 'page_day': local_today().isoformat(), **values}


def configure_pages(templates: Jinja2Templates) -> APIRouter:
    @router.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, database: Database):
        modules = [get_module_config(database, module) for module in MODULES]
        return templates.TemplateResponse(request=request, name="settings.html",
                                          context=page_context(request, modules=modules, credential_configured=agnes_configured(), token_min=OUTPUT_TOKEN_MIN, token_max=OUTPUT_TOKEN_MAX))

    @router.get("/", response_class=HTMLResponse)
    def today_page(request: Request, database: Database):
        from app.services.theory_scope import document_scope_ids, theory_catalog
        today = local_today()
        plans = database.execute(
            "SELECT p.*, COUNT(t.id) AS assigned, "
            "SUM(CASE WHEN t.status='completed' THEN 1 ELSE 0 END) AS completed "
            "FROM daily_plan p LEFT JOIN task t ON t.plan_id=p.id AND t.status!='cancelled' "
            "GROUP BY p.id ORDER BY p.plan_date DESC LIMIT 7"
        ).fetchall()
        tasks = database.execute(
            "SELECT t.*, v.prompt, q.question_type, (SELECT s.id FROM interview_session s WHERE s.task_id=t.id ORDER BY s.rowid DESC LIMIT 1) session_id FROM task t "
            "JOIN question q ON q.id=t.question_id "
            "JOIN question_version v ON v.id=t.question_version_id "
            "WHERE t.plan_id=(SELECT id FROM daily_plan WHERE plan_date=?) "
            "AND t.status!='cancelled' AND t.target_kind='base' "
            "ORDER BY t.created_at, t.id",
            (today.isoformat(),),
        ).fetchall()
        current_plan = next((dict(plan) for plan in plans if plan['plan_date'] == today.isoformat()), None)
        plan_id = current_plan['id'] if current_plan else None
        from app.services.current_practice import current_daily_batch, legacy_daily_batch
        current_batch = current_daily_batch(database, plan_id) if plan_id else None
        legacy_batch = legacy_daily_batch(database, plan_id) if plan_id else None
        preference_plan = current_plan
        if preference_plan is None:
            saved = database.execute(
                "SELECT p.* FROM idempotency_record r JOIN daily_plan p "
                "ON p.id=json_extract(r.result_json,'$.plan_id') "
                "WHERE r.operation='daily_batch' "
                "AND json_extract(r.result_json,'$.batch_key') NOT LIKE 'daily-auto:%' "
                "ORDER BY p.plan_date DESC,r.rowid DESC LIMIT 1"
            ).fetchone()
            preference_plan = dict(saved) if saved else None
        batch_visible = bool(current_batch)
        legacy_tasks = [task for task in tasks if task['id'] in legacy_batch['task_ids']] if legacy_batch else []
        tasks = [task for task in tasks if task['id'] in current_batch['task_ids']] if batch_visible else []
        base_tasks = tasks
        base_counts = {kind: sum(task['question_type'] == kind for task in base_tasks) for kind in ('code', 'theory')}
        catalog = theory_catalog(database)
        saved_scope = json.loads(preference_plan['theory_scope_json'] or '{}') if preference_plan else {}
        selected_scope = saved_scope.get('selected_ids')
        if selected_scope is None:
            selected_scope = [node['id'] for node in catalog['nodes'] if node['parent_id'] is None]
        selected_scope = document_scope_ids(
            [*catalog['nodes'], *saved_scope.get('nodes', [])], selected_scope,
        )
        documents = theory_document_options(database, preserved_scope=saved_scope)
        return templates.TemplateResponse(
            request=request,
            name="today.html",
            context=page_context(request, plans=plans, tasks=tasks, today=today,
                                 base_tasks=base_tasks,
                                 base_counts=base_counts, base_completed=sum(task['status'] == 'completed' for task in base_tasks),
                                 reflection=latest_reflections(database, today),
                                 theory_documents=[node for node in documents if node['total']],
                                 empty_theory_documents=[node for node in documents if not node['total']],
                                 heatmap=heatmap(database, today, 'daily'), sources=source_status(database),
                                 theory_scope=selected_scope, theory_catalog_version=catalog['version'],
                                 plan_preferences=preference_plan,
                                 current_plan=current_plan, current_batch=current_batch, batch_visible=batch_visible,
                                 legacy_batch=legacy_batch, legacy_tasks=legacy_tasks),
        )

    @router.get("/days/{day}", response_class=HTMLResponse)
    def day_page(day: date, request: Request, database: Database, scope: Literal['all', 'daily', 'free_practice', 'review', 'interview'] = 'all'):
        return templates.TemplateResponse(request=request, name="day.html", context=page_context(request, **day_details(database, day, scope)))

    @router.get("/practice/{task_id}", response_class=HTMLResponse)
    def practice_page(task_id: str, request: Request, database: Database):
        from app.services.reference_state import verification
        task = database.execute(
            "SELECT t.*, q.question_type,q.is_classic, q.current_version_id, v.prompt, v.reference_text, "
            "v.material_status, v.category_path,v.review_basis_id "
            "FROM task t JOIN question q ON q.id=t.question_id "
            "JOIN question_version v ON v.id=t.question_version_id WHERE t.id=?",
            (task_id,),
        ).fetchone()
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        resource = database.execute("SELECT materials_json FROM version_resources WHERE version_id=?",
                                    (task["question_version_id"],)).fetchone()
        materials = json.loads(resource[0]) if resource else []
        latest = database.execute("SELECT * FROM attempt WHERE task_id=? ORDER BY rowid DESC LIMIT 1",
                                  (task_id,)).fetchone()
        job = database.execute("SELECT j.* FROM model_job j JOIN evaluation_job_target target ON target.job_id=j.id "
                               "JOIN attempt a ON a.id=target.attempt_id WHERE a.task_id=? "
                               "ORDER BY a.rowid DESC,j.rowid DESC LIMIT 1", (task_id,)).fetchone()
        return templates.TemplateResponse(
            request=request,
            name="practice.html",
            context=page_context(request, task=task, materials=materials, latest=latest, job=job,
                review_basis_conflict=bool(database.execute('SELECT 1 FROM review_round r JOIN question_version v ON v.id=? '
                    "WHERE r.question_id=? AND r.status='active' AND r.review_basis_id!=v.review_basis_id",
                    (task['question_version_id'], task['question_id'])).fetchone()),
                mastery=database.execute('SELECT id,level FROM mastery_assessment WHERE question_id=? '
                    'AND review_basis_id=? ORDER BY rowid DESC LIMIT 1',
                    (task['question_id'], task['review_basis_id'])).fetchone(),
                explicit_unable=bool(latest and database.execute(
                    'SELECT 1 FROM mastery_assessment WHERE attempt_id=?', (latest['id'],)).fetchone()),
                has_correction=bool(database.execute('SELECT 1 FROM reference_correction WHERE version_id=?', (task['question_version_id'],)).fetchone()),
                reference_verification=verification(database, task['question_version_id']),
                next_task=database.execute("SELECT t.id,(SELECT s.id FROM interview_session s WHERE s.task_id=t.id ORDER BY s.rowid DESC LIMIT 1) session_id "
                    "FROM task t WHERE t.plan_id=? AND t.origin=? AND t.id!=? AND t.status IN ('pending','in_progress') ORDER BY t.created_at LIMIT 1",
                    (task['plan_id'], task['origin'], task_id)).fetchone(),
                interview_derivation=database.execute('SELECT * FROM interview_derivation WHERE question_id=?', (task['question_id'],)).fetchone(),
                adopted=database.execute('SELECT id,verdict FROM evaluation WHERE attempt_id=? AND adopted=1', (latest['id'],)).fetchone() if latest else None,
                counted=bool(database.execute('SELECT 1 FROM valid_review_pass WHERE attempt_id=?', (latest['id'],)).fetchone()) if latest else False,
                derivation=database.execute('SELECT v.prompt FROM question_derivation d JOIN question_version v ON v.id=d.base_version_id WHERE d.question_id=?', (task['question_id'],)).fetchone()),
        )

    @router.get("/free-practice", response_class=HTMLResponse)
    def free_page(request: Request, database: Database):
        from app.services.free_batches import practice_state
        plans = database.execute(
            "SELECT id, plan_date FROM daily_plan ORDER BY plan_date DESC LIMIT 7"
        ).fetchall()
        return templates.TemplateResponse(
            request=request,
            name="free_practice.html",
            context=page_context(request, plans=plans, sources=source_status(database), heatmap=heatmap(database, local_today(), 'free_practice'),
                                 original_limit=ORIGINAL_LIMIT, variant_limit=VARIANT_LIMIT, free_state=practice_state(database)),
        )

    @router.get("/interview", response_class=HTMLResponse)
    def interview_page(request: Request, database: Database):
        return templates.TemplateResponse(
            request=request,
            name="interview.html",
            context=page_context(request),
        )

    @router.get("/interview/{session_id}", response_class=HTMLResponse)
    def interview_session_page(session_id: str, request: Request, database: Database):
        session = database.execute("SELECT s.*,v.prompt,t.question_id,q.question_type FROM interview_session s JOIN question_version v "
                                   "ON v.id=s.question_version_id JOIN task t ON t.id=s.task_id JOIN question q ON q.id=t.question_id WHERE s.id=?", (session_id,)).fetchone()
        if session is None:
            raise HTTPException(404, "面试会话不存在")
        turns = database.execute("SELECT * FROM interview_turn WHERE session_id=? ORDER BY rowid", (session_id,)).fetchall()
        dialogue = database.execute("SELECT t.* FROM interview_turn t WHERE t.session_id=? AND NOT EXISTS "
            "(SELECT 1 FROM model_job j WHERE j.result_id=t.id AND j.purpose='interview_feedback') "
            "ORDER BY t.rowid", (session_id,)).fetchall()
        latest_question = None
        if dialogue and session["status"] == "active" and database.execute("SELECT 1 FROM model_job WHERE result_id=? "
                                                                       "AND purpose='interview_followup'", (dialogue[-1]["id"],)).fetchone():
            latest_question = dialogue[-1]["content"]
        return templates.TemplateResponse(request=request, name="interview_session.html",
                                          context=page_context(request, session=session, turns=turns, latest_question=latest_question,
                                                               latest_turn_id=dialogue[-1]['id'] if latest_question else '',
                                                               code_assessment=database.execute('SELECT code_self_result FROM attempt WHERE task_id=? AND submitted_at IS NOT NULL ORDER BY rowid DESC LIMIT 1', (session['task_id'],)).fetchone()))

    @router.get("/sources", response_class=HTMLResponse)
    def sources_page(request: Request, database: Database,
                     after: str | None = Query(None, min_length=1, max_length=200),
                     before: str | None = Query(None, min_length=1, max_length=200)):
        if after and before:
            raise HTTPException(422, '请使用上一页或下一页，不要同时指定两个方向')
        sources = database.execute("SELECT * FROM source ORDER BY id").fetchall()
        where = " FROM parser_candidate WHERE status='pending'"
        candidate_total = database.execute('SELECT COUNT(*)' + where).fetchone()[0]
        clause, values = (' AND id>?', (after,)) if after else (' AND id<?', (before,)) if before else ('', ())
        order = 'DESC' if before else 'ASC'
        candidate_rows = database.execute('SELECT *' + where + clause + f' ORDER BY id {order} LIMIT 100', values).fetchall()
        if before:
            candidate_rows.reverse()
        previous_url = next_url = None
        if candidate_rows:
            first, last = candidate_rows[0]['id'], candidate_rows[-1]['id']
            if database.execute('SELECT 1' + where + ' AND id<? LIMIT 1', (first,)).fetchone():
                previous_url = '/sources?' + urlencode({'before': first}) + '#pending-candidates'
            if database.execute('SELECT 1' + where + ' AND id>? LIMIT 1', (last,)).fetchone():
                next_url = '/sources?' + urlencode({'after': last}) + '#pending-candidates'
        candidates = [
            {**dict(row), "draft": json.loads(row["draft_json"])} for row in candidate_rows
        ]
        material_errors = database.execute("SELECT source_id,last_error,COUNT(*) AS count FROM source_dependency "
                                           "WHERE status='pending' GROUP BY source_id,last_error").fetchall()
        return templates.TemplateResponse(
            request=request,
            name="sources.html",
            context=page_context(request, sources=sources, candidates=candidates, material_errors=material_errors,
                                 candidate_total=candidate_total, previous_url=previous_url, next_url=next_url, candidate_page=bool(after or before),
                                 theory_modules=module_tree(database), code_modules=module_tree(database, 'code'),
                                 coverage={source["id"]: coverage(database, source["id"]) for source in sources if source["question_type"] == "code"}),
        )

    @router.get("/review", response_class=HTMLResponse)
    def review_page(request: Request, database: Database,
                    mastery: Literal['all', 'unknown', 'vague', 'partial', 'ungraded'] = 'all'):
        rounds = database.execute(
            "SELECT r.*, q.question_type, v.id AS question_version_id, v.prompt, v.category_path, COUNT(p.id) AS valid_pass_count, "
            "MIN(p.submitted_at) AS first_pass_at, MAX(p.submitted_at) AS last_pass_at,"
            "(SELECT m.id FROM mastery_assessment m WHERE m.question_id=r.question_id "
            "AND m.review_basis_id=r.review_basis_id ORDER BY m.rowid DESC LIMIT 1) AS mastery_id,"
            "(SELECT m.level FROM mastery_assessment m WHERE m.question_id=r.question_id "
            "AND m.review_basis_id=r.review_basis_id ORDER BY m.rowid DESC LIMIT 1) AS mastery_level "
            "FROM review_round r JOIN question q ON q.id=r.question_id "
            "JOIN question_version v ON v.id=(SELECT v2.id FROM question_version v2 "
            "WHERE v2.question_id=q.id AND v2.review_basis_id=r.review_basis_id ORDER BY v2.rowid DESC LIMIT 1) "
            "LEFT JOIN valid_review_pass p ON p.review_round_id=r.id "
            "GROUP BY r.id ORDER BY CASE WHEN r.status='active' THEN 0 ELSE 1 END,r.started_at DESC,r.id"
        ).fetchall()
        rounds = [dict(row) for row in rounds]
        if mastery != 'all':
            rounds = [item for item in rounds if (item['mastery_level'] or 'ungraded') == mastery]
        level_order = {'unknown': 0, 'vague': 1, 'partial': 2, None: 3}
        rounds.sort(key=lambda item: (item['status'] != 'active', level_order[item['mastery_level']],
                                      item['started_at'], item['id']))
        now = local_now()
        today_passes, exposed_questions = review_hint_flags(database, now)
        for item in rounds:
            item['span_days'] = round((datetime.fromisoformat(item['last_pass_at']) - datetime.fromisoformat(item['first_pass_at'])).total_seconds() / 86400, 2) if item['first_pass_at'] else 0
            if item['id'] in today_passes:
                item['hint'] = '今天已记过一次独立答对，可以继续练，但不会重复计数。'
            elif item['question_id'] in exposed_questions:
                item['hint'] = '最近24小时查看过参考答案或含答案的历史记录；现在可以练，独立答对次数暂不增加。'
            else:
                item['hint'] = '先独立作答，通过后记录今天的一次进步。'
        return templates.TemplateResponse(
            request=request,
            name="review.html",
            context=page_context(request, rounds=rounds, mastery_filter=mastery),
        )

    @router.get('/questions/{question_id}/history', response_class=HTMLResponse)
    def question_history_page(question_id: str, request: Request, database: Database, page: int = Query(1, ge=1)):
        from app.services.history import question_history
        history = question_history(database, question_id, page)
        if history is None:
            raise HTTPException(404, '题目不存在')
        return templates.TemplateResponse(request=request, name='question_history.html', context=page_context(request, **history))

    @router.get("/history")
    def retired_history_page():
        return RedirectResponse('/review', status_code=307)

    return router
